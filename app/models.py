"""数据模型 - Pydantic 模型定义"""

from pydantic import BaseModel
from typing import Optional


class TaskAddRequest(BaseModel):
    """添加任务请求"""
    url: str
    filename: Optional[str] = None
    teldrive_path: Optional[str] = "/"


class TaskResponse(BaseModel):
    """任务响应"""
    task_id: str
    url: str
    filename: Optional[str] = None
    status: str = "pending"
    download_progress: float = 0.0
    upload_progress: float = 0.0
    download_speed: str = ""
    upload_speed: str = ""
    file_size: str = ""
    error: Optional[str] = None
    teldrive_path: str = "/"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class Aria2Settings(BaseModel):
    """aria2 设置"""
    rpc_url: str = "http://localhost"
    rpc_port: int = 6800
    rpc_secret: str = ""
    max_concurrent: int = 3
    download_dir: str = "./downloads"


class TelDriveSettings(BaseModel):
    """TelDrive 设置"""
    api_host: str = "http://localhost:8080"
    access_token: str = ""
    channel_id: int = 0
    chunk_size: str = "500M"
    upload_concurrency: int = 4
    upload_dir: str = ""
    target_path: str = "/"


class GeneralSettings(BaseModel):
    """通用设置"""
    max_retries: int = 3
    auto_delete: bool = True


class AllSettings(BaseModel):
    """所有设置"""
    aria2: Aria2Settings = Aria2Settings()
    teldrive: TelDriveSettings = TelDriveSettings()
    general: GeneralSettings = GeneralSettings()


class TestResult(BaseModel):
    """连接测试结果"""
    success: bool
    message: str
    version: Optional[str] = None
