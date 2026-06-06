"""
TeamDispatcher — 跨 agent 分发父-子任务。

用法：
    d = TeamDispatcher()
    result = d.dispatch(
        parent_agent="claude",
        parent_title="Hook Bridge 团队交付",
        dispatch_key="hook-bridge-v1",
        children=[
            ChildSpec(agent_name="claude", title="【分析】…", description="…"),
            ChildSpec(agent_name="ux_master",  title="【UX】…",  description="…"),
        ],
    )

样板来自 competitor_agent.py:55-65，扩展为跨 agent_name 的多子任务分发。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ChildSpec:
    agent_name: str
    title: str
    description: str = ""
    payload: dict = field(default_factory=dict)


class TeamDispatcher:
    """
    创建一个 parent 任务 + 多个 child 任务（可不同 agent_name），
    全部写入 agent_tasks 表，agents.html Strip 自动显示待确认数量。
    """

    def dispatch(
        self,
        *,
        parent_agent: str,
        parent_title: str,
        children: List[ChildSpec],
        dispatch_key: Optional[str] = None,
        skip_registry_check: bool = False,
    ) -> dict:
        """
        Returns {"parent_id": str, "child_ids": list[str], "skipped": bool}

        dispatch_key 用于幂等检查：同 key 已存在则跳过，避免重复写入。
        """
        from agents.base import AgentTask, AgentStatus
        from agents.registry import AgentRegistry
        from services.agent_task_store import AgentTaskStore

        store = AgentTaskStore.get_instance()
        reg = AgentRegistry.get_instance()

        # 幂等检查：同 dispatch_key 的 parent 已存在则直接返回
        if dispatch_key:
            existing = store.list_recent(agent_name=parent_agent, limit=50)
            for t in existing:
                try:
                    p = json.loads(t.payload_json or "{}")
                    if p.get("dispatch_key") == dispatch_key:
                        terminal = {"failed", "succeeded", "cancelled"}
                        if t.status.value not in terminal:
                            logger.info(
                                f"[TeamDispatcher] dispatch_key={dispatch_key!r} already exists "
                                f"(parent={t.id}, status={t.status.value}), skipping"
                            )
                            return {"parent_id": t.id, "child_ids": [], "skipped": True}
                        logger.info(
                            f"[TeamDispatcher] dispatch_key={dispatch_key!r} parent={t.id} is "
                            f"{t.status.value}, allowing re-dispatch"
                        )
                except Exception:
                    pass

        # 校验每个 child agent 都在注册表中（standalone 脚本可跳过）
        if not skip_registry_check:
            for spec in children:
                if not reg.get(spec.agent_name):
                    raise ValueError(
                        f"TeamDispatcher: agent '{spec.agent_name}' not in AgentRegistry"
                    )

        # 创建 parent 任务（running 表示"正在协调中"）
        parent_payload: dict = {}
        if dispatch_key:
            parent_payload["dispatch_key"] = dispatch_key
        parent_task = AgentTask.new(
            agent_name=parent_agent,
            title=parent_title,
            trigger_src="team_dispatch",
            payload_json=json.dumps(parent_payload, ensure_ascii=False),
        )
        parent_task.status = AgentStatus.RUNNING
        store.insert(parent_task)
        logger.info(f"[TeamDispatcher] parent task={parent_task.id} agent={parent_agent}")

        # 创建 child 任务（queued = 等待各自父 agent 批准/接单）
        child_ids: List[str] = []
        for spec in children:
            child_payload = {
                "description": spec.description,
                **spec.payload,
            }
            if dispatch_key:
                child_payload["dispatch_key"] = dispatch_key
            child_task = AgentTask.new(
                agent_name=spec.agent_name,
                title=spec.title,
                trigger_src=f"team_dispatch:parent={parent_task.id}",
                payload_json=json.dumps(child_payload, ensure_ascii=False),
                parent_id=parent_task.id,
            )
            child_task.status = AgentStatus.QUEUED
            store.insert(child_task)
            child_ids.append(child_task.id)
            logger.info(
                f"[TeamDispatcher] child task={child_task.id} "
                f"agent={spec.agent_name} parent={parent_task.id}"
            )

        return {"parent_id": parent_task.id, "child_ids": child_ids, "skipped": False}
