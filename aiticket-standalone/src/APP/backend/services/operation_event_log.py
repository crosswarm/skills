"""
看板操作事件日志 — 记录跨项目移动与移交行为，供 HandoverSuggestAgent 挖掘团队习惯。

写入路径: data/operation_history.jsonl（JSONL，每行一个事件）
每行字段：ts, event_type, issue_key, actor, from_assignee, to_assignee,
          from_project_key, to_project_key, module, customer, product_version,
          summary, source
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

_JSONL_PATH = os.path.join(os.path.dirname(__file__), "../data/operation_history.jsonl")
_lock = threading.Lock()
# debounce: key -> epoch_seconds of last write
_debounce: dict[str, float] = {}
_DEBOUNCE_SEC = 60


def log_event(
    event_type: str,          # "move_jira" | "transfer" | "move_column"
    issue_key: str,
    actor: str = "unknown",
    *,
    from_assignee: Optional[str] = None,
    to_assignee: Optional[str] = None,
    from_project_key: Optional[str] = None,
    to_project_key: Optional[str] = None,
    module: Optional[str] = None,
    customer: Optional[str] = None,
    product_version: Optional[str] = None,
    summary: Optional[str] = None,
    comment: Optional[str] = None,
    source: str = "api",
) -> bool:
    """
    追加一条操作事件。相同 (issue_key, event_type) 在 60 秒内仅记一次，
    防止拖拽抖动或重试造成重复。返回 True 表示实际写入，False 表示被去抖跳过。
    """
    dedup_key = f"{issue_key}:{event_type}"
    now = time.monotonic()

    with _lock:
        if now - _debounce.get(dedup_key, 0) < _DEBOUNCE_SEC:
            return False
        _debounce[dedup_key] = now

        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_type": event_type,
            "issue_key": issue_key,
            "actor": actor or "unknown",
            "from_assignee": from_assignee or "",
            "to_assignee": to_assignee or "",
            "from_project_key": from_project_key or (issue_key.split("-")[0] if issue_key else ""),
            "to_project_key": to_project_key or "",
            "module": module or "",
            "customer": customer or "",
            "product_version": product_version or "",
            "summary": (summary or "")[:120],
            "comment": (comment or "")[:200],
            "source": source,
        }
        try:
            os.makedirs(os.path.dirname(_JSONL_PATH), exist_ok=True)
            with open(_JSONL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return True
        except Exception as exc:
            print(f"[OperationEventLog] write failed: {exc}")
            return False
