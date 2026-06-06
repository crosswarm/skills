"""
飞书多轮互动服务 (Spec Phase 2)

交互流程:
  后端推送分析卡片 → 用户回复方向意见 → AI修订 → 再确认 → 触发PRD生成 → 推送结果

会话状态机:
  pending_review → direction_confirmed / direction_revision → prd_generating → prd_done / deferred
"""

import json
import logging
import re
import requests
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from services.feishu_notifier import get_notifier

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "data" / "feishu_sessions.db"

# 用户输入关键词映射
CONFIRM_KEYWORDS = ["方向ok", "方向OK", "ok", "OK", "确认", "生成prd", "生成PRD", "方向确认", "同意"]
REVISE_KEYWORDS  = ["调整", "修改", "需要调整", "修订"]
DEFER_KEYWORDS   = ["暂不", "暂不处理", "跳过", "先不做"]


def compute_value_score(ai_analysis: dict, similar_count: int = 0) -> float:
    """从 ai_analysis 字段计算需求价值评分 (0-10)"""
    score = 5.0
    individual = (ai_analysis or {}).get('individual', {}) or {}
    batch = (ai_analysis or {}).get('batch_context', {}) or {}

    if individual.get('detailed_solution'):
        score += 1.0

    criteria = individual.get('acceptance_criteria') or []
    score += min(len(criteria) * 0.4, 2.0)

    if individual.get('mvp_suggestion'):
        score += 0.5

    effort = individual.get('effort_estimation', '') or ''
    nums = re.findall(r'\d+', effort)
    if nums:
        total = sum(int(n) for n in nums[:3])
        if total <= 24:
            score += 1.5
        elif total <= 48:
            score += 0.5

    roi = ((batch.get('value_analysis') or {}).get('roi_assessment', '') or '').lower()
    if any(kw in roi for kw in ['短', '快', '低成本', '低风险']):
        score += 0.5

    # 维度6：相似需求数量加分（反映需求普遍性，越多人提 = 价值越高）
    if similar_count > 0:
        score += min(similar_count * 0.3, 2.0)

    return round(min(score, 10.0), 1)


def _init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feishu_sessions (
                session_id    TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                req_id        TEXT NOT NULL,
                req_title     TEXT NOT NULL,
                analysis_json TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending_review',
                round         INTEGER DEFAULT 0,
                feedback      TEXT,
                prd_url       TEXT,
                prd_task_id   TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
        """)
        conn.commit()
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("ALTER TABLE feishu_sessions ADD COLUMN prd_task_id TEXT")
            conn.commit()
    except Exception:
        pass  # column already exists
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("ALTER TABLE feishu_sessions ADD COLUMN notified_at TEXT DEFAULT NULL")
            conn.commit()
    except Exception:
        pass  # column already exists


class FeishuInteractionService:
    """
    飞书多轮互动服务 — 管理需求分析→方向确认→PRD生成的全流程对话
    """

    def __init__(self):
        _init_db(DB_PATH)
        self.notifier = get_notifier()

    # ─── 会话管理 ──────────────────────────────────────────────────────────────

    def _save_session(self, conn: sqlite3.Connection, session: dict) -> None:
        session["updated_at"] = datetime.utcnow().isoformat()
        conn.execute("""
            INSERT OR REPLACE INTO feishu_sessions
              (session_id, user_id, req_id, req_title, analysis_json,
               status, round, feedback, prd_url, prd_task_id, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session["session_id"], session["user_id"], session["req_id"],
            session["req_title"], json.dumps(session["analysis"], ensure_ascii=False),
            session["status"], session["round"], session.get("feedback"),
            session.get("prd_url"), session.get("prd_task_id"),
            session["created_at"], session["updated_at"],
        ))
        conn.commit()

    def _load_session(self, session_id: str) -> Optional[dict]:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM feishu_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["analysis"] = json.loads(d["analysis_json"])
            return d

    def get_session_by_req(self, req_id: str) -> Optional[dict]:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM feishu_sessions WHERE req_id=? ORDER BY created_at DESC LIMIT 1",
                (req_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["analysis"] = json.loads(d["analysis_json"])
            return d

    # ─── 推送分析卡片 (F1) ────────────────────────────────────────────────────

    def push_analysis_card(
        self,
        user_id: str,
        req_id: str,
        req_title: str,
        analysis: dict,
    ) -> str:
        """
        推送需求分析卡片到飞书。
        analysis 包含: summary, value_score, suggestion, references
        返回 session_id
        """
        session_id = str(uuid.uuid4())[:8]
        now = datetime.utcnow().isoformat()

        session = {
            "session_id": session_id,
            "user_id": user_id,
            "req_id": req_id,
            "req_title": req_title,
            "analysis": analysis,
            "status": "pending_review",
            "round": 1,
            "feedback": None,
            "prd_url": None,
            "created_at": now,
            "updated_at": now,
        }

        with sqlite3.connect(str(DB_PATH)) as conn:
            self._save_session(conn, session)

        msg = self._format_analysis_card(session_id, req_id, req_title, analysis, round_=1)
        self.notifier.send_message(msg)
        logger.info(f"[Feishu] 分析卡片已推送 session={session_id} req={req_id}")
        return session_id

    def _format_analysis_card(
        self,
        session_id: str,
        req_id: str,
        req_title: str,
        analysis: dict,
        round_: int = 1,
    ) -> str:
        round_label = f"(第{round_}轮)" if round_ > 1 else ""
        value = analysis.get("value_score", "N/A")
        summary = (analysis.get("summary", "") or "")[:200]
        suggestion = (analysis.get("suggestion", "") or "")[:300]
        refs = analysis.get("references", "") or ""
        detail_url = ""

        return f"""📋 **新需求待确认** {round_label}
━━━━━━━━━━━━━━━━━━━━
**{req_title}**
🔗 详情：{detail_url}
会话：`{session_id}`

📊 **价值评分**：{value}/10
**核心问题**：{summary}

📝 **AI规划建议（PRD编写计划）**：
{suggestion}

{f"📎 参考依据：{refs}" if refs else ""}
━━━━━━━━━━━━━━━━━━━━
回复关键词：
✅ **方向OK** — 立即生成PRD
✏️ **调整: [意见]** — 修订分析方向
⏸ **暂不处理** — 跳过此需求
⏱ 不回复 = **60秒后自动执行**
"""

    # ─── 处理用户回复 (F2/F3) ─────────────────────────────────────────────────

    def handle_user_reply(self, session_id: str, reply_text: str) -> dict:
        """
        处理用户在飞书的回复，驱动会话状态机。
        返回: {"action": "confirm"|"revise"|"defer"|"unknown", "session": ...}
        """
        session = self._load_session(session_id)
        if not session:
            logger.warning(f"[Feishu] 未找到会话: {session_id}")
            return {"action": "unknown", "reason": "session_not_found"}

        if session["status"] not in ("pending_review", "pending_revision"):
            return {"action": "stale", "status": session["status"]}

        reply_lower = reply_text.strip().lower()

        # 判断意图
        if any(kw.lower() in reply_lower for kw in CONFIRM_KEYWORDS):
            action = "confirm"
        elif any(kw.lower() in reply_lower for kw in REVISE_KEYWORDS):
            action = "revise"
        elif any(kw.lower() in reply_lower for kw in DEFER_KEYWORDS):
            action = "defer"
        else:
            # 含实质内容的自由文本 → 视为修订意见
            action = "revise" if len(reply_text.strip()) > 5 else "unknown"

        with sqlite3.connect(str(DB_PATH)) as conn:
            if action == "confirm":
                session["status"] = "direction_confirmed"
                session["feedback"] = reply_text
                self._save_session(conn, session)
                self._notify_direction_confirmed(session)

            elif action == "revise":
                session["status"] = "pending_revision"
                session["feedback"] = reply_text
                session["round"] += 1
                self._save_session(conn, session)
                self._notify_revision_received(session)

            elif action == "defer":
                session["status"] = "deferred"
                self._save_session(conn, session)
                self.notifier.send_message(
                    f"⏸ 需求 **{session['req_title']}** 已标记为暂不处理。\n会话 `{session_id}` 已关闭。"
                )

        logger.info(f"[Feishu] 用户回复处理完成 session={session_id} action={action}")
        return {"action": action, "session_id": session_id}

    def _notify_direction_confirmed(self, session: dict) -> None:
        req_id = session['req_id']
        self.notifier.send_message(
            f"✅ 方向已确认！\n"
            f"需求 **{session['req_title']}** 已确认（PRD 生成在 deployable 版本中不可用）。\n"
            f"会话 `{session['session_id']}`"
        )
        # PRD draft generation not available in this build
        logger.info(f"[Feishu] direction confirmed for req={req_id}; PRD pipeline disabled")

    def _notify_revision_received(self, session: dict) -> None:
        self.notifier.send_message(
            f"✏️ 收到修订意见，正在调整分析...\n"
            f"需求 **{session['req_title']}** 第{session['round']}轮分析即将推送。"
        )

    def push_revised_card(self, session_id: str, revised_analysis: dict) -> bool:
        """推送修订版分析卡片 (F2 多轮循环)"""
        session = self._load_session(session_id)
        if not session:
            return False

        session["analysis"] = revised_analysis
        session["status"] = "pending_review"
        with sqlite3.connect(str(DB_PATH)) as conn:
            self._save_session(conn, session)

        msg = self._format_analysis_card(
            session_id,
            session["req_id"],
            session["req_title"],
            revised_analysis,
            round_=session["round"],
        )
        self.notifier.send_message(msg)
        logger.info(f"[Feishu] 修订卡片已推送 session={session_id} round={session['round']}")
        return True

    # ─── 推送 PRD 结果 (F4) ──────────────────────────────────────────────────

    def push_prd_result(self, session_id: str, prd_path: str, prd_size_kb: float) -> bool:
        session = self._load_session(session_id)
        if not session:
            return False

        now = datetime.utcnow().isoformat()
        # 原子抢锁：只有第一个把 status 从非 prd_done 改过去的调用才发飞书
        with sqlite3.connect(str(DB_PATH), isolation_level=None) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE feishu_sessions SET status='prd_done', prd_url=?, updated_at=? "
                "WHERE session_id=? AND status!='prd_done'",
                (prd_path, now, session_id),
            )
            won = cur.rowcount > 0
            conn.execute("COMMIT")

        if not won:
            logger.info(f"[Feishu] PRD已推送过 session={session_id}，跳过重复")
            return True

        req_id = session['req_id']
        detail_url = ""
        criteria_count = len((session['analysis'].get('acceptance_criteria') or []))

        msg = f"""🎉 **PRD生成完成**
━━━━━━━━━━━━━━━━━━━━
**需求**：{session['req_title']}
会话：`{session_id}`

📄 **已生成内容**：
• 需求背景与问题定义
• 核心功能规格说明
• MVP建议与实施阶段
• 接口设计建议
• 验收标准（{criteria_count}条）
• 工作量估算

**文件**：`{prd_path}` ({prd_size_kb:.1f} KB)
━━━━━━━━━━━━━━━━━━━━
回复 **再次修订** 可重新调整方向并生成新版本。
"""
        self.notifier.send_message(msg)
        logger.info(f"[Feishu] PRD结果已推送 session={session_id} path={prd_path}")

        # PRD 完成 → 自动编译入 KB（标注为"计划中"）
        try:
            from kb_compile_service import get_compile_service
            _compile_svc = get_compile_service()
            if _compile_svc and prd_path:
                from pathlib import Path as _Path
                prd_content = ""
                try:
                    prd_content = _Path(prd_path).read_text(encoding='utf-8')
                except Exception:
                    pass
                if prd_content:
                    _compile_svc.compile_topic(
                        topic=session.get('req_title', req_id),
                        override_content=prd_content,
                        extra_metadata={'status': '计划中', 'req_id': req_id},
                    )
        except Exception as e:
            logger.warning(f"[KB] PRD 编译入库失败: {e}")

        return True

    # ─── 自动确认 & PRD轮询 ───────────────────────────────────────────────────

    def auto_confirm_timed_out_sessions(self, timeout_seconds: int = 60) -> list:
        """自动确认超时的 pending_review 会话，触发PRD生成（幂等：UPDATE 抢锁，避免并发双发）"""
        now = datetime.utcnow()
        cutoff = (now - timedelta(seconds=timeout_seconds)).isoformat()
        confirmed = []
        with sqlite3.connect(str(DB_PATH), isolation_level=None) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 原子 UPDATE：一锅把所有超时 pending_review 改到 direction_confirmed
            conn.execute(
                "UPDATE feishu_sessions SET status='direction_confirmed', "
                "feedback='[auto-confirm: timeout]', updated_at=? "
                "WHERE status='pending_review' AND created_at < ?",
                (now.isoformat(), cutoff),
            )
            # 只捞本次自己写的（notified_at IS NULL 保证每行只通知一次）
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM feishu_sessions "
                "WHERE status='direction_confirmed' AND feedback='[auto-confirm: timeout]' "
                "AND notified_at IS NULL"
            ).fetchall()
            for row in rows:
                d = dict(row)
                d['analysis'] = json.loads(d['analysis_json'])
                self.notifier.send_message(
                    f"⏱ **自动确认**\n"
                    f"需求 **{d['req_title']}** 已超时自动确认。\n"
                    f"会话 `{d['session_id']}`"
                )
                conn.execute(
                    "UPDATE feishu_sessions SET notified_at=? WHERE session_id=?",
                    (now.isoformat(), d['session_id']),
                )
                self._notify_direction_confirmed(d)
                confirmed.append(d['session_id'])
            conn.execute("COMMIT")
        return confirmed

    def poll_prd_tasks(self) -> list:
        """PRD draft generation removed in deployable build — no-op."""
        return []

    # ─── 查询 ─────────────────────────────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[dict]:
        return self._load_session(session_id)

    def list_sessions(self, user_id: Optional[str] = None, status: Optional[str] = None) -> list:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM feishu_sessions WHERE 1=1"
            params = []
            if user_id:
                sql += " AND user_id=?"
                params.append(user_id)
            if status:
                sql += " AND status=?"
                params.append(status)
            sql += " ORDER BY created_at DESC LIMIT 50"
            rows = conn.execute(sql, params).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["analysis"] = json.loads(d["analysis_json"])
                del d["analysis_json"]
                result.append(d)
            return result


# 全局单例
_interaction_service: Optional[FeishuInteractionService] = None


def get_interaction_service() -> FeishuInteractionService:
    global _interaction_service
    if _interaction_service is None:
        _interaction_service = FeishuInteractionService()
    return _interaction_service
