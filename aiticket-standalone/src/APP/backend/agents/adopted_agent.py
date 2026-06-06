"""AdoptedAgent — 采纳回复反推 Agent，接入五层记忆体系"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from agents.base import AgentTask, BaseAgent
from agents.self_monitor_mixin import AgentSelfMonitorMixin

logger = logging.getLogger(__name__)

_SCRIPT  = Path(__file__).resolve().parent.parent / "scripts" / "extract_facts_from_adopted.py"
_FB_FILE = Path(__file__).resolve().parent.parent / "data" / "reply_feedback.json"


class AdoptedAgent(AgentSelfMonitorMixin, BaseAgent):
    expected_run_interval_hours: float = 168
    name         = "adopted"
    display_name = "采纳反推 Agent"
    description  = "高采纳回复→候选事实→审核队列；发现跨工单采纳模式写入 L3 公共记忆"
    version      = "1.1"

    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "capabilities": self.list_capabilities(),
        }

    def list_capabilities(self) -> List[str]:
        caps = ["adopted-analysis", "fact-candidate", "review-queue", "l3-pattern-write"]
        if not _SCRIPT.exists():
            caps.append("stub-pending")
        return caps

    def health_check(self) -> dict:
        if not _SCRIPT.exists():
            return {"healthy": False, "detail": f"script pending: {_SCRIPT.name}"}
        return {"healthy": True, "detail": "script ok"}

    def run_task(self, task: AgentTask) -> Optional[dict]:
        """
        主任务逻辑：
        1. 调用 extract_facts_from_adopted.py（若存在）
        2. 从 reply_feedback.json 提取高采纳模式写入 L3 shared
        """
        self.report_progress(task.id, 10, "读取采纳记录")

        # Step 1: 调用提取脚本（若存在）
        script_result = None
        if _SCRIPT.exists():
            try:
                r = subprocess.run(
                    [sys.executable, str(_SCRIPT)],
                    capture_output=True, text=True, timeout=300,
                    cwd=str(_SCRIPT.parent.parent),
                )
                script_result = {"stdout": r.stdout[-500:], "returncode": r.returncode}
                self.append_log(task.id, f"[script] rc={r.returncode} {r.stdout[-200:]}")
            except Exception as e:
                self.append_log(task.id, f"[script] 调用失败: {e}")

        self.report_progress(task.id, 50, "分析采纳模式，写入 L3 记忆")

        # Step 2: 从 reply_feedback.json 提取模式写入 L3
        patterns_written = self._extract_and_remember()

        self.report_progress(task.id, 100, f"完成，写入 {patterns_written} 条模式")
        return {
            "patterns_written": patterns_written,
            "script_result": script_result,
        }

    def _extract_and_remember(self) -> int:
        """从 reply_feedback.json 提炼高采纳模式，写入 L3 shared"""
        written = 0
        try:
            if not _FB_FILE.exists():
                return 0
            fb = json.loads(_FB_FILE.read_text(encoding="utf-8"))
            records = fb.get("records", [])

            # 找采纳率 >= 70% 的记录（至少3条同类）
            adopted = [r for r in records if float(r.get("adoption_rate", 0)) >= 0.7]
            if len(adopted) < 3:
                logger.info(f"[AdoptedAgent] 采纳样本不足3条，跳过模式提炼")
                return 0

            # 按 module/issue_type 分组，每组 >=3 条才提炼
            from collections import defaultdict
            groups: dict = defaultdict(list)
            for r in adopted:
                key = r.get("module") or r.get("issue_type") or "general"
                groups[key].append(r)

            for group_key, items in groups.items():
                if len(items) < 3:
                    continue
                ticket_ids = [i.get("ticket_id", "unknown") for i in items[:5]]
                sample_solution = items[0].get("solution", "")[:200]
                content = (
                    f"采纳模式[{group_key}]：共{len(items)}条高采纳回复，"
                    f"样本解决方案：{sample_solution}"
                )
                mem_id = self.remember(
                    content=content,
                    source_id=ticket_ids[0],
                    scope="shared",
                    extra_meta={
                        "pattern_group": group_key,
                        "sample_count": len(items),
                        "ticket_ids": ticket_ids,
                        "memory_type": "adoption_pattern",
                    },
                )
                if mem_id:
                    written += 1
                    logger.info(f"[AdoptedAgent] 写入模式 {group_key}: {mem_id}")
        except Exception as e:
            logger.error(f"[AdoptedAgent] _extract_and_remember 失败: {e}")
        return written
