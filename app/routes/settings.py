"""设置路由 - 配置管理和连接测试"""

from fastapi import APIRouter
from app.models import AllSettings, TestResult
from app.config import load_config, save_config
from app.aria2_client import Aria2Client
from app.teldrive_client import TelDriveClient
from app.task_manager import task_manager

router = APIRouter(prefix="/api/settings")


@router.get("")
async def get_settings():
    """获取当前设置"""
    config = load_config()
    return config


@router.put("")
async def update_settings(settings: AllSettings):
    """保存设置"""
    config = settings.model_dump()
    save_config(config)
    # 重新加载配置到任务管理器
    task_manager.reload_config()
    return {"success": True, "message": "设置已保存"}


@router.post("/test/aria2")
async def test_aria2():
    """测试 aria2 连接"""
    config = load_config()
    client = Aria2Client(
        rpc_url=config["aria2"]["rpc_url"],
        rpc_port=config["aria2"]["rpc_port"],
        rpc_secret=config["aria2"]["rpc_secret"]
    )
    result = await client.test_connection()
    return result


@router.post("/test/teldrive")
async def test_teldrive():
    """测试 TelDrive 连接"""
    config = load_config()
    client = TelDriveClient(
        api_host=config["teldrive"]["api_host"],
        access_token=config["teldrive"]["access_token"]
    )
    result = await client.test_connection()
    return result

