"""CLI Bridge — tmux pane capture + send-keys for agents.html remote control."""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

_ENABLED: bool = os.environ.get("CLI_BRIDGE_ENABLED", "true").lower() != "false"
_SESSION_ALLOWLIST: list[str] = [
    s.strip()
    for s in os.environ.get("CLI_BRIDGE_TMUX_SESSIONS", "AITicket").split(",")
    if s.strip()
]
_KEY_ALLOWLIST: frozenset[str] = frozenset({
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "Enter", "Escape", "Up", "Down", "Left", "Right",
    "Tab", "BTab",
    "y", "n", "q", "Y", "N", "Q", "Space",
})


def is_enabled() -> bool:
    return _ENABLED


def capture_pane(session: str, lines: int = 40) -> str:
    """Return the last <lines> lines of the named tmux pane as plain text."""
    if not _ENABLED:
        raise RuntimeError("CLI Bridge disabled (CLI_BRIDGE_ENABLED=false)")
    if session not in _SESSION_ALLOWLIST:
        raise ValueError(f"Session {session!r} not in allowlist {_SESSION_ALLOWLIST}")
    lines = max(1, min(lines, 200))
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        raise RuntimeError(f"tmux capture-pane failed: {r.stderr.strip() or 'session not found'}")
    return r.stdout


def send_keys(session: str, keys: list[str], *, audit_ref: str | None = None) -> None:
    """Forward an allowlisted key sequence to the named tmux session."""
    if not _ENABLED:
        raise RuntimeError("CLI Bridge disabled (CLI_BRIDGE_ENABLED=false)")
    if session not in _SESSION_ALLOWLIST:
        raise ValueError(f"Session {session!r} not in allowlist {_SESSION_ALLOWLIST}")
    bad = [k for k in keys if k not in _KEY_ALLOWLIST]
    if bad:
        raise ValueError(f"Keys not in allowlist: {bad!r}. Allowed: {sorted(_KEY_ALLOWLIST)}")
    r = subprocess.run(
        ["tmux", "send-keys", "-t", session] + keys,
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        raise RuntimeError(f"tmux send-keys failed: {r.stderr.strip()}")
    logger.info(f"[CLIBridge] send-keys → {session}: {keys!r}  ref={audit_ref}")
    _write_audit(session, keys, audit_ref)


def _write_audit(session: str, keys: list[str], ref: str | None) -> None:
    import json
    try:
        from agents.base import AgentTask, AgentStatus
        from services.agent_task_store import AgentTaskStore
        t = AgentTask.new(
            agent_name="claude",
            title=f"[cli] send-keys→{session}: {' '.join(keys)}",
            trigger_src="cli_bridge:send_keys",
            payload_json=json.dumps({
                "kind": "cli_keystroke",
                "session": session,
                "keys": keys,
                "ref_task_id": ref or "",
            }),
        )
        t.status = AgentStatus.SUCCEEDED
        AgentTaskStore.get_instance().insert(t)
    except Exception as exc:
        logger.debug(f"[CLIBridge] audit skipped: {exc}")
