"""任务管理器 - 监控 aria2 下载并自动上传到 TelDrive"""

import asyncio
import uuid
import os
import shutil
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
        # 异步同步 aria2 全局选项
        asyncio.create_task(self._apply_aria2_options())

    async def _apply_aria2_options(self):
        """将本地配置同步到远端 aria2"""
        try:
            cfg = self.config
            options = {
                "max-concurrent-downloads": str(cfg["aria2"].get("max_concurrent", 3)),
                "dir": cfg["aria2"].get("download_dir", "./downloads"),
            }
            await self.aria2.change_global_option(options)
            logger.info(f"已同步 aria2 全局选项: {options}")
        except Exception as e:
            logger.warning(f"同步 aria2 全局选项失败: {e}")

    async def start(self):
        """启动任务管理器"""
        await db.init_db()
        self._init_clients()
        # 同步配置到 aria2
        await self._apply_aria2_options()
        # 加载已有任务的 GID 到缓存
        all_tasks = await db.get_all_tasks()
        for t in all_tasks:
            if t.get("aria2_gid"):
                self._known_gids.add(t["aria2_gid"])

        # 恢复僵死的 uploading 任务（应用重启后 uploading 状态不会自动恢复）
        for t in all_tasks:
            if t["status"] == "uploading":
                task_id = t["task_id"]
                local_path = t.get("local_path", "")
                if local_path and os.path.exists(local_path):
                    logger.info(f"恢复僵死的上传任务: {task_id} ({t.get('filename', '?')})")
                    asyncio.create_task(self._retry_upload(task_id))
                else:
                    logger.warning(f"僵死上传任务 {task_id} 本地文件不存在，标记失败")
                    await db.update_task(task_id, status="failed",
                                         error="上传中断且本地文件不存在")

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

            # BT 多文件下载：用 aria2 的 dir + bittorrent.info.name 拼出文件夹路径
            bt_name = item.get("bittorrent", {}).get("info", {}).get("name", "")
            task_dir = item.get("dir", "")
            files_count = len(item.get("files", []))
            if bt_name and task_dir:
                logger.debug(f"[BT检测] gid={gid}, bt_name={bt_name}, "
                             f"task_dir={task_dir}, files_count={files_count}")
                if files_count > 1:
                    bt_folder = os.path.join(task_dir, bt_name)
                    parsed["file_path"] = bt_folder
                    parsed["filename"] = bt_name
                    logger.info(f"[BT检测] 多文件BT，使用文件夹路径: {bt_folder}")

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

    def _map_upload_path(self, local_path: str) -> str:
        """如果配置了 upload_dir，将 aria2 下载路径映射到本地上传路径"""
        upload_dir = self.config["teldrive"].get("upload_dir", "")
        if not upload_dir:
            return local_path

        download_dir = get_download_dir(self.config)
        norm_local = os.path.normpath(local_path)
        norm_dl = os.path.normpath(download_dir)
        if norm_local.startswith(norm_dl):
            rel = os.path.relpath(norm_local, norm_dl)
            mapped = os.path.join(upload_dir, rel)
            logger.info(f"路径映射: {local_path} -> {mapped}")
            return mapped
        else:
            mapped = os.path.join(upload_dir, os.path.basename(local_path))
            logger.info(f"路径映射(basename): {local_path} -> {mapped}")
            return mapped

    def _detect_folder_path(self, file_path: str) -> str:
        """检测文件是否在 BT 下载子文件夹内，如果是则返回文件夹路径，否则返回原始文件路径。

        原理：aria2 BT 下载会在 download_dir 下创建子文件夹（如 download_dir/TorrentName/file.mp4）。
        通过对比文件路径和 download_dir，如果相对路径有多级（如 TorrentName/file.mp4），
        说明文件在 BT 子文件夹内，取出顶层文件夹路径作为上传根。
        """
        download_dir = get_download_dir(self.config)
        norm_dl = os.path.normpath(download_dir)
        norm_fp = os.path.normpath(file_path)

        # 文件必须在 download_dir 下
        if not norm_fp.startswith(norm_dl + os.sep):
            return file_path

        rel = os.path.relpath(norm_fp, norm_dl)
        parts = Path(rel).parts

        if len(parts) > 1:
            # 文件在子文件夹内（如 TorrentName/file.mp4 或 TorrentName/Sub/file.mp4）
            top_folder = parts[0]
            folder_path = os.path.join(download_dir, top_folder)
            if os.path.isdir(folder_path):
                logger.info(f"检测到 BT 文件夹: {folder_path}")
                return folder_path

        return file_path

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

            original_path = task["local_path"]
            teldrive_path = task.get("teldrive_path", "/")

            logger.info(f"[上传调试] 任务 {task_id}: "
                        f"DB local_path={original_path}, "
                        f"teldrive_path={teldrive_path}")

            # 检测是否在 BT 子文件夹内（在 upload_dir 映射之前，用原始路径判断）
            detected_path = self._detect_folder_path(original_path)
            logger.info(f"[上传调试] _detect_folder_path: {original_path} -> {detected_path}")

            # 再做 upload_dir 映射
            local_path = self._map_upload_path(detected_path)
            logger.info(f"[上传调试] _map_upload_path: {detected_path} -> {local_path}")

            is_dir = os.path.isdir(local_path)
            is_file = os.path.isfile(local_path)
            exists = os.path.exists(local_path)
            logger.info(f"[上传调试] local_path={local_path}, "
                        f"exists={exists}, isdir={is_dir}, isfile={is_file}")

            # 检查文件/文件夹是否存在
            if not exists:
                error_msg = f"文件不存在: {local_path}"
                logger.error(f"任务 {task_id} 上传失败: {error_msg}")
                await db.update_task(task_id, status="failed", error=error_msg)
                await self._broadcast_task_update(task_id)
                return

            await db.update_task(task_id, status="uploading",
                                 download_progress=100.0, download_speed="")
            await self._broadcast_task_update(task_id)

            # 判断是文件夹还是单文件
            if is_dir:
                logger.info(f"[上传调试] 走文件夹上传路径: {local_path}")
                await self._upload_directory(task_id, local_path, teldrive_path)
            else:
                logger.info(f"[上传调试] 走单文件上传路径: {local_path}")
                await self._upload(task_id, local_path, teldrive_path)

            # 清理本地文件/文件夹
            task = await db.get_task(task_id)
            if task and task["status"] == "completed" and self.config["general"]["auto_delete"]:
                if local_path and os.path.exists(local_path):
                    if os.path.isdir(local_path):
                        shutil.rmtree(local_path, ignore_errors=True)
                        logger.info(f"已删除本地文件夹: {local_path}")
                    else:
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

    async def _upload_directory(self, task_id: str, dir_path: str, teldrive_path: str = "/"):
        """递归上传文件夹到 TelDrive，保留目录结构"""
        import time

        # 收集所有文件及其大小
        dir_name = os.path.basename(dir_path)
        base_teldrive_path = teldrive_path.rstrip("/") + "/" + dir_name
        all_files = []
        for root, _dirs, filenames in os.walk(dir_path):
            for fname in filenames:
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, dir_path)
                file_size = os.path.getsize(full_path)
                all_files.append((full_path, rel_path, file_size))

        if not all_files:
            logger.warning(f"任务 {task_id} 文件夹为空: {dir_path}")
            await db.update_task(task_id, status="completed", upload_progress=100.0)
            await self._broadcast_task_update(task_id)
            return

        total_size = sum(s for _, _, s in all_files)
        uploaded_total = [0]  # 已上传的总字节数
        _last_broadcast = [0.0]
        _last_progress = [0.0]

        logger.info(f"任务 {task_id} 检测到文件夹: {dir_path}，"
                    f"共 {len(all_files)} 个文件，总大小 {total_size} bytes，"
                    f"上传到 {base_teldrive_path}")

        upload_timeout_per_file = self.config["general"].get("max_retries", 3) * 600

        for idx, (full_path, rel_path, file_size) in enumerate(all_files, 1):
            # 计算该文件在 TelDrive 上的目标路径
            rel_dir = os.path.dirname(rel_path).replace("\\", "/")
            if rel_dir:
                file_teldrive_path = base_teldrive_path + "/" + rel_dir
            else:
                file_teldrive_path = base_teldrive_path

            logger.info(f"任务 {task_id} 上传文件 [{idx}/{len(all_files)}]: "
                        f"{rel_path} -> {file_teldrive_path}")

            # 为每个文件创建进度回调（汇总到整体进度）
            file_uploaded_before = uploaded_total[0]

            async def make_progress_cb(base_uploaded):
                async def progress_callback(uploaded: int, total: int):
                    if total_size > 0:
                        current_total = base_uploaded + uploaded
                        progress = round(current_total / total_size * 100, 1)
                        now = time.monotonic()
                        if (progress - _last_progress[0] >= 1.0 or
                                now - _last_broadcast[0] >= 1.0 or
                                progress >= 100.0):
                            _last_progress[0] = progress
                            _last_broadcast[0] = now
                            await db.update_task(task_id, upload_progress=progress)
                            await self._broadcast_task_update(task_id)
                return progress_callback

            cb = await make_progress_cb(file_uploaded_before)

            try:
                result = await asyncio.wait_for(
                    self.teldrive.upload_file_chunked(
                        full_path, file_teldrive_path, cb
                    ),
                    timeout=upload_timeout_per_file
                )
            except asyncio.TimeoutError:
                raise Exception(f"上传超时: {rel_path}（超过 {upload_timeout_per_file}s）")

            if not result.get("success"):
                raise Exception(f"上传失败: {rel_path} - {result.get('error', '未知错误')}")

            uploaded_total[0] += file_size
            logger.info(f"任务 {task_id} 文件上传成功: {rel_path}")

        # 所有文件上传完成
        await db.update_task(task_id, status="completed", upload_progress=100.0)
        await self._broadcast_task_update(task_id)
        logger.info(f"任务 {task_id} 文件夹上传完成: {dir_path}，共 {len(all_files)} 个文件")

    async def _upload(self, task_id: str, local_path: str, teldrive_path: str = "/"):
        """上传单个文件到 TelDrive"""
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

        # 整体超时保护：防止上传无限挂起 (30 分钟)
        upload_timeout = self.config["general"].get("max_retries", 3) * 600
        try:
            result = await asyncio.wait_for(
                self.teldrive.upload_file_chunked(
                    local_path, teldrive_path, progress_callback
                ),
                timeout=upload_timeout
            )
        except asyncio.TimeoutError:
            raise Exception(f"上传超时（超过 {upload_timeout}s）")

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
        download_dir = self.config["aria2"].get("download_dir", "./downloads")
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
            # 删除本地文件/文件夹
            local = task.get("local_path", "")
            if local:
                mapped = self._map_upload_path(local)
                for p in (local, mapped):
                    if p and os.path.exists(p):
                        if os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            os.remove(p)
            await db.update_task(task_id, status="cancelled")
            await self._broadcast_task_update(task_id)
            return {"success": True, "message": "已取消"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def retry_task(self, task_id: str) -> dict:
        """重试失败/卡住的任务"""
        task = await db.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}
        if task["status"] not in ("failed", "uploading"):
            return {"success": False, "message": "只能重试失败或上传中的任务"}

        # 如果本地文件/文件夹已存在，直接重试上传
        local_path = task.get("local_path", "")
        mapped_path = self._map_upload_path(local_path) if local_path else ""

        # 检查原始路径或映射路径是否存在
        if mapped_path and os.path.exists(mapped_path):
            asyncio.create_task(self._retry_upload(task_id))
            return {"success": True, "message": "正在重试上传"}
        if local_path and os.path.exists(local_path):
            asyncio.create_task(self._retry_upload(task_id))
            return {"success": True, "message": "正在重试上传"}

        # 否则需要重新下载
        if not task.get("url"):
            return {"success": False, "message": "无法重试：缺少下载 URL 且本地文件不存在"}

        download_dir = self.config["aria2"].get("download_dir", "./downloads")
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
            return {"success": True, "message": "正在重新下载"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def _retry_upload(self, task_id: str):
        """仅重试上传步骤"""
        try:
            task = await db.get_task(task_id)
            if not task:
                return

            original_path = task.get("local_path", "")
            teldrive_path = task.get("teldrive_path", "/")

            # 检测 BT 文件夹 + upload_dir 映射
            detected_path = self._detect_folder_path(original_path) if original_path else ""
            local_path = self._map_upload_path(detected_path) if detected_path else ""

            if not local_path or not os.path.exists(local_path):
                await db.update_task(task_id, status="failed",
                                     error="本地文件不存在，无法重试上传")
                await self._broadcast_task_update(task_id)
                return

            # 重置上传状态
            await db.update_task(task_id, status="uploading",
                                 upload_progress=0.0, error=None)
            await self._broadcast_task_update(task_id)

            # 判断是文件夹还是单文件
            if os.path.isdir(local_path):
                await self._upload_directory(task_id, local_path, teldrive_path)
            else:
                await self._upload(task_id, local_path, teldrive_path)

            # 清理本地文件/文件夹
            task = await db.get_task(task_id)
            if task and task["status"] == "completed" and self.config["general"]["auto_delete"]:
                if local_path and os.path.exists(local_path):
                    if os.path.isdir(local_path):
                        shutil.rmtree(local_path, ignore_errors=True)
                        logger.info(f"已删除本地文件夹: {local_path}")
                    else:
                        os.remove(local_path)
                        logger.info(f"已删除本地文件: {local_path}")

        except Exception as e:
            logger.error(f"任务 {task_id} 重试上传失败: {e}")
            await db.update_task(task_id, status="failed", error=str(e))
            await self._broadcast_task_update(task_id)

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
