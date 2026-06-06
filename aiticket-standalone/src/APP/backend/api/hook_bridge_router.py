"""
Hook Bridge Router — POST /api/hooks/claude-input  +  /cli/pane  +  /cli/keys
接收 Claude Code hook 脚本上报的事件，写入 agent_tasks 表，
让 agents.html action_required 可见并可远程批准/驳回/驱键。

事件种类：
  ExitPlanMode (PreToolUse) — queued，等待 approve/reject
  AskUserQuestion (PostToolUse) — running，已回答，仅作日志
  Notification — queued，通知类，需人工「已读」
"""
from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

try:
    from auth_deps import require_admin_user as _require_admin
except ImportError:
    async def _require_admin():  # type: ignore[misc]
        pass

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/hooks", tags=["hooks"])

_DEFAULT_TMUX_SESSION = os.environ.get("CLI_BRIDGE_TMUX_SESSIONS", "AITicket").split(",")[0].strip()
_DEFAULT_CLI_KEYMAP = {"approve": ["Enter"], "reject": ["Escape"]}


class HookInputEvent(BaseModel):
    event_type: str = ""
    session_id: str = ""
    tool_name: str = ""
    message: str = ""
    question: str = ""
    answer: str = ""
    plan_summary: str = ""
    cwd: str = ""
    transcript_path: str = ""
    tmux_session: str = ""


@router.post("/claude-input")
def receive_hook(event: HookInputEvent):
    from agents.base import AgentTask, AgentStatus
    from services.agent_task_store import AgentTaskStore

    store = AgentTaskStore.get_instance()
    tool = (event.tool_name or "").lower()
    tmux_session = event.tmux_session or _DEFAULT_TMUX_SESSION

    if "exitplanmode" in tool or "exit_plan_mode" in tool:
        summary = event.plan_summary or event.message or ""
        title = "[计划审批] Claude 请求退出计划模式开始执行"
        payload = {
            "kind": "hook_plan_exit",
            "session_id": event.session_id,
            "plan_summary": summary[:400],
            "cwd": event.cwd,
            "transcript_path": event.transcript_path,
            "tmux_session": tmux_session,
            "cli_keymap": _DEFAULT_CLI_KEYMAP,
            "description": f"Claude 正准备结束计划阶段并开始执行。{('计划摘要：' + summary[:120]) if summary else ''}",
        }
        status = AgentStatus.QUEUED

    elif "askuserquestion" in tool:
        q = event.question or event.message or ""
        title = f"[Claude 提问] {q[:60]}"
        payload = {
            "kind": "hook_ask",
            "session_id": event.session_id,
            "question": q,
            "answer": event.answer,
            "cwd": event.cwd,
            "transcript_path": event.transcript_path,
            "description": f"Q：{q[:120]}" + (f"\nA：{event.answer[:80]}" if event.answer else ""),
        }
        status = AgentStatus.RUNNING

    elif event.event_type == "Notification" or event.message:
        msg = event.message or ""
        title = f"[Claude 通知] {msg[:60]}"
        payload = {
            "kind": "hook_notification",
            "session_id": event.session_id,
            "message": msg,
            "cwd": event.cwd,
            "transcript_path": event.transcript_path,
            "description": msg[:300],
        }
        status = AgentStatus.QUEUED

    else:
        logger.debug(f"[HookBridge] 忽略未识别事件: {event.event_type}/{event.tool_name}")
        return {"ok": False, "reason": "unrecognized event"}

    # Cancel any stale queued hook tasks of the same kind before inserting
    new_kind = payload.get("kind", "")
    new_session = payload.get("session_id", event.session_id or "")
    if status == AgentStatus.QUEUED and new_kind:
        try:
            for old in store.list_recent(limit=100):
                old_src = (old.trigger_src or "")
                if old.status.value != "queued" or not old_src.startswith("hook:"):
                    continue
                try:
                    old_pl = json.loads(old.payload_json or "{}")
                except Exception:
                    old_pl = {}
                if old_pl.get("kind") == new_kind and old_pl.get("session_id") == new_session:
                    store.cancel(old.id)
                    logger.debug(f"[HookBridge] cancelled stale task={old.id} kind={new_kind}")
        except Exception:
            pass

    task = AgentTask.new(
        agent_name="claude",
        title=title,
        trigger_src=f"hook:{event.event_type}:{event.tool_name or 'notification'}",
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    task.status = status
    store.insert(task)
    logger.info(f"[HookBridge] task={task.id} kind={payload['kind']} status={status.value}")

    # agents_router was removed in deployable build — websocket broadcast not available
    return {"ok": True, "task_id": task.id}


@router.get("/claude-input/{task_id}")
def get_hook_task_status(task_id: str):
    """Hook 脚本轮询：等 approve → running / reject → cancelled / else → queued"""
    from services.agent_task_store import AgentTaskStore
    store = AgentTaskStore.get_instance()
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return {"task_id": task_id, "status": task.status.value}


# ── CLI Bridge endpoints (admin only) ─────────────────────────────────────────

class SendKeysBody(BaseModel):
    session: str
    keys: list[str]
    ref_task_id: str = ""


@router.get("/cli/pane")
def cli_pane(session: str = _DEFAULT_TMUX_SESSION, lines: int = 35,
             _: None = Depends(_require_admin)):
    """返回 tmux pane 快照（纯文本，agents.html drawer 轮询用）"""
    try:
        from services.cli_bridge import capture_pane
        text = capture_pane(session, lines)
        return {"ok": True, "text": text, "session": session}
    except Exception as e:
        return {"ok": False, "error": str(e), "text": ""}


@router.post("/cli/keys")
def cli_send_keys(body: SendKeysBody, _: None = Depends(_require_admin)):
    """向 tmux session 发送按键序列（白名单限制）"""
    try:
        from services.cli_bridge import send_keys
        send_keys(body.session, body.keys, audit_ref=body.ref_task_id or None)
        return {"ok": True, "session": body.session, "keys": body.keys}
    except Exception as e:
        raise HTTPException(400, str(e))
