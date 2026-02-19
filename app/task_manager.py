"""任务管理器 - 监控 aria2 下载并自动上传到 TelDrive"""

import asyncio
import uuid
import os
import shutil
import logging
import psutil
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
        # 上传协程追踪：task_id -> asyncio.Task，重试时可取消旧任务
        self._upload_tasks: dict = {}
        # 上传速度跟踪：per-task 已上传字节 → monitor loop 汇总算总速度
        self._task_uploaded_bytes: dict = {}   # task_id -> 当前已上传字节
        self._upload_total_snapshot: int = 0   # 上次快照时的总字节
        self._upload_time_snapshot: float = 0.0
        self._upload_speed: float = 0.0
        # 磁盘空间限制：空间不足时暂停 aria2 下载
        self._disk_paused: bool = False
        self._disk_usage_info: dict = {}  # 缓存磁盘使用信息
        # CPU 连续调控
        self._cpu_paused_gids: set = set()  # 因 CPU 限流暂停的 aria2 GID
        self._cpu_info: dict = {}
        self._original_upload_concurrency: int = 0  # 保存原始上传并发数

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
                    upload_t = asyncio.create_task(self._retry_upload(task_id))
                    self._upload_tasks[task_id] = upload_t
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
        import time
        self._upload_time_snapshot = time.monotonic()
        self._upload_total_snapshot = 0
        while self._running:
            try:
                # 计算总上传速度
                now = time.monotonic()
                elapsed = now - self._upload_time_snapshot
                if elapsed >= 2.0:
                    current_total = sum(self._task_uploaded_bytes.values())
                    self._upload_speed = (current_total - self._upload_total_snapshot) / elapsed
                    if self._upload_speed < 0:
                        self._upload_speed = 0.0
                    self._upload_total_snapshot = current_total
                    self._upload_time_snapshot = now

                # 检测磁盘空间
                await self._check_disk_usage()
                # 检测 CPU 使用率
                await self._check_cpu_usage()

                await self._sync_aria2_tasks()
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"监控循环异常: {e}")
                await asyncio.sleep(5)

    async def _check_disk_usage(self):
        """检测磁盘使用量，空间不足时暂停 aria2 下载"""
        max_gb = self.config["general"].get("max_disk_usage", 0)

        # 始终采集磁盘信息用于仪表盘显示
        try:
            download_dir = get_download_dir(self.config)
            usage = shutil.disk_usage(download_dir)
            used_gb = round(usage.used / (1024 ** 3), 2)
            total_gb = round(usage.total / (1024 ** 3), 2)
            free_gb = round(usage.free / (1024 ** 3), 2)
            percent = round(used_gb / total_gb * 100, 1) if total_gb > 0 else 0

            self._disk_usage_info = {
                "used_gb": used_gb,
                "total_gb": total_gb,
                "free_gb": free_gb,
                "percent": percent,
                "limit_gb": max_gb,
                "paused": self._disk_paused
            }
        except Exception as e:
            logger.debug(f"检测磁盘使用失败: {e}")
            return

        # 限流逻辑：仅在设置了上限时生效
        if max_gb <= 0:
            if self._disk_paused:
                await self._resume_aria2_downloads()
            return

        if used_gb >= max_gb and not self._disk_paused:
            # 超限 → 暂停所有下载（不只是阻止新任务，活跃下载也要停）
            logger.warning(f"磁盘使用 {used_gb}GB >= 限制 {max_gb}GB，暂停所有 aria2 下载")
            try:
                await self.aria2.change_global_option(
                    {"max-concurrent-downloads": "0"})
                await self.aria2.pause_all()
                self._cpu_paused_gids.clear()  # 磁盘全暂停，CPU 计数清零
                self._disk_paused = True
                self._disk_usage_info["paused"] = True
            except Exception as e:
                logger.error(f"暂停 aria2 下载失败: {e}")

        elif used_gb < max_gb * 0.9 and self._disk_paused:
            # 恢复（留 10% 缓冲避免反复切换）
            await self._resume_aria2_downloads()

    def _get_cpu_adjusted_concurrent(self) -> int:
        """获取 CPU 限流调整后的并发数"""
        max_c = self.config["aria2"].get("max_concurrent", 3)
        return max(0, max_c - len(self._cpu_paused_gids))

    async def _resume_aria2_downloads(self):
        """恢复 aria2 下载并发数（考虑 CPU 限流状态）"""
        target = str(self._get_cpu_adjusted_concurrent())
        try:
            await self.aria2.unpause_all()
            await self.aria2.change_global_option(
                {"max-concurrent-downloads": target})
            self._disk_paused = False
            self._disk_usage_info["paused"] = False
            logger.info(f"磁盘空间已恢复，恢复 aria2 下载，并发数={target}")
        except Exception as e:
            logger.error(f"恢复 aria2 下载失败: {e}")

    async def _check_cpu_usage(self):
        """检测 CPU 使用率，通过逐个暂停/恢复下载保持 CPU 在上下限之间"""
        cpu_limit = self.config["general"].get("cpu_limit", 85)
        if cpu_limit <= 0:
            if self._cpu_paused_gids:
                await self._cpu_resume_all()
            self._cpu_info = {}
            return

        try:
            cpu_pct = psutil.cpu_percent(interval=None)
            cpu_lower = cpu_limit * 0.75  # 下限，例如 85*0.75 ≈ 64%

            # 计算限流级别用于前端显示
            max_c = self.config["aria2"].get("max_concurrent", 3)
            paused_count = len(self._cpu_paused_gids)
            if paused_count == 0:
                throttle_level = 0
            elif paused_count < max_c:
                throttle_level = 1
            else:
                throttle_level = 2

            self._cpu_info = {
                "percent": cpu_pct,
                "limit": cpu_limit,
                "throttled": throttle_level
            }

            # 保存原始上传并发数（仅首次）
            if self._original_upload_concurrency == 0 and self.teldrive:
                self._original_upload_concurrency = self.teldrive.upload_concurrency

            if cpu_pct >= cpu_limit:
                # CPU 超上限：暂停一个活跃下载
                await self._cpu_pause_one()
            elif cpu_pct < cpu_lower and self._cpu_paused_gids:
                # CPU 低于下限：恢复一个暂停的下载
                await self._cpu_resume_one()

        except Exception as e:
            logger.debug(f"检测 CPU 使用失败: {e}")

    async def _cpu_pause_one(self):
        """暂停一个活跃的 aria2 下载以降低 CPU"""
        try:
            active = await self.aria2.tell_active() or []
            for item in active:
                gid = item.get("gid", "")
                if not gid or gid in self._cpu_paused_gids:
                    continue
                try:
                    await self.aria2.pause(gid)
                except Exception:
                    # BT 任务可能需要 forcePause
                    try:
                        await self.aria2._call("aria2.forcePause", gid)
                    except Exception:
                        continue
                self._cpu_paused_gids.add(gid)
                # 同步减少并发数，防止等待中的任务补位
                new_c = self._get_cpu_adjusted_concurrent()
                if not self._disk_paused:
                    await self.aria2.change_global_option(
                        {"max-concurrent-downloads": str(new_c)})
                # 有暂停的下载时降低上传并发
                if self.teldrive and self._original_upload_concurrency > 1:
                    self.teldrive.upload_concurrency = max(
                        1, self._original_upload_concurrency // 2)
                logger.info(f"CPU 限流：暂停下载 {gid}，并发调整为 {new_c}")
                return
            logger.debug("CPU 过高但无活跃下载可暂停")
        except Exception as e:
            logger.error(f"CPU 限流暂停下载失败: {e}")

    async def _cpu_resume_one(self):
        """恢复一个因 CPU 限流暂停的下载"""
        if not self._cpu_paused_gids:
            return
        gid = next(iter(self._cpu_paused_gids))
        try:
            await self.aria2.unpause(gid)
            logger.info(f"CPU 恢复：恢复下载 {gid}")
        except Exception:
            # GID 可能已不存在（用户取消等），静默清理
            logger.debug(f"恢复下载 {gid} 失败，清理记录")
        self._cpu_paused_gids.discard(gid)

        # 同步恢复并发数
        new_c = self._get_cpu_adjusted_concurrent()
        if not self._disk_paused:
            try:
                await self.aria2.change_global_option(
                    {"max-concurrent-downloads": str(new_c)})
            except Exception:
                pass
        # 全部恢复后还原上传并发
        if not self._cpu_paused_gids and self.teldrive and self._original_upload_concurrency > 0:
            self.teldrive.upload_concurrency = self._original_upload_concurrency
        logger.info(f"CPU 恢复：并发调整为 {new_c}")

    async def _cpu_resume_all(self):
        """恢复所有因 CPU 限流暂停的下载"""
        for gid in list(self._cpu_paused_gids):
            try:
                await self.aria2.unpause(gid)
            except Exception:
                pass
        self._cpu_paused_gids.clear()

        max_c = self.config["aria2"].get("max_concurrent", 3)
        if not self._disk_paused:
            try:
                await self.aria2.change_global_option(
                    {"max-concurrent-downloads": str(max_c)})
            except Exception:
                pass
        if self.teldrive and self._original_upload_concurrency > 0:
            self.teldrive.upload_concurrency = self._original_upload_concurrency
        logger.info(f"CPU 限流解除：恢复所有下载，并发={max_c}")

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
            broadcast_data = {
                "download_speed": int(stat.get("downloadSpeed", 0)),
                "upload_speed": int(self._upload_speed),
                "active": len(active),
                "waiting": len(waiting),
                "stopped": len(stopped)
            }
            # 附加磁盘使用信息
            if self._disk_usage_info:
                broadcast_data["disk"] = self._disk_usage_info
            # 附加 CPU 使用信息
            if self._cpu_info:
                broadcast_data["cpu"] = self._cpu_info
            await self.broadcast({
                "type": "global_stat",
                "data": broadcast_data
            })
        except Exception:
            pass

        for item in all_aria2_tasks:
            gid = item.get("gid", "")
            if not gid:
                continue

            parsed = Aria2Client.parse_status(item)
            aria2_status = parsed["status"]

            # BT 下载：用 aria2 的 dir + bittorrent.info.name 作为路径
            # 单文件BT: bt_path 是文件路径 → isdir=False → 单文件上传
            # 多文件BT: bt_path 是文件夹路径 → isdir=True → 文件夹上传
            bt_name = item.get("bittorrent", {}).get("info", {}).get("name", "")
            task_dir = item.get("dir", "")
            if bt_name and task_dir:
                bt_path = os.path.join(task_dir, bt_name)
                logger.info(f"[BT检测] gid={gid}, bt_name={bt_name}, "
                            f"task_dir={task_dir}, bt_path={bt_path}, "
                            f"files={len(item.get('files', []))}")
                parsed["file_path"] = bt_path
                parsed["filename"] = bt_name

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
                            t = asyncio.create_task(self._handle_download_complete(task_id, gid))
                            self._upload_tasks[task_id] = t
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
                    t = asyncio.create_task(self._handle_download_complete(task_id, gid))
                    self._upload_tasks[task_id] = t
                else:
                    await db.update_task(task_id, status="completed")
                    await self._broadcast_task_update(task_id)

    def _calc_teldrive_path(self, local_path: str) -> str:
        """计算文件在 TelDrive 上的目标目录。

        公式: target_path + (文件所在目录 - download_dir)

        例如:
            target_path   = /movies
            download_dir  = /downloads
            local_path    = /downloads/电视剧/Season1/ep01.mp4
            → 返回 /movies/电视剧/Season1
        """
        target_path = self.config["teldrive"].get("target_path", "/")
        download_dir = get_download_dir(self.config)
        norm_dl = os.path.normpath(download_dir)
        norm_fp = os.path.normpath(local_path)

        # 取文件所在目录（如果是目录则取其父目录，因为目录名会在上传时拼上去）
        if os.path.isdir(norm_fp):
            file_dir = norm_fp
        else:
            file_dir = os.path.dirname(norm_fp)

        # 计算相对路径: 文件所在目录 - download_dir
        if file_dir.startswith(norm_dl + os.sep) or file_dir == norm_dl:
            rel_dir = os.path.relpath(file_dir, norm_dl).replace("\\", "/")
            if rel_dir == ".":
                result = target_path
            else:
                result = target_path.rstrip("/") + "/" + rel_dir
        else:
            # 不在 download_dir 下时直接用 target_path
            result = target_path

        logger.info(f"[路径] {local_path} -> teldrive={result}")
        return result

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
            teldrive_path = self._calc_teldrive_path(local_path)

            # 等待文件就绪（aria2 可能还在写入/移动文件）
            for attempt in range(5):
                if os.path.exists(local_path):
                    break
                logger.info(f"任务 {task_id} 等待文件就绪 ({attempt+1}/5): {local_path}")
                await asyncio.sleep(1)

            logger.info(f"[上传] 任务 {task_id}: "
                        f"local={local_path}, "
                        f"isdir={os.path.isdir(local_path)}, "
                        f"exists={os.path.exists(local_path)}, "
                        f"teldrive={teldrive_path}")

            if not os.path.exists(local_path):
                # 输出调试信息：列出下载目录内容，帮助排查路径问题
                download_dir = get_download_dir(self.config)
                try:
                    dir_contents = os.listdir(download_dir) if os.path.isdir(download_dir) else []
                    logger.error(
                        f"任务 {task_id} 文件不存在!\n"
                        f"  期望路径: {local_path}\n"
                        f"  下载目录: {download_dir}\n"
                        f"  目录存在: {os.path.isdir(download_dir)}\n"
                        f"  目录内容: {dir_contents[:20]}")
                except Exception:
                    pass
                error_msg = f"本地文件不存在: {local_path}"
                await db.update_task(task_id, status="failed", error=error_msg)
                await self._broadcast_task_update(task_id)
                return

            await db.update_task(task_id, status="uploading",
                                 download_progress=100.0, download_speed="")
            await self._broadcast_task_update(task_id)

            if os.path.isdir(local_path):
                logger.info(f"[上传] 走文件夹上传: {local_path}")
                await self._upload_directory(task_id, local_path, teldrive_path)
            else:
                logger.info(f"[上传] 走单文件上传: {local_path} -> "
                            f"teldrive={teldrive_path}")
                await self._upload(task_id, local_path, teldrive_path)

            # 上传成功后清理本地文件
            await self._auto_delete_local(task_id, local_path)

        except asyncio.CancelledError:
            logger.info(f"任务 {task_id} 上传被取消（用户重试或取消）")
            # 不标记 failed，让重试逻辑接管
        except Exception as e:
            logger.error(f"任务 {task_id} 上传失败: {e}")
            await db.update_task(task_id, status="failed", error=str(e))
            await self._broadcast_task_update(task_id)
        finally:
            self._uploading_gids.discard(gid)
            self._upload_tasks.pop(task_id, None)

    async def _auto_delete_local(self, task_id: str, local_path: str):
        """上传成功后自动删除本地文件（如果配置了 auto_delete）"""
        try:
            task = await db.get_task(task_id)
            if not task or task["status"] != "completed":
                return
            if not self.config["general"].get("auto_delete", True):
                return
            if not local_path or not os.path.exists(local_path):
                return
            if os.path.isdir(local_path):
                shutil.rmtree(local_path, ignore_errors=True)
                logger.info(f"已删除本地文件夹: {local_path}")
            else:
                os.remove(local_path)
                logger.info(f"已删除本地文件: {local_path}")
        except Exception as e:
            logger.warning(f"删除本地文件失败: {local_path}, {e}")

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

        # 注册 per-task 字节追踪
        self._task_uploaded_bytes[task_id] = 0

        logger.info(f"任务 {task_id} 检测到文件夹: {dir_path}，"
                    f"共 {len(all_files)} 个文件，总大小 {total_size} bytes，"
                    f"上传到 {base_teldrive_path}")

        upload_timeout_per_file = self.config["general"].get("max_retries", 3) * 300

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
                        self._task_uploaded_bytes[task_id] = current_total
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
        self._task_uploaded_bytes.pop(task_id, None)
        await db.update_task(task_id, status="completed", upload_progress=100.0)
        await self._broadcast_task_update(task_id)
        logger.info(f"任务 {task_id} 文件夹上传完成: {dir_path}，共 {len(all_files)} 个文件")

    async def _upload(self, task_id: str, local_path: str, teldrive_path: str = "/"):
        """上传单个文件到 TelDrive"""
        import time
        _last_broadcast = [0.0]   # 上次广播时间
        _last_progress = [0.0]    # 上次广播的进度值

        # 注册 per-task 字节追踪
        self._task_uploaded_bytes[task_id] = 0

        async def progress_callback(uploaded: int, total: int):
            if total > 0:
                self._task_uploaded_bytes[task_id] = uploaded
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

        # 整体超时保护：防止上传无限挂起 (15 分钟)
        upload_timeout = self.config["general"].get("max_retries", 3) * 300
        try:
            result = await asyncio.wait_for(
                self.teldrive.upload_file_chunked(
                    local_path, teldrive_path, progress_callback
                ),
                timeout=upload_timeout
            )
        except asyncio.TimeoutError:
            raise Exception(f"上传超时（超过 {upload_timeout}s）")

        self._task_uploaded_bytes.pop(task_id, None)  # 上传完成，移除追踪

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
            if local and os.path.exists(local):
                if os.path.isdir(local):
                    shutil.rmtree(local, ignore_errors=True)
                else:
                    os.remove(local)
            await db.update_task(task_id, status="cancelled")
            await self._broadcast_task_update(task_id)
            return {"success": True, "message": "已取消"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _cancel_existing_upload(self, task_id: str):
        """取消正在进行的上传任务（如果有）"""
        existing_task = self._upload_tasks.pop(task_id, None)
        if existing_task and not existing_task.done():
            existing_task.cancel()
            logger.info(f"已取消任务 {task_id} 的旧上传协程")

        # 清理 _uploading_gids 中对应的 GID，解除去重锁定
        # task_id 本身可能就是 GID（直接用 GID 做 task_id 的情况）
        self._uploading_gids.discard(task_id)

    async def retry_task(self, task_id: str) -> dict:
        """重试失败/卡住的任务"""
        task = await db.get_task(task_id)
        if not task:
            return {"success": False, "message": "任务不存在"}
        if task["status"] not in ("failed", "uploading"):
            return {"success": False, "message": "只能重试失败或上传中的任务"}

        # 取消正在卡住的旧上传协程，清理 GID 去重标记
        self._cancel_existing_upload(task_id)
        # 也清理 aria2_gid 对应的 uploading_gids 标记
        gid = task.get("aria2_gid", "")
        if gid:
            self._uploading_gids.discard(gid)

        # 如果本地文件/文件夹已存在，直接重试上传
        local_path = task.get("local_path", "")
        if local_path and os.path.exists(local_path):
            t = asyncio.create_task(self._retry_upload(task_id))
            self._upload_tasks[task_id] = t
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

            local_path = task.get("local_path", "")

            if not local_path or not os.path.exists(local_path):
                await db.update_task(task_id, status="failed",
                                     error="本地文件不存在，无法重试上传")
                await self._broadcast_task_update(task_id)
                return

            teldrive_path = self._calc_teldrive_path(local_path)

            # 重置上传状态
            await db.update_task(task_id, status="uploading",
                                 upload_progress=0.0, error=None)
            await self._broadcast_task_update(task_id)

            # 判断是文件夹还是单文件
            if os.path.isdir(local_path):
                await self._upload_directory(task_id, local_path, teldrive_path)
            else:
                await self._upload(task_id, local_path, teldrive_path)

            # 上传成功后清理本地文件
            await self._auto_delete_local(task_id, local_path)

        except asyncio.CancelledError:
            logger.info(f"任务 {task_id} 重试上传被取消")
        except Exception as e:
            logger.error(f"任务 {task_id} 重试上传失败: {e}")
            await db.update_task(task_id, status="failed", error=str(e))
            await self._broadcast_task_update(task_id)
        finally:
            self._upload_tasks.pop(task_id, None)

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
