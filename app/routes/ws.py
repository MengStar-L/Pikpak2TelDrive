"""WebSocket 路由 - 实时进度推送"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.task_manager import task_manager
from app.auth import is_auth_enabled, verify_token

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket 连接，推送实时任务进度"""
    # 认证检查
    if is_auth_enabled():
        token = ws.query_params.get("token")
        if not token or not verify_token(token):
            await ws.close(code=4001, reason="未认证")
            return

    await ws.accept()
    task_manager.register_ws(ws)
    try:
        # 发送当前所有任务状态
        tasks = await task_manager.get_all_tasks()
        await ws.send_json({
            "type": "init",
            "data": {"tasks": tasks}
        })
        # 保持连接，接收客户端心跳
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        task_manager.unregister_ws(ws)
