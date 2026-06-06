"""Unit tests for confidence_calculator — 5 original + 3 MS-2 unit-mix cases."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.confidence_calculator import calculate_grounded_confidence, NO_EVIDENCE_CAP


def test_both_evidence():
    result = calculate_grounded_confidence(
        similar_issues_scored=[{"key": "T-1", "score": 0.85, "summary": "similar"}],
        kb_evidence=[{"title": "KB article", "score": 0.78, "category": "ops"}],
        supervisor_score=0.80,
        ai_raw_confidence=0.62,
    )
    assert result["evidence_status"] == "both"
    assert result["score"] > NO_EVIDENCE_CAP
    assert result["breakdown"]["similar_ticket"]["top_match"]["key"] == "T-1"
    assert result["breakdown"]["kb_hit"]["top_match"]["title"] == "KB article"
    assert "0.85×0.40" in result["formula"]


def test_ticket_only():
    result = calculate_grounded_confidence(
        similar_issues_scored=[{"key": "T-2", "score": 0.75, "summary": "x"}],
        kb_evidence=[{"title": "weak", "score": 0.30, "category": "misc"}],
        supervisor_score=None,
        ai_raw_confidence=0.70,
    )
    assert result["evidence_status"] == "ticket_only"
    assert result["score"] > NO_EVIDENCE_CAP


def test_kb_only():
    result = calculate_grounded_confidence(
        similar_issues_scored=[{"key": "T-3", "score": 0.20, "summary": "weak"}],
        kb_evidence=[{"title": "Strong KB", "score": 0.90, "category": "guide"}],
        supervisor_score=0.75,
        ai_raw_confidence=0.60,
    )
    assert result["evidence_status"] == "kb_only"
    assert result["score"] > NO_EVIDENCE_CAP


def test_no_evidence_caps_at_50():
    result = calculate_grounded_confidence(
        similar_issues_scored=[],
        kb_evidence=[],
        supervisor_score=None,
        ai_raw_confidence=0.95,
    )
    assert result["evidence_status"] == "no_evidence"
    assert result["score"] <= NO_EVIDENCE_CAP
    assert "cap" in result["formula"]


def test_all_none_defaults():
    result = calculate_grounded_confidence(
        similar_issues_scored=None,
        kb_evidence=None,
        supervisor_score=None,
        ai_raw_confidence=None,
    )
    assert result["evidence_status"] == "no_evidence"
    assert result["score"] <= NO_EVIDENCE_CAP
    # Both sup and ai_raw default to 0.5
    assert result["breakdown"]["supervisor"]["value"] == 0.5
    assert result["breakdown"]["ai_raw"]["value"] == 0.5


# ── MS-2: 单位混乱兼容测试 ──────────────────────────────────────────────────

def test_legacy_100_scale_sim_score_does_not_saturate():
    """老缓存里 similar_issues_scored 可能是 0-100 整数，_norm 应将其 /100 而不是溢出。"""
    result = calculate_grounded_confidence(
        similar_issues_scored=[{"key": "T-OLD", "score": 86, "summary": "legacy"}],
        kb_evidence=[{"title": "KB", "score": 0.20, "category": "ops"}],
        supervisor_score=0.86,
        ai_raw_confidence=0.86,
    )
    # _norm(86) → 86/100 = 0.86，加权后 score 应 < 1.0
    assert result["score"] < 1.0, f"score saturated: {result['score']}"
    # sim_top 经 _norm 后应被识别为 >= 0.6，evidence_status 包含 ticket
    assert result["evidence_status"] in ("both", "ticket_only")


def test_kb_large_raw_score_normalized():
    """KB 累加分 21.6 经 _norm(/100) → 0.216，不应被 clamp 为 1.0。"""
    result = calculate_grounded_confidence(
        similar_issues_scored=[{"key": "T-X", "score": 0.86, "summary": "x"}],
        kb_evidence=[{"title": "KB large", "score": 21.6, "category": "guide"}],
        supervisor_score=0.86,
        ai_raw_confidence=0.86,
    )
    kb_val = result["breakdown"]["kb_hit"]["value"]
    assert 0.20 <= kb_val <= 0.25, f"kb_top expected ~0.216 after _norm, got {kb_val}"
    assert result["score"] < 1.0, f"score saturated: {result['score']}"


def test_mixed_units_all_large_score_still_sane():
    """sim=86(0-100), kb=21.6(累加), sup=0.86, ai=0.86 — 即原始 MYPROJECT-62893 场景。
    修后: score 应在 [0.6, 0.75] 范围内，不再是 1.0。"""
    result = calculate_grounded_confidence(
        similar_issues_scored=[{"key": "MYPROJECT-62893", "score": 86, "summary": "分支"}],
        kb_evidence=[{"title": "KB", "score": 21.6, "category": "dsp"}],
        supervisor_score=0.86,
        ai_raw_confidence=0.86,
    )
    assert 0.60 <= result["score"] <= 0.80, (
        f"Expected score in [0.60, 0.80], got {result['score']}. "
        f"formula: {result['formula']}"
    )
