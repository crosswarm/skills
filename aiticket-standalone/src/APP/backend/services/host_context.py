"""
host_context — 双主机部署环境标识与 peer bridge 代理

环境变量：
  AITICKET_HOST=mini|qcl  (默认 mini)
  AITICKET_PEER_BRIDGE    (例：http://127.0.0.1:13800)
  AITICKET_HOME           (可选，覆盖 session 文件目录，默认 tempfile.gettempdir())

用法：
  from services.host_context import HOST, PEER_BRIDGE_URL, proxy_to_peer, session_dir, session_path
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

HOST: str = os.environ.get("AITICKET_HOST", "mini").lower()
PEER_BRIDGE_URL: str | None = os.environ.get("AITICKET_PEER_BRIDGE")


def is_mini() -> bool:
    return HOST != "qcl"


def is_qcl() -> bool:
    return HOST == "qcl"


def proxy_to_peer(path: str, method: str = "POST", **kw) -> dict:
    """将请求转发到对端主机；对端不可达时返回 503。"""
    import requests
    from fastapi import HTTPException
    if not PEER_BRIDGE_URL:
        raise HTTPException(503, "Peer bridge not configured (AITICKET_PEER_BRIDGE unset)")
    url = f"{PEER_BRIDGE_URL.rstrip('/')}{path}"
    try:
        r = requests.request(method, url, timeout=15, **kw)
        r.raise_for_status()
        return r.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Peer bridge unreachable: {exc}")


# ---------------------------------------------------------------------------
# 跨平台 session 文件目录
# ---------------------------------------------------------------------------

def session_dir() -> Path:
    """返回 session 文件存放目录（跨平台）。

    优先级：
    1. 环境变量 AITICKET_HOME/data/session/
    2. tempfile.gettempdir()（Windows: %TEMP%，Unix: /tmp）
    """
    home = os.environ.get("AITICKET_HOME", "").strip()
    if home:
        d = Path(home) / "data" / "session"
    else:
        d = Path(tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_path(user: str | None = None, prefix: str = "jira") -> str:
    """返回 session 文件完整路径字符串（跨平台替代 /tmp/jira-session*.json）。

    - user=None  → <session_dir>/jira-session.json
    - user='abc' → <session_dir>/jira-session-abc.json
    - prefix='pm'→ <session_dir>/pm-session-{user}.json
    """
    d = session_dir()
    if user:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", user)
        return str(d / f"{prefix}-session-{safe}.json")
    return str(d / f"{prefix}-session.json")
