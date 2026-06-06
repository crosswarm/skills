"""透明置信度计算 — 多源证据加权 + 无证据 cap + 完整 breakdown"""
from __future__ import annotations

EVIDENCE_WEIGHTS = {
    "similar_ticket": 0.40,
    "kb_hit": 0.30,
    "supervisor": 0.20,
    "ai_raw": 0.10,
}
NO_EVIDENCE_CAP = 0.50
EVIDENCE_THRESHOLD = 0.60


def calculate_grounded_confidence(
    *,
    similar_issues_scored: list[dict] | None,
    kb_evidence: list[dict] | None,
    supervisor_score: float | None,
    ai_raw_confidence: float | None,
) -> dict:
    """
    Returns {score, raw_score, evidence_status, breakdown, formula}.

    similar_issues_scored: list of {key, score, summary}
    kb_evidence: list of {title, score, category}
    supervisor_score: Gate 5 supervisor score (0-1), None if not available
    ai_raw_confidence: LLM self-rating from batch classify (0-1)
    """
    def _norm(v: float) -> float:
        return min(1.0, max(0.0, v / 100.0 if v > 1.0 else v))

    sim_top = max((_norm(float(s.get("score", 0))) for s in (similar_issues_scored or [])), default=0.0)
    kb_top  = max((_norm(float(k.get("score", 0))) for k in (kb_evidence or [])), default=0.0)
    sup     = supervisor_score if supervisor_score is not None else (ai_raw_confidence or 0.5)
    ai_raw  = ai_raw_confidence if ai_raw_confidence is not None else 0.5

    contributions = {
        "similar_ticket": sim_top * EVIDENCE_WEIGHTS["similar_ticket"],
        "kb_hit":         kb_top  * EVIDENCE_WEIGHTS["kb_hit"],
        "supervisor":     sup     * EVIDENCE_WEIGHTS["supervisor"],
        "ai_raw":         ai_raw  * EVIDENCE_WEIGHTS["ai_raw"],
    }
    raw_score = sum(contributions.values())

    if sim_top >= EVIDENCE_THRESHOLD and kb_top >= EVIDENCE_THRESHOLD:
        evidence_status = "both"
    elif sim_top >= EVIDENCE_THRESHOLD:
        evidence_status = "ticket_only"
    elif kb_top >= EVIDENCE_THRESHOLD:
        evidence_status = "kb_only"
    else:
        evidence_status = "no_evidence"

    score = min(raw_score, NO_EVIDENCE_CAP) if evidence_status == "no_evidence" else raw_score
    score = max(0.0, min(1.0, score))

    formula = (
        f"{sim_top:.2f}×0.40 + {kb_top:.2f}×0.30 + "
        f"{sup:.2f}×0.20 + {ai_raw:.2f}×0.10 = {raw_score:.3f}"
    )
    if evidence_status == "no_evidence":
        formula += f" → cap {NO_EVIDENCE_CAP}"

    return {
        "score": round(score, 4),
        "raw_score": round(raw_score, 4),
        "evidence_status": evidence_status,
        "breakdown": {
            "similar_ticket": _detail("similar_ticket", sim_top, similar_issues_scored),
            "kb_hit":         _detail("kb_hit", kb_top, kb_evidence),
            "supervisor": {
                "weight": EVIDENCE_WEIGHTS["supervisor"],
                "value": round(sup, 4),
                "contribution": round(contributions["supervisor"], 4),
                "source": "gate5" if supervisor_score is not None else "ai_raw_fallback",
            },
            "ai_raw": {
                "weight": EVIDENCE_WEIGHTS["ai_raw"],
                "value": round(ai_raw, 4),
                "contribution": round(contributions["ai_raw"], 4),
                "source": "llm_batch_classify",
            },
        },
        "formula": formula,
    }


def _detail(kind: str, top_score: float, items: list[dict] | None) -> dict:
    weight = EVIDENCE_WEIGHTS[kind]
    result: dict = {
        "weight": weight,
        "value": round(top_score, 4),
        "contribution": round(top_score * weight, 4),
        "top_match": None,
    }
    if items:
        best = max(items, key=lambda x: float(x.get("score", 0)))
        if kind == "similar_ticket":
            result["top_match"] = {
                "key": best.get("key", ""),
                "score": round(float(best.get("score", 0)), 4),
                "summary": (best.get("summary", "") or "")[:80],
            }
        else:
            result["top_match"] = {
                "title": best.get("title", ""),
                "score": round(float(best.get("score", 0)), 4),
                "category": best.get("category", ""),
            }
    return result
