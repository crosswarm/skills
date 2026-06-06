"""
services/fiona_question_service.py — Fiona 每日 BIP 实体确认问询

分工：
- Fiona (本模块) 负责：扫描 pending_kb_questions / 形成问题 / 解析回复 / 写回 kb_compiled
- JobMaster 负责：每日 10:00 调度 / 通过 escalate() 进飞书 / 12h timeout 自动 default
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
_DB_PATH = _BACKEND.parent.parent / "data" / "sqlite" / "kb_chunks.db"
MAX_QUESTIONS_PER_DAY = int(os.environ.get("FIONA_MAX_QUESTIONS_PER_DAY", "5"))


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


class FionaQuestionService:

    def enqueue_topic(self, topic: str, reason: str, source_compiled_id: str = None) -> str:
        """将指代不明的 topic 入 pending_kb_questions 队列（status='queued'）。"""
        q_id = "Q" + uuid.uuid4().hex[:6].upper()
        options = [
            {"id": "A", "label": f"是独立 BIP 业务对象，使用「{topic}」作为词条"},
            {"id": "B", "label": "合并到已有 BIP 对象（回复时附 目标=XXX）"},
            {"id": "C", "label": f"拒绝该词条（{reason[:60]}）"},
        ]
        with _db() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO pending_kb_questions
                   (id, topic, source_compiled_id, options_json, default_choice,
                    llm_reasoning, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    q_id, topic, source_compiled_id,
                    json.dumps(options, ensure_ascii=False),
                    "C", reason, "queued",
                    datetime.utcnow().isoformat(),
                ),
            )
        print(f"[Fiona] 已入队问题 {q_id}: 「{topic}」")
        return q_id

    def scan_and_ask(self) -> int:
        """
        扫描 queued 问题，打包成一条 JobMaster escalate，更新 status='asked'。
        返回本次打包的问题数。
        """
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_kb_questions WHERE status='queued' ORDER BY created_at LIMIT ?",
                (MAX_QUESTIONS_PER_DAY,),
            ).fetchall()

        if not rows:
            print("[Fiona] 没有待确认的 BIP 实体问题")
            return 0

        options: dict[str, str] = {}
        desc_parts = [f"Fiona 今日 BIP 实体确认（共 {len(rows)} 条）\n"]
        q_ids: list[str] = []

        for row in rows:
            q_id = row["id"]
            q_ids.append(q_id)
            opts = json.loads(row["options_json"])
            desc_parts.append(f"\n#{q_id} 「{row['topic']}」")
            desc_parts.append(f"  Fiona 判断: {row['llm_reasoning'] or '指代不明'}")
            for opt in opts:
                key = f"{q_id}_{opt['id']}"
                options[key] = f"fiona_apply:{q_id}:{opt['id']} | {opt['label']}"
                desc_parts.append(f"  {key}: {opt['label']}")

        desc_parts.append(f"\n12h 内未回复则全部默认选 C（拒绝）。回复示例：『执行 #<决策ID> {q_ids[0]}_A』")

        try:
            if str(_BACKEND / "scripts") not in sys.path:
                sys.path.insert(0, str(_BACKEND / "scripts"))
            from jobmaster_agent import JobMasterAgent
            decision_id = JobMasterAgent().escalate(
                description="".join(desc_parts),
                options=options,
                default=f"{q_ids[0]}_C",
                timeout_hours=12,
            )
        except Exception as exc:
            print(f"[Fiona] escalate 失败: {exc}")
            return 0

        now = datetime.utcnow().isoformat()
        with _db() as conn:
            for q_id in q_ids:
                conn.execute(
                    "UPDATE pending_kb_questions SET status='asked', asked_at=?, jobmaster_decision_id=? WHERE id=?",
                    (now, decision_id, q_id),
                )

        print(f"[Fiona] 已发送 {len(rows)} 条问询，决策 ID={decision_id}")
        return len(rows)

    def apply_answer(self, question_id: str, choice: str, resolved_by: str = "user") -> str:
        """
        用户回复后由 JobMaster._execute_decision_action 路由调用。
        A=接受为独立 BIP 对象  B=待合并  C=拒绝
        """
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM pending_kb_questions WHERE id=?", (question_id,)
            ).fetchone()

        if not row:
            return f"[Fiona] 找不到问题 {question_id}"

        topic = row["topic"]
        now = datetime.utcnow().isoformat()

        if choice == "C":
            self._mark_compiled_rejected(topic, row["source_compiled_id"])
            result = f"已拒绝词条「{topic}」"
        elif choice == "A":
            self._mark_compiled_accepted(topic, row["source_compiled_id"])
            result = f"已接受词条「{topic}」为独立 BIP 对象"
        elif choice == "B":
            self._mark_compiled_needs_merge(topic, row["source_compiled_id"])
            result = f"已标记「{topic}」待合并（请人工指定目标对象）"
        else:
            result = f"未知选项 {choice!r}，跳过"

        with _db() as conn:
            conn.execute(
                """UPDATE pending_kb_questions
                   SET status='resolved', resolved_at=?, resolved_choice=?, resolved_by=?
                   WHERE id=?""",
                (now, choice, resolved_by, question_id),
            )

        print(f"[Fiona] apply_answer: {question_id} choice={choice} → {result}")
        return result

    def expire_stale(self, days: int = 7) -> int:
        """将超过 days 天未回复的 asked 问题自动拒绝并标 expired。"""
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_kb_questions WHERE status='asked' AND asked_at < ?",
                (cutoff,),
            ).fetchall()
        for row in rows:
            self.apply_answer(row["id"], "C", resolved_by="auto:expire")
            with _db() as conn:
                conn.execute(
                    "UPDATE pending_kb_questions SET status='expired' WHERE id=?",
                    (row["id"],),
                )
        return len(rows)

    # ── 写回 kb_compiled ──────────────────────────────────────────────────────

    def _mark_compiled_rejected(self, topic: str, content_id: str = None) -> None:
        cid = content_id or f"kb_compiled:{hashlib.md5(topic.encode()).hexdigest()[:8]}"
        try:
            with _db() as conn:
                conn.execute(
                    "UPDATE documents SET doc_type='kb_compiled_rejected', summary=summary||' [Fiona:已拒绝]' WHERE content_id=? AND source_kind='kb_compiled'",
                    (cid,),
                )
        except Exception as exc:
            print(f"[Fiona] _mark_compiled_rejected error: {exc}")

    def _mark_compiled_accepted(self, topic: str, content_id: str = None) -> None:
        cid = content_id or f"kb_compiled:{hashlib.md5(topic.encode()).hexdigest()[:8]}"
        try:
            with _db() as conn:
                conn.execute(
                    "UPDATE documents SET summary=replace(summary, ' [bip_judgment_status=pending_user]', '') WHERE content_id=?",
                    (cid,),
                )
        except Exception as exc:
            print(f"[Fiona] _mark_compiled_accepted error: {exc}")

    def _mark_compiled_needs_merge(self, topic: str, content_id: str = None) -> None:
        cid = content_id or f"kb_compiled:{hashlib.md5(topic.encode()).hexdigest()[:8]}"
        try:
            with _db() as conn:
                conn.execute(
                    "UPDATE documents SET doc_type='kb_compiled_needs_merge', summary=summary||' [Fiona:待合并]' WHERE content_id=? AND source_kind='kb_compiled'",
                    (cid,),
                )
        except Exception as exc:
            print(f"[Fiona] _mark_compiled_needs_merge error: {exc}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ask"
    if cmd == "ask":
        n = FionaQuestionService().scan_and_ask()
        print(f"[Fiona] 完成，共发送 {n} 条问询")
    elif cmd == "expire":
        n = FionaQuestionService().expire_stale()
        print(f"[Fiona] 清理过期 {n} 条")
    else:
        print(f"[Fiona] 未知命令 {cmd!r}。用法: python -m services.fiona_question_service ask")
