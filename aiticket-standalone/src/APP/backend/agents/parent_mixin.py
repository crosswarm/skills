"""
ParentAgentMixin — 父 Agent 授权协议

继承此 mixin 的 Agent（UXMaster / PRDMaster / ClaudeAgent）获得：
  - authorize_subagent()      授权 OMC subagent 派工
  - on_subagent_finished()    子 agent 任务完成回调
  - list_managed_subagents()  列出我直辖的 OMC subagent 名称列表
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ParentAgentMixin:
    """父 Agent 授权协议 mixin。需与 BaseAgent 一起继承。"""

    def authorize_subagent(
        self,
        child_name: str,
        payload: dict,
        requester: str = "api:manual",
        parent_task_id: Optional[str] = None,
    ) -> dict:
        """
        检查 child_name 是否归我管辖，通过则创建任务并调 JobMaster 派工。
        返回 dict 包含 task_id / status / message。
        """
        managed = self.list_managed_subagents()
        if child_name not in managed:
            return {
                "status": "rejected",
                "reason": f"{child_name} 不归 {self.name} 管辖，请检查父 agent 映射",
                "managed": managed,
            }

        if not self._is_healthy():
            return {
                "status": "rejected",
                "reason": f"父 agent {self.name} 当前不可用",
            }

        try:
            from agents.base import AgentTask, AgentStatus
            from services.agent_task_store import AgentTaskStore

            store = AgentTaskStore.get_instance()
            task = AgentTask.new(
                agent_name=child_name,
                title=payload.get("title") or f"{child_name} (由 {self.name} 授权)",
                trigger_src=f"parent:{self.name}",
                payload_json=json.dumps(payload, ensure_ascii=False),
                parent_id=parent_task_id,
            )
            store.insert(task)

            # 调 JobMaster 派工到 Claude Code 端
            self._dispatch_to_jobmaster(child_name, task.id, payload)

            logger.info(f"[{self.name}] authorized subagent={child_name} task={task.id}")
            return {
                "status": "authorized",
                "task_id": task.id,
                "message": f"已由 {self.display_name} 授权，任务等待 Claude Code 拾取",
                "parent": self.name,
                "child": child_name,
            }
        except Exception as e:
            logger.error(f"[{self.name}] authorize_subagent failed: {e}")
            return {"status": "error", "reason": str(e)}

    def on_subagent_finished(self, task_id: str, result: dict) -> None:
        """子 agent 任务完成后的回调（默认只记日志，子类可覆盖实现 follow-up）。"""
        logger.info(f"[{self.name}] subagent task={task_id} finished: {result.get('status')}")

    def list_managed_subagents(self) -> List[str]:
        """返回归我直辖的 OMC subagent name 列表（从注册表中查找 parent_agent==self.name）。"""
        try:
            from agents.registry import AgentRegistry
            from agents.identity_schema import load_identity

            reg = AgentRegistry.get_instance()
            result = []
            for agent in reg.list():
                identity = load_identity(agent.name)
                if identity and identity.kind == "omc_subagent" and identity.parent_agent == self.name:
                    result.append(agent.name)
            return result
        except Exception as e:
            logger.warning(f"[{self.name}] list_managed_subagents failed: {e}")
            return []

    # ── 内部辅助 ────────────────────────────────────────────────────────

    def _is_healthy(self) -> bool:
        try:
            h = self.health_check()
            return bool(h.get("healthy", True))
        except Exception:
            return True

    def _dispatch_to_jobmaster(self, child_name: str, task_id: str, payload: dict) -> None:
        """调 JobMaster authorize_claude_task，将任务加入 Claude Code 拾取队列。"""
        try:
            from services.jobmaster_lifecycle import JobMasterLifecycle
            from agents.identity_schema import load_identity

            identity = load_identity(child_name)
            omc_type = (identity.omc_subagent_type if identity else None) or child_name

            lc = JobMasterLifecycle.get_instance()
            lc.authorize_claude_task(
                session_id=task_id,
                description=payload.get("title") or f"OMC subagent task: {child_name}",
                subagent_type=omc_type,
            )
        except Exception as e:
            logger.warning(f"[{self.name}] _dispatch_to_jobmaster: {e}")
