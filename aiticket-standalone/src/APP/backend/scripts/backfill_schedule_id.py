#!/usr/bin/env python3
"""
backfill_schedule_id.py — 一次性回填 agent_tasks.schedule_id

对历史任务，通过 trigger_src 字段中的 jobmaster:{schedule_id} 子串
反推出 schedule_id 并写入，使新的调度视图能展示历史数据。

运行：
    python3 scripts/backfill_schedule_id.py [--dry-run]
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from services.agent_task_store import AgentTaskStore
from services.task_bridge import SCHEDULE_AGENT_MAP


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    store = AgentTaskStore.get_instance()
    all_sids = list(SCHEDULE_AGENT_MAP.keys())

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, trigger_src FROM agent_tasks WHERE schedule_id IS NULL AND trigger_src IS NOT NULL"
        ).fetchall()

    updated = 0
    for row in rows:
        tid = row["id"]
        src = row["trigger_src"] or ""
        matched_sid = None
        for sid in all_sids:
            if sid in src:
                matched_sid = sid
                break
        if not matched_sid:
            continue
        if args.dry_run:
            print(f"  [dry] {tid} → {matched_sid}")
        else:
            with store._write_lock, store._connect() as conn:
                conn.execute(
                    "UPDATE agent_tasks SET schedule_id=? WHERE id=?",
                    (matched_sid, tid),
                )
        updated += 1

    print(f"{'[dry-run] would update' if args.dry_run else 'Updated'} {updated} rows.")


if __name__ == "__main__":
    main()
