"""FastAPI 应用入口"""

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，支持直接运行本文件
_project_root = str(Path(__file__).parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from app.routes import api, settings, ws
from app.task_manager import task_manager
from app.config import load_config
from app import database as db

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    config = load_config()
    port = config.get("server", {}).get("port", 8000)
    logger.info("Pikpak2TelDrive 正在启动...")
    await task_manager.start()
    logger.info(f"应用已启动 - http://localhost:{port}")
    yield
    logger.info("正在关闭...")
    await task_manager.stop()
    await db.close_db()


app = FastAPI(
    title="Pikpak2TelDrive",
    description="aria2 下载 + TelDrive 上传中转服务",
    version="1.0.0",
    lifespan=lifespan
)

# 注册路由
app.include_router(api.router)
app.include_router(settings.router)
app.include_router(ws.router)

# 挂载静态文件
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    """返回主页"""
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import os
    config = load_config()
    port = config.get("server", {}).get("port", 8000)
    is_docker = os.environ.get("DOCKER", "0") == "1"
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=not is_docker
    )
