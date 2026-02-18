"""API 路由 - 任务管理接口"""

from fastapi import APIRouter, HTTPException
from app.models import TaskAddRequest, TaskResponse
from app.task_manager import task_manager

router = APIRouter(prefix="/api")


@router.post("/task/add")
async def add_task(req: TaskAddRequest):
    """添加下载任务"""
    task = await task_manager.add_task(
        url=req.url,
        filename=req.filename,
        teldrive_path=req.teldrive_path
    )
    return {"success": True, "data": task}


@router.get("/tasks")
async def get_all_tasks():
    """获取所有任务"""
    tasks = await task_manager.get_all_tasks()
    return {"tasks": tasks}


@router.get("/task/{task_id}")
async def get_task(task_id: str):
    """获取单个任务"""
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"data": task}


@router.post("/task/{task_id}/pause")
async def pause_task(task_id: str):
    """暂停任务"""
    result = await task_manager.pause_task(task_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/task/{task_id}/resume")
async def resume_task(task_id: str):
    """恢复任务"""
    result = await task_manager.resume_task(task_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/task/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消任务"""
    result = await task_manager.cancel_task(task_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/task/{task_id}/retry")
async def retry_task(task_id: str):
    """重试任务"""
    result = await task_manager.retry_task(task_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.delete("/task/{task_id}")
async def delete_task(task_id: str):
    """删除任务"""
    result = await task_manager.delete_task(task_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.post("/tasks/clear-completed")
async def clear_completed_tasks():
    """清除所有已完成的任务"""
    tasks = await task_manager.get_all_tasks()
    count = 0
    for t in tasks:
        if t["status"] in ("completed", "cancelled"):
            await task_manager.delete_task(t["task_id"])
            count += 1
    return {"success": True, "message": f"已清除 {count} 个任务"}
