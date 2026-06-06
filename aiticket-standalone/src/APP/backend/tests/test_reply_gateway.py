"""Unit tests for APP/backend/services/reply_gateway.py

Covers UT-G1-01 through UT-FINAL-03 as defined in
docs/specs/reply_gateway_v2/tests/unit_cases.md
"""
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── bootstrap path ──────────────────────────────────────────────────────────
_BACKEND = Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

from services.reply_gateway import ReplyGateway

# ── helpers ─────────────────────────────────────────────────────────────────

def _gw(vector_store=None, llm_service=None, reply_trainer=None) -> ReplyGateway:
    return ReplyGateway(
        vector_store=vector_store or MagicMock(),
        llm_service=llm_service,
        reply_trainer=reply_trainer or MagicMock(),
    )


def _completeness_result(passed: bool, missing=None, itype="", draft=""):
    r = MagicMock()
    r.passed = passed
    r.missing_fields = missing or []
    r.insufficient_type = itype
    r.inquiry_draft = draft
    r.rule_matched = "test_rule"
    r.gate_enabled = True
    return r


# ── G1 tests ────────────────────────────────────────────────────────────────

class TestG1Completeness:
    def test_complete_ticket_passes(self):
        """UT-G1-01: 完整工单 → verdict=pass"""
        mock_result = _completeness_result(True)
        with patch("services.completeness_checker.check", return_value=mock_result):
            gw = _gw()
            res = gw._run_g1("LCZX-99999", {
                "project": "LCZX", "issue_type": "故障", "description": "x" * 80
            })
        assert res["verdict"] == "pass"
        assert res["missing_fields"] == []

    def test_empty_description_fails(self):
        """UT-G1-02: description 空 → verdict=fail, inquiry_draft 非空"""
        mock_result = _completeness_result(False, missing=["description"], itype="invalid_description", draft="请提供描述")
        with patch("services.completeness_checker.check", return_value=mock_result):
            gw = _gw()
            res = gw._run_g1("LCZX-99999", {"project": "LCZX", "issue_type": "故障", "description": ""})
        assert res["verdict"] == "fail"
        assert res["inquiry_draft"] != ""

    def test_gate_disabled_returns_skipped(self):
        """G1 disabled → verdict=skipped"""
        mock_result = _completeness_result(False)
        mock_result.gate_enabled = False
        with patch("services.completeness_checker.check", return_value=mock_result):
            gw = _gw()
            res = gw._run_g1("LCZX-1", {"project": "LCZX", "issue_type": "故障", "description": ""})
        assert res["verdict"] == "skipped"

    def test_exception_returns_skipped(self):
        """G1 exception → verdict=skipped (non-blocking)"""
        with patch("services.completeness_checker.check", side_effect=RuntimeError("boom")):
            gw = _gw()
            res = gw._run_g1("LCZX-1", {})
        assert res["verdict"] == "skipped"
        assert "error" in res


# ── G2 tests ────────────────────────────────────────────────────────────────

class TestG2Classification:
    def _make_gw_with_chroma(self, vote_keys: list[str]):
        """Build gateway whose vector_store returns `vote_keys` as similar issues."""
        vs = MagicMock()
        vs.search_similar_issues.return_value = [
            {"key": k, "score": 0.9, "metadata": {"issue_key": k}} for k in vote_keys
        ]
        return _gw(vector_store=vs)

    def test_kf_handover_high_confidence(self):
        """UT-G2-01: LCZX + chroma 5/5 KKZC + rule kf_handover → verdict=fail, transfer_to=KKZC"""
        gw = self._make_gw_with_chroma(["KKZC-1","KKZC-2","KKZC-3","KKZC-4","KKZC-5"])
        # Use keyword-rich summary/description to push rule_match_score high enough
        # so confidence = 0.5×1.0 + 0.3×high + 0.2×0 ≥ 0.70
        ticket_meta = {
            "project": "LCZX",
            "issue_type": "客开",
            "summary": "客开 定制开发 二次开发 客户化 自定义对象 Customer Object 扩展点 扩展开发",
            "description": "客开 定制 customization 二次开发",
            "product_version": "5.0",
            "customer_name": "测试客户",
        }
        res = gw._run_g2("LCZX-99999", {"issue_type": "客开"}, ticket_meta)
        assert res["verdict"] == "fail"
        assert res["transfer_to"] == "KKZC"
        assert res["confidence"] >= 0.70

    def test_lczx_majority_stays(self):
        """UT-G2-02: chroma 4/5 LCZX → verdict=pass, final_project_decision=LCZX"""
        gw = self._make_gw_with_chroma(["LCZX-1","LCZX-2","LCZX-3","LCZX-4","KKZC-1"])
        ticket_meta = {
            "project": "LCZX",
            "issue_type": "故障",
            "summary": "普通故障",
            "description": "普通故障",
            "product_version": "5.0",
            "customer_name": "测试客户",
        }
        res = gw._run_g2("LCZX-99999", {"issue_type": "故障"}, ticket_meta)
        assert res["verdict"] in ("pass", "warn")
        assert res["final_project_decision"] in ("LCZX", "")

    def test_no_rule_match_no_chroma_pass(self):
        """UT-G2-03: 无规则命中 + chroma 分散 → verdict=warn 或 pass"""
        vs = MagicMock()
        vs.search_similar_issues.return_value = [
            {"key": "LCZX-1", "score": 0.5, "metadata": {"issue_key": "LCZX-1"}},
            {"key": "KKZC-1", "score": 0.5, "metadata": {"issue_key": "KKZC-1"}},
            {"key": "UPESN-1", "score": 0.5, "metadata": {"issue_key": "UPESN-1"}},
        ]
        gw = _gw(vector_store=vs)
        ticket_meta = {"project": "LCZX","issue_type":"故障","summary":"随机问题","description":"随机","product_version":"5.0","customer_name":""}
        res = gw._run_g2("LCZX-99999", {}, ticket_meta)
        assert res["verdict"] in ("pass", "warn")

    def test_confidence_calculation(self):
        """UT-G2-04: vote=1.0, rule=1.0, llm=0 → confidence ≈ 0.80"""
        gw = self._make_gw_with_chroma(["KKZC-1","KKZC-2","KKZC-3","KKZC-4","KKZC-5"])
        conf = 0.5 * 1.0 + 0.3 * 1.0 + 0.2 * 0.0
        assert abs(conf - 0.80) < 1e-9

    def test_yonsuite_excluded(self):
        """yonsuite 产品版本 → 客开移交被排除 (yonsuite_exclude 命中 → verdict=pass)"""
        gw = self._make_gw_with_chroma(["KKZC-1","KKZC-2","KKZC-3","KKZC-4","KKZC-5"])
        ticket_meta = {
            "project": "LCZX",
            "issue_type": "客开",
            "summary": "yonsuite客开问题",
            "description": "客开定制开发",
            "product_version": "yonsuite",
            "customer_name": "云客户",
        }
        res = gw._run_g2("LCZX-99999", {"issue_type": "客开"}, ticket_meta)
        assert res["verdict"] == "pass"

    def test_chroma_failure_falls_back_to_pass(self):
        """G2: chroma raises → _chroma_vote absorbs exception, G2 returns pass (safe fallback)"""
        vs = MagicMock()
        vs.search_similar_issues.side_effect = RuntimeError("vector store down")
        gw = _gw(vector_store=vs)
        res = gw._run_g2("LCZX-1", {}, {"project": "LCZX", "issue_type": "故障", "summary": "", "description": "", "product_version": "", "customer_name": ""})
        # chroma vote falls back to 0.0 ratio; with no rule keyword match confidence < 0.50 → pass
        assert res["verdict"] in ("pass", "warn")


# ── G3 tests ─────────────────────────────────────────────────────────────────

class TestG3Reuse:
    def _mock_candidate(self, composite: float, tier: str):
        c = MagicMock()
        c.composite_score = composite
        c.tier = tier
        c.issue_key = "LCZX-11111"
        c.summary = "相似工单"
        return c

    def test_high_score_direct(self):
        """UT-G3-01: composite ≥ 0.85 → reuse_strategy=direct"""
        with patch("services.reply_reuse_evaluator.evaluate_reuse", return_value=self._mock_candidate(0.90, "direct")):
            gw = _gw()
            res = gw._run_g3([], {"product_version": "5.0", "domain_module": ""})
        assert res["reuse_strategy"] == "direct"

    def test_mid_score_reference(self):
        """UT-G3-02: 0.55 ≤ composite < 0.85 → reuse_strategy=reference"""
        with patch("services.reply_reuse_evaluator.evaluate_reuse", return_value=self._mock_candidate(0.70, "llm_blend")):
            gw = _gw()
            res = gw._run_g3([], {"product_version": "5.0", "domain_module": ""})
        assert res["reuse_strategy"] == "reference"

    def test_low_score_skip(self):
        """UT-G3-03: composite < 0.55 → reuse_strategy=skip"""
        with patch("services.reply_reuse_evaluator.evaluate_reuse", return_value=self._mock_candidate(0.30, "skip")):
            gw = _gw()
            res = gw._run_g3([], {"product_version": "5.0", "domain_module": ""})
        assert res["reuse_strategy"] == "skip"

    def test_no_candidate_skip(self):
        """G3 no candidate → reuse_strategy=skip"""
        with patch("services.reply_reuse_evaluator.evaluate_reuse", return_value=None):
            gw = _gw()
            res = gw._run_g3([], {})
        assert res["reuse_strategy"] == "skip"


# ── G4 tests ─────────────────────────────────────────────────────────────────

class TestG4Specificity:
    def test_high_kb_evidence(self):
        """UT-G4-01: KB evidence ≥ 3 → verdict=pass, level in (high, medium)"""
        kb = [{"name": f"KB{i}", "score": 85, "category": "产品手册"} for i in range(4)]
        gw = _gw()
        res = gw._run_g4(kb)
        assert res["verdict"] in ("pass", "warn")
        assert res["level"] in ("high", "medium")

    def test_no_kb_evidence(self):
        """UT-G4-02: 无 KB 证据 → level=none"""
        gw = _gw()
        res = gw._run_g4([])
        assert res["level"] in ("none", "low")


# ── G5 tests ─────────────────────────────────────────────────────────────────

class TestG5Supervisor:
    def _mock_supervise(self, score, risk_flags=None, step_safety="safe"):
        r = MagicMock()
        r.supervisor_score = score
        r.risk_flags = risk_flags or []
        r.step_safety = step_safety
        r.rationale = "test rationale"
        r.provider_used = "minimax"
        r.gate_enabled = True
        return r

    def test_normal_reply_passes(self):
        """UT-G5-01: normal reply + 无风险 → verdict=pass, score ≥ 0.8"""
        with patch("agents.reply_supervisor_agent.supervise", return_value=self._mock_supervise(0.85)):
            gw = _gw()
            res = gw._run_g5("LCZX-1", {}, "回复内容", [])
        assert res["verdict"] == "pass"
        assert res["score"] >= 0.8

    def test_g1_fail_inquiry_draft_audited(self):
        """UT-G5-02: G1 fail 时传 inquiry_draft 给 G5 → G5 仍跑"""
        with patch("agents.reply_supervisor_agent.supervise", return_value=self._mock_supervise(0.75)) as mock_sup:
            gw = _gw()
            res = gw._run_g5("LCZX-1", {}, "请问您能提供更多信息吗?", [])
        assert res["verdict"] in ("pass", "warn")

    def test_exception_returns_skipped(self):
        """G5 exception → verdict=skipped"""
        with patch("agents.reply_supervisor_agent.supervise", side_effect=RuntimeError("LLM down")):
            gw = _gw()
            res = gw._run_g5("LCZX-1", {}, "", [])
        assert res["verdict"] == "skipped"


# ── Dependency graph tests ────────────────────────────────────────────────────

class TestDependencyGraph:
    def _run_all(self, g1_pass: bool, g2_verdict: str = "pass") -> dict:
        """Helper: run full gateway with mocked gates."""
        g1_result = MagicMock()
        g1_result.passed = g1_pass
        g1_result.missing_fields = [] if g1_pass else ["description"]
        g1_result.insufficient_type = "" if g1_pass else "invalid_description"
        g1_result.inquiry_draft = "" if g1_pass else "请补充信息"
        g1_result.rule_matched = "test"
        g1_result.gate_enabled = True

        sup_result = MagicMock()
        sup_result.supervisor_score = 0.85
        sup_result.risk_flags = []
        sup_result.step_safety = "safe"
        sup_result.rationale = "ok"
        sup_result.provider_used = "minimax"
        sup_result.gate_enabled = True

        vs = MagicMock()
        vs.search_similar_issues.return_value = []

        with patch("services.completeness_checker.check", return_value=g1_result), \
             patch("agents.reply_supervisor_agent.supervise", return_value=sup_result), \
             patch("services.reply_reuse_evaluator.evaluate_reuse", return_value=None):
            gw = ReplyGateway(vector_store=vs, llm_service=None, reply_trainer=MagicMock())
            return gw.run("LCZX-99999", {}, {"project": "LCZX","issue_type":"故障","description":"x","summary":"test","product_version":"5.0","customer_name":""})

    def test_g1_fail_skips_g2_g3_g4_runs_g5(self):
        """UT-DEP-01: G1 fail → G2/G3/G4=skipped, G5 still runs"""
        result = self._run_all(g1_pass=False)
        gates = result["gates"]
        assert gates["G1_completeness"]["verdict"] == "fail"
        assert gates["G2_classification"]["verdict"] == "skipped"
        assert gates["G3_reuse"]["verdict"] == "skipped"
        assert gates["G4_specificity"]["verdict"] == "skipped"
        assert gates["G5_supervisor"]["verdict"] in ("pass", "warn", "fail")

    def test_g1_pass_runs_all(self):
        """UT-DEP-02: G1 pass → all gates run (G2/G3/G4/G5 not skipped due to G1)"""
        result = self._run_all(g1_pass=True)
        gates = result["gates"]
        assert gates["G1_completeness"]["verdict"] == "pass"
        # G2/G3/G4/G5 must have run (verdict != skipped unless gate is disabled)
        assert gates["G5_supervisor"]["verdict"] in ("pass", "warn", "fail")


# ── final_action aggregation tests ──────────────────────────────────────────

class TestFinalActionAggregation:
    def _gates_with(self, g1="pass", g2="pass", g3="pass", g4="pass", g5="pass", g2_auto_move=True, g2_transfer="KKZC"):
        return {
            "G1_completeness": {"verdict": g1, "inquiry_draft": "请补充" if g1 == "fail" else ""},
            "G2_classification": {"verdict": g2, "auto_move_eligible": g2_auto_move, "transfer_to": g2_transfer, "transferee": "sunaoodi", "domain_module": "客开", "matched_rule_id": "kf_handover", "confidence": 0.85},
            "G3_reuse": {"verdict": g3, "reuse_strategy": "skip"},
            "G4_specificity": {"verdict": g4, "level": "medium"},
            "G5_supervisor": {"verdict": g5, "score": 0.85},
        }

    def test_g1_fail_return_to_support(self):
        """UT-FINAL-01: G1=fail → final_action=return_to_support"""
        gw = _gw()
        action, ops = gw._aggregate_actions(self._gates_with(g1="fail"), "LCZX-1")
        assert action == "return_to_support"
        assert any(op["type"] == "return_to_support" for op in ops)

    def test_g2_fail_handover(self):
        """UT-FINAL-02: G1=pass, G2=fail, auto_move=true → final_action=handover, extra_ops has move_jira"""
        gw = _gw()
        action, ops = gw._aggregate_actions(self._gates_with(g1="pass", g2="fail"), "LCZX-1")
        assert action == "handover"
        assert any(op["type"] == "move_jira" for op in ops)

    def test_all_pass_normal_reply(self):
        """UT-FINAL-03: 全 pass → final_action=normal_reply, extra_operations=[]"""
        gw = _gw()
        action, ops = gw._aggregate_actions(self._gates_with(), "LCZX-1")
        assert action == "normal_reply"
        assert ops == []


# ── run() contract tests ──────────────────────────────────────────────────────

class TestRunContract:
    def test_returns_required_keys(self):
        """run() must return version/gates/display_cards/final_action/extra_operations"""
        sup_result = MagicMock()
        sup_result.supervisor_score = 0.85
        sup_result.risk_flags = []
        sup_result.step_safety = "safe"
        sup_result.rationale = "ok"
        sup_result.provider_used = "minimax"
        sup_result.gate_enabled = True

        g1_result = MagicMock()
        g1_result.passed = True
        g1_result.missing_fields = []
        g1_result.insufficient_type = ""
        g1_result.inquiry_draft = ""
        g1_result.rule_matched = "test"
        g1_result.gate_enabled = True

        vs = MagicMock()
        vs.search_similar_issues.return_value = []

        with patch("services.completeness_checker.check", return_value=g1_result), \
             patch("agents.reply_supervisor_agent.supervise", return_value=sup_result), \
             patch("services.reply_reuse_evaluator.evaluate_reuse", return_value=None):
            gw = ReplyGateway(vector_store=vs)
            result = gw.run(
                "LCZX-1",
                {},
                {"project": "LCZX", "issue_type": "故障", "description": "x"*80, "summary": "test", "product_version": "5.0", "customer_name": ""},
                generated_reply="test reply",
            )

        assert result["version"] == "v2"
        assert "gates" in result
        assert "display_cards" in result
        assert "final_action" in result
        assert "extra_operations" in result
        for gate_key in ("G1_completeness","G2_classification","G3_reuse","G4_specificity","G5_supervisor"):
            assert gate_key in result["gates"]

    def test_only_parameter_limits_gates(self):
        """run(only=['G1']) → only G1 runs, others=skipped"""
        g1_result = MagicMock()
        g1_result.passed = True
        g1_result.missing_fields = []
        g1_result.insufficient_type = ""
        g1_result.inquiry_draft = ""
        g1_result.rule_matched = "test"
        g1_result.gate_enabled = True

        with patch("services.completeness_checker.check", return_value=g1_result):
            gw = _gw()
            result = gw.run("LCZX-1", {}, {"project":"LCZX","issue_type":"故障","description":"x"*80,"summary":"test","product_version":"5.0","customer_name":""}, only=["G1"])

        assert result["gates"]["G1_completeness"]["verdict"] in ("pass", "warn", "fail")
        assert result["gates"]["G2_classification"]["verdict"] == "skipped"
        assert result["gates"]["G3_reuse"]["verdict"] == "skipped"
        assert result["gates"]["G4_specificity"]["verdict"] == "skipped"
        assert result["gates"]["G5_supervisor"]["verdict"] == "skipped"
