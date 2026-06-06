"""
BaseAgent — 多智能体编排抽象基类

所有 Agent 适配器必须继承此类，业务逻辑保留在原 service/script 中不变。
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, List, Optional

from agents.memory_mixin import AgentMemoryMixin


class AgentStatus(str, Enum):
    QUEUED    = "queued"
    RUNNING   = "running"
    SUCCEEDED = "succeeded"
    FAILED    = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentTask:
    id:           str
    agent_name:   str
    title:        str
    status:       AgentStatus   = AgentStatus.QUEUED
    progress:     int           = 0
    next_plan:    Optional[str] = None
    parent_id:    Optional[str] = None
    trigger_src:  Optional[str] = None
    schedule_id:  Optional[str] = None
    started_at:   Optional[datetime] = None
    finished_at:  Optional[datetime] = None
    payload_json: Optional[str] = None
    result_json:  Optional[str] = None
    log_tail:     Optional[str] = None
    created_at:   datetime      = field(default_factory=datetime.utcnow)

    @staticmethod
    def new(agent_name: str, title: str,
            trigger_src: str = None, payload_json: str = None,
            parent_id: str = None, schedule_id: str = None) -> "AgentTask":
        now = datetime.utcnow()
        return AgentTask(
            id=f"at_{agent_name}_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}",
            agent_name=agent_name,
            title=title,
            trigger_src=trigger_src,
            schedule_id=schedule_id,
            payload_json=payload_json,
            parent_id=parent_id,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "title": self.title,
            "status": self.status.value,
            "progress": self.progress,
            "next_plan": self.next_plan,
            "parent_id": self.parent_id,
            "trigger_src": self.trigger_src,
            "schedule_id": self.schedule_id,
            "started_at": self.started_at.isoformat() + "Z" if self.started_at else None,
            "finished_at": self.finished_at.isoformat() + "Z" if self.finished_at else None,
            "payload_json": self.payload_json,
            "result_json": self.result_json,
            "log_tail": self.log_tail,
            "created_at": self.created_at.isoformat() + "Z",
        }


class UnauthorizedAgentError(RuntimeError):
    """Agent 在 enforce 模式下未持有有效 JobMaster token 时抛出。"""


class BaseAgent(AgentMemoryMixin, ABC):  # noqa: E501  五层记忆 Mixin 接入
    name:         str  # 唯一英文 ID，e.g. "reply"
    display_name: str  # 中文展示名
    description:  str  # 模块职责一句话
    version:      str = "1.0"
    hidden:       bool = False        # 隐藏在主列表（子功能 Agent 设为 True）
    tags:         list = []           # 可见标签，e.g. ["子任务", "智能回复"]
    parent_agent: str  = ""           # 所属父 Agent name（隐藏 Agent 专用）

    def __init__(self, *, _job_token: Optional[str] = None):
        """
        _job_token: JobMaster 签发的一次性 token。
        - AUDIT 模式：缺失时记录告警，不阻断。
        - ENFORCE 模式：缺失或无效时 raise UnauthorizedAgentError。
        子类调用 super().__init__(_job_token=token) 即可，其余业务参数放在自己的 __init__。
        """
        self._job_token: Optional[str] = _job_token
        self._agent_registry_id: Optional[str] = None
        try:
            from services.jobmaster_lifecycle import JobMasterLifecycle
            lc = JobMasterLifecycle.get_instance()
            if _job_token:
                agent_info = lc.verify_token(_job_token)
                if agent_info:
                    self._agent_registry_id = agent_info["agent_id"]
                else:
                    msg = f"[JobMaster] invalid token for {self.__class__.__name__}"
                    if lc.mode == "enforce":
                        raise UnauthorizedAgentError(msg)
                    print(f"WARN {msg}")
            else:
                msg = f"[JobMaster] no token — {self.__class__.__name__} created without authorization"
                if lc.mode == "enforce":
                    raise UnauthorizedAgentError(msg)
                # audit 模式：记录但放行；跳过已有 pending/running 同类 agent 避免重启重复注册
                class_name = self.__class__.__name__
                already = any(
                    a.get("name") == class_name and a.get("state") in ("pending", "running")
                    for a in lc.list_agents()
                )
                if not already:
                    lc.spawn(
                        agent_class_name=class_name,
                        parent_token=None,
                        context={"source": "direct_instantiation", "audit": True},
                    )
        except UnauthorizedAgentError:
            raise
        except Exception:
            pass  # JobMaster 不可用（含 ImportError）时 fail-open，不阻断 agent 启动

    # ── 子类必须实现 ────────────────────────────────────────────────
    @abstractmethod
    def describe(self) -> dict:
        """静态描述：name/display_name/description/version/capabilities"""

    @abstractmethod
    def list_capabilities(self) -> List[str]:
        """能力标签列表，e.g. ['kb-search','llm-reply']"""

    # ── 子类可选实现（默认 no-op）──────────────────────────────────
    def on_register(self, registry) -> None:
        pass

    def health_check(self) -> dict:
        return {"healthy": True, "detail": "ok"}

    def run_task(self, task: "AgentTask") -> Optional[dict]:
        """手动触发时执行的主体逻辑；子类可 override，在后台线程中运行"""
        return None

    # ── 框架工具方法（不需要 override）─────────────────────────────
    def spawn_subagent(
        self,
        title: str,
        fn: Callable[..., Any],
        payload: dict = None,
        parent_id: str = None,
    ) -> AgentTask:
        from agents.registry import AgentRegistry
        return AgentRegistry.get_instance().dispatch_subagent(
            agent=self, title=title, fn=fn,
            payload=payload, parent_id=parent_id,
        )

    def report_progress(self, task_id: str, progress: int, next_plan: str = None) -> None:
        from services.agent_task_store import AgentTaskStore
        AgentTaskStore.get_instance().update_progress(task_id, progress, next_plan)

    def get_meta(self) -> dict:
        """从 YAML identity 读取完整 meta，包含运行时合成的 LLM 链。"""
        from agents.identity_schema import load_identity, resolve_llm_chain
        identity = load_identity(self.name)
        if identity is None:
            return {}
        meta = {
            "id": identity.id,
            "code": self.name,
            "default_nickname": identity.default_nickname,
            "agent_type": identity.agent_type,
            "role": identity.role,
            "jobs": identity.jobs or [],
        }
        meta.update(resolve_llm_chain(identity.llm_feature_key))
        return meta

    def append_log(self, task_id: str, text: str) -> None:
        from services.agent_task_store import AgentTaskStore
        AgentTaskStore.get_instance().append_log(task_id, text)

    def checkpoint(self, task_id: str) -> List[dict]:
        """Call at loop checkpoints. Consumes pending user messages and raises on abort keywords."""
        from services.agent_task_store import AgentTaskStore
        store = AgentTaskStore.get_instance()
        msgs = store.consume_pending_messages(task_id)
        if msgs:
            for m in msgs:
                text = m.get("text", "")
                store.append_message(task_id, "agent", f"📥 已收到指令：「{text[:80]}」，将在本检查点处理")
                if any(k in text.lower() for k in ("abort", "取消", "停止", "stop")):
                    raise RuntimeError(f"用户中止：{text}")
        return msgs
