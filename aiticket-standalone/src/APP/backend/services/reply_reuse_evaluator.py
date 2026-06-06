"""Gate 3 历史复用评估：对 reply_examples 候选计算复合分，返回最佳候选及复用层级。"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent

# 权重（与 config/reply_gates.yaml score_weights 对应）
_DEFAULT_WEIGHTS = {
    "similarity": 0.475,
    "adoption_signal": 0.375,
    "recency": 0.0,
    "version_match": 0.15,
}


@dataclass
class ReuseCandidate:
    example: dict
    composite_score: float
    tier: str   # "direct" | "llm_blend" | "skip"
    score_breakdown: dict


def evaluate_reuse(
    reply_examples: list[dict],
    current_product_version: str = "",
    current_module: str = "",
) -> Optional[ReuseCandidate]:
    """
    评估 reply_examples 候选，返回最佳候选 (ReuseCandidate) 或 None。
    None 表示：无候选、门关闭、或最高分 < skip 阈值。
    """
    if not reply_examples:
        return None

    cfg = _load_gate_config()
    if not cfg.get("enabled", False):
        return None

    weights = {**_DEFAULT_WEIGHTS, **cfg.get("score_weights", {})}
    composite_threshold = float(cfg.get("composite_threshold", 0.85))
    llm_blend_min = float(cfg.get("llm_blend_min", 0.60))

    best: Optional[ReuseCandidate] = None
    for ex in reply_examples:
        score_bd = _score_example(ex, current_product_version, current_module, weights)
        composite = score_bd["composite"]
        if best is None or composite > best.composite_score:
            if composite >= llm_blend_min:  # 低于 llm_blend_min 的不做任何复用
                best = ReuseCandidate(
                    example=ex,
                    composite_score=round(composite, 4),
                    tier=_assign_tier(composite, ex, composite_threshold, llm_blend_min),
                    score_breakdown=score_bd,
                )

    return best


def _assign_tier(
    composite: float,
    example: dict,
    composite_threshold: float,
    llm_blend_min: float,
) -> str:
    if composite >= composite_threshold and example.get("adopted"):
        return "direct"
    if composite >= llm_blend_min:
        return "llm_blend"
    return "skip"


def _score_example(
    ex: dict,
    current_version: str,
    current_module: str,
    weights: dict,
) -> dict:
    # 1. 相似度：优先用 sim_score（归一化的 [0,1] 值），fallback 到 score（兼容旧数据）
    # reply_trainer 返回的 score 已乘以排序权重（最高 ≈2.1），sim_score 是原始值
    sim = min(max(float(ex.get("sim_score", ex.get("score", 0.0))), 0.0), 1.0)

    # 2. 采纳信号
    if ex.get("adopted"):
        adoption = 1.0
    elif ex.get("is_modified"):
        adoption = 0.6
    else:
        adoption = 0.0

    # 3. 近期性（无时间字段，根据 issue_key 数字部分推断）
    recency = _score_recency(ex.get("issue_key", ""))

    # 4. 版本匹配
    version_match = _score_version_match(ex.get("reply", ""), current_version)

    composite = (
        sim * weights.get("similarity", 0.40)
        + adoption * weights.get("adoption_signal", 0.30)
        + recency * weights.get("recency", 0.15)
        + version_match * weights.get("version_match", 0.15)
    )

    return {
        "composite": composite,
        "similarity": round(sim, 4),
        "adoption_signal": round(adoption, 4),
        "recency": round(recency, 4),
        "version_match": round(version_match, 4),
    }


def _score_recency(issue_key: str) -> float:
    """用 issue_key 尾部数字估算近期性（数字越大越新）。无法解析 → 0.5。"""
    m = re.search(r'(\d+)$', issue_key)
    if not m:
        return 0.5
    num = int(m.group(1))
    # 假设当前最大编号约 50000；数字越大近期性越高，最低 0.3
    recency = min(num / 50000.0, 1.0) * 0.7 + 0.3
    return round(min(recency, 1.0), 4)


def _score_version_match(reply_text: str, current_version: str) -> float:
    """
    检测回复中是否提及版本号，并与当前工单版本比较。
    - 当前版本未知 → 0.5（中性）
    - 回复无版本提及 → 0.7（较安全）
    - 版本提及且主版本匹配 → 1.0
    - 版本提及但不匹配 → 0.2
    """
    if not current_version:
        return 0.5
    version_mentions = re.findall(r'\d+\.\d+[\.\d]*', reply_text)
    if not version_mentions:
        return 0.7
    # 比较主版本（前两段）
    current_major = ".".join(current_version.split(".")[:2])
    for v in version_mentions:
        major = ".".join(v.split(".")[:2])
        if major == current_major:
            return 1.0
    return 0.2


def _load_gate_config() -> dict:
    try:
        path = _PROJECT_ROOT / "config" / "reply_gates.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return raw.get("gates", {}).get("reuse", {})
    except Exception:
        return {}
