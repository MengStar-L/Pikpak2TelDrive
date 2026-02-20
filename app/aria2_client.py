"""aria2 RPC 客户端 - 通过 JSON-RPC 与 aria2c 通信"""

import aiohttp
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class Aria2Client:
    """aria2 JSON-RPC 客户端"""

    def __init__(self, rpc_url: str = "http://localhost", rpc_port: int = 6800,
                 rpc_secret: str = ""):
        self.rpc_url = f"{rpc_url}:{rpc_port}/jsonrpc"
        self.secret = rpc_secret
        self._id_counter = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=10, connect=5)

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建复用的 HTTP 会话"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self):
        """关闭 HTTP 会话"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _build_params(self, *args):
        """构建带 secret 的参数列表"""
        if self.secret:
            return [f"token:{self.secret}"] + list(args)
        return list(args)

    async def _call(self, method: str, *args) -> dict:
        """发送 JSON-RPC 请求"""
        self._id_counter += 1
        payload = {
            "jsonrpc": "2.0",
            "id": str(self._id_counter),
            "method": method,
            "params": self._build_params(*args)
        }
        try:
            session = await self._get_session()
            async with session.post(self.rpc_url, json=payload) as resp:
                result = await resp.json()
                if "error" in result:
                    raise Exception(f"aria2 RPC error: {result['error']}")
                return result.get("result")
        except aiohttp.ClientError as e:
            # 连接失败时关闭旧会话，下次重建
            await self.close()
            raise ConnectionError(f"无法连接到 aria2 RPC: {e}")

    async def get_version(self) -> dict:
        """获取 aria2 版本信息"""
        return await self._call("aria2.getVersion")

    async def add_uri(self, uri: str, options: dict = None) -> str:
        """添加下载任务，返回 GID"""
        opts = options or {}
        return await self._call("aria2.addUri", [uri], opts)

    async def tell_status(self, gid: str) -> dict:
        """查询下载状态"""
        return await self._call("aria2.tellStatus", gid)

    async def pause(self, gid: str) -> str:
        """暂停下载"""
        return await self._call("aria2.pause", gid)

    async def unpause(self, gid: str) -> str:
        """恢复下载"""
        return await self._call("aria2.unpause", gid)

    async def pause_all(self) -> str:
        """暂停所有下载"""
        return await self._call("aria2.pauseAll")

    async def unpause_all(self) -> str:
        """恢复所有暂停的下载"""
        return await self._call("aria2.unpauseAll")

    async def remove(self, gid: str) -> str:
        """移除下载"""
        try:
            return await self._call("aria2.remove", gid)
        except Exception:
            # 如果任务已完成，尝试移除结果
            return await self._call("aria2.removeDownloadResult", gid)

    async def force_remove(self, gid: str) -> str:
        """强制移除下载"""
        try:
            return await self._call("aria2.forceRemove", gid)
        except Exception:
            return await self._call("aria2.removeDownloadResult", gid)

    async def tell_active(self) -> list:
        """获取所有活跃下载"""
        return await self._call("aria2.tellActive")

    async def tell_waiting(self, offset: int = 0, num: int = 100) -> list:
        """获取等待中的下载"""
        return await self._call("aria2.tellWaiting", offset, num)

    async def tell_stopped(self, offset: int = 0, num: int = 100) -> list:
        """获取已停止的下载（含完成和出错）"""
        return await self._call("aria2.tellStopped", offset, num)

    async def tell_stopped_all(self, page_size: int = 500) -> list:
        """分页获取所有已停止的下载，避免遗漏"""
        all_stopped = []
        offset = 0
        while True:
            batch = await self._call("aria2.tellStopped", offset, page_size)
            if not batch:
                break
            all_stopped.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return all_stopped

    async def get_global_stat(self) -> dict:
        """获取全局统计"""
        return await self._call("aria2.getGlobalStat")

    async def change_global_option(self, options: dict):
        """修改 aria2 全局配置"""
        return await self._call("aria2.changeGlobalOption", options)

    async def test_connection(self) -> dict:
        """测试连接"""
        try:
            version = await self.get_version()
            return {
                "success": True,
                "message": "aria2 连接成功",
                "version": version.get("version", "unknown")
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"aria2 连接失败: {str(e)}",
                "version": None
            }

    @staticmethod
    def parse_status(status: dict) -> dict:
        """解析 aria2 下载状态为可读格式"""
        total_length = int(status.get("totalLength", 0))
        completed_length = int(status.get("completedLength", 0))
        download_speed = int(status.get("downloadSpeed", 0))

        progress = 0.0
        if total_length > 0:
            progress = round(completed_length / total_length * 100, 1)

        # 获取文件名和路径
        filename = None
        file_path = ""
        is_dir = False
        dir_path = ""
        files = status.get("files", [])

        if files:
            path = files[0].get("path", "")
            if path:
                filename = path.replace("\\", "/").split("/")[-1]
            file_path = path

        # 检测 BT 多文件下载：有 bittorrent 字段且含多个文件
        bt_info = status.get("bittorrent", {})
        if bt_info and len(files) > 1:
            # 收集所有有效文件路径
            all_paths = [f.get("path", "") for f in files if f.get("path")]
            if len(all_paths) > 1:
                # 计算公共父目录
                import os
                common = os.path.commonpath(all_paths)
                if common and os.path.dirname(all_paths[0]) != common or any(
                    os.path.dirname(p) != common for p in all_paths
                ):
                    # common 是公共父目录（文件夹）
                    is_dir = True
                    dir_path = common
                    file_path = common  # 用文件夹路径作为主路径
                    # 文件名用 BT 任务名或文件夹名
                    bt_name = bt_info.get("info", {}).get("name", "")
                    if bt_name:
                        filename = bt_name
                    else:
                        filename = os.path.basename(common)

        return {
            "status": status.get("status", "unknown"),
            "progress": progress,
            "total_length": total_length,
            "completed_length": completed_length,
            "download_speed": download_speed,
            "speed_str": _format_speed(download_speed),
            "file_size": _format_size(total_length),
            "filename": filename,
            "file_path": file_path,
            "is_dir": is_dir,
            "dir_path": dir_path,
            "gid": status.get("gid", "")
        }


def _format_speed(speed: int) -> str:
    """格式化速度"""
    if speed < 1024:
        return f"{speed} B/s"
    elif speed < 1024 * 1024:
        return f"{speed / 1024:.1f} KB/s"
    elif speed < 1024 * 1024 * 1024:
        return f"{speed / (1024 * 1024):.1f} MB/s"
    else:
        return f"{speed / (1024 * 1024 * 1024):.1f} GB/s"


def _format_size(size: int) -> str:
    """格式化文件大小"""
    if size == 0:
        return "0 B"
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"
