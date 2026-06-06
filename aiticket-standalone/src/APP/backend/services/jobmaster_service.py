"""
JobMasterService — 调度聚合层

只读聚合 schedules/*.json + agent_tasks SQLite。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from croniter import croniter

_BACKEND = Path(__file__).resolve().parent.parent
_SCHEDULES_DIR = _BACKEND / "data" / "schedules"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _next_run(cron_expr: str) -> Optional[str]:
    try:
        it = croniter(cron_expr, datetime.utcnow())
        return it.get_next(datetime).isoformat() + "Z"
    except Exception:
        return None


# agent_name → schedule IDs
_AGENT_SCHEDULE_IDS: Dict[str, List[str]] = {
    "competitor":  ["nightly-exploration"],
    "darwin":      [],
    "reply":       ["nightly-training"],
    "req_analyst": ["weekly-report"],
    "kb_fact":     ["weekly-fact-extraction", "oneshot-backfill-facts"],
    "adopted":          ["weekly-adopted-extract"],
    "handover_suggest": ["weekly-handover-extract"],
}


def get_agent_schedule_info(agent_name: str) -> Dict:
    """返回 agent 对应调度的 next_run / last_run_schedule"""
    sched_ids = _AGENT_SCHEDULE_IDS.get(agent_name, [])
    if not sched_ids:
        return {}
    schedules = [s for s in get_schedules() if s["id"] in sched_ids]
    next_runs = [s["next_run"] for s in schedules if s.get("next_run")]
    last_runs = [s["last_run"] for s in schedules if s.get("last_run")]
    return {
        "next_run": min(next_runs) if next_runs else None,
        "last_run_schedule": max(last_runs) if last_runs else None,
    }


def get_schedules() -> List[Dict]:
    results = []
    if not _SCHEDULES_DIR.exists():
        return results
    for p in sorted(_SCHEDULES_DIR.glob("*.json")):
        data = _read_json(p)
        if not data or not isinstance(data, dict):
            continue
        sid = data.get("id", p.stem)
        cron = data.get("cron", "")
        last = data.get("last_run")
        if sid.startswith("jobmaster-"):
            continue
        host_constraint = data.get("host_constraint")
        results.append({
            "id": sid,
            "name": data.get("name", sid),
            "cron": cron,
            "enabled": data.get("enabled", True),
            "task_type": data.get("task_type", "unknown"),
            "last_run": last,
            "next_run": _next_run(cron) if cron else None,
            "status": "running" if _is_running(sid) else ("waiting" if data.get("enabled") else "disabled"),
            "host_constraint": host_constraint,
        })
    return results


def _is_running(schedule_id: str) -> bool:
    from services.agent_task_store import AgentTaskStore
    from agents.base import AgentStatus
    store = AgentTaskStore.get_instance()
    tasks = store.list_recent(status=AgentStatus.RUNNING.value, limit=50)
    for t in tasks:
        if t.trigger_src and schedule_id in t.trigger_src:
            return True
    return False


def get_schedule_summary() -> List[Dict]:
    """每个 schedule 一张卡片：含最近 1 条 task 摘要 + 近 30 天历史计数。"""
    from services.agent_task_store import AgentTaskStore
    store = AgentTaskStore.get_instance()
    _sched_to_agent: Dict[str, str] = {}
    for agent_name, sched_ids in _AGENT_SCHEDULE_IDS.items():
        for sid in sched_ids:
            _sched_to_agent[sid] = agent_name
    result = []
    for sched in get_schedules():
        sid = sched["id"]
        recent = store.list_by_schedule(sid, limit=30, days=30)
        latest = recent[0] if recent else None
        lt_agent = (getattr(latest, "agent_name", None) if latest else None) or _sched_to_agent.get(sid)
        result.append({
            **sched,
            "agent_name": lt_agent,
            "history_count": len(recent),
            "latest_task": {
                "id": latest.id,
                "status": latest.status.value,
                "title": latest.title,
                "agent_name": getattr(latest, "agent_name", None),
                "started_at": latest.started_at.isoformat() + "Z" if latest.started_at else None,
                "finished_at": latest.finished_at.isoformat() + "Z" if latest.finished_at else None,
                "progress": latest.progress,
            } if latest else None,
        })
    return result
