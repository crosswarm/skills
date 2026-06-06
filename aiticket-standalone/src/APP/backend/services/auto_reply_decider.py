"""多维加权 Auto-Reply 决策：supervisor_score × product_priority × customer_importance_inverse。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).parent.parent

_PRODUCT_PRIORITY = {"yonsuite": 1.0, "standard": 0.7, "custom": 0.4}
_DEFAULT_THRESHOLDS = {
    "yonsuite": {"normal": 0.65, "key_customer": 0.80},
    "standard": {"normal": 0.72, "key_customer": 0.85},
    "custom":   {"normal": 0.80, "key_customer": None},  # None = 不允许
}
_DEFAULT_WEIGHTS = {
    "supervisor_confidence": 0.50,
    "product_priority": 0.20,
    "customer_importance_inverse": 0.30,
}


@dataclass
class AutoReplyDecision:
    auto_reply: bool
    composite_score: float | None
    threshold: float | None   # None = 不允许 auto-reply
    action: str  # "auto_reply" | "auto_reply_low_risk" | "pending_batch_approve" | "manual_with_steps" | "manual_review" | "human_required"
    product_type: str
    is_key_customer: bool
    blocked_by: list = None  # hard gates that blocked auto_reply, for observability
    reply_gateway_version: str = ""

    def __post_init__(self):
        if self.blocked_by is None:
            self.blocked_by = []


def decide(
    supervisor_score: Optional[float],
    product_type: str = "standard",
    is_key_customer: bool = False,
    *,
    reuse_matched: bool = False,
    risk_flags: list = None,
    specificity_level: str = None,
    reply_gateway: dict = None,  # NEW: v2 gateway result
) -> AutoReplyDecision:
    """
    综合 supervisor_score、产品优先级、客户重要度，决定是否自动回复。
    """
    gateway_version = ""
    if reply_gateway:
        gateway_version = reply_gateway.get("version", "v2")
        gates = reply_gateway.get("gates", {})
        # blocked_by: any gate with verdict=fail
        blocked = [
            name for name, g in gates.items()
            if isinstance(g, dict) and g.get("verdict") == "fail"
        ]
        # override supervisor_score from G5 if not provided
        if supervisor_score is None:
            supervisor_score = gates.get("G5_supervisor", {}).get("score")
        # override risk_flags from G5
        if not risk_flags:
            risk_flags = gates.get("G5_supervisor", {}).get("risk_flags", [])
        # override specificity_level from G4
        if not specificity_level:
            specificity_level = gates.get("G4_specificity", {}).get("level")
        # override reuse_matched from G3
        if not reuse_matched:
            g3 = gates.get("G3_reuse", {})
            reuse_matched = g3.get("composite_score", 0) >= 0.85
        # G2 fail -> force needs_decision immediately
        g2 = gates.get("G2_classification", {})
        if g2.get("verdict") == "fail":
            return AutoReplyDecision(
                auto_reply=False,
                composite_score=None,
                threshold=None,
                action="needs_decision",
                product_type=product_type,
                is_key_customer=is_key_customer,
                blocked_by=blocked,
                reply_gateway_version=gateway_version,
            )
    else:
        blocked = []

    if supervisor_score is None:
        return AutoReplyDecision(
            auto_reply=False,
            composite_score=None,
            threshold=None,
            action="needs_decision",
            product_type=product_type,
            is_key_customer=is_key_customer,
        )
    cfg = _load_config()
    if not cfg.get("enabled", False):
        return AutoReplyDecision(
            auto_reply=False,
            composite_score=supervisor_score,
            threshold=None,
            action="manual_review",
            product_type=product_type,
            is_key_customer=is_key_customer,
        )

    weights = {**_DEFAULT_WEIGHTS, **cfg.get("weights", {})}
    thresholds_cfg = cfg.get("thresholds", _DEFAULT_THRESHOLDS)

    product_priority_factor = _PRODUCT_PRIORITY.get(product_type, 0.7)
    customer_importance_factor = 1.0 if is_key_customer else 0.0

    composite = (
        supervisor_score * weights["supervisor_confidence"]
        + product_priority_factor * weights["product_priority"]
        + (1.0 - customer_importance_factor) * weights["customer_importance_inverse"]
    )
    composite = round(min(composite, 1.0), 4)

    # 取该 product_type 的阈值
    pt_thresholds = thresholds_cfg.get(product_type, _DEFAULT_THRESHOLDS.get(product_type, {}))
    if not pt_thresholds:
        pt_thresholds = _DEFAULT_THRESHOLDS.get("standard", {})

    threshold_key = "key_customer" if is_key_customer else "normal"
    threshold = pt_thresholds.get(threshold_key)

    if threshold is None:
        # 明确禁止 auto-reply（如重点客户×客开）
        return AutoReplyDecision(
            auto_reply=False,
            composite_score=composite,
            threshold=None,
            action="human_required",
            product_type=product_type,
            is_key_customer=is_key_customer,
        )

    auto_reply = composite >= threshold
    action = ""

    # VIP 专有路径：极高 supervisor + 无风险 + 历史复用 可绕过 composite 阈值直接自动回复
    if is_key_customer and not auto_reply:
        lr = cfg.get("low_risk_auto_threshold", {})
        flags = risk_flags or []
        if (supervisor_score >= lr.get("supervisor_score", 0.95)
                and (not lr.get("require_no_risk_flags", True) or len(flags) == 0)
                and (not lr.get("require_reuse_match", True) or reuse_matched)):
            auto_reply = True
            action = "auto_reply_low_risk"

    if not action:
        if not auto_reply:
            action = "manual_with_steps" if composite >= threshold * 0.85 else "manual_review"
        elif not is_key_customer and cfg.get("require_human_approval_for_normal_customer", True):
            # 非重点客户高置信度 → staging 待批准，不直接发送
            action = "pending_batch_approve"
            auto_reply = False
        else:
            action = "auto_reply"

    # 硬门：非 VIP low-risk 旁路的 auto_reply 必须同时满足所有条件
    blocked_by: list = list(blocked)  # start with any reply_gateway gate failures
    if action == "auto_reply":
        hg = cfg.get("hard_gates", {})
        sup_min = hg.get("supervisor_score_min", 0.85)
        if supervisor_score < sup_min:
            blocked_by.append(f"supervisor_score={supervisor_score:.2f}<{sup_min}")
        if hg.get("require_no_risk_flags", True) and (risk_flags or []):
            blocked_by.append(f"risk_flags={risk_flags}")
        if hg.get("require_reuse_match", True) and not reuse_matched:
            blocked_by.append("reuse_skipped")
        if hg.get("require_specificity_high", True) and specificity_level != "high":
            blocked_by.append(f"specificity={specificity_level}")
        if blocked_by:
            action = "pending_batch_approve"
            auto_reply = False

    return AutoReplyDecision(
        auto_reply=auto_reply,
        composite_score=composite,
        threshold=float(threshold),
        action=action,
        product_type=product_type,
        is_key_customer=is_key_customer,
        blocked_by=blocked_by,
        reply_gateway_version=gateway_version,
    )


def _load_config() -> dict:
    try:
        raw = yaml.safe_load((_PROJECT_ROOT / "config" / "reply_gates.yaml").read_text(encoding="utf-8"))
        return raw.get("auto_reply_decision", {})
    except Exception:
        return {}
