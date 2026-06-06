"""
定时调度管理 API 路由 (Spec Phase 3)

端点:
  GET    /api/schedules             列出所有调度任务
  POST   /api/schedules             创建调度任务 (S1)
  GET    /api/schedules/{id}        查询调度任务
  PUT    /api/schedules/{id}        更新调度任务
  DELETE /api/schedules/{id}        删除调度任务
  POST   /api/schedules/{id}/trigger 立即触发 (测试用)
  GET    /api/schedules/handlers    查看已注册的任务处理器类型
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.scheduler_service import get_scheduler, _task_handlers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schedules", tags=["scheduler"])


# ─── 请求模型 ──────────────────────────────────────────────────────────────────

class CreateScheduleRequest(BaseModel):
    name: str
    task_type: str
    trigger_type: str = "cron"        # "cron" | "event"
    cron: Optional[str] = None        # cron 表达式，trigger_type=cron 时必填
    condition: Optional[str] = None   # 事件条件，trigger_type=event 时必填
    enabled: bool = True
    cooldown_hours: int = 0
    params: dict = {}
    notify_users: list = []
    created_by: Optional[str] = None


class UpdateScheduleRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    cron: Optional[str] = None
    condition: Optional[str] = None
    cooldown_hours: Optional[int] = None
    params: Optional[dict] = None
    notify_users: Optional[list] = None


# ─── 端点 ──────────────────────────────────────────────────────────────────────

@router.get("")
async def list_schedules():
    """列出所有调度任务"""
    svc = get_scheduler()
    schedules = svc.list_schedules()
    return {"total": len(schedules), "schedules": schedules}


@router.post("")
async def create_schedule(req: CreateScheduleRequest):
    """创建调度任务 (Spec S1 验证项)"""
    # 基本校验
    if req.trigger_type == "cron" and not req.cron:
        raise HTTPException(status_code=422, detail="trigger_type=cron 时 cron 表达式不能为空")
    if req.trigger_type == "event" and not req.condition:
        raise HTTPException(status_code=422, detail="trigger_type=event 时 condition 不能为空")

    svc = get_scheduler()
    data = req.model_dump(exclude_none=False)
    schedule = svc.create_schedule(data)
    return schedule


@router.get("/handlers")
async def list_handlers():
    """查看已注册的任务处理器类型"""
    return {"handlers": list(_task_handlers.keys())}


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str):
    """查询调度任务详情"""
    svc = get_scheduler()
    schedule = svc.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail=f"调度任务 {schedule_id} 不存在")
    return schedule


@router.put("/{schedule_id}")
async def update_schedule(schedule_id: str, req: UpdateScheduleRequest):
    """更新调度任务"""
    svc = get_scheduler()
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    schedule = svc.update_schedule(schedule_id, updates)
    if not schedule:
        raise HTTPException(status_code=404, detail=f"调度任务 {schedule_id} 不存在")
    return schedule


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str):
    """删除调度任务"""
    svc = get_scheduler()
    ok = svc.delete_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"调度任务 {schedule_id} 不存在")
    return {"deleted": True, "schedule_id": schedule_id}


@router.post("/{schedule_id}/trigger")
async def trigger_now(schedule_id: str):
    """立即触发指定调度任务（测试/手动补跑）"""
    svc = get_scheduler()
    ok = svc.trigger_now(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"调度任务 {schedule_id} 不存在")
    return {"triggered": True, "schedule_id": schedule_id}
