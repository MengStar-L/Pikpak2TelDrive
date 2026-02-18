"""任务管理器 - 监控 aria2 下载并自动上传到 TelDrive"""

import asyncio
import uuid
import os
import logging
from typing import Optional, Set
from pathlib import Path

from app.config import load_config, get_aria2_rpc_url, get_download_dir
from app.aria2_client import Aria2Client
from app.teldrive_client import TelDriveClient
from app import database as db

logger = logging.getLogger(__name__)


class TaskManager:
    """任务管理器 - 监控 aria2 并自动上传"""

    def __init__(self):
        self.config = load_config()
        self.aria2: Optional[Aria2Client] = None
        self.teldrive: Optional[TelDriveClient] = None
        self._ws_clients: Set = set()
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False
        # 内存缓存：已知的 GID 集合，避免重复查库
        self._known_gids: set = set()
        # 正在上传的 GID 集合，避免重复触发上传
        self._uploading_gids: set = set()

    def _init_clients(self):
        """根据当前配置初始化客户端"""
        cfg = self.config
        self.aria2 = Aria2Client(
            rpc_url=cfg["aria2"]["rpc_url"],
            rpc_port=cfg["aria2"]["rpc_port"],
            rpc_secret=cfg["aria2"]["rpc_secret"]
        )
        self.teldrive = TelDriveClient(
            api_host=cfg["teldrive"]["api_host"],
            access_token=cfg["teldrive"]["access_token"],
            channel_id=cfg["teldrive"]["channel_id"],
            chunk_size=cfg["teldrive"]["chunk_size"],
            upload_concurrency=cfg["teldrive"]["upload_concurrency"],
            random_chunk_name=cfg["teldrive"].get("random_chunk_name", True),
            max_retries=cfg["general"].get("max_retries", 3)
        )

    def reload_config(self):
        """重新加载配置并重建客户端"""
        self.config = load_config()
        self._init_clients()

    async def start(self):
        """启动任务管理器"""
        await db.init_db()
        self._init_clients()
        # 加载已有任务的 GID 到缓存
        all_tasks = await db.get_all_tasks()
        for t in all_tasks:
            if t.get("aria2_gid"):
                self._known_gids.add(t["aria2_gid"])
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("任务管理器已启动")

    async def stop(self):
        """停止任务管理器"""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("任务管理器已停止")

    def register_ws(self, ws):
        """注册 WebSocket 客户端"""
        self._ws_clients.add(ws)

    def unregister_ws(self, ws):
        """注销 WebSocket 客户端"""
        self._ws_clients.discard(ws)

    async def broadcast(self, message: dict):
        """向所有 WebSocket 客户端广播消息"""
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # ===========================================
    # 核心：监控循环 — 主动轮询 aria2 全部任务
    # ===========================================

    async def _monitor_loop(self):
        """定期轮询 aria2，同步所有下载任务到数据库和前端"""
        while self._running:
            try:
                await self._sync_aria2_tasks()
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"监控循环异常: {e}")
                await asyncio.sleep(5)

    async def _sync_aria2_tasks(self):
        """从 aria2 获取所有任务，同步到本地数据库"""
        try:
            # 获取 aria2 全部任务
            active = await self.aria2.tell_active() or []
            waiting = await self.aria2.tell_waiting(0, 100) or []
            stopped = await self.aria2.tell_stopped(0, 100) or []
        except Exception as e:
            # aria2 连接失败时静默跳过（仅每 30 秒打一次日志）
            logger.debug(f"aria2 轮询失败: {e}")
            return

        all_aria2_tasks = active + waiting + stopped

        # 广播全局统计
        try:
            stat = await self.aria2.get_global_stat()
            await self.broadcast({
                "type": "global_stat",
                "data": {
                    "download_speed": int(stat.get("downloadSpeed", 0)),
                    "upload_speed": int(stat.get("uploadSpeed", 0)),
                    "active": len(active),
                    "waiting": len(waiting),
                    "stopped": len(stopped)
                }
            })
        except Exception:
            pass

        for item in all_aria2_tasks:
            gid = item.get("gid", "")
            if not gid:
                continue

            parsed = Aria2Client.parse_status(item)
            aria2_status = parsed["status"]

            # 判断是否已入库
            if gid not in self._known_gids:
                # 新发现的 aria2 任务，检查是否已在数据库
                existing = await db.get_task_by_gid(gid)
                if not existing:
                    # GID 可能直接作为 task_id 存在（如重启后）
                    existing = await db.get_task(gid)
                if existing:
                    self._known_gids.add(gid)
                else:
                    task_id = gid  # 直接用 GID 作为 task_id
                    url = ""
                    files = item.get("files", [])
                    if files:
                        uris = files[0].get("uris", [])
                        if uris:
                            url = uris[0].get("uri", "")

                    status_map = {
                        "active": "downloading",
                        "waiting": "pending",
                        "paused": "paused",
                        "complete": "completed",
                        "error": "failed",
                        "removed": "cancelled"
                    }
                    initial_status = status_map.get(aria2_status, "pending")

                    await db.add_task(
                        task_id=task_id,
                        url=url,
                        filename=parsed["filename"],
                        teldrive_path=self.config["teldrive"].get("target_path", "/")
                    )
                    await db.update_task(
                        task_id,
                        status=initial_status,
                        aria2_gid=gid,
                        download_progress=parsed["progress"],
                        download_speed=parsed["speed_str"],
                        file_size=parsed["file_size"],
                        local_path=parsed["file_path"]
                    )
                    self._known_gids.add(gid)
                    logger.info(f"发现 aria2 任务: {gid} ({parsed['filename']}) 状态={initial_status}")
                    await self._broadcast_task_update(task_id)

                    # 如果发现时已经完成，触发上传
                    if aria2_status == "complete":
                        local_path = parsed["file_path"]
                        if local_path:
                            asyncio.create_task(self._handle_download_complete(task_id, gid))
                        else:
                            await db.update_task(task_id, status="completed")
                            await self._broadcast_task_update(task_id)
                    continue

            # 已入库的任务，更新状态
            task = await db.get_task_by_gid(gid)
            if not task:
                continue

            task_id = task["task_id"]
            current_status = task["status"]

            # 已完成上传、已取消、已失败的任务不再更新
            if current_status in ("completed", "cancelled", "failed"):
                continue

            # 正在上传中的任务不更新下载状态
            if current_status == "uploading":
                continue

            update_data = {
                "download_progress": parsed["progress"],
                "download_speed": parsed["speed_str"],
                "file_size": parsed["file_size"],
            }
            if parsed["filename"]:
                update_data["filename"] = parsed["filename"]
            if parsed["file_path"]:
                update_data["local_path"] = parsed["file_path"]

            if aria2_status == "active":
                update_data["status"] = "downloading"
            elif aria2_status == "waiting":
                update_data["status"] = "pending"
            elif aria2_status == "paused":
                update_data["status"] = "paused"
            elif aria2_status == "complete":
                update_data["status"] = "uploading"
                update_data["download_progress"] = 100.0
                update_data["download_speed"] = ""
            elif aria2_status == "error":
                error_code = item.get("errorCode", "")
                error_msg = item.get("errorMessage", "下载失败")
                update_data["status"] = "failed"
                update_data["error"] = f"aria2 错误 [{error_code}]: {error_msg}"
            elif aria2_status == "removed":
                update_data["status"] = "cancelled"

            await db.update_task(task_id, **update_data)
            await self._broadcast_task_update(task_id)

            # 下载完成 → 触发上传
            if aria2_status == "complete" and current_status != "uploading":
                local_path = parsed["file_path"]
                if local_path:
                    asyncio.create_task(self._handle_download_complete(task_id, gid))
                else:
                    await db.update_task(task_id, status="completed")
                    await self._broadcast_task_update(task_id)

    async def _handle_download_complete(self, task_id: str, gid: str):
        """下载完成后自动上传到 TelDrive"""
        if gid in self._uploading_gids:
            return
        self._uploading_gids.add(gid)

        try:
            task = await db.get_task(task_id)
            if not task or not task.get("local_path"):
                logger.warning(f"任务 {task_id} 无本地文件路径，跳过上传")
                return

            local_path = task["local_path"]
            teldrive_path = task.get("teldrive_path", "/")

            # 如果配置了 upload_dir，将 aria2 下载路径映射到本地上传路径
            upload_dir = self.config["teldrive"].get("upload_dir", "")
            if upload_dir:
                download_dir = get_download_dir(self.config)
                # 将下载目录前缀替换为 upload_dir
                norm_local = os.path.normpath(local_path)
                norm_dl = os.path.normpath(download_dir)
                if norm_local.startswith(norm_dl):
                    rel = os.path.relpath(norm_local, norm_dl)
                    local_path = os.path.join(upload_dir, rel)
                    logger.info(f"路径映射: {task['local_path']} -> {local_path}")
                else:
                    # local_path 不在 download_dir 下，直接用 upload_dir + 文件名
                    local_path = os.path.join(upload_dir, os.path.basename(local_path))
                    logger.info(f"路径映射(basename): {task['local_path']} -> {local_path}")

            # 检查文件是否存在
            if not os.path.exists(local_path):
                error_msg = f"文件不存在: {local_path}"
                logger.error(f"任务 {task_id} 上传失败: {error_msg}")
                await db.update_task(task_id, status="failed", error=error_msg)
                await self._broadcast_task_update(task_id)
                return

            await db.update_task(task_id, status="uploading",
                                 download_progress=100.0, download_speed="")
            await self._broadcast_task_update(task_id)

            # 上传
            await self._upload(task_id, local_path, teldrive_path)

            # 清理本地文件
            task = await db.get_task(task_id)
            if task and task["status"] == "completed" and self.config["general"]["auto_delete"]:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
                    logger.info(f"已删除本地文件: {local_path}")

        except Exception as e:
            logger.error(f"任务 {task_id} 上传失败: {e}")
            await db.update_task(task_id, status="failed", error=str(e))
            await self._broadcast_task_update(task_id)
        finally:
            self._uploading_gids.discard(gid)

    # ===========================================
    # 上传
    # ===========================================

    async def _upload(self, task_id: str, local_path: str, teldrive_path: str = "/"):
        """上传文件到 TelDrive"""
        import time
        _last_broadcast = [0.0]   # 上次广播时间
        _last_progress = [0.0]    # 上次广播的进度值

        async def progress_callback(uploaded: int, total: int):
            if total > 0:
                progress = round(uploaded / total * 100, 1)
                now = time.monotonic()
                # 节流：进度变化 ≥ 1% 或距上次超 1 秒才广播
                if (progress - _last_progress[0] >= 1.0 or
                        now - _last_broadcast[0] >= 1.0 or
                        progress >= 100.0):
                    _last_progress[0] = progress
                    _last_broadcast[0] = now
                    await db.update_task(task_id, upload_progress=progress)
                    await self._broadcast_task_update(task_id)

        result = await self.teldrive.upload_file_chunked(
            local_path, teldrive_path, progress_callback
        )

        if result.get("success"):
            await db.update_task(task_id, status="completed",
                                 upload_progress=100.0)
            await self._broadcast_task_update(task_id)
            logger.info(f"任务 {task_id} 上传完成")
        else:
            error = result.get("error", "上传失败")
            raise Exception(error)

    async def _broadcast_task_update(self, task_id: str):
        """广播任务状态更新"""
        task = await db.get_task(task_id)
        if task:
            await self.broadcast({
                "type": "task_update",
                "data": task
            })

    # ===========================================
    # 手动添加任务（通过面板）
    # ===========================================

    async def add_task(self, url: str, filename: str = None,
                       teldrive_path: str = "/") -> dict:
        """通过面板手动添加下载+上传任务"""
        download_dir = get_download_dir(self.config)
        options = {"dir": download_dir}
        if filename:
            options["out"] = filename

        # 提交给 aria2
        gid = await self.aria2.add_uri(url, options)

        # 入库（用 GID 作为 task_id）
        task = await db.add_task(gid, url, filename, teldrive_path)
        await db.update_task(gid, status="downloading", aria2_gid=gid)
        self._known_gids.add(gid)

        await self._broadcast_task_update(gid)
        return await db.get_task(gid)

    # ===========================================
    # 任务操作
    # ===========================================

    async def pause_task(self, task_id: str) -> dict:
        """暂停任务"""
        task = await db.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}
        if task["status"] != "downloading":
            return {"success": False, "message": "只能暂停下载中的任务"}

        try:
            await self.aria2.pause(task["aria2_gid"])
            await db.update_task(task_id, status="paused")
            await self._broadcast_task_update(task_id)
            return {"success": True, "message": "已暂停"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def resume_task(self, task_id: str) -> dict:
        """恢复任务"""
        task = await db.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}
        if task["status"] != "paused":
            return {"success": False, "message": "只能恢复已暂停的任务"}

        try:
            await self.aria2.unpause(task["aria2_gid"])
            await db.update_task(task_id, status="downloading")
            await self._broadcast_task_update(task_id)
            return {"success": True, "message": "已恢复"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def cancel_task(self, task_id: str) -> dict:
        """取消任务"""
        task = await db.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}
        if task["status"] in ("completed", "cancelled"):
            return {"success": False, "message": "任务已结束"}

        try:
            if task.get("aria2_gid"):
                try:
                    await self.aria2.force_remove(task["aria2_gid"])
                except Exception:
                    pass
            # 删除本地文件
            if task.get("local_path") and os.path.exists(task["local_path"]):
                os.remove(task["local_path"])
            await db.update_task(task_id, status="cancelled")
            await self._broadcast_task_update(task_id)
            return {"success": True, "message": "已取消"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def retry_task(self, task_id: str) -> dict:
        """重试失败的任务"""
        task = await db.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}
        if task["status"] != "failed":
            return {"success": False, "message": "只能重试失败的任务"}

        if not task.get("url"):
            return {"success": False, "message": "无法重试：缺少下载 URL"}

        # 重新提交给 aria2
        download_dir = get_download_dir(self.config)
        options = {"dir": download_dir}
        if task.get("filename"):
            options["out"] = task["filename"]

        try:
            gid = await self.aria2.add_uri(task["url"], options)
            # 从旧 GID 缓存中移除
            old_gid = task.get("aria2_gid")
            if old_gid:
                self._known_gids.discard(old_gid)

            await db.update_task(
                task_id, status="downloading", aria2_gid=gid,
                download_progress=0, upload_progress=0,
                download_speed="", upload_speed="",
                error=None, local_path=None
            )
            self._known_gids.add(gid)
            await self._broadcast_task_update(task_id)
            return {"success": True, "message": "正在重试"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def delete_task(self, task_id: str) -> dict:
        """删除任务记录"""
        task = await db.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}

        # 如果任务还在进行中，先取消
        if task["status"] in ("downloading", "uploading", "pending"):
            await self.cancel_task(task_id)

        gid = task.get("aria2_gid")
        if gid:
            self._known_gids.discard(gid)
            # 从 aria2 移除下载记录
            try:
                await self.aria2.remove(gid)
            except Exception:
                pass

        await db.delete_task(task_id)
        await self.broadcast({"type": "task_deleted", "data": {"task_id": task_id}})
        return {"success": True, "message": "已删除"}

    async def get_all_tasks(self) -> list:
        """获取所有任务"""
        return await db.get_all_tasks()

    async def get_task(self, task_id: str) -> Optional[dict]:
        """获取单个任务"""
        return await db.get_task(task_id)


# 全局单例
task_manager = TaskManager()
