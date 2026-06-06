"""活跃用户 Jira/PM 会话池：为后台任务提供真实用户的 session。

strict 模式（AITICKET_ROLE=qcl/deployable）下，后台任务必须使用真实用户会话，
禁止使用默认账号（qiangxiao）。pick_jira_service_for_bg() 扫描所有
/tmp/jira-session-{username}.json，返回最近修改（最活跃）用户的 JiraService。
找不到活跃用户时返回 None，调用方应跳过本次执行并记录 warn 日志。

非 strict 模式返回全局默认单例（qiangxiao），与历史行为完全一致。
"""
import glob
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_WALLET_DIR = Path(__file__).parent.parent / "data_cache" / "pm_tokens"


# ---------------------------------------------------------------------------
# Jira session pool
# ---------------------------------------------------------------------------

def _scan_jira_sessions() -> list:
    """扫描所有 jira-session-{username}.json，按最近修改时间降序返回。"""
    from services.host_context import session_dir as _session_dir
    _sdir = str(_session_dir())
    results = []
    for path in sorted(glob.glob(os.path.join(_sdir, "jira-session-*.json")), key=os.path.getmtime, reverse=True):
        fname = os.path.basename(path)
        if fname == "jira-session.json":
            continue  # 全局 fallback 文件，不用于池
        match = re.match(r"jira-session-(.+)\.json", fname)
        if not match:
            continue
        username = match.group(1)
        try:
            with open(path) as f:
                state = json.load(f)
            cookies = {
                c["name"]: c["value"]
                for c in state.get("cookies", [])
                if "yyrd.com" in c.get("domain", "")
            }
            if not cookies.get("JSESSIONID"):
                continue
            results.append({"username": username, "session_cookies": cookies, "path": path})
        except Exception as e:
            logger.debug(f"[session_pool] skip {path}: {e}")
    return results


def pick_jira_service_for_bg(task_type: str = "background"):
    """
    为后台任务返回一个有真实 session 的 JiraService。

    - strict 模式：扫描 per-user session 文件，取最近活跃用户。找不到 → None。
    - 非 strict 模式：返回全局默认单例（qiangxiao）。
    """
    from role_guard import is_strict_role
    from jira_service import JiraService, jira_service as _default_svc

    if not is_strict_role():
        return _default_svc

    sessions = _scan_jira_sessions()
    if not sessions:
        logger.warning(f"[session_pool] strict mode: no active Jira sessions for task_type={task_type!r}")
        return None

    best = sessions[0]
    logger.info(f"[session_pool] bg task {task_type!r} → using session of {best['username']!r}")
    return JiraService(session_cookies=best["session_cookies"])


# ---------------------------------------------------------------------------
# Diagnostics / monitoring
# ---------------------------------------------------------------------------

def active_users_with_jira() -> list:
    """返回有 valid Jira session 的所有用户（供监控/诊断）。"""
    return _scan_jira_sessions()


def active_users_with_pm() -> list:
    """返回 wallet 已绑定的 PM 用户列表（供监控/诊断）。"""
    if not _WALLET_DIR.is_dir():
        return []
    results = []
    for path in sorted(_WALLET_DIR.glob("*.json"), key=os.path.getmtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("yht_access_token"):
                results.append({"username": path.stem, "path": str(path)})
        except Exception:
            pass
    return results
