"""KB 写操作队列。

API 端（多 worker）不直接写 KB；通过此模块投递 job 到 SQLite 队列，
由 daemon（scripts/local_jobmaster_daemon.py）内的 dispatcher 线程消费。

API 端调：submit(kind, payload) → job_id，GET /api/kb/jobs/{job_id} 轮询。
Daemon 端调：register_kb_handlers() 启动消费线程。

启动后 daemon 会把 status='running' 且 started_at 超过 5 分钟的孤立 job
重置为 'pending'（launchd 重启后自动续跑）。
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "sqlite" / "kb_jobs.db"


# ─── SQLite helpers ───────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=30.0)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.row_factory = sqlite3.Row
    return c


def _init_schema() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = _conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS kb_jobs (
            id          TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            payload_json TEXT,
            status      TEXT NOT NULL DEFAULT 'pending',
            result_json TEXT,
            error       TEXT,
            created_at  REAL,
            started_at  REAL,
            ended_at    REAL
        )
    """)
    c.commit()
    c.close()


# ─── API 端（提交 + 查询） ────────────────────────────────────────────────────

def submit(kind: str, payload: dict) -> str:
    """投递写请求，立即返回 job_id（<1s 完成）。"""
    _init_schema()
    job_id = "kb_" + uuid.uuid4().hex[:12]
    c = _conn()
    c.execute(
        "INSERT INTO kb_jobs (id, kind, payload_json, status, created_at) VALUES (?,?,?,?,?)",
        (job_id, kind, json.dumps(payload, ensure_ascii=False), "pending", time.time()),
    )
    c.commit()
    c.close()
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    """返回 job 状态；不存在返回 None。"""
    _init_schema()
    c = _conn()
    row = c.execute("SELECT * FROM kb_jobs WHERE id=?", (job_id,)).fetchone()
    c.close()
    if not row:
        return None
    d = dict(row)
    if d.get("result_json"):
        try:
            d["result"] = json.loads(d["result_json"])
        except Exception:
            d["result"] = None
    return d


def list_jobs(limit: int = 30) -> list[dict]:
    """按创建时间倒序列出最近的 jobs。"""
    _init_schema()
    c = _conn()
    rows = c.execute(
        "SELECT * FROM kb_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    c.close()
    result = []
    for row in rows:
        d = dict(row)
        if d.get("result_json"):
            try:
                d["result"] = json.loads(d["result_json"])
            except Exception:
                d["result"] = None
        result.append(d)
    return result


# ─── Daemon 端（消费） ────────────────────────────────────────────────────────

def register_kb_handlers() -> None:
    """Daemon 启动时调用：初始化 schema，恢复孤立 job，启动消费线程。"""
    _init_schema()
    _recover_stale_jobs()
    t = threading.Thread(target=_dispatcher_loop, daemon=True, name="kb-write-dispatcher")
    t.start()
    logger.info("[KBDispatcher] 消费线程已启动")


def _recover_stale_jobs() -> None:
    """把 daemon 重启前未完成的 running job 重置为 pending。"""
    threshold = time.time() - 300  # 5 分钟前启动的
    c = _conn()
    affected = c.execute(
        "UPDATE kb_jobs SET status='pending', started_at=NULL WHERE status='running' AND started_at<?",
        (threshold,),
    ).rowcount
    c.commit()
    c.close()
    if affected:
        logger.info(f"[KBDispatcher] 恢复 {affected} 个孤立 job")


def _dispatcher_loop() -> None:
    while True:
        try:
            job = _claim_one()
            if job:
                _execute(job)
            else:
                time.sleep(1)
        except Exception:
            logger.exception("[KBDispatcher] dispatcher loop 异常")
            time.sleep(2)


def _claim_one() -> Optional[dict]:
    """乐观锁：SELECT + UPDATE WHERE status='pending'，避免并发抢占。"""
    c = _conn()
    try:
        row = c.execute(
            "SELECT id, kind, payload_json FROM kb_jobs WHERE status='pending' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        job_id, kind, payload_json = row["id"], row["kind"], row["payload_json"]
        updated = c.execute(
            "UPDATE kb_jobs SET status='running', started_at=? WHERE id=? AND status='pending'",
            (time.time(), job_id),
        ).rowcount
        c.commit()
        if updated == 0:
            return None
        return {"id": job_id, "kind": kind, "payload": json.loads(payload_json or "{}")}
    finally:
        c.close()


def _finish(job_id: str, status: str, result: Optional[dict], error: Optional[str]) -> None:
    c = _conn()
    c.execute(
        "UPDATE kb_jobs SET status=?, result_json=?, error=?, ended_at=? WHERE id=?",
        (status, json.dumps(result) if result is not None else None, error, time.time(), job_id),
    )
    c.commit()
    c.close()


_kb_runtime_cache = None
_kb_runtime_lock = threading.Lock()


def _get_kb_runtime():
    global _kb_runtime_cache
    with _kb_runtime_lock:
        if _kb_runtime_cache is None:
            from kb_runtime_service import KnowledgeRuntimeService
            _kb_runtime_cache = KnowledgeRuntimeService()
        return _kb_runtime_cache


def _execute(job: dict) -> None:
    kind = job["kind"]
    payload = job["payload"]
    logger.info(f"[KBDispatcher] 开始执行 {kind} job {job['id']}")
    try:
        if kind == "compile":
            from kb_compile_service import get_or_create_compile_service
            svc = get_or_create_compile_service()
            result = svc.compile_topic(
                topic=payload.get("topic", ""),
                llm_config=payload.get("llm_config"),
                override_content=payload.get("override_content"),
                extra_metadata=payload.get("extra_metadata"),
                project_key=payload.get("project_key", "_global"),
                skip_bip_validation=payload.get("skip_bip_validation", True),
            )
            if result:
                _finish(job["id"], "done", result, None)
                logger.info(f"[KBDispatcher] compile job {job['id']} 完成")
            else:
                _finish(job["id"], "failed", None, "LLM 未返回有效内容")
                logger.warning(f"[KBDispatcher] compile job {job['id']} 无结果")

        elif kind == "delete":
            ok = _get_kb_runtime().hybrid_index.delete_item(payload["content_id"])
            _finish(job["id"], "done" if ok else "failed", {"deleted": ok},
                    None if ok else "content_id 不存在")
            logger.info(f"[KBDispatcher] delete job {job['id']} ok={ok}")

        elif kind == "rebuild":
            chunk_count = _get_kb_runtime().sync()
            _finish(job["id"], "done", {"chunk_count": chunk_count}, None)
            logger.info(f"[KBDispatcher] rebuild job {job['id']} 完成 {chunk_count} chunks")

        else:
            _finish(job["id"], "failed", None, f"未知 kind: {kind}")
            logger.error(f"[KBDispatcher] 未知 job kind: {kind}")

    except Exception as e:
        logger.exception(f"[KBDispatcher] job {job['id']} ({kind}) 执行失败")
        _finish(job["id"], "failed", None, str(e))
