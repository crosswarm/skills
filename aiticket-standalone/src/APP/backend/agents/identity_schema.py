"""
L5 Identity YAML schema — Pydantic v2 校验模型

启动期 fail-fast: registry.py 调用 validate_all_identities() 时，
任何不合规的 YAML 均导致后端拒绝启动，而非运行时静默错误。

触发器枚举 (memory_write_trigger):
  on_adoption          人类采纳回复/建议后写入
  on_evaluation        评估完成后写入
  on_discovery         发现新的有价值事实后写入
  on_pattern_found     聚类分析发现模式后写入
  on_pattern_confirmed 模式被多次验证后写入（高置信）
  on_reflect_long_task 长任务结束前的 memory-flush 反思（借鉴 Hermes）
  on_nudge             周期性 nudge（reflect_interval_steps 触发）
  always               每次执行都写（慎用，仅 audit 类）
  never                只读不写

作用域枚举 (memory_write_scope):
  shared   → user_id = "shared"           (所有 agent 共享读写)
  private  → user_id = "agent:{name}"     (per-agent 隔离)
  user     → user_id = "user:{ticket_uid}" (用户级，reply_agent 专用)
  none     → 不写入 L3（仅 trigger=never 时有效）
"""
from __future__ import annotations

import glob
import json
import logging
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

IDENTITY_DIR = Path(__file__).resolve().parent / "identity"
_ROUTING_FILE = Path(__file__).resolve().parent.parent / "llm_feature_routing.json"

WriteTrigger = Literal[
    "on_adoption",
    "on_evaluation",
    "on_discovery",
    "on_pattern_found",
    "on_pattern_confirmed",
    "on_reflect_long_task",
    "on_nudge",
    "always",
    "never",
]

WriteScope = Literal["shared", "private", "user", "none"]

AgentType = Literal[
    "ingest", "cluster", "analyst", "enricher", "solution",
    "retrieval", "synthesis", "bridge", "evaluator", "meta",
]

# 角色种类（决定 trigger 路径与 agents.html 展示分组）
AgentKind = Literal[
    "internal_master",   # 常驻主导（UXMaster / PRDMaster / ClaudeAgent）
    "internal_dever",    # 常驻执行（UXDever / PRDDever / worker）
    "internal_worker",   # 内部工作者（默认）
    "omc_subagent",      # OMC 外部 subagent（需父 agent 授权）
    "external",          # 第三方桥接
]

# trigger 入口限制
TriggerableVia = Literal[
    "direct",       # 可直接从 agents.html / API 触发（默认）
    "parent_only",  # 只能由父 agent 授权触发（OMC subagent 强制）
    "schedule_only", # 只能由定时任务触发
]


class AgentIdentity(BaseModel):
    name: str = Field(..., min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str

    # 硬字符上限：≤600（借鉴 Hermes 强制优先级，防止 prompt bloat）
    personality: str = Field(..., min_length=20, max_length=600)

    # 至少 2 条、每条 ≤120 字（借鉴 Hermes 精简准则）
    behavioral_guidelines: List[str] = Field(..., min_length=2)

    tool_chain: List[str] = Field(..., min_length=1)

    memory_write_trigger: WriteTrigger
    memory_write_scope: WriteScope
    memory_write_template: Optional[str] = None

    # 周期性反思步数（0 = 关闭；借鉴 Hermes nudge_interval）
    reflect_interval_steps: int = Field(0, ge=0, le=100)

    # META-V1 可选字段（全部有默认值，向后兼容旧 YAML）
    id: Optional[str] = None
    default_nickname: Optional[str] = None
    agent_type: Optional[AgentType] = None
    role: Optional[str] = None
    jobs: Optional[List[str]] = None
    llm_feature_key: Optional[str] = None
    version: str = "1.0"
    hidden: bool = False
    parent_agent: str = ""
    tags: List[str] = Field(default_factory=list)

    # 角色层级与 OMC bridge 字段
    kind: AgentKind = "internal_worker"
    triggerable_via: TriggerableVia = "direct"
    # OMC subagent 专用
    source_md: Optional[str] = None
    omc_subagent_type: Optional[str] = None
    # 父 agent 专用（master / dever 类填写）
    manages_subagent_kinds: List[str] = Field(default_factory=list)
    authorize_subagent_policy: Optional[str] = None

    # 历史 agent_name 别名（合并后的归档映射，stats 展示时聚合）
    aliases: List[str] = Field(default_factory=list)

    @field_validator("behavioral_guidelines", mode="before")
    @classmethod
    def _guideline_length(cls, v: list) -> list:
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and len(item) > 120:
                    raise ValueError(f"单条 guideline ≤120 字（当前 {len(item)} 字）")
        return v

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "AgentIdentity":
        trigger = self.memory_write_trigger
        scope = self.memory_write_scope
        template = self.memory_write_template

        if trigger != "never" and not template:
            raise ValueError(
                f"memory_write_trigger='{trigger}' 时必须提供 memory_write_template"
            )
        if scope == "none" and trigger != "never":
            raise ValueError("memory_write_scope='none' 仅允许与 trigger='never' 同用")

        if self.kind == "omc_subagent":
            if not self.parent_agent:
                raise ValueError("kind='omc_subagent' 时必须填写 parent_agent")
            if not self.omc_subagent_type:
                raise ValueError("kind='omc_subagent' 时必须填写 omc_subagent_type")
            if self.triggerable_via != "parent_only":
                # 强制修正，不报错（便于自动落盘）
                object.__setattr__(self, "triggerable_via", "parent_only")

        return self


def validate_all_identities(strict: bool = False) -> list[str]:
    """
    校验 agents/identity/ 下所有 YAML（排除 _schema.yaml）。
    strict=False: 返回错误列表（warn 模式，供 main.py 决定是否 raise）
    strict=True:  直接 raise RuntimeError

    返回: [] 表示全部通过；否则是错误字符串列表。
    """
    errors: list[str] = []
    pattern = str(IDENTITY_DIR / "*.yaml")
    for path in sorted(glob.glob(pattern)):
        filename = Path(path).name
        if filename.startswith("_"):
            continue
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                errors.append(f"{filename}: YAML 顶层不是 dict")
                continue
            AgentIdentity(**data)
        except Exception as e:
            errors.append(f"{filename}: {e}")

    if errors and strict:
        raise RuntimeError("L5 Identity 校验失败:\n" + "\n".join(errors))

    if errors:
        for err in errors:
            logger.warning(f"[IdentitySchema] {err}")

    return errors


def resolve_llm_chain(feature_key: Optional[str]) -> dict:
    """从 llm_feature_routing.json 解析三级 LLM 链"""
    try:
        routing = json.loads(_ROUTING_FILE.read_text(encoding="utf-8"))
    except Exception:
        routing = {}
    default_provider = routing.get("_default", "minimax")
    val = routing.get(feature_key, default_provider) if feature_key else default_provider
    chain = val if isinstance(val, list) else [val]
    while len(chain) < 3:
        chain.append(default_provider if len(chain) == 1 else "local")
    return {
        "llm_default": chain[0],
        "llm_fallback1": chain[1],
        "llm_fallback2": chain[2],
    }


def load_identity(name: str) -> Optional[AgentIdentity]:
    """加载并校验单个 agent identity，失败返回 None"""
    try:
        p = IDENTITY_DIR / f"{name}.yaml"
        if not p.exists():
            return None
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return AgentIdentity(**data)
    except Exception as e:
        logger.warning(f"[IdentitySchema] load_identity({name}): {e}")
        return None
