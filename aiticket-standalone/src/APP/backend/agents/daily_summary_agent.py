"""
Daily Summary Agent — 每日 07:25 总结昨日所有 Claude Code 会话，飞书推送。

保障机制：
1. 三重飞书发送重试（5s / 10s / 15s 退避）
2. 失败时写 agent_tasks AWAITING_HUMAN_REVIEW 卡片，agents.html 可见
3. 报告永远先归档到 conclusion/daily_reports/YYYY-MM-DD.md
4. 静态 self_validate 拦截空报告 / LLM 拒答 / 缺失关键 section

数据源（五层）：
- Layer 1: Claude Code session JSONL（会话对话）
- Layer 2: agent_tasks 表（未完成 / 失败任务）
- Layer 3: data/schedules/*.json（调度状态 / 失败 cron）
- Layer 4: ~/.claude/.../memory/*.md（项目记忆）
- Layer 5: conclusion/ 最近产出文件
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent   # APP/backend/
_PROJECT_ROOT = _BASE_DIR.parent.parent              # project root
_MEMORY_DIR = Path.home() / ".claude" / "projects" / "-Users-cfone-Studio-aiticket" / "memory"
_SCHEDULES_DIR = _BASE_DIR / "data" / "schedules"
_CONCLUSION_DIR = _PROJECT_ROOT / "conclusion"
_AGENT_TASKS_DB = _BASE_DIR / "data" / "sqlite" / "agent_tasks.db"


class QualityCheckFailed(RuntimeError):
    pass


try:
    from agents.base import AgentTask as _AgentTask, BaseAgent as _BaseAgent
    from agents.self_monitor_mixin import AgentSelfMonitorMixin as _SelfMonitorMixin
    _BASES = (_SelfMonitorMixin, _BaseAgent)
except ImportError:
    _AgentTask = object  # type: ignore
    _BASES = (object,)  # type: ignore


class DailySummaryAgent(*_BASES):  # type: ignore[misc]
    name = "daily_summary"
    display_name = "Daily Summarizer"
    description = "每日 07:25 总结昨日所有 Claude Code 会话，飞书推送"
    version = "1.0"

    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "capabilities": self.list_capabilities(),
        }

    def list_capabilities(self) -> List[str]:
        return ["session-summarize", "feishu-push", "daily-report"]

    # ── public entry point ──────────────────────────────────────────────────

    def run_task(self, payload: dict, trigger_src: str = "schedule:daily") -> dict:
        raw_date = payload.get("date")
        if raw_date:
            try:
                target_date = date.fromisoformat(str(raw_date))
            except ValueError:
                target_date = date.today() - timedelta(days=1)
        else:
            target_date = date.today() - timedelta(days=1)

        logger.info(f"[DailySummary] generating report for {target_date}")

        from services.session_log_reader import read_sessions_for_date

        events = read_sessions_for_date(target_date)
        if not events:
            logger.info("[DailySummary] no sessions found — skipping")
            return {"status": "skipped", "reason": "no sessions yesterday"}

        sections = self._build_all_sections(events, target_date)
        summary_md = self._llm_summarize(sections, target_date)

        if not self._self_validate(summary_md):
            logger.warning("[DailySummary] quality check failed — using template fallback")
            summary_md = self._template_fallback(sections, target_date)

        archive_path = self._archive(target_date, summary_md)
        send_result = self._send_with_retry(summary_md, archive_path, target_date)

        return {
            "status": "ok",
            "summary_path": archive_path,
            "event_count": len(events),
            "feishu": send_result,
        }

    # ── section builders ────────────────────────────────────────────────────

    def _build_all_sections(self, events, target_date: date) -> dict:
        """聚合五层数据源。"""
        base = self._build_session_sections(events)
        base["open_tasks"] = self._load_agent_tasks(target_date)
        base["schedule_state"] = self._load_schedules_state(target_date)
        base["memory_context"] = self._load_memory_files()
        base["conclusion_recent"] = self._load_conclusion_recent(target_date)
        base["reply_kpi"] = self._collect_reply_kpi()
        return base

    def _build_session_sections(self, events) -> dict:
        from services.session_log_reader import extract_user_questions, extract_tool_uses

        user_qs = extract_user_questions(events)
        tool_uses = extract_tool_uses(events)

        bugs: list[str] = []
        tasks: list[str] = []
        plans: list[str] = []

        bug_kw = ("bug", "fix", "修复", "报错", "error", "failed", "失败", "crash")
        task_kw = ("TaskCreate", "任务", "安排", "dispatch", "派单", "子任务")
        plan_kw = ("plan", "规划", "设计", "spec", "方案", "Stream", "架构")

        for e in events:
            low = e.content.lower()
            if any(k.lower() in low for k in bug_kw):
                bugs.append(e.content[:200])
            if any(k in e.content for k in task_kw):
                tasks.append(e.content[:200])
            if any(k in e.content for k in plan_kw):
                plans.append(e.content[:200])

        return {
            "user_questions": user_qs[:30],
            "tool_uses": tool_uses[:20],
            "bugs": bugs[:15],
            "tasks": tasks[:15],
            "plans": plans[:15],
        }

    def _load_agent_tasks(self, target_date: date) -> list[dict]:
        """从 agent_tasks.db 读取未完成/失败任务。"""
        if not _AGENT_TASKS_DB.exists():
            return []
        try:
            since = datetime(target_date.year, target_date.month, target_date.day,
                             tzinfo=timezone.utc) - timedelta(days=1)
            since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
            open_statuses = ("in_progress", "awaiting_human_review",
                             "awaiting_parent_authorization", "failed", "running")
            placeholders = ",".join("?" * len(open_statuses))
            with sqlite3.connect(str(_AGENT_TASKS_DB)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"SELECT id, agent_name, title, status, created_at, payload_json "
                    f"FROM agent_tasks "
                    f"WHERE status IN ({placeholders}) AND created_at >= ? "
                    f"ORDER BY created_at DESC LIMIT 50",
                    (*open_statuses, since_str),
                ).fetchall()
            return [
                {
                    "id": r["id"][:8],
                    "agent": r["agent_name"],
                    "title": r["title"] or "(无标题)",
                    "status": r["status"],
                }
                for r in rows
            ]
        except Exception as exc:
            logger.warning(f"[DailySummary] agent_tasks load failed: {exc}")
            return []

    def _load_schedules_state(self, target_date: date) -> list[dict]:
        """扫描 data/schedules/*.json，找失败/错过的 cron 任务。"""
        if not _SCHEDULES_DIR.exists():
            return []
        issues: list[dict] = []
        try:
            today_str = target_date.isoformat()
            for p in _SCHEDULES_DIR.glob("*.json"):
                try:
                    cfg = json.loads(p.read_text())
                    if not cfg.get("enabled", True):
                        continue
                    last_run = cfg.get("last_run") or ""
                    # 错过：next_run 在昨日内但 last_run 没更新到昨日
                    next_run = cfg.get("next_run") or ""
                    if next_run and next_run < today_str and (not last_run or last_run < today_str[:10]):
                        issues.append({
                            "id": cfg.get("id", p.stem),
                            "name": cfg.get("name", p.stem),
                            "cron": cfg.get("cron", ""),
                            "last_run": last_run[:16] if last_run else "从未",
                            "issue": "可能错过",
                        })
                except Exception:
                    continue
        except Exception as exc:
            logger.warning(f"[DailySummary] schedules state load failed: {exc}")
        return issues[:20]

    def _load_memory_files(self) -> str:
        """读取项目记忆 MEMORY.md（索引）。单条记忆太多，只加载索引。"""
        if not _MEMORY_DIR.exists():
            return ""
        try:
            index_file = _MEMORY_DIR / "MEMORY.md"
            if index_file.exists():
                content = index_file.read_text(encoding="utf-8", errors="replace")
                return content[:3000]
        except Exception as exc:
            logger.warning(f"[DailySummary] memory load failed: {exc}")
        return ""

    def _load_conclusion_recent(self, target_date: date) -> list[str]:
        """列出 conclusion/ 下当日或昨日修改的文件（标题摘要）。"""
        if not _CONCLUSION_DIR.exists():
            return []
        try:
            cutoff = datetime(target_date.year, target_date.month, target_date.day,
                              tzinfo=timezone.utc) - timedelta(days=1)
            cutoff_ts = cutoff.timestamp()
            results: list[str] = []
            for p in _CONCLUSION_DIR.rglob("*.md"):
                if p.stat().st_mtime >= cutoff_ts:
                    # 取文件第一行作为标题
                    try:
                        first_line = p.read_text(encoding="utf-8", errors="replace").split("\n")[0][:100]
                    except Exception:
                        first_line = p.name
                    results.append(f"{p.relative_to(_CONCLUSION_DIR)}: {first_line}")
            return results[:20]
        except Exception as exc:
            logger.warning(f"[DailySummary] conclusion recent load failed: {exc}")
            return []

    def _collect_reply_kpi(self) -> dict:
        """读取智能回复采纳率 + 训练器最后产出时间。"""
        result: dict = {
            "live_total": 0, "live_adopted": 0, "adoption_rate_pct": 0.0,
            "target_pct": 4.5, "trainer_last_run": None,
        }
        try:
            feedback_path = _BASE_DIR / "data" / "reply_feedback.json"
            if feedback_path.exists():
                data = json.loads(feedback_path.read_text(encoding="utf-8"))
                total = data.get("live_total", 0)
                adopted = data.get("live_adopted", 0)
                result["live_total"] = total
                result["live_adopted"] = adopted
                result["adoption_rate_pct"] = round(adopted / total * 100, 1) if total > 0 else 0.0
        except Exception as exc:
            logger.warning(f"[DailySummary] reply_feedback load failed: {exc}")
        try:
            rules_path = _BASE_DIR / "data" / "reply_style_rules.md"
            if rules_path.exists():
                mtime = rules_path.stat().st_mtime
                result["trainer_last_run"] = datetime.fromtimestamp(
                    mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
        except Exception as exc:
            logger.warning(f"[DailySummary] reply_style_rules mtime failed: {exc}")
        return result

    # ── LLM prompt + summarize ───────────────────────────────────────────────

    def _llm_summarize(self, sections: dict, target_date: date) -> str:
        try:
            cfg = self._load_llm_config()
            if not cfg.get("apiKey"):
                return self._template_fallback(sections, target_date)

            prompt = self._build_prompt(sections, target_date)
            from llm_service import LLMService
            svc = LLMService()
            result = svc.call_llm(
                prompt=prompt,
                api_key=cfg["apiKey"],
                provider=cfg.get("provider", "openai"),
                model_name=cfg.get("modelName", ""),
                base_url=cfg.get("baseUrl", ""),
                temperature=0.3,
            )
            return result.strip() if result else self._template_fallback(sections, target_date)
        except Exception as exc:
            logger.warning(f"[DailySummary] LLM call failed: {exc} — using template fallback")
            return self._template_fallback(sections, target_date)

    def _build_prompt(self, sections: dict, target_date: date) -> str:
        qs_text = "\n".join(f"- {q[:150]}" for q in sections["user_questions"][:20])
        tools_text = "\n".join(f"- {t}" for t in sections["tool_uses"][:15])
        bugs_text = "\n".join(f"- {b[:150]}" for b in sections["bugs"][:10])
        tasks_text = "\n".join(f"- {t[:150]}" for t in sections["tasks"][:10])
        plans_text = "\n".join(f"- {p[:150]}" for p in sections["plans"][:10])

        open_tasks = sections.get("open_tasks", [])
        open_tasks_text = "\n".join(
            f"- [{t['id']}] [{t['agent']}] {t['title']} — 状态:{t['status']}"
            for t in open_tasks
        ) or "无"

        schedule_issues = sections.get("schedule_state", [])
        schedule_text = "\n".join(
            f"- {s['name']} (cron:{s['cron']}) — {s['issue']}，上次运行:{s['last_run']}"
            for s in schedule_issues
        ) or "无"

        memory_text = sections.get("memory_context", "")[:1500]
        conclusion_text = "\n".join(sections.get("conclusion_recent", [])) or "无"
        _kpi = sections.get("reply_kpi", {})
        if _kpi.get("live_total", 0) > 0:
            reply_kpi_text = (
                f"采纳率 {_kpi.get('adoption_rate_pct', 0.0)}%"
                f"（{_kpi.get('live_adopted', 0)}/{_kpi.get('live_total', 0)} 条），"
                f"阶段目标 {_kpi.get('target_pct', 4.5)}%；"
                f"训练器最后产出：{_kpi.get('trainer_last_run') or '未知'}"
            )
        else:
            reply_kpi_text = "暂无数据"

        return f"""你是 AITicket 项目的日报编写助手。
基于 {target_date} 的所有数据，按下列固定结构写一份飞书日报（≤ 2500 字）：

# 📅 AITicket 日报 - {target_date}

## 🔥 昨日重点问题
（最多 3 条，每条：问题 / 现状 / 后续计划）

## ✅ 昨日完成
（最多 5 条 bullet：bug 修复 / 功能上线 / 重构。bug 修复要带文件路径）

## 📋 昨日规划/编排
（用户给出的新任务、Plan mode 产出的 spec、JobMaster 派发的子任务）

## 📌 未完成事务清单（含下一步动作）
按优先级列出：
- [task_id] [agent] 标题 — 状态：__ — 下一步：__
（来自 agent_tasks 未完成任务 + schedules 错过条目）

## ⚠️ 失败 / 异常需要关注
列出昨日 status=failed 的任务、错过的 cron、SuperGemma4 启动失败等异常事件

## ⏳ 待展开/未分析
（会话中提到但未展开的需求、plan mode 中标记 pending 的任务）

## 🎯 今日建议优先
（基于上述待办给 3 条建议）

要求：
- 只总结数据里有的内容，不要臆造
- 每段 ≤ 5 条
- bug / 修复要带文件路径
- 任务要带 task_id 便于追溯
- 简洁，无客套话

---

**用户提问（{len(sections['user_questions'])} 条，摘选）**:
{qs_text or '无'}

**工具调用（{len(sections['tool_uses'])} 条，摘选）**:
{tools_text or '无'}

**Bug/修复相关（{len(sections['bugs'])} 条，摘选）**:
{bugs_text or '无'}

**任务/派单相关（{len(sections['tasks'])} 条，摘选）**:
{tasks_text or '无'}

**规划/方案相关（{len(sections['plans'])} 条，摘选）**:
{plans_text or '无'}

**未完成 / 失败任务（来自 agent_tasks 表，{len(open_tasks)} 条）**:
{open_tasks_text}

**错过 / 异常 cron 任务（{len(schedule_issues)} 条）**:
{schedule_text}

**近期产出文件（conclusion/）**:
{conclusion_text}

**项目记忆索引（MEMORY.md 节选）**:
{memory_text or '无'}

**🤖 智能回复 KPI（请务必单独成节写入日报）**:
{reply_kpi_text}
"""

    def _template_fallback(self, sections: dict, target_date: date) -> str:
        qs = "\n".join(f"- {q[:120]}" for q in sections["user_questions"][:5]) or "- 无"
        bugs = "\n".join(f"- {b[:120]}" for b in sections["bugs"][:5]) or "- 无"
        tasks = "\n".join(f"- {t[:120]}" for t in sections["tasks"][:5]) or "- 无"
        plans = "\n".join(f"- {p[:120]}" for p in sections["plans"][:5]) or "- 无"

        open_tasks = sections.get("open_tasks", [])
        open_tasks_text = "\n".join(
            f"- [{t['id']}] [{t['agent']}] {t['title']} — {t['status']}"
            for t in open_tasks[:10]
        ) or "- 无"

        schedule_issues = sections.get("schedule_state", [])
        schedule_text = "\n".join(
            f"- {s['name']} — {s['issue']}"
            for s in schedule_issues[:5]
        ) or "- 无"
        _kpi = sections.get("reply_kpi", {})
        reply_kpi_text = (
            f"采纳率 {_kpi.get('adoption_rate_pct', 0.0)}%"
            f"（{_kpi.get('live_adopted', 0)}/{_kpi.get('live_total', 0)} 条），"
            f"目标 {_kpi.get('target_pct', 4.5)}%；"
            f"训练器最后产出：{_kpi.get('trainer_last_run') or '未知'}"
        ) if _kpi.get("live_total", 0) > 0 else "暂无数据"

        return f"""# 📅 AITicket 日报 - {target_date}

## 🔥 昨日重点问题
（LLM 摘要不可用，以下为原始内容节选）
{qs}

## ✅ 昨日完成
{bugs}

## 📋 昨日规划/编排
{plans}

## 📌 未完成事务清单（含下一步动作）
{open_tasks_text}

## ⚠️ 失败 / 异常需要关注
{schedule_text}

## 🤖 智能回复 KPI
{reply_kpi_text}

## ⏳ 待展开/未分析
{tasks}

## 🎯 今日建议优先
- 检查 agents.html 中待审批的任务
- 跟进昨日未完成的 bug 修复
- 确认 plan mode 相关的 spec 是否已落地

（本报告为模板降级版本，LLM 总结不可用）"""

    def _self_validate(self, md: str) -> bool:
        checks = [
            len(md) >= 200,
            "昨日重点问题" in md,
            "待展开" in md or "未分析" in md,
            "未完成事务清单" in md,
            "失败" in md and "异常" in md,   # LLM may alter emoji variation selectors
            len(md) <= 5000,
            not md.strip().startswith("抱歉"),
            not md.strip().startswith("我无法"),
            not md.strip().startswith("Sorry"),
        ]
        return all(checks)

    def _archive(self, target_date: date, md: str) -> str:
        reports_dir = _BASE_DIR / "conclusion" / "daily_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        archive_path = reports_dir / f"{target_date}.md"
        archive_path.write_text(md, encoding="utf-8")
        logger.info(f"[DailySummary] archived to {archive_path}")
        return str(archive_path)

    def _send_with_retry(self, md: str, archive_path: str,
                         target_date: date) -> dict:
        from services.feishu_notifier import get_notifier
        notifier = get_notifier()

        for attempt in range(1, 4):
            try:
                if notifier.send_message(md):
                    logger.info(f"[DailySummary] feishu sent on attempt {attempt}")
                    return {"ok": True, "attempt": attempt, "channel": "feishu"}
            except Exception as exc:
                logger.warning(f"[DailySummary] send attempt {attempt} error: {exc}")
            if attempt < 3:
                time.sleep(5 * attempt)

        logger.error("[DailySummary] all 3 feishu attempts failed — writing fallback task")
        try:
            self._write_failure_task(archive_path, target_date)
        except Exception as exc:
            logger.error(f"[DailySummary] failed to write failure task: {exc}")

        return {"ok": False, "archive_path": archive_path}

    def _write_failure_task(self, archive_path: str, target_date: date) -> None:
        from services.agent_task_store import AgentTaskStore
        from agents.base import AgentTask, AgentStatus

        task = AgentTask.new(
            agent_name="daily_summary",
            title=f"⚠️ 日报飞书发送失败 - {target_date}",
            trigger_src="schedule:daily:failure",
            payload_json=json.dumps({
                "kind": "daily_report_failed",
                "archive_path": archive_path,
                "target_date": str(target_date),
                "reason": "feishu 三次重试均失败",
            }, ensure_ascii=False),
        )
        task.status = AgentStatus.AWAITING_HUMAN_REVIEW
        AgentTaskStore().insert(task)

    @staticmethod
    def _load_llm_config() -> dict:
        cfg_path = _BASE_DIR / "llm_config.json"
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            provider = cfg.get("last_provider", "minimax")
            prov_cfg = cfg.get(provider, {})
            return {
                "provider": "openai",
                "apiKey": prov_cfg.get("api_key") or prov_cfg.get("apiKey", ""),
                "modelName": prov_cfg.get("model_name") or prov_cfg.get("modelName", ""),
                "baseUrl": prov_cfg.get("base_url") or prov_cfg.get("baseUrl", ""),
            }
        except Exception:
            return {}
