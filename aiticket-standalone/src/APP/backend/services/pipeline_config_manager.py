"""
Pipeline 配置管理公共方法 — 流水线类配置（reply_gates / 未来其他模块）的统一读写、agent 解析、运行时统计聚合。
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable, Optional
import yaml


def _deep_merge(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class PipelineConfigManager:
    """
    封装 pipeline 类配置的通用 CRUD + agent 解析 + 运行时指标聚合。
    每种 pipeline 一个实例（如 reply_gates_manager = PipelineConfigManager('reply_gates.yaml')）。
    """

    def __init__(self, yaml_path: str | Path, *, schema_validator=None):
        self.yaml_path = Path(yaml_path)
        self._schema_validator = schema_validator

    def load(self) -> dict:
        try:
            data = yaml.safe_load(self.yaml_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def patch(self, partial: dict) -> dict:
        merged = _deep_merge(self.load(), partial)
        self.yaml_path.write_text(
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return merged

    def resolve_agent(self, node_key: str) -> dict | None:
        cfg = self.load()
        gate_cfg = cfg.get("gates", {}).get(node_key, {})
        agent_name = gate_cfg.get("agent")
        if not agent_name:
            return None
        try:
            from agents.identity_schema import load_identity, resolve_llm_chain
            identity = load_identity(agent_name)
            llm_chain = resolve_llm_chain(identity.llm_feature_key if identity else None)
            return {"name": agent_name, "llm_feature_key": getattr(identity, "llm_feature_key", None), **llm_chain}
        except Exception:
            return {"name": agent_name}

    def get_runtime_metrics(
        self,
        node_key: str,
        *,
        hours: int = 24,
        stats_fn: Callable,
    ) -> dict:
        try:
            stats = stats_fn(hours=hours)
            by_gate = stats.get("by_gate", {})
            gate_stats = by_gate.get(node_key)
            if not gate_stats:
                return {}
            total = gate_stats.get("passed", 0) + gate_stats.get("blocked", 0)
            pass_rate = round(gate_stats["passed"] / total, 4) if total > 0 else None
            return {**gate_stats, "pass_rate": pass_rate}
        except Exception:
            return {}

    def list_nodes(self) -> list[str]:
        return list(self.load().get("gates", {}).keys())

    def validate(self) -> list[str]:
        if self._schema_validator is None:
            return []
        try:
            result = self._schema_validator(self.load())
            if isinstance(result, list):
                return [str(w) for w in result]
            return []
        except Exception as e:
            return [str(e)]
