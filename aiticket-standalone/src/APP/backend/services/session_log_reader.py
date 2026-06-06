"""
Session JSONL 解析器 — 扫描昨日所有 Claude Code 会话，抽取结构化事件。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ~/.claude/projects 下本项目的 slug 目录
_SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-Users-cfone-Studio-aiticket"

_MAX_FILE_BYTES = 500 * 1024 * 1024  # 500 MB per file (line-by-line, safe)
_MAX_CONTENT_CHARS = 2000
_MAX_SESSIONS = 100


@dataclass
class SessionEvent:
    session_id: str
    timestamp: datetime
    role: str               # "user" | "assistant" | "tool_use" | "tool_result"
    content: str            # 截断至 _MAX_CONTENT_CHARS
    tool_name: Optional[str] = None
    is_plan_mode: bool = False


def _parse_content(raw) -> tuple[str, Optional[str]]:
    """返回 (text, tool_name)"""
    if isinstance(raw, str):
        return raw[:_MAX_CONTENT_CHARS], None

    if isinstance(raw, list):
        parts: list[str] = []
        tool_name = None
        for item in raw:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            if t == "text":
                parts.append(item.get("text", "")[:_MAX_CONTENT_CHARS])
            elif t == "tool_use":
                tool_name = item.get("name", "")
                inp = item.get("input", {})
                parts.append(f"[tool_use:{tool_name}] {json.dumps(inp, ensure_ascii=False)[:300]}")
            elif t == "tool_result":
                c = item.get("content", "")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                parts.append(f"[tool_result] {str(c)[:300]}")
        return "\n".join(parts)[:_MAX_CONTENT_CHARS], tool_name

    return str(raw)[:_MAX_CONTENT_CHARS], None


def _parse_ts(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def read_sessions_for_date(target_date: date) -> list[SessionEvent]:
    """
    扫描 _SESSIONS_DIR/*.jsonl，返回 target_date 当天所有 user/assistant 事件。
    按时间升序排列。
    """
    if not _SESSIONS_DIR.exists():
        logger.warning(f"[SessionLogReader] sessions dir not found: {_SESSIONS_DIR}")
        return []

    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    # 按 mtime 倒序取最近 _MAX_SESSIONS 个文件
    files = sorted(
        _SESSIONS_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:_MAX_SESSIONS]

    events: list[SessionEvent] = []

    for filepath in files:
        try:
            if filepath.stat().st_size > _MAX_FILE_BYTES:
                logger.debug(f"[SessionLogReader] skipping large file {filepath.name}")
                continue
            session_id = filepath.stem
            _scan_file(filepath, session_id, day_start, day_end, events)
        except Exception as exc:
            logger.warning(f"[SessionLogReader] error reading {filepath.name}: {exc}")

    events.sort(key=lambda e: e.timestamp)
    return events


def _scan_file(filepath: Path, session_id: str,
               day_start: datetime, day_end: datetime,
               events: list[SessionEvent]) -> None:
    plan_mode_active = False
    with filepath.open("r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            try:
                d = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            msg_type = d.get("type", "")

            if msg_type == "permission-mode":
                plan_mode_active = d.get("permissionMode") == "plan"
                continue

            if msg_type not in ("user", "assistant"):
                continue

            ts_str = d.get("timestamp", "")
            ts = _parse_ts(ts_str)
            if ts is None or not (day_start <= ts < day_end):
                continue

            msg = d.get("message", {})
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", msg_type)
            raw_content = msg.get("content", "")
            text, tool_name = _parse_content(raw_content)

            if not text.strip():
                continue

            events.append(SessionEvent(
                session_id=session_id,
                timestamp=ts,
                role=role,
                content=text,
                tool_name=tool_name,
                is_plan_mode=plan_mode_active,
            ))


def extract_user_questions(events: list[SessionEvent]) -> list[str]:
    results = []
    for e in events:
        if e.role != "user":
            continue
        text = e.content.strip()
        # skip API tool_result turns (start with [tool_result])
        if text.startswith("[tool_result]") or text.startswith("[tool_use:"):
            continue
        if len(text) < 4:
            continue
        results.append(text)
    return results


def extract_tool_uses(events: list[SessionEvent]) -> list[str]:
    return [f"{e.tool_name}: {e.content[:100]}"
            for e in events if e.tool_name and e.role == "assistant"]


def extract_plan_mode_events(events: list[SessionEvent]) -> list[str]:
    return [f"[plan] {e.content[:200]}" for e in events if e.is_plan_mode]
