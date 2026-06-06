"""
ProjectIndexService — lazy 工单索引触发 + 进度追踪

职责：
  - 检测某 project_key 是否已有 Chroma 索引数据
  - 在用户首次切换到空项目时，后台触发拉取 + 写入
  - 暴露进度查询（供前端轮询）

QCL 守卫：AITICKET_ROLE=qcl 时所有写操作直接 noop（QCL 只读副本，由 Mini rsync 填数据）。
"""

import logging
import os
import sqlite3
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DB = os.environ.get(
    "APP_AUTH_DB_PATH",
    os.path.join(BASE_DIR, "..", "data", "app_auth.db"),
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class ProjectIndexService:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _DEFAULT_DB

    # ──────────────────────────── public API ────────────────────────────

    def has_data(self, project_key: str) -> bool:
        """Return True if this project already has a completed or running index job."""
        try:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT status FROM project_index_jobs WHERE project_key = ?",
                    (project_key,),
                ).fetchone()
            return bool(row and row["status"] in ("done", "running"))
        except Exception:
            return False

    def trigger_if_empty(self, project_key: str, lookback_days: int = 180) -> bool:
        """
        Trigger a background index job for project_key if no data exists yet.
        Returns True if a new job was enqueued, False if skipped.
        """
        if os.environ.get("AITICKET_ROLE", "").lower() == "qcl":
            return False  # QCL is read-only mirror
        if not project_key or project_key == "_global":
            return False
        if self.has_data(project_key):
            return False

        try:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT status FROM project_index_jobs WHERE project_key = ?",
                    (project_key,),
                ).fetchone()
                if row and row["status"] in ("pending", "running"):
                    return False  # already in flight
                conn.execute(
                    """INSERT OR REPLACE INTO project_index_jobs
                       (project_key, status, total, done, started_at, finished_at, error)
                       VALUES (?, 'pending', 0, 0, ?, NULL, '')""",
                    (project_key, int(time.time())),
                )
        except Exception as e:
            logger.warning(f"[ProjectIndex] DB write failed for {project_key}: {e}")
            return False

        t = threading.Thread(
            target=self._run_job,
            args=(project_key, lookback_days),
            daemon=True,
            name=f"idx-{project_key}",
        )
        t.start()
        logger.info(f"[ProjectIndex] Enqueued lazy index job for {project_key} ({lookback_days}d)")
        return True

    def get_status(self, project_key: str) -> dict:
        """Return {status, total, done, percent, error} for the given project."""
        try:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM project_index_jobs WHERE project_key = ?",
                    (project_key,),
                ).fetchone()
        except Exception as e:
            return {"project_key": project_key, "status": "error", "error": str(e), "percent": 0}

        if not row:
            return {"project_key": project_key, "status": "not_started", "percent": 0,
                    "total": 0, "done": 0, "error": ""}

        total = row["total"] or 0
        done = row["done"] or 0
        if row["status"] == "done":
            percent = 100
        elif total > 0:
            percent = min(99, int(done * 100 / total))
        else:
            percent = 0

        return {
            "project_key": project_key,
            "status": row["status"],
            "total": total,
            "done": done,
            "percent": percent,
            "error": row["error"] or "",
        }

    # ──────────────────────────── background job ────────────────────────────

    def _run_job(self, project_key: str, lookback_days: int) -> None:
        try:
            self._set_status(project_key, "running", started_at=int(time.time()))
            sys_path_patch()  # ensure scripts dir is importable

            from scripts.incremental_issues_index import run_for_project
            from jira_service import jira_service

            # Fetch total count first (for progress bar)
            jql = f'project = "{project_key}" AND updated >= -{lookback_days}d'
            result = jira_service.search_issues_rest_api(jql, start_at=0, max_results=1)
            total = result.get("total", 0) if isinstance(result, dict) else 0
            self._set_total(project_key, total)

            added = run_for_project(project_key, days=lookback_days)
            self._update_done(project_key, added)
            self._set_status(project_key, "done", finished_at=int(time.time()))
            logger.info(f"[ProjectIndex] {project_key} indexed {added} new issues")
        except Exception as e:
            logger.error(f"[ProjectIndex] Job failed for {project_key}: {e}", exc_info=True)
            self._set_status(project_key, "failed", error=str(e))

    # ──────────────────────────── DB helpers ────────────────────────────

    def _set_status(self, project_key: str, status: str,
                    started_at: Optional[int] = None,
                    finished_at: Optional[int] = None,
                    error: str = "") -> None:
        try:
            with _connect(self.db_path) as conn:
                updates = ["status = ?"]
                params: list = [status]
                if started_at is not None:
                    updates.append("started_at = ?")
                    params.append(started_at)
                if finished_at is not None:
                    updates.append("finished_at = ?")
                    params.append(finished_at)
                if error:
                    updates.append("error = ?")
                    params.append(error)
                params.append(project_key)
                conn.execute(
                    f"UPDATE project_index_jobs SET {', '.join(updates)} WHERE project_key = ?",
                    params,
                )
        except Exception as e:
            logger.warning(f"[ProjectIndex] _set_status failed: {e}")

    def _set_total(self, project_key: str, total: int) -> None:
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE project_index_jobs SET total = ? WHERE project_key = ?",
                    (total, project_key),
                )
        except Exception:
            pass

    def _update_done(self, project_key: str, done: int) -> None:
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE project_index_jobs SET done = ? WHERE project_key = ?",
                    (done, project_key),
                )
        except Exception:
            pass


def sys_path_patch() -> None:
    """Ensure APP/backend is on sys.path so scripts/ can be imported."""
    import sys
    backend = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if backend not in sys.path:
        sys.path.insert(0, backend)


_service: Optional[ProjectIndexService] = None


def get_project_index_service() -> ProjectIndexService:
    global _service
    if _service is None:
        _service = ProjectIndexService()
    return _service
