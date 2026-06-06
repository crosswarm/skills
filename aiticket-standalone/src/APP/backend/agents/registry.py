"""
AgentRegistry — Agent 注册表与 subagent 调度

单例模式，进程启动时由 main.py 完成注册。
启动期会校验所有 L5 Identity YAML（warn-only），
校验错误记录到日志，不阻断启动（strict 模式下可改为 raise）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional

from agents.base import AgentStatus, AgentTask, BaseAgent

logger = logging.getLogger(__name__)


def validate_agent_identities(strict: bool = False) -> None:
    """
    校验 agents/identity/ 下所有 YAML（启动期调用）。
    strict=False: 仅 log warning（当前默认，兼容现有 8 个 YAML）
    strict=True:  任何错误直接 raise RuntimeError，阻断后端启动
    """
    try:
        from agents.identity_schema import validate_all_identities
        errors = validate_all_identities(strict=strict)
        if not errors:
            logger.info("[IdentitySchema] 所有 L5 Identity YAML 校验通过")
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning(f"[IdentitySchema] 校验过程异常: {e}")


class AgentRegistry:
    _instance: Optional["AgentRegistry"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._agents: Dict[str, BaseAgent] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    def get_instance(cls) -> "AgentRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── 注册 ────────────────────────────────────────────────────────
    def register(self, agent: BaseAgent) -> None:
        # 审计钩子：记录注册事件（audit/enforce 均写日志）
        try:
            from services.jobmaster_lifecycle import JobMasterLifecycle
            lc = JobMasterLifecycle.get_instance()
            if not agent._job_token:
                class_name = agent.__class__.__name__
                already = any(
                    a.get("name") == class_name and a.get("state") in ("pending", "running")
                    for a in lc.list_agents()
                )
                if not already:
                    lc.spawn(
                        agent_class_name=class_name,
                        parent_token=None,
                        context={"source": "registry_register_no_token", "agent_name": agent.name},
                    )
        except Exception:
            pass
        agent.on_register(self)
        self._agents[agent.name] = agent

    # ── 查询 ────────────────────────────────────────────────────────
    def list(self) -> List[BaseAgent]:
        return list(self._agents.values())

    def get(self, name: str) -> Optional[BaseAgent]:
        return self._agents.get(name)

    # ── subagent 调度 ───────────────────────────────────────────────
    def dispatch_subagent(
        self,
        agent: BaseAgent,
        title: str,
        fn,
        payload: dict = None,
        parent_id: str = None,
    ) -> "AgentTask":
        # 审计钩子：校验父 agent 有创建 subagent 的权限
        try:
            from services.jobmaster_lifecycle import JobMasterLifecycle
            lc = JobMasterLifecycle.get_instance()
            result = lc.spawn(
                agent_class_name=f"subagent:{title[:40]}",
                parent_token=getattr(agent, "_job_token", None),
                context={
                    "source": "dispatch_subagent",
                    "parent_agent": agent.name,
                    "parent_registry_id": getattr(agent, "_agent_registry_id", None),
                    "title": title,
                },
            )
            if not result["approved"] and lc.mode == "enforce":
                from agents.base import UnauthorizedAgentError
                raise UnauthorizedAgentError(
                    f"dispatch_subagent denied for {agent.name}: {result['reason']}"
                )
        except (ImportError, Exception) as _e:
            if "UnauthorizedAgentError" in type(_e).__name__:
                raise
        from services.agent_task_store import AgentTaskStore
        store = AgentTaskStore.get_instance()

        task = AgentTask.new(
            agent_name=agent.name,
            title=title,
            trigger_src=f"agent:{agent.name}",
            payload_json=json.dumps(payload or {}, ensure_ascii=False),
            parent_id=parent_id,
        )
        store.insert(task)

        # 在后台线程中运行（兼容同步 fn 和 async fn）
        def _run():
            store.update_status(task.id, AgentStatus.RUNNING,
                                started_at=datetime.utcnow())
            try:
                if asyncio.iscoroutinefunction(fn):
                    loop = asyncio.new_event_loop()
                    result = loop.run_until_complete(fn(task))
                    loop.close()
                else:
                    result = fn(task)
                store.update_status(
                    task.id, AgentStatus.SUCCEEDED,
                    finished_at=datetime.utcnow(),
                    result_json=json.dumps(result or {}, ensure_ascii=False, default=str),
                    progress=100,
                )
            except Exception as e:
                store.update_status(
                    task.id, AgentStatus.FAILED,
                    finished_at=datetime.utcnow(),
                    result_json=json.dumps({"error": str(e)}, ensure_ascii=False),
                )
                store.append_log(task.id, f"ERROR: {e}")

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return task

    def build_agent_summary(self, name: str, *, user_id: Optional[str] = None,
                            prefetch: Optional[dict] = None) -> dict:
        """构建单个 Agent 的 REST 响应 dict（含今日统计 + META-V1 字段）

        prefetch: {"stats": {agent_name: {total,succeeded,failed}},
                   "running": {agent_name: task_id}}
        当传入 prefetch 时跳过逐 agent DB 查询，大幅降低 /api/agents 响应时间。
        """
        from services.agent_task_store import AgentTaskStore
        agent = self._agents.get(name)
        if not agent:
            return {}
        store = AgentTaskStore.get_instance()
        _pstats = (prefetch or {}).get("stats", {})
        _prun = (prefetch or {}).get("running", {})
        stats = _pstats.get(name) or store.today_stats(name)
        running = _prun.get(name) or store.get_running_task_id(name)
        # 聚合历史别名（已归档 agent）的统计，保证合并后卡片数据完整
        _aliases: list = []
        try:
            from agents.identity_schema import load_identity as _li
            _id = _li(name)
            if _id and _id.aliases:
                _aliases = _id.aliases
                for _alias in _aliases:
                    _a_stats = _pstats.get(_alias) or store.today_stats(_alias)
                    stats = {k: stats.get(k, 0) + _a_stats.get(k, 0) for k in set(stats) | set(_a_stats)}
                    if not running:
                        running = _prun.get(_alias) or store.get_running_task_id(_alias)
        except Exception:
            pass
        try:
            from services.jobmaster_service import get_agent_schedule_info
            sched = get_agent_schedule_info(name)
        except Exception:
            sched = {}

        # META-V1: identity + LLM chain + per-user nickname
        meta_v1: dict = {}
        try:
            from agents.identity_schema import load_identity, resolve_llm_chain
            identity = load_identity(name)
            if identity:
                meta_v1 = {
                    "id": identity.id,
                    "code": name,
                    "default_nickname": identity.default_nickname,
                    # type / role / jobs: user-facing semantic naming
                    # type  = Agent | Subagent  (derived at return time from hidden)
                    # role  = categorical function (cluster/synthesis/…) ← was agent_type
                    # jobs  = one-line description              ← was role text
                    "role": identity.agent_type,
                    "jobs": identity.role or "",
                    "task_list": identity.jobs or [],
                    # keep agent_type for backward-compat consumers
                    "agent_type": identity.agent_type,
                    # 角色层级（前缀 _ 避免被 describe() 覆盖，return 时再提升）
                    "_kind": identity.kind,
                    "_parent_agent": identity.parent_agent,
                    "_triggerable_via": identity.triggerable_via,
                }
                meta_v1.update(resolve_llm_chain(identity.llm_feature_key))
                nickname = identity.default_nickname
                if user_id:
                    try:
                        from auth_service import get_auth_service
                        overrides = get_auth_service().get_user_nicknames(user_id)
                        nickname = overrides.get(name, nickname)
                    except Exception:
                        pass
                meta_v1["nickname"] = nickname
        except Exception as _e:
            logger.warning(f"[AgentRegistry] meta_v1 load failed for {name}: {_e}")

        sub_agents = []
        for child in self._agents.values():
            if getattr(child, "hidden", False) and getattr(child, "parent_agent", "") == name:
                c_stats = store.today_stats(child.name)
                sub_agents.append({
                    **child.describe(),
                    "health": child.health_check(),
                    "today_tasks": c_stats["total"],
                    "today_succeeded": c_stats["succeeded"],
                    "today_failed": c_stats["failed"],
                    "running_task_id": store.get_running_task_id(child.name),
                    "hidden": True,
                    "tags": getattr(child, "tags", []),
                    "parent_agent": name,
                })
        # kind / triggerable_via / parent_agent from identity (or agent attrs as fallback)
        _identity_kind = meta_v1.get("_kind") or getattr(agent, "kind", None) or "internal_worker"
        _identity_parent = meta_v1.get("_parent_agent") or getattr(agent, "parent_agent", "") or ""
        _triggerable_via = meta_v1.get("_triggerable_via") or getattr(agent, "triggerable_via", "direct") or "direct"

        return {
            **meta_v1,
            **agent.describe(),
            "health": agent.health_check(),
            "today_tasks": stats["total"],
            "today_succeeded": stats["succeeded"],
            "today_failed": stats["failed"],
            "running_task_id": running,
            "next_run": sched.get("next_run"),
            "last_run_schedule": sched.get("last_run_schedule"),
            "hidden": getattr(agent, "hidden", False),
            "tags": getattr(agent, "tags", []),
            "sub_agents": sub_agents,
            # surface meta_v1 fields that describe() would overwrite
            "id": meta_v1.get("id"),
            "code": name,
            "default_nickname": meta_v1.get("default_nickname"),
            "nickname": meta_v1.get("nickname"),
            "type": "Subagent" if getattr(agent, "hidden", False) else "Agent",
            "role": meta_v1.get("role"),
            "jobs": meta_v1.get("jobs", ""),
            "task_list": meta_v1.get("task_list", []),
            "agent_type": meta_v1.get("agent_type"),
            "llm_default": meta_v1.get("llm_default"),
            "llm_fallback1": meta_v1.get("llm_fallback1"),
            "llm_fallback2": meta_v1.get("llm_fallback2"),
            # 角色层级字段
            "kind": _identity_kind,
            "parent_agent": _identity_parent,
            "triggerable_via": _triggerable_via,
        }
