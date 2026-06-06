"""
task_bridge.py — JobMaster ↔ AgentTaskStore 无侵入桥接

JobMaster 通过 try/import 引入，失败时静默降级，不影响主流程。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# ── schedule_id → agent_name 映射（从 schedule JSON agent_hint 自动派生 + 硬编码兜底）──
_SCHEDULE_AGENT_MAP_FALLBACK: dict = {
    "nightly-exploration":    "competitor",
    "nightly-training":       "reply",
    "weekly-report":          "daily_summary",
    "weekly-fact-extraction": "kb_fact",
    "weekly-adopted-extract": "adopted",
    "oneshot-backfill-facts": "kb_fact",
    "jobmaster-monitor":      "darwin",
    "jobmaster-heartbeat":    "darwin",
    "jobmaster-daily":        "daily_summary",
}


def _build_schedule_agent_map() -> dict:
    """从 data/schedules/*.json 的 agent_hint 字段自动派生，缺失时用硬编码兜底。"""
    import json as _json
    out = dict(_SCHEDULE_AGENT_MAP_FALLBACK)
    sched_dir = _BACKEND / "data" / "schedules"
    if sched_dir.is_dir():
        for f in sched_dir.glob("*.json"):
            if f.name.startswith("deferred-") or f.name.startswith("__"):
                continue
            try:
                d = _json.loads(f.read_text(encoding="utf-8"))
                hint = d.get("agent_hint") or d.get("agent_name")
                if hint:
                    sid = d.get("id") or f.stem
                    out.setdefault(sid, hint)  # JSON 优先，不覆盖硬编码兜底
            except Exception:
                pass
    return out


SCHEDULE_AGENT_MAP: dict = _build_schedule_agent_map()


def _store():
    from services.agent_task_store import AgentTaskStore
    return AgentTaskStore.get_instance()


def notify_trigger(schedule_id: str, title: str) -> Optional[str]:
    """在 agent_tasks 插入 running 行，返回 task_id（失败返回 None）"""
    try:
        from agents.base import AgentTask, AgentStatus
        agent_name = SCHEDULE_AGENT_MAP.get(schedule_id, "daily_summary")
        task = AgentTask.new(
            agent_name=agent_name,
            title=title,
            trigger_src=f"jobmaster:{schedule_id}",
            schedule_id=schedule_id,
        )
        task.status = AgentStatus.RUNNING
        task.started_at = datetime.utcnow()
        _store().insert(task)
        return task.id
    except Exception:
        return None


def notify_done(task_id: Optional[str], success: bool, msg: str = "") -> None:
    """更新任务状态为 succeeded / failed（失败静默）"""
    if not task_id:
        return
    try:
        from agents.base import AgentStatus
        _store().update_status(
            task_id,
            AgentStatus.SUCCEEDED if success else AgentStatus.FAILED,
            finished_at=datetime.utcnow(),
            result_json=json.dumps({"msg": msg}, ensure_ascii=False),
            progress=100 if success else 0,
        )
    except Exception:
        pass


def sync_jobmaster_run(mode: str) -> Optional[str]:
    """记录 JobMaster 自身运行（mode=monitor/heartbeat/daily）"""
    labels = {"monitor": "任务监控", "heartbeat": "健康巡检", "daily": "日报编排"}
    return notify_trigger(f"jobmaster-{mode}", f"JobMaster {labels.get(mode, mode)}")


def backfill_from_schedules(schedules_dir: Path) -> int:
    """
    补录 schedules/*.json 的历史 last_run 为 succeeded 条目。
    跳过已有 trigger_src 匹配的记录，避免重复。
    返回新插入行数。
    """
    try:
        from agents.base import AgentTask, AgentStatus
        store = _store()
        count = 0
        for p in sorted(schedules_dir.glob("*.json")):
            try:
                sched = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            sid = sched.get("id", p.stem)
            last_run = sched.get("last_run")
            if not last_run or sid.startswith("jobmaster-"):
                continue
            agent_name = SCHEDULE_AGENT_MAP.get(sid)
            if not agent_name:
                continue
            try:
                run_dt = datetime.fromisoformat(last_run.rstrip("Z"))
            except Exception:
                continue
            since = (run_dt - timedelta(minutes=10)).isoformat()
            src = f"jobmaster:{sid}"
            existing = store.list_recent(agent_name=agent_name, since=since)
            if any(t.trigger_src == src for t in existing):
                continue
            task = AgentTask.new(
                agent_name=agent_name,
                title=sched.get("name", sid),
                trigger_src=src,
                schedule_id=sid,
            )
            task.status = AgentStatus.SUCCEEDED
            task.started_at = run_dt
            task.finished_at = run_dt
            task.created_at = run_dt
            task.progress = 100
            store.insert(task)
            count += 1
        return count
    except Exception:
        return 0
