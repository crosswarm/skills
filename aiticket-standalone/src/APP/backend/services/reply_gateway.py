"""Smart Reply Gateway v2 — 5段网关全跑，每段产出 verdict + 展示卡片。

设计原则：
- 所有门都跑完（除非 only= 限定）
- 禁用的门产出 verdict="skipped"，不阻断流程
- 每个门独立 try/except，错误不传播到下一门
- 不导入 board_service_chroma（由调用方传入数据）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_GATES_YAML = _PROJECT_ROOT / "config" / "reply_gates.yaml"
_GATE2_ROUTING = _PROJECT_ROOT / "data" / "gate2_routing.json"


class ReplyGateway:
    """智能回复网关 v2 — 5段网关全跑，每段产出 verdict + 展示卡片"""

    def __init__(self, vector_store=None, llm_service=None, reply_trainer=None):
        self.vector_store = vector_store
        self.llm_service = llm_service
        self.reply_trainer = reply_trainer
        self._routing_rules: Optional[dict] = None  # lazy-loaded

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        issue_key: str,
        ai_analysis: dict,
        ticket_meta: dict,
        *,
        only: list = None,
        kb_evidence: list = None,
        reply_examples: list = None,
        generated_reply: str = "",
    ) -> dict:
        """
        Parameters
        ----------
        issue_key       : Jira 工单号，如 "LCZX-12345"
        ai_analysis     : 已获取的 AI 分析结果（含 issue_title / issue_description 等）
        ticket_meta     : 工单元数据 dict（project / issue_type / description / summary /
                          product_version / customer_name 等）
        only            : 若设置，仅运行指定门 e.g. ["G1", "G3"]
        kb_evidence     : 调用方预取的 KB 证据列表（供 G4/G5 使用）
        reply_examples  : 调用方预取的历史回复样例列表（供 G3 使用）
        generated_reply : 将要发送的回复正文（供 G5 审计使用）

        Returns
        -------
        reply_gateway dict（含 version / gates / display_cards / final_action / extra_operations）
        """
        gates: dict = {}

        # ---- G1: 信息完整性 ----
        if self._should_run("G1", only):
            gates["G1_completeness"] = self._run_g1(issue_key, ticket_meta)
        else:
            gates["G1_completeness"] = _skipped_gate()

        g1_failed = gates["G1_completeness"]["verdict"] == "fail"

        # ---- G2: 工单分类路由 ----
        if self._should_run("G2", only):
            if g1_failed:
                gates["G2_classification"] = _skipped_gate()
            else:
                gates["G2_classification"] = self._run_g2(
                    issue_key, ai_analysis, ticket_meta
                )
        else:
            gates["G2_classification"] = _skipped_gate()

        # ---- G3: 历史复用评估 ----
        if self._should_run("G3", only):
            if g1_failed:
                gates["G3_reuse"] = _skipped_gate()
            else:
                gates["G3_reuse"] = self._run_g3(reply_examples, ticket_meta)
        else:
            gates["G3_reuse"] = _skipped_gate()

        # ---- G4: 回复具体性（KB证据） ----
        if self._should_run("G4", only):
            if g1_failed:
                gates["G4_specificity"] = _skipped_gate()
            else:
                gates["G4_specificity"] = self._run_g4(kb_evidence)
        else:
            gates["G4_specificity"] = _skipped_gate()

        # ---- G5: 监督审计（G1失败时审计 inquiry_draft） ----
        if self._should_run("G5", only):
            g5_reply = (
                gates["G1_completeness"].get("inquiry_draft", "")
                if g1_failed
                else generated_reply
            )
            gates["G5_supervisor"] = self._run_g5(
                issue_key, ai_analysis, g5_reply, kb_evidence
            )
        else:
            gates["G5_supervisor"] = _skipped_gate()

        # ---- 聚合 final_action + extra_operations ----
        final_action, extra_operations = self._aggregate_actions(
            gates, issue_key
        )

        # ---- 生成 display_cards ----
        display_cards = self._build_display_cards(gates)

        return {
            "version": "v2",
            "gates": gates,
            "display_cards": display_cards,
            "final_action": final_action,
            "extra_operations": extra_operations,
        }

    def run_g5_only(
        self,
        issue_key: str,
        ai_analysis: dict,
        generated_reply: str,
        kb_evidence: list = None,
    ) -> dict:
        """Run only G5 supervisor gate. Used after reply content generation."""
        return self._run_g5(issue_key, ai_analysis, generated_reply, kb_evidence)

    def finalize_with_g5(
        self,
        gateway_result: dict,
        issue_key: str,
        ai_analysis: dict,
        generated_reply: str,
        kb_evidence: list = None,
    ) -> dict:
        """Add G5 result to a partial gateway result (G1-G4 already run), re-aggregate."""
        g5 = self._run_g5(issue_key, ai_analysis, generated_reply, kb_evidence)
        gateway_result["gates"]["G5_supervisor"] = g5
        final_action, extra_ops = self._aggregate_actions(gateway_result["gates"], issue_key)
        gateway_result["final_action"] = final_action
        gateway_result["extra_operations"] = extra_ops
        gateway_result["display_cards"] = self._build_display_cards(gateway_result["gates"])
        return gateway_result

    def inject_g5_from_supervisor(self, gateway_result: dict, issue_key: str, supervisor_result) -> dict:
        """Inject G5 from an already-computed supervisor result (avoids duplicate LLM call).

        supervisor_result must have: supervisor_score, risk_flags, step_safety, rationale, provider_used.
        Re-aggregates final_action, extra_operations, display_cards.
        """
        cfg = self._load_gate_cfg("supervisor")
        pass_thr = float(cfg.get("pass_threshold", 0.80))
        rev_thr = float(cfg.get("review_threshold", 0.50))
        score = getattr(supervisor_result, "supervisor_score", None) if supervisor_result else None
        if score is None:
            verdict = "warn"
        elif score >= pass_thr and not (getattr(supervisor_result, "risk_flags", None) or []):
            verdict = "pass"
        elif score >= rev_thr:
            verdict = "warn"
        else:
            verdict = "fail"
        gateway_result["gates"]["G5_supervisor"] = {
            "verdict": verdict,
            "score": score,
            "risk_flags": getattr(supervisor_result, "risk_flags", []) if supervisor_result else [],
            "step_safety": getattr(supervisor_result, "step_safety", None) if supervisor_result else None,
            "rationale": getattr(supervisor_result, "rationale", "") if supervisor_result else "",
            "provider_used": getattr(supervisor_result, "provider_used", "") if supervisor_result else "",
        }
        final_action, extra_ops = self._aggregate_actions(gateway_result["gates"], issue_key)
        gateway_result["final_action"] = final_action
        gateway_result["extra_operations"] = extra_ops
        gateway_result["display_cards"] = self._build_display_cards(gateway_result["gates"])
        return gateway_result

    # ------------------------------------------------------------------
    # Gate implementations
    # ------------------------------------------------------------------

    def _run_g1(self, issue_key: str, ticket_meta: dict) -> dict:
        defaults = _g1_defaults()
        try:
            from services import completeness_checker

            cfg = self._load_gate_cfg("completeness")
            if not cfg.get("enabled", True):
                return {**defaults, "verdict": "skipped"}

            result = completeness_checker.check(
                issue_key=issue_key,
                project=ticket_meta.get("project", ""),
                issue_type_confirmed=ticket_meta.get("issue_type", ""),
                description=ticket_meta.get("description", ""),
                attachment_texts=[],
            )

            if not result.gate_enabled:
                return {**defaults, "verdict": "skipped"}

            verdict = "pass" if result.passed else "fail"
            # 对 warn 的支持：若 missing_fields 存在但 rule 非精确匹配视为 warn
            if not result.passed and result.insufficient_type == "invalid_description":
                verdict = "fail"
            elif not result.passed:
                verdict = "fail"

            return {
                "verdict": verdict,
                "score": 0.0 if not result.passed else 1.0,
                "missing_fields": result.missing_fields,
                "insufficient_type": result.insufficient_type,
                "inquiry_draft": result.inquiry_draft,
                "rule_matched": result.rule_matched,
            }
        except Exception as exc:
            logger.warning("[G1] error: %s", exc)
            return {**defaults, "verdict": "skipped", "error": str(exc)}

    def _run_g2(
        self, issue_key: str, ai_analysis: dict, ticket_meta: dict
    ) -> dict:
        defaults = _g2_defaults()
        try:
            cfg = self._load_gate_cfg("classification")
            if not cfg.get("enabled", True):
                return {**defaults, "verdict": "skipped"}

            routing = self._load_routing_rules()
            ticket_project = issue_key.split("-")[0] if "-" in issue_key else ""

            summary = ticket_meta.get("summary", "")
            description = ticket_meta.get("description", "")
            issue_type = ticket_meta.get("issue_type", "")
            product_version = ticket_meta.get("product_version", "")

            query = f"{summary} {description}"[:500]

            # Signal 1: chroma vector vote
            chroma_project, chroma_vote_ratio = self._chroma_vote(
                query, ticket_project
            )

            # Signal 2: rule matching
            matched_rule, rule_match_score = self._match_routing_rule(
                ticket_project, summary, description, issue_type, product_version
            )

            # Signal 3: LLM domain match
            predicted_module = ai_analysis.get("domain_module", "") or ai_analysis.get("category", "")
            llm_match_score = self._llm_domain_match(predicted_module, matched_rule)

            confidence = (
                0.5 * chroma_vote_ratio
                + 0.3 * rule_match_score
                + 0.2 * llm_match_score
            )
            confidence = round(min(confidence, 1.0), 4)

            # Determine transfer target
            transfer_to = ""
            transferee = ""
            transferee_display = ""
            domain_module = predicted_module
            auto_move_eligible = False
            matched_rule_id = ""
            final_project = ticket_project

            if matched_rule:
                transfer_to = matched_rule.get("target_project", "")
                transferee = matched_rule.get("transferee", "")
                transferee_display = matched_rule.get("transferee_display", "")
                domain_module = matched_rule.get("domain_module", domain_module)
                auto_move_eligible = matched_rule.get("auto_move_eligible", False)
                matched_rule_id = matched_rule.get("id", "")
                final_project = transfer_to or ticket_project

                # yonsuite_exclude: block_handover → always pass
                if matched_rule.get("action") == "block_handover":
                    return {
                        **defaults,
                        "verdict": "pass",
                        "confidence": confidence,
                        "ticket_project_from_key": ticket_project,
                        "predicted_project_from_chroma": chroma_project,
                        "predicted_module_from_llm": predicted_module,
                        "final_project_decision": ticket_project,
                        "transfer_to": "",
                        "transferee": "",
                        "transferee_display": "",
                        "domain_module": domain_module,
                        "auto_move_eligible": False,
                        "matched_rule_id": matched_rule_id,
                        "chroma_vote_ratio": chroma_vote_ratio,
                        "rule_match_score": rule_match_score,
                        "llm_match_score": llm_match_score,
                    }

            # Verdict thresholds
            target_differs = bool(transfer_to) and transfer_to != ticket_project
            if confidence >= 0.70 and matched_rule and target_differs:
                verdict = "fail"
            elif 0.50 <= confidence < 0.70:
                verdict = "warn"
            else:
                verdict = "pass"

            return {
                "verdict": verdict,
                "confidence": confidence,
                "ticket_project_from_key": ticket_project,
                "predicted_project_from_chroma": chroma_project,
                "predicted_module_from_llm": predicted_module,
                "final_project_decision": final_project,
                "transfer_to": transfer_to,
                "transferee": transferee,
                "transferee_display": transferee_display,
                "domain_module": domain_module,
                "auto_move_eligible": auto_move_eligible,
                "matched_rule_id": matched_rule_id,
                "chroma_vote_ratio": chroma_vote_ratio,
                "rule_match_score": rule_match_score,
                "llm_match_score": llm_match_score,
            }
        except Exception as exc:
            logger.warning("[G2] error: %s", exc)
            return {**defaults, "verdict": "skipped", "error": str(exc)}

    def _run_g3(self, reply_examples: list, ticket_meta: dict) -> dict:
        defaults = _g3_defaults()
        try:
            cfg = self._load_gate_cfg("reuse")
            if not cfg.get("enabled", True):
                return {**defaults, "verdict": "skipped"}

            # Fetch examples from reply_trainer if not pre-supplied
            examples = reply_examples
            if not examples and self.reply_trainer is not None:
                try:
                    _query = (ticket_meta.get("summary") or ticket_meta.get("description") or "")[:500]
                    examples = self.reply_trainer.search_examples(
                        _query,
                        project_key=ticket_meta.get("project", ""),
                        module=ticket_meta.get("domain_module", ""),
                    )
                except Exception as exc:
                    logger.warning("[G3] reply_trainer.search_examples() failed: %s", exc)
                    examples = []

            from services.reply_reuse_evaluator import evaluate_reuse

            candidate = evaluate_reuse(
                reply_examples=examples or [],
                current_product_version=ticket_meta.get("product_version", ""),
                current_module=ticket_meta.get("domain_module", ""),
            )

            if candidate is None:
                return {
                    **defaults,
                    "verdict": "pass",
                    "composite_score": 0.0,
                    "candidate_key": "",
                    "candidate_summary": "",
                    "reuse_strategy": "skip",
                }

            tier_map = {"direct": "direct", "llm_blend": "reference", "skip": "skip"}
            reuse_strategy = tier_map.get(candidate.tier, "skip")

            # warn if score is low but still present
            if candidate.composite_score < 0.60:
                verdict = "warn"
            else:
                verdict = "pass"

            return {
                "verdict": verdict,
                "composite_score": candidate.composite_score,
                "candidate_key": candidate.example.get("issue_key", ""),
                "candidate_summary": (candidate.example.get("reply", ""))[:120],
                "reuse_strategy": reuse_strategy,
            }
        except Exception as exc:
            logger.warning("[G3] error: %s", exc)
            return {**defaults, "verdict": "skipped", "error": str(exc)}

    def _run_g4(self, kb_evidence: list) -> dict:
        defaults = _g4_defaults()
        try:
            cfg = self._load_gate_cfg("specificity")
            if not cfg.get("enabled", True):
                return {**defaults, "verdict": "skipped"}

            evidence_list = kb_evidence or []
            count = len(evidence_list)

            if count >= 3:
                level = "high"
            elif count == 2:
                level = "medium"
            elif count == 1:
                level = "low"
            else:
                level = "none"

            # Identify weak points: evidence items with low score
            weak_points = []
            evidence_items = []
            for item in evidence_list[:6]:
                score = item.get("score", item.get("similarity", 1.0))
                name = item.get("name", item.get("title", item.get("chunk_id", "unknown")))
                source = item.get("source", item.get("kb_path", item.get("issue_key", "")))
                evidence_items.append({
                    "name": str(name)[:80],
                    "score": float(score) if isinstance(score, (int, float)) else None,
                    "source": str(source)[:80] if source else "",
                })
                if isinstance(score, (int, float)) and score < 0.5:
                    weak_points.append(str(name))

            if level == "none":
                verdict = "fail"
            elif level == "low":
                verdict = "warn"
            else:
                verdict = "pass"

            return {
                "verdict": verdict,
                "level": level,
                "kb_evidence_count": count,
                "weak_points": weak_points,
                "evidence_items": evidence_items,
            }
        except Exception as exc:
            logger.warning("[G4] error: %s", exc)
            return {**defaults, "verdict": "skipped", "error": str(exc)}

    def _run_g5(
        self,
        issue_key: str,
        ai_analysis: dict,
        generated_reply: str,
        kb_evidence: list,
    ) -> dict:
        defaults = _g5_defaults()
        try:
            cfg = self._load_gate_cfg("supervisor")
            if not cfg.get("enabled", True):
                return {**defaults, "verdict": "skipped"}

            from agents.reply_supervisor_agent import supervise

            result = supervise(
                issue_key=issue_key,
                issue_title=ai_analysis.get("issue_title", ""),
                issue_description=ai_analysis.get("issue_description", ""),
                generated_reply=generated_reply,
                kb_evidence=kb_evidence or [],
                gate_decisions={},
                main_provider="minimax",
            )

            if not result.gate_enabled:
                return {**defaults, "verdict": "skipped"}

            pass_threshold = float(cfg.get("pass_threshold", 0.80))
            review_threshold = float(cfg.get("review_threshold", 0.50))

            score = result.supervisor_score
            if score is None:
                verdict = "warn"
            elif score >= pass_threshold and not result.risk_flags:
                verdict = "pass"
            elif score >= review_threshold:
                verdict = "warn"
            else:
                verdict = "fail"

            return {
                "verdict": verdict,
                "score": score,
                "risk_flags": result.risk_flags,
                "step_safety": result.step_safety,
                "rationale": result.rationale,
                "provider_used": result.provider_used,
            }
        except Exception as exc:
            logger.warning("[G5] error: %s", exc)
            return {**defaults, "verdict": "skipped", "error": str(exc)}

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate_actions(
        self, gates: dict, issue_key: str
    ) -> tuple[str, list]:
        g1 = gates.get("G1_completeness", {})
        g2 = gates.get("G2_classification", {})

        if g1.get("verdict") == "fail":
            return "return_to_support", [
                {"type": "return_to_support", "target": issue_key, "payload": {}}
            ]

        if g2.get("verdict") == "fail":
            ops = []
            if g2.get("auto_move_eligible"):
                ops.append(
                    {
                        "type": "move_jira",
                        "target": g2.get("transfer_to", ""),
                        "payload": {
                            "transferee": g2.get("transferee", ""),
                            "domain_module": g2.get("domain_module", ""),
                        },
                    }
                )
            else:
                transfer_to = g2.get("transfer_to", "")
                transferee = g2.get("transferee", "")
                ops.append(
                    {
                        "type": "notify",
                        "target": transferee,
                        "payload": {
                            "message": f"建议将此工单移交至 {transfer_to}"
                        },
                    }
                )
            return "handover", ops

        # G1 inquiry_draft → inquiry action
        if g1.get("verdict") == "pass" and g1.get("inquiry_draft"):
            return "inquiry", []

        return "normal_reply", []

    # ------------------------------------------------------------------
    # Display cards
    # ------------------------------------------------------------------

    def _build_display_cards(self, gates: dict) -> list:
        cards = []

        g1 = gates.get("G1_completeness", {})
        g2 = gates.get("G2_classification", {})
        g3 = gates.get("G3_reuse", {})
        g4 = gates.get("G4_specificity", {})
        g5 = gates.get("G5_supervisor", {})

        # G1
        if g1.get("verdict") == "fail":
            missing = g1.get("missing_fields", [])
            cards.append(
                _card(
                    gate="G1",
                    badge_color="red",
                    badge_text="信息不足",
                    message=f"缺少必填信息：{', '.join(missing)}" if missing else "工单描述不完整",
                    detail=g1.get("inquiry_draft", ""),
                )
            )

        # G2
        if g2.get("verdict") == "fail":
            transfer_to = g2.get("transfer_to", "")
            evidence = (
                f"相似度 {int(g2.get('chroma_vote_ratio', 0) * 100)}%，"
                f"置信度 {int(g2.get('confidence', 0) * 100)}%"
            )
            cards.append(
                _card(
                    gate="G2",
                    badge_color="red",
                    badge_text=f"建议移交 {transfer_to}",
                    message=evidence,
                    detail=f"移交人：{g2.get('transferee_display', g2.get('transferee', ''))}",
                )
            )
        elif g2.get("verdict") == "warn":
            confidence = g2.get("confidence", 0)
            cards.append(
                _card(
                    gate="G2",
                    badge_color="amber",
                    badge_text="疑似错分",
                    message=f"分类置信度 {int(confidence * 100)}%，请人工核查",
                    detail="",
                )
            )

        # G3
        if g3.get("verdict") == "warn":
            score = g3.get("composite_score", 0)
            cards.append(
                _card(
                    gate="G3",
                    badge_color="amber",
                    badge_text="复用度低",
                    message=f"历史回复复合分 {int(score * 100)}%，建议人工撰写",
                    detail="",
                )
            )

        # G4
        if g4.get("verdict") == "fail":
            count = g4.get("kb_evidence_count", 0)
            cards.append(
                _card(
                    gate="G4",
                    badge_color="red",
                    badge_text="KB证据不足",
                    message=f"未找到相关知识库条目（当前 {count} 条）",
                    detail="",
                )
            )
        elif g4.get("verdict") == "warn":
            count = g4.get("kb_evidence_count", 0)
            cards.append(
                _card(
                    gate="G4",
                    badge_color="amber",
                    badge_text="证据较少",
                    message=f"仅找到 {count} 条知识库证据，回复可靠性存疑",
                    detail="",
                )
            )

        # G5
        if g5.get("verdict") in ("fail", "warn"):
            risk_flags = g5.get("risk_flags", [])
            badge_color = "red" if g5["verdict"] == "fail" else "amber"
            if risk_flags:
                badge_text = "风险: " + ", ".join(risk_flags[:2])
            else:
                score = g5.get("score")
                badge_text = (
                    f"审计分 {int(score * 100)}%" if score is not None else "审计警告"
                )
            cards.append(
                _card(
                    gate="G5",
                    badge_color=badge_color,
                    badge_text=badge_text,
                    message=g5.get("rationale", ""),
                    detail=f"step_safety: {g5.get('step_safety', '')}",
                )
            )

        return cards

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _should_run(self, gate_id: str, only: list) -> bool:
        if only is None:
            return True
        return gate_id in only

    def _load_gate_cfg(self, gate_name: str) -> dict:
        try:
            raw = yaml.safe_load(_GATES_YAML.read_text(encoding="utf-8"))
            return raw.get("gates", {}).get(gate_name, {})
        except Exception:
            return {}

    def _load_routing_rules(self) -> dict:
        if self._routing_rules is None:
            try:
                self._routing_rules = json.loads(
                    _GATE2_ROUTING.read_text(encoding="utf-8")
                )
            except Exception as exc:
                logger.warning("[G2] failed to load gate2_routing.json: %s", exc)
                self._routing_rules = {"rules": [], "project_name_map": {}}
        return self._routing_rules

    def _chroma_vote(self, query: str, current_project: str) -> tuple[str, float]:
        """向量搜索 top_k 结果，多数投票确定最可能的目标项目。"""
        if self.vector_store is None:
            return current_project, 0.0
        try:
            top_k = 5
            results = self.vector_store.search_similar_issues(query, top_k=top_k)
            if not results:
                return current_project, 0.0
            project_counts: dict[str, int] = {}
            for item in results:
                meta = item.get("metadata", {})
                key = meta.get("issue_key", "")
                proj = key.split("-")[0] if "-" in key else ""
                if proj:
                    project_counts[proj] = project_counts.get(proj, 0) + 1
            if not project_counts:
                return current_project, 0.0
            majority_project = max(project_counts, key=lambda p: project_counts[p])
            vote_ratio = round(project_counts[majority_project] / top_k, 4)
            return majority_project, vote_ratio
        except Exception as exc:
            logger.warning("[G2] chroma vote failed: %s", exc)
            return current_project, 0.0

    def _match_routing_rule(
        self,
        ticket_project: str,
        summary: str,
        description: str,
        issue_type: str,
        product_version: str,
    ) -> tuple[Optional[dict], float]:
        """遍历 gate2_routing.json 规则，返回（匹配规则, 匹配分数）。"""
        routing = self._load_routing_rules()
        rules = routing.get("rules", [])

        summary_lower = summary.lower()
        description_lower = description.lower()
        product_version_lower = product_version.lower()

        for rule in sorted(rules, key=lambda r: r.get("priority", 99)):
            match = rule.get("match", {})

            # exclude_product_version check
            exclude_versions = [v.lower() for v in match.get("exclude_product_version", [])]
            if exclude_versions and any(ev in product_version_lower for ev in exclude_versions):
                continue

            # product_version_contains check (positive match required)
            version_contains = [v.lower() for v in match.get("product_version_contains", [])]
            if version_contains and not any(vc in product_version_lower for vc in version_contains):
                continue

            # ticket_project filter
            ticket_projects = match.get("ticket_project", [])
            if ticket_projects and ticket_project not in ticket_projects:
                continue

            # issue_type filter
            rule_issue_types = match.get("issue_type", [])
            if rule_issue_types and issue_type not in rule_issue_types:
                continue

            # keyword scoring
            summary_kws = match.get("summary_keywords", [])
            description_kws = match.get("description_keywords", [])
            domain_kws = match.get("domain_keywords", [])
            all_kws = summary_kws + description_kws + domain_kws
            if not all_kws:
                # structural match only (e.g. yonsuite_exclude) → score 1.0
                return rule, 1.0

            matched = 0
            for kw in summary_kws:
                if kw.lower() in summary_lower:
                    matched += 1
            for kw in description_kws:
                if kw.lower() in description_lower:
                    matched += 1
            for kw in domain_kws:
                if kw.lower() in summary_lower or kw.lower() in description_lower:
                    matched += 1

            score = round(min(matched / len(all_kws), 1.0), 4)
            if score > 0:
                return rule, score

        return None, 0.0

    def _llm_domain_match(self, predicted_module: str, matched_rule: Optional[dict]) -> float:
        """判断 LLM 预测的领域模块是否与规则 domain_module 对应。"""
        if not predicted_module or matched_rule is None:
            return 0.0
        rule_domain = matched_rule.get("domain_module", "")
        if not rule_domain:
            return 0.0
        # 模糊匹配：预测模块包含规则领域关键词之一
        rule_tokens = [t.strip() for t in rule_domain.replace("-", " ").replace("_", " ").split()]
        predicted_lower = predicted_module.lower()
        for token in rule_tokens:
            if token.lower() in predicted_lower:
                return 1.0
        return 0.0


# ------------------------------------------------------------------
# Gate default dicts (all fields present, all values as neutral defaults)
# ------------------------------------------------------------------

def _skipped_gate() -> dict:
    return {"verdict": "skipped"}


def _g1_defaults() -> dict:
    return {
        "verdict": "skipped",
        "score": 0.0,
        "missing_fields": [],
        "insufficient_type": "",
        "inquiry_draft": "",
        "rule_matched": "",
    }


def _g2_defaults() -> dict:
    return {
        "verdict": "skipped",
        "confidence": 0.0,
        "ticket_project_from_key": "",
        "predicted_project_from_chroma": "",
        "predicted_module_from_llm": "",
        "final_project_decision": "",
        "transfer_to": "",
        "transferee": "",
        "transferee_display": "",
        "domain_module": "",
        "auto_move_eligible": False,
        "matched_rule_id": "",
        "chroma_vote_ratio": 0.0,
        "rule_match_score": 0.0,
        "llm_match_score": 0.0,
    }


def _g3_defaults() -> dict:
    return {
        "verdict": "skipped",
        "composite_score": 0.0,
        "candidate_key": "",
        "candidate_summary": "",
        "reuse_strategy": "skip",
    }


def _g4_defaults() -> dict:
    return {
        "verdict": "skipped",
        "level": "none",
        "kb_evidence_count": 0,
        "weak_points": [],
    }


def _g5_defaults() -> dict:
    return {
        "verdict": "skipped",
        "score": None,
        "risk_flags": [],
        "step_safety": "safe",
        "rationale": "",
        "provider_used": "",
    }


def _card(gate: str, badge_color: str, badge_text: str, message: str, detail: str) -> dict:
    return {
        "gate": gate,
        "badge_color": badge_color,
        "badge_text": badge_text,
        "message": message,
        "detail": detail,
    }
