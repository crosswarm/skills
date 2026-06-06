"""
记忆管理 API 路由 (Spec 3.3.4)

端点:
  GET    /api/memory/{user_id}/list          查看用户记忆列表
  DELETE /api/memory/{user_id}/{mem_id}      删除有害记忆
  POST   /api/memory/{user_id}/audit         人工审核标记
  GET    /api/memory/health                  全局记忆健康度报告
  POST   /api/memory/{user_id}/cleanup       清理低质量记忆
  POST   /api/memory/{user_id}/decay         手动触发时效衰减
  POST   /api/memory/{user_id}/add           添加学习记忆
  GET    /api/memory/{user_id}/contradictions 矛盾检测
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.memory_service import MemoryService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["memory"])

# ─── 依赖注入 ─────────────────────────────────────────────────────────────────
# MemoryService 是重型对象（加载嵌入模型），应用级单例，通过 app.state 传递
_service_instance: Optional[MemoryService] = None


def get_memory_service() -> MemoryService:
    global _service_instance
    if _service_instance is None:
        _service_instance = MemoryService()
    return _service_instance


# ─── 请求/响应模型 ─────────────────────────────────────────────────────────────

class AddLearningRequest(BaseModel):
    content: str
    source_ticket_id: Optional[str] = None
    source_kb_id: Optional[str] = None
    memory_type: Optional[str] = None      # 如: "requirement_rule", "reply_style"
    module: Optional[str] = None           # 如: "流程中心", "消息"


class AuditRequest(BaseModel):
    mem_id: str
    approved: bool


class ConfidenceDeltaRequest(BaseModel):
    mem_id: str
    delta: float   # +0.1 采纳 / -0.2 驳回


# ─── 端点 ──────────────────────────────────────────────────────────────────────

@router.get("/{user_id}/list")
async def list_memories(
    user_id: str,
    svc: MemoryService = Depends(get_memory_service),
):
    """查看用户所有记忆（管理员工具）"""
    try:
        memories = svc.list_memories(user_id)
        return {"user_id": user_id, "total": len(memories), "memories": memories}
    except Exception as e:
        logger.error(f"list_memories 失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{user_id}/{mem_id}")
async def delete_memory(
    user_id: str,
    mem_id: str,
    svc: MemoryService = Depends(get_memory_service),
):
    """删除有害记忆（管理员工具）"""
    success = svc.delete_memory(mem_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"记忆 {mem_id} 删除失败或不存在")
    return {"deleted": True, "mem_id": mem_id}


@router.post("/{user_id}/audit")
async def audit_memory(
    user_id: str,
    req: AuditRequest,
    svc: MemoryService = Depends(get_memory_service),
):
    """人工审核标记 — approved=true 提升置信度至~0.8，false 降至~0.2"""
    success = svc.audit_memory(req.mem_id, req.approved)
    if not success:
        raise HTTPException(status_code=404, detail=f"审核失败: {req.mem_id}")
    return {"audited": True, "mem_id": req.mem_id, "approved": req.approved}


@router.post("/confidence")
async def update_confidence(
    req: ConfidenceDeltaRequest,
    svc: MemoryService = Depends(get_memory_service),
):
    """更新置信度（反馈闭环：采纳 +0.1，驳回 -0.2）"""
    success = svc.update_confidence(req.mem_id, req.delta)
    if not success:
        raise HTTPException(status_code=404, detail=f"记忆 {req.mem_id} 不存在")
    return {"updated": True, "mem_id": req.mem_id, "delta": req.delta}


@router.get("/health")
async def get_health_report(
    user_id: str = "all",
    svc: MemoryService = Depends(get_memory_service),
):
    """5维度记忆健康度报告（可指定 user_id 或传 all 查看全局）"""
    try:
        report = svc.get_health_report(user_id)
        return report
    except Exception as e:
        logger.error(f"health_report 失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/cleanup")
async def cleanup_memories(
    user_id: str,
    threshold: float = 0.3,
    svc: MemoryService = Depends(get_memory_service),
):
    """清理低置信度记忆（管理员工具，默认阈值 0.3）"""
    removed = svc.cleanup_low_quality(user_id, threshold)
    return {"user_id": user_id, "removed": removed, "threshold": threshold}


@router.post("/{user_id}/decay")
async def run_decay(
    user_id: str,
    svc: MemoryService = Depends(get_memory_service),
):
    """手动触发时效衰减（通常由调度器自动执行）"""
    decayed = svc.run_time_decay(user_id)
    return {"user_id": user_id, "decayed": decayed}


@router.post("/{user_id}/add")
async def add_learning(
    user_id: str,
    req: AddLearningRequest,
    svc: MemoryService = Depends(get_memory_service),
):
    """添加一条学习记忆（需提供来源 ticket_id 或 kb_id）"""
    meta = {
        k: v for k, v in {
            "source_ticket_id": req.source_ticket_id,
            "source_kb_id": req.source_kb_id,
            "memory_type": req.memory_type,
            "module": req.module,
        }.items() if v is not None
    }
    mem_id = svc.add_learning(user_id, req.content, meta)
    if mem_id is None:
        raise HTTPException(
            status_code=422,
            detail="记忆被拒绝: 内容含推测性语言 或 缺少来源信息 (source_ticket_id / source_kb_id)"
        )
    return {"added": True, "mem_id": mem_id, "user_id": user_id}


@router.get("/{user_id}/contradictions")
async def get_contradictions(
    user_id: str,
    svc: MemoryService = Depends(get_memory_service),
):
    """检测用户记忆中的矛盾对（管理员审核使用）"""
    contradictions = svc.detect_contradictions(user_id)
    return {"user_id": user_id, "total": len(contradictions), "contradictions": contradictions}
