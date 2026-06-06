"""ReplyAgent — 智能回复 Agent 适配器，包装 BoardService。"""
from __future__ import annotations
from typing import TYPE_CHECKING, List
from agents.base import BaseAgent
from agents.self_monitor_mixin import AgentSelfMonitorMixin

if TYPE_CHECKING:
    from board_service_chroma import BoardService


class ReplyAgent(AgentSelfMonitorMixin, BaseAgent):
    expected_run_interval_hours: float = 24
    name         = "reply"
    display_name = "智能回复 Agent"
    description  = "KB检索+事实注入+样式风格+生成回复；负责回复质量持续提升与采纳率监控"
    version      = "1.0"

    def __init__(self, board_service: "BoardService"):
        self._svc = board_service

    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "capabilities": self.list_capabilities(),
        }

    def list_capabilities(self) -> List[str]:
        return ["kb-search", "llm-reply", "style-inject", "fact-lookup", "similarity-score"]

    def health_check(self) -> dict:
        try:
            self._svc.get_stats()
            return {"healthy": True, "detail": "BoardService ok"}
        except Exception as e:
            return {"healthy": False, "detail": str(e)[:120]}
