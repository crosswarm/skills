"""Strict-mode判定与无用户上下文异常。

AITICKET_ROLE in ('qcl', 'deployable')  →  strict 模式
  - Jira 调用必须携带显式 session_cookies 或 username
  - PM 调用必须有 per-user 钱包绑定，禁止回落到默认管理员 token
  - 违规请求收到 HTTP 401 NO_USER_CONTEXT

本机 Mini（AITICKET_ROLE 未设置或 == 'mini'）：所有 fallback 行为保持不变。
"""
import os

STRICT_ROLES = {"qcl", "deployable"}


def is_strict_role() -> bool:
    return os.environ.get("AITICKET_ROLE", "deployable").lower() in STRICT_ROLES


class NoUserContextError(Exception):
    """strict 模式下漏传用户上下文。FastAPI exception handler 映射为 HTTP 401。"""

    def __init__(self, where: str = "", hint: str = ""):
        self.where = where
        self.hint = hint
        super().__init__(f"NoUserContext at {where}: {hint}")


class PMNotBoundError(NoUserContextError):
    """PM 钱包未绑定或已过期，且 strict 模式禁止回落管理员 token。"""

    def __init__(self, username: str = ""):
        self.username = username
        super().__init__("pm_wallet", f"PM session not bound for user: {username!r}")
