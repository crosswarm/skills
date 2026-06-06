"""
回复链路健康度指标收集 — append-only JSONL，10MB 自动轮转。

用法：
    from services.reply_metrics import emit
    emit("query_empty", issue_key="MYPROJECT-123")
    emit("kb_hit_count", issue_key="MYPROJECT-123", count=3)
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
_METRICS_PATH = _BACKEND / "data" / "reply_metrics.jsonl"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB rotate

_lock = threading.Lock()


def emit(event: str, **kv) -> None:
    """Append one metric event. Never raises — metrics must not break callers."""
    try:
        record = {"ts": datetime.now().isoformat(), "event": event, **kv}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _lock:
            _metrics_path().parent.mkdir(parents=True, exist_ok=True)
            p = _metrics_path()
            if p.exists() and p.stat().st_size >= _MAX_BYTES:
                _rotate(p)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass  # metrics must never crash callers


def _metrics_path() -> Path:
    return _METRICS_PATH


def _rotate(p: Path) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p.rename(p.with_suffix(f".{ts}.jsonl"))
