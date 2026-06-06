"""
多用户通道管理 API 路由 (Spec Phase 4 / Section 6)

端点:
  GET  /api/channels/me           获取当前用户配置
  PUT  /api/channels/me           更新当前用户配置
  POST /api/channels/me/test      测试通道连通性
  GET  /api/channels/all          列出所有用户配置 (admin only)
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

from services.channel_service import get_channel_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels", tags=["channels"])


# ─── 请求模型 ──────────────────────────────────────────────────────────────────

class FeishuChannelConfig(BaseModel):
    enabled: Optional[bool] = None
    chat_id: Optional[str] = None
    openclaw_user_id: Optional[str] = None
    notify_types: Optional[list] = None


class UserPreferences(BaseModel):
    auto_confirm_timeout: Optional[int] = None
    min_score_for_notify: Optional[int] = None
    assigned_modules: Optional[list] = None


class UpdateChannelRequest(BaseModel):
    display_name: Optional[str] = None
    feishu: Optional[FeishuChannelConfig] = None
    preferences: Optional[UserPreferences] = None


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _get_user_id(x_user_id: Optional[str]) -> str:
    """从请求头获取 user_id，未提供则用默认值"""
    from role_guard import is_strict_role, NoUserContextError
    if not x_user_id and is_strict_role():
        raise NoUserContextError("channel_router", "X-User-ID header required in strict mode")
    return (x_user_id or "qiangxiao").strip()


# ─── 端点 ──────────────────────────────────────────────────────────────────────

@router.get("/me")
async def get_my_config(x_user_id: Optional[str] = Header(default=None)):
    """获取当前用户的通道配置"""
    user_id = _get_user_id(x_user_id)
    svc = get_channel_service()
    return svc.get_config(user_id)


@router.put("/me")
async def update_my_config(
    req: UpdateChannelRequest,
    x_user_id: Optional[str] = Header(default=None),
):
    """更新当前用户的通道配置（部分更新，深度合并）"""
    user_id = _get_user_id(x_user_id)
    svc = get_channel_service()

    updates: dict = {}
    if req.display_name is not None:
        updates["display_name"] = req.display_name
    if req.feishu is not None:
        updates["channels"] = {"feishu": {k: v for k, v in req.feishu.model_dump().items() if v is not None}}
    if req.preferences is not None:
        updates["preferences"] = {k: v for k, v in req.preferences.model_dump().items() if v is not None}

    if not updates:
        raise HTTPException(status_code=422, detail="未提供任何更新字段")

    return svc.update_config(user_id, updates)


@router.post("/me/test")
async def test_my_channel(x_user_id: Optional[str] = Header(default=None)):
    """发送测试消息验证飞书通道连通性"""
    user_id = _get_user_id(x_user_id)
    svc = get_channel_service()
    ok = svc.test_channel(user_id)
    return {"success": ok, "user_id": user_id}


@router.get("/all")
async def list_all_configs():
    """列出所有用户通道配置（管理员接口）"""
    svc = get_channel_service()
    configs = svc.list_all()
    return {"total": len(configs), "configs": configs}
