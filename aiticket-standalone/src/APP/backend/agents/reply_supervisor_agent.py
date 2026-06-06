"""Gate 5 独立监督审计：用不同 LLM provider 评审生成回复的质量。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_GATES_YAML = _PROJECT_ROOT / "config" / "reply_gates.yaml"

_PROVIDER_ISOLATION: dict = {
    "minimax": ["local", "minimax"],   # zhipu=GLM-5.1(reasoning-only) → 降级用 minimax
    "zhipu":   ["local", "minimax"],
    "local":   ["minimax", "zhipu"],
    "gemini":  ["minimax", "zhipu"],
}


def _get_llm_for_gate(node_key: str, main_provider: str) -> str:
    """返回监督 LLM provider：优先用 reply_gates.yaml 中配置的 agent，否则回退隔离策略。"""
    try:
        from services.pipeline_config_manager import PipelineConfigManager as _PCM
        agent_cfg = _PCM(_GATES_YAML).resolve_agent(node_key)
        if agent_cfg and agent_cfg.get("llm_feature_key"):
            # agent 已配置，用其 feature_key 的 fallback chain
            from services.local_llm_lifecycle import with_fallback_chain, daytime_chain
            feat = agent_cfg["llm_feature_key"]
            return with_fallback_chain(feat, daytime_chain(feat))
    except Exception as exc:
        logger.debug("[_get_llm_for_gate] agent lookup failed for %s: %s", node_key, exc)
    return _pick_supervisor_provider(main_provider)


@dataclass
class SupervisorResult:
    supervisor_score: Optional[float] = None
    risk_flags: list[str] = field(default_factory=list)
    evidence_coverage: float = 0.5
    step_safety: str = "safe"   # "safe" | "risky" | "unsafe"
    rationale: str = ""
    provider_used: str = ""
    gate_enabled: bool = True
    status: str = "ok"


def supervise(
    issue_key: str,
    issue_title: str,
    issue_description: str,
    generated_reply: str,
    kb_evidence: list[dict],
    gate_decisions: dict,
    main_provider: str = "minimax",
) -> SupervisorResult:
    """
    调用独立 LLM 对生成回复进行质量审计。
    主 provider 与监督 provider 强制隔离，避免自评偏差。
    """
    cfg = _load_gate_config()
    if not cfg.get("enabled", False):
        return SupervisorResult(gate_enabled=False)

    provider = _get_llm_for_gate("supervisor", main_provider)

    # 构造证据摘要（最多 3 条，每条 300 字符）
    evidence_parts = []
    for i, item in enumerate((kb_evidence or [])[:3], 1):
        text = (item.get("chunk_text") or item.get("raw_content") or "")[:300]
        name = item.get("name", f"资料{i}")
        evidence_parts.append(f"[资料{i}] {name}: {text}")
    evidence_summary = "\n".join(evidence_parts) or "（无知识库证据）"

    prompt = (
        "你是一个独立的回复质量审计员，请评估以下客服回复的质量。\n\n"
        f"工单标题：{issue_title[:200]}\n"
        f"工单描述：{issue_description[:400]}\n\n"
        f"知识库证据：\n{evidence_summary}\n\n"
        f"待审回复：\n{generated_reply[:800]}\n\n"
        "请从以下维度评分，返回纯 JSON（不加代码块标记）：\n"
        "1. supervisor_score：综合质量分 0.0-1.0\n"
        "2. risk_flags：问题标签数组，可选值：hallucination（幻觉）/ evidence_mismatch（与证据不符）/ "
        "over_specific（过度具体化）/ user_intent_drift（偏离用户意图）/ version_conflict（版本冲突）\n"
        "3. evidence_coverage：知识库证据覆盖率 0.0-1.0\n"
        "4. step_safety：步骤安全性 safe / risky / unsafe\n"
        "5. rationale：简短审计说明（50字以内）\n\n"
        "格式：{\"supervisor_score\":0.8,\"risk_flags\":[],\"evidence_coverage\":0.75,"
        "\"step_safety\":\"safe\",\"rationale\":\"...\"}"
    )

    from services.local_llm_lifecycle import is_alive, shutdown_if_started_by_us
    started_local = False
    raw = ""
    try:
        if provider == "local":
            if is_alive():
                # 本地模型已在线，直接用；不由我们启动，结束后也不关
                started_local = False  # 已由别处管理，不触发 shutdown
            else:
                # 热路径不等待本地模型启动，立即降级到第二候选 provider
                candidates = _PROVIDER_ISOLATION.get(main_provider, ["minimax", "zhipu"])
                provider = next((c for c in candidates if c != "local"), "minimax")
                logger.info("[supervisor] local LLM offline, falling back to %s", provider)
        from llm_service import LLMService
        llm = LLMService()
        _pcfg = _load_provider_cfg(provider)
        # reasoning models (MiniMax-M2.7, DeepSeek-R1) need ≥1500 tokens to clear their think block
        max_tok = 1500 if provider in ("minimax", "local") else 512
        raw = llm.call_llm(prompt, api_key=_pcfg["api_key"], provider=provider, model_name=_pcfg["model_name"], base_url=_pcfg["base_url"], max_tokens=max_tok, temperature=0.1)
        logger.info("[supervisor] %s scored by %s: %.80s", issue_key, provider, raw)
    except Exception as e:
        logger.warning("[supervisor] LLM call failed (%s), using neutral score: %s", provider, e)
        return SupervisorResult(supervisor_score=None, status="llm_failed", rationale="审计调用失败", provider_used=provider)
    finally:
        if started_local:
            shutdown_if_started_by_us("reply_supervisor")

    return _parse_result(raw, provider)


def _load_provider_cfg(provider_name: str) -> dict:
    try:
        raw = json.loads((_PROJECT_ROOT / "llm_config.json").read_text(encoding="utf-8"))
        p = raw.get(provider_name, {})
        return {"api_key": p.get("api_key", ""), "model_name": p.get("model_name", ""), "base_url": p.get("base_url", "")}
    except Exception:
        return {"api_key": "", "model_name": "", "base_url": ""}


def _pick_supervisor_provider(main_provider: str) -> str:
    candidates = _PROVIDER_ISOLATION.get(main_provider, ["zhipu", "minimax"])
    # 优先尝试 local（节省成本），但不做 ensure_running（在 supervise 内处理）
    return candidates[0] if candidates else "zhipu"


def _parse_result(raw: str, provider: str) -> SupervisorResult:
    # 提取 JSON（兼容 LLM 可能输出的前缀/后缀文本）
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        logger.warning("[supervisor] no JSON found in response, using neutral score")
        return SupervisorResult(supervisor_score=None, status="llm_failed", rationale="无法解析审计结果", provider_used=provider)
    try:
        d = json.loads(m.group(0))
        return SupervisorResult(
            supervisor_score=float(d.get("supervisor_score", 0.5)),
            risk_flags=list(d.get("risk_flags", [])),
            evidence_coverage=float(d.get("evidence_coverage", 0.5)),
            step_safety=str(d.get("step_safety", "safe")),
            rationale=str(d.get("rationale", ""))[:200],
            provider_used=provider,
            status="ok",
        )
    except Exception as e:
        logger.warning("[supervisor] JSON parse error: %s", e)
        return SupervisorResult(supervisor_score=None, status="llm_failed", rationale="JSON解析异常", provider_used=provider)


def _load_gate_config() -> dict:
    try:
        raw = yaml.safe_load((_PROJECT_ROOT / "config" / "reply_gates.yaml").read_text(encoding="utf-8"))
        return raw.get("gates", {}).get("supervisor", {})
    except Exception:
        return {}


# ── Agent registry wrapper ────────────────────────────────────────────────────
from typing import List  # noqa: E402

try:
    from agents.base import AgentTask, BaseAgent
    from agents.self_monitor_mixin import AgentSelfMonitorMixin

    class ReplySupervisorAgent(AgentSelfMonitorMixin, BaseAgent):
        """Agent registry wrapper around the supervise() function."""
        name = "reply_supervisor"
        display_name = "回复质量监督 Agent"
        description = "独立 LLM 审计生成回复质量，防止低质/风险回复流出"
        version = "1.0"
        hidden = True
        tags = ["子任务", "质量审计"]
        parent_agent = "reply"

        def __init__(self, board_service=None):
            super().__init__()
            self._board_service = board_service

        def describe(self) -> dict:
            return {
                "name": self.name,
                "display_name": self.display_name,
                "description": self.description,
                "version": self.version,
                "capabilities": self.list_capabilities(),
            }

        def list_capabilities(self) -> List[str]:
            return ["llm-supervise", "quality-audit", "risk-flag"]

        def health_check(self) -> dict:
            cfg = _load_gate_config()
            enabled = cfg.get("enabled", False)
            return {"healthy": True, "detail": f"gate_enabled={enabled}"}

except ImportError:
    pass  # base not available — module still usable as plain functions
