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
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pathlib import Path

from app.routes import api, settings, ws
from app.routes import login as login_route
from app.task_manager import task_manager
from app.config import load_config
from app.auth import is_auth_enabled, verify_token
from app import database as db

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# 不需要认证的路径前缀
AUTH_WHITELIST = ["/api/login", "/api/auth/check", "/static/", "/docs", "/openapi.json", "/favicon.ico"]


class AuthMiddleware(BaseHTTPMiddleware):
    """简单的认证中间件"""

    async def dispatch(self, request: Request, call_next):
        # 不启用认证时直接放行
        if not is_auth_enabled():
            return await call_next(request)

        path = request.url.path

        # 白名单路径放行
        if path == "/" or any(path.startswith(p) for p in AUTH_WHITELIST):
            return await call_next(request)

        # WebSocket 走自己的认证逻辑
        if path == "/ws":
            return await call_next(request)

        # 检查 Cookie 中的 token
        token = request.cookies.get("auth_token")
        if not token or not verify_token(token):
            return JSONResponse(
                status_code=401,
                content={"detail": "未登录"}
            )

        return await call_next(request)


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

# 认证中间件
app.add_middleware(AuthMiddleware)

# 注册路由
app.include_router(login_route.router)
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
    config = load_config()
    port = config.get("server", {}).get("port", 8000)
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=True
    )
