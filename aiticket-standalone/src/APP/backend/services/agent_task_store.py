"""
AgentTaskStore — agent_tasks SQLite 持久化层

自动建表，WAL 模式，asyncio-safe（单 threading.Lock）。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from agents.base import AgentStatus, AgentTask

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sqlite" / "agent_tasks.db"

_DDL = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    id            TEXT PRIMARY KEY,
    agent_name    TEXT NOT NULL,
    parent_id     TEXT,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    progress      INTEGER NOT NULL DEFAULT 0,
    next_plan     TEXT,
    trigger_src   TEXT,
    schedule_id   TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    payload_json  TEXT,
    result_json   TEXT,
    log_tail      TEXT,
    messages_json TEXT,
    created_at    TEXT NOT NULL,
    project_key   TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_at_agent ON agent_tasks(agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_at_parent ON agent_tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_at_status ON agent_tasks(status, created_at DESC);
"""

_LOG_MAX = 2000


def _row_to_task(row: sqlite3.Row) -> AgentTask:
    def _dt(s):
        if not s:
            return None
        s = s.rstrip("Z")
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    return AgentTask(
        id=row["id"],
        agent_name=row["agent_name"],
        title=row["title"],
        status=AgentStatus(row["status"]),
        progress=row["progress"] or 0,
        next_plan=row["next_plan"],
        parent_id=row["parent_id"],
        trigger_src=row["trigger_src"],
        schedule_id=row["schedule_id"] if "schedule_id" in row.keys() else None,
        started_at=_dt(row["started_at"]),
        finished_at=_dt(row["finished_at"]),
        payload_json=row["payload_json"],
        result_json=row["result_json"],
        log_tail=row["log_tail"],
        created_at=_dt(row["created_at"]) or datetime.utcnow(),
    )


class AgentTaskStore:
    _instance: Optional["AgentTaskStore"] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Path = _DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(db_path)
        self._write_lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(_DDL)
            for col in ("messages_json TEXT", "schedule_id TEXT", "project_key TEXT DEFAULT NULL"):
                try:
                    conn.execute(f"ALTER TABLE agent_tasks ADD COLUMN {col}")
                except Exception:
                    pass
            # schedule_id index must be created after the column exists
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_at_schedule "
                    "ON agent_tasks(schedule_id, created_at DESC)"
                )
            except Exception:
                pass

    @classmethod
    def get_instance(cls) -> "AgentTaskStore":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── 写操作（串行）───────────────────────────────────────────────

    def insert(self, task: AgentTask) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO agent_tasks
                   (id, agent_name, parent_id, title, status, progress,
                    next_plan, trigger_src, schedule_id, started_at, finished_at,
                    payload_json, result_json, log_tail, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task.id, task.agent_name, task.parent_id, task.title,
                    task.status.value, task.progress, task.next_plan,
                    task.trigger_src, task.schedule_id,
                    task.started_at.isoformat() if task.started_at else None,
                    task.finished_at.isoformat() if task.finished_at else None,
                    task.payload_json, task.result_json, task.log_tail,
                    task.created_at.isoformat(),
                ),
            )

    def update_status(
        self,
        task_id: str,
        status: AgentStatus,
        started_at: datetime = None,
        finished_at: datetime = None,
        result_json: str = None,
        progress: int = None,
    ) -> None:
        fields, vals = ["status=?"], [status.value]
        if started_at is not None:
            fields.append("started_at=?"); vals.append(started_at.isoformat())
        if finished_at is not None:
            fields.append("finished_at=?"); vals.append(finished_at.isoformat())
        if result_json is not None:
            fields.append("result_json=?"); vals.append(result_json)
        if progress is not None:
            fields.append("progress=?"); vals.append(progress)
        vals.append(task_id)
        with self._write_lock, self._connect() as conn:
            conn.execute(f"UPDATE agent_tasks SET {', '.join(fields)} WHERE id=?", vals)

    def update_progress(self, task_id: str, progress: int, next_plan: str = None) -> None:
        fields, vals = ["progress=?"], [min(100, max(0, progress))]
        if next_plan is not None:
            fields.append("next_plan=?"); vals.append(next_plan)
        vals.append(task_id)
        with self._write_lock, self._connect() as conn:
            conn.execute(f"UPDATE agent_tasks SET {', '.join(fields)} WHERE id=?", vals)

    def append_log(self, task_id: str, text: str) -> None:
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT log_tail FROM agent_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                return
            current = (row["log_tail"] or "") + text + "\n"
            if len(current) > _LOG_MAX:
                current = current[-_LOG_MAX:]
            conn.execute(
                "UPDATE agent_tasks SET log_tail=? WHERE id=?", (current, task_id)
            )

    def cancel(self, task_id: str) -> bool:
        """返回 True=成功取消，False=已是终态"""
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM agent_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] in (AgentStatus.SUCCEEDED, AgentStatus.FAILED, AgentStatus.CANCELLED):
                return False
            conn.execute(
                "UPDATE agent_tasks SET status=?, finished_at=? WHERE id=?",
                (AgentStatus.CANCELLED.value, datetime.utcnow().isoformat(), task_id),
            )
            return True

    # ── 读操作 ──────────────────────────────────────────────────────

    def get(self, task_id: str) -> Optional[AgentTask]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def list_recent(
        self,
        agent_name: str = None,
        status: str = None,
        limit: int = 50,
        since: str = None,
    ) -> List[AgentTask]:
        q = "SELECT * FROM agent_tasks WHERE 1=1"
        params: list = []
        if agent_name:
            q += " AND agent_name=?"; params.append(agent_name)
        if status:
            q += " AND status=?"; params.append(status)
        if since:
            q += " AND created_at >= ?"; params.append(since)
        else:
            # 默认近 48h
            from datetime import timedelta
            cutoff = (datetime.utcnow() - timedelta(hours=48)).isoformat()
            q += " AND created_at >= ?"; params.append(cutoff)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(min(limit, 200))
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_recent_diverse(
        self,
        limit: int = 200,
        since: str = None,
        limit_per_agent: int = 15,
    ) -> List[AgentTask]:
        """返回近期任务，每个 agent 最多 limit_per_agent 条，避免单一 agent 占满视图。"""
        from datetime import timedelta
        cutoff = since or (datetime.utcnow() - timedelta(hours=48)).isoformat()
        q = """
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY agent_name ORDER BY created_at DESC) AS rn
            FROM agent_tasks
            WHERE created_at >= ?
        ) WHERE rn <= ?
        ORDER BY created_at DESC
        LIMIT ?
        """
        params = [cutoff, limit_per_agent, min(limit, 500)]
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_children(self, parent_id: str) -> List[AgentTask]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE parent_id=? ORDER BY created_at",
                (parent_id,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_by_schedule(
        self,
        schedule_id: str,
        limit: int = 50,
        days: int = 30,
    ) -> List[AgentTask]:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE schedule_id=? AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT ?",
                (schedule_id, cutoff, min(limit, 200)),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def today_stats(self, agent_name: str) -> Dict[str, int]:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM agent_tasks "
                "WHERE agent_name=? AND created_at >= ? GROUP BY status",
                (agent_name, today),
            ).fetchall()
        stats = {"total": 0, "succeeded": 0, "failed": 0}
        for r in rows:
            stats["total"] += r["cnt"]
            if r["status"] == AgentStatus.SUCCEEDED.value:
                stats["succeeded"] = r["cnt"]
            elif r["status"] == AgentStatus.FAILED.value:
                stats["failed"] = r["cnt"]
        return stats

    def recent_succeeded(self, agent_name: str, n: int = 5) -> List[Dict]:
        """返回最近 n 次成功任务的摘要（供 L2 情节记忆使用）"""
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, result_json, finished_at FROM agent_tasks "
                "WHERE agent_name=? AND status=? AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_name, AgentStatus.SUCCEEDED.value, cutoff, min(n, 20)),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "result_json": r["result_json"],
                "finished_at": r["finished_at"],
            }
            for r in rows
        ]

    def get_running_task_id(self, agent_name: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM agent_tasks WHERE agent_name=? AND status=? "
                "ORDER BY created_at DESC LIMIT 1",
                (agent_name, AgentStatus.RUNNING.value),
            ).fetchone()
        return row["id"] if row else None

    def today_stats_all(self) -> Dict[str, Dict[str, int]]:
        """Single query returning {agent_name: {total, succeeded, failed}} for today."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_name, status, COUNT(*) as cnt FROM agent_tasks "
                "WHERE created_at >= ? GROUP BY agent_name, status",
                (today,),
            ).fetchall()
        result: Dict[str, Dict[str, int]] = {}
        for r in rows:
            an = r["agent_name"] or ""
            if an not in result:
                result[an] = {"total": 0, "succeeded": 0, "failed": 0}
            result[an]["total"] += r["cnt"]
            if r["status"] == AgentStatus.SUCCEEDED.value:
                result[an]["succeeded"] = r["cnt"]
            elif r["status"] == AgentStatus.FAILED.value:
                result[an]["failed"] = r["cnt"]
        return result

    def all_running_task_ids(self) -> Dict[str, str]:
        """Single query returning {agent_name: task_id} for all currently running tasks."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_name, id FROM agent_tasks WHERE status=? "
                "ORDER BY created_at DESC",
                (AgentStatus.RUNNING.value,),
            ).fetchall()
        result: Dict[str, str] = {}
        for r in rows:
            an = r["agent_name"] or ""
            if an not in result:
                result[an] = r["id"]
        return result

    def append_message(self, task_id: str, role: str, text: str) -> None:
        import json as _json
        ts = datetime.utcnow().isoformat() + "Z"
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT messages_json FROM agent_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                return
            try:
                msgs = _json.loads(row["messages_json"] or "[]")
            except Exception:
                msgs = []
            msgs.append({"role": role, "text": text, "ts": ts})
            conn.execute(
                "UPDATE agent_tasks SET messages_json=? WHERE id=?",
                (_json.dumps(msgs, ensure_ascii=False), task_id),
            )

    def consume_pending_messages(self, task_id: str) -> list:
        """取出所有未消费的 user 消息，标记为已消费，返回消费列表"""
        import json as _json
        ts = datetime.utcnow().isoformat() + "Z"
        consumed = []
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT messages_json FROM agent_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                return []
            try:
                msgs = _json.loads(row["messages_json"] or "[]")
            except Exception:
                msgs = []
            for msg in msgs:
                if msg.get("role") == "user" and not msg.get("consumed"):
                    msg["consumed"] = True
                    msg["consumed_at"] = ts
                    consumed.append(msg)
            conn.execute(
                "UPDATE agent_tasks SET messages_json=? WHERE id=?",
                (_json.dumps(msgs, ensure_ascii=False), task_id),
            )
        return consumed

    def append_system_message(self, task_id: str, text: str) -> None:
        """记录 agent 收到指令并采取行动的系统消息"""
        self.append_message(task_id, "system", text)

    def get_messages(self, task_id: str) -> list:
        import json as _json
        with self._connect() as conn:
            row = conn.execute(
                "SELECT messages_json FROM agent_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if not row:
            return []
        try:
            return _json.loads(row["messages_json"] or "[]")
        except Exception:
            return []
