"""
auth_deps — FastAPI 依赖函数，供 main.py 与各 router 共用。
不引入任何业务逻辑，只读 request.state.current_user。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Final, Optional

from fastapi import HTTPException, Request

# 匿名用户每日硬上限 — 禁止改为可配置，禁止写入 system_settings 表
DAILY_ANON_LIMIT: Final[int] = 1


def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    return getattr(request.state, "current_user", None)


def require_authenticated_user(request: Request) -> Dict[str, Any]:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin_user(request: Request) -> Dict[str, Any]:
    user = require_authenticated_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_reply_quota(request: Request) -> Dict[str, Any]:
    """智能回复类端点的鉴权+配额依赖。返回调用方身份上下文供日志使用。

    优先级:
    1. Web 端 session：middleware 已填充 request.state.current_user → 放行
    2. Skill 端 device token：X-AiTicket-Token + X-AiTicket-Client-Id → 在线验证 → 放行
    3. 匿名：按 client_id + IP 双维度计数，任一超过 DAILY_ANON_LIMIT 则 429
    """
    # 1. Web 端已认证
    web_user = getattr(request.state, "current_user", None)
    if web_user:
        return {
            "user_id": web_user.get("id", ""),
            "display_name": web_user.get("display_name", ""),
            "client_id": "",
            "is_anon": False,
        }

    device_token = request.headers.get("X-AiTicket-Token", "").strip()
    client_id = request.headers.get("X-AiTicket-Client-Id", "").strip()

    # 2. Skill 端 device token
    if device_token and client_id:
        from auth_service import get_auth_service
        svc = get_auth_service()
        user = svc.verify_device_token(device_token, client_id)
        if user:
            return {
                "user_id": user.get("id", ""),
                "display_name": user.get("display_name", ""),
                "client_id": client_id,
                "is_anon": False,
            }
        raise HTTPException(
            status_code=401,
            detail="Device token 无效或与当前机器不匹配，请重新运行 --login",
        )

    # 3. 匿名路径
    if not client_id:
        raise HTTPException(
            status_code=400,
            detail="缺少 X-AiTicket-Client-Id header，请通过 setup_config.py 获取认证信息",
        )

    forwarded_for = request.headers.get("X-Forwarded-For", "")
    client_ip = forwarded_for.split(",")[0].strip() if forwarded_for else (request.client.host if request.client else "unknown")

    from auth_service import get_auth_service
    svc = get_auth_service()
    allowed = svc.check_and_increment_anon_quota(client_id, client_ip, DAILY_ANON_LIMIT)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"今日匿名提问额度已用完（每日 {DAILY_ANON_LIMIT} 次硬上限，不可修改）。"
                "请运行 `python3 .agent/skills/aiticket-reply/scripts/setup_config.py --login`"
                " 登录 QCL 账号以继续使用。"
            ),
        )
    return {"user_id": "", "display_name": "", "client_id": client_id, "is_anon": True}


def log_api_request(
    request: Request,
    quota_ctx: Dict[str, Any],
    issue_key: str = "",
    query_text: str = "",
) -> None:
    """静默记录 API 请求日志，不阻塞响应。"""
    try:
        from auth_service import get_auth_service
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        client_ip = forwarded_for.split(",")[0].strip() if forwarded_for else (
            request.client.host if request.client else "unknown"
        )
        project = ""
        if issue_key:
            m = re.match(r'^([A-Z][A-Z0-9]+)-\d+', issue_key)
            project = m.group(1) if m else ""
        get_auth_service().log_request(
            interface=str(request.url.path),
            user_id=quota_ctx.get("user_id") or "",
            display_name=quota_ctx.get("display_name") or "",
            is_anon=bool(quota_ctx.get("is_anon", True)),
            client_id=quota_ctx.get("client_id") or request.headers.get("X-AiTicket-Client-Id", ""),
            client_ip=client_ip,
            project=project,
            issue_key=issue_key,
            query_text=query_text,
        )
    except Exception:
        pass
