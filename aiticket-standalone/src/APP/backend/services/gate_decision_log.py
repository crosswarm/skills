"""
Gate Decision 日志 — 记录每次 generate_reply_content 调用的门控决策结果，
供看板列 2/4 状态徽章、批量审批、运营统计使用。

写入路径: data/gate_decisions.jsonl（JSONL，每行一条决策记录）
超过 10MB 自动轮转为 gate_decisions.<timestamp>.jsonl。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

_JSONL_PATH = os.path.join(os.path.dirname(__file__), "../data/gate_decisions.jsonl")
_JSONL_PATH_obj = Path(_JSONL_PATH)
_FEEDBACK_LOG_PATH = Path(os.path.dirname(__file__)) / "../data/reply_trainer/feedback_log.jsonl"
_JSONL_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

_lock = threading.Lock()
# debounce: issue_key -> monotonic timestamp of last write
_debounce: dict[str, float] = {}
_DEBOUNCE_SEC = 60

# simple TTL cache for get_recent_decision
_recent_decision_cache: dict[str, tuple[float, dict]] = {}  # issue_key -> (timestamp, result)
_RECENT_DECISION_TTL = 300  # 5 minutes

_ALL_ACTIONS = [
    "auto_returned",
    "auto_moved",
    "auto_replied_normal",
    "auto_replied_low_risk",
    "auto_assigned",
    "pending_batch_approve",
    "needs_decision",
    "manual",
]

_ALL_GATES = ["completeness", "classification", "reuse", "specificity", "supervisor"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _maybe_rotate() -> None:
    """P4: JSONL >10MB 自动轮转（必须在 _lock 内调用）。"""
    try:
        if _JSONL_PATH_obj.exists() and _JSONL_PATH_obj.stat().st_size > _JSONL_MAX_BYTES:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            rotated = _JSONL_PATH_obj.with_name(f"gate_decisions.{ts}.jsonl")
            _JSONL_PATH_obj.rename(rotated)
    except Exception as exc:
        print(f"[GateDecisionLog] rotation failed: {exc}")


def _iter_records(hours: int):
    """逐行读取 JSONL，过滤 hours 窗口内的记录，跳过损坏行。"""
    if not _JSONL_PATH_obj.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        with open(_JSONL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    ts_str = record.get("ts", "")
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts >= cutoff:
                        yield record
                except Exception:
                    continue
    except Exception as exc:
        print(f"[GateDecisionLog] read failed: {exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_gate_decision(
    issue_key: str,
    *,
    project: str = "",
    issue_type: str = "",
    customer_name: str = "",
    is_key_customer: bool = False,
    product_priority: str = "",
    due_date: str = "",
    gate_decisions: dict = None,
    missing_fields: list = None,
    reuse_score: float = None,
    specificity_level: str = None,
    supervisor_score: float = None,
    risk_flags: list = None,
    auto_reply_decision: dict = None,
    final_action: str = "",
    reply_summary: str = "",
    operation_steps: list = None,
    actor: str = "system",
    force: bool = False,
    blocked_by: list = None,
    reply_gateway: dict = None,
) -> bool:
    """
    追加一条 gate 决策记录。同一 issue_key 在 60 秒内仅记一次，防止重试重复。
    返回 True 表示实际写入，False 表示被去抖跳过。
    """
    now = time.monotonic()

    with _lock:
        if not force and now - _debounce.get(issue_key, 0) < _DEBOUNCE_SEC:
            return False
        _debounce[issue_key] = now

        _maybe_rotate()

        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "issue_key": issue_key,
            "project": project or "",
            "issue_type": issue_type or "",
            "customer_name": customer_name or "",
            "is_key_customer": bool(is_key_customer),
            "product_priority": product_priority or "",
            "due_date": due_date or "",
            "gate_decisions": gate_decisions if gate_decisions is not None else {
                "completeness": "skipped",
                "classification": "skipped",
                "reuse": "skipped",
                "specificity": "skipped",
                "supervisor": "skipped",
            },
            "missing_fields": missing_fields if missing_fields is not None else [],
            "reuse_score": reuse_score,
            "specificity_level": specificity_level or "",
            "supervisor_score": supervisor_score,
            "risk_flags": risk_flags if risk_flags is not None else [],
            "auto_reply_decision": auto_reply_decision if auto_reply_decision is not None else {},
            "final_action": final_action or "",
            "reply_summary": (reply_summary or "")[:80],
            "operation_steps": operation_steps if operation_steps is not None else [],
            "actor": actor or "system",
            "reply_gateway": reply_gateway or {},
        }
        try:
            os.makedirs(os.path.dirname(_JSONL_PATH), exist_ok=True)
            with open(_JSONL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return True
        except Exception as exc:
            print(f"[GateDecisionLog] write failed: {exc}")
            return False


def get_stats(hours: int = 24) -> dict:
    """
    返回过去 hours 小时内的门控决策统计摘要。
    """
    by_action: dict[str, int] = {a: 0 for a in _ALL_ACTIONS}
    by_gate: dict[str, dict] = {
        g: {"passed": 0, "blocked": 0, "skipped": 0} for g in _ALL_GATES
    }
    missing_counter: Counter = Counter()
    supervisor_scores: list[float] = []
    risk_counter: Counter = Counter()
    total = 0

    for record in _iter_records(hours):
        total += 1

        action = record.get("final_action", "")
        if action in by_action:
            by_action[action] += 1

        gate_dec = record.get("gate_decisions") or {}
        for gate in _ALL_GATES:
            verdict = gate_dec.get(gate, "skipped")
            if verdict in ("passed", "blocked", "skipped"):
                by_gate[gate][verdict] += 1

        for field in record.get("missing_fields") or []:
            if field:
                missing_counter[field] += 1

        sv = record.get("supervisor_score")
        if sv is not None:
            try:
                supervisor_scores.append(float(sv))
            except (TypeError, ValueError):
                pass

        for flag in record.get("risk_flags") or []:
            if flag:
                risk_counter[flag] += 1

    # block_rate per gate
    by_gate_out: dict[str, dict] = {}
    for gate in _ALL_GATES:
        g = by_gate[gate]
        block_rate = round(g["blocked"] / total, 4) if total > 0 else 0.0
        by_gate_out[gate] = {
            "passed": g["passed"],
            "blocked": g["blocked"],
            "skipped": g["skipped"],
            "block_rate": block_rate,
            "enabled": g["passed"] + g["blocked"] > 0,
        }

    auto_replied = by_action.get("auto_replied_normal", 0) + by_action.get("auto_replied_low_risk", 0)
    auto_reply_rate = round(auto_replied / total, 4) if total > 0 else 0.0

    supervisor_avg = (
        round(sum(supervisor_scores) / len(supervisor_scores), 4)
        if supervisor_scores
        else None
    )

    top_missing = [[field, cnt] for field, cnt in missing_counter.most_common(5)]
    risk_dist = dict(risk_counter)

    return {
        "window_hours": hours,
        "total": total,
        "by_action": by_action,
        "by_gate": by_gate_out,
        "auto_reply_rate": auto_reply_rate,
        "pending_count": by_action.get("pending_batch_approve", 0),
        "top_missing_fields": top_missing,
        "supervisor_avg": supervisor_avg,
        "risk_flag_distribution": risk_dist,
    }


def get_tickets_by_action(hours: int = 72) -> dict:
    """
    返回过去 hours 小时内按 final_action 分组的工单列表。
    by_key 字典供前端做 gate 状态徽章 + 列 4 日期分桶。
    同一 issue_key 多次出现时保留最新记录（ts 最大）。
    每个 action 最多返回 1000 条（安全限制）。
    """
    # latest_by_key: issue_key -> {"ts": ..., "action": ..., "is_key_customer": ..., "due_date": ...}
    latest_by_key: dict[str, dict] = {}

    for record in _iter_records(hours):
        key = record.get("issue_key", "")
        if not key:
            continue
        ts_str = record.get("ts", "")
        existing = latest_by_key.get(key)
        if existing is None or ts_str > existing["ts"]:
            ar = record.get("auto_reply_decision") or {}
            latest_by_key[key] = {
                "ts": ts_str,
                "action": record.get("final_action", ""),
                "is_key_customer": record.get("is_key_customer", False),
                "due_date": record.get("due_date", ""),
                "supervisor_score": record.get("supervisor_score"),
                "reuse_score": record.get("reuse_score"),
                "specificity_level": record.get("specificity_level", ""),
                "risk_flags": record.get("risk_flags", []) or [],
                "composite_score": ar.get("composite_score"),
                "missing_fields": record.get("missing_fields", []) or [],
                "reply_summary": (record.get("reply_summary", "") or "")[:80],
            }

    # group by action
    grouped: dict[str, list[str]] = {a: [] for a in _ALL_ACTIONS}
    for issue_key, info in latest_by_key.items():
        action = info["action"]
        if action in grouped:
            grouped[action].append(issue_key)

    # apply safety cap
    for action in _ALL_ACTIONS:
        if len(grouped[action]) > 1000:
            grouped[action] = grouped[action][:1000]

    # build by_key without internal ts field
    _SKIP = {"ts"}
    by_key: dict[str, dict] = {
        k: {fk: fv for fk, fv in v.items() if fk not in _SKIP}
        for k, v in latest_by_key.items()
    }

    result = dict(grouped)
    result["by_key"] = by_key
    return result


_SIM_DIRECT_THRESHOLD = 0.85   # 文本相似度 ≥85% → 直接采纳
_SIM_PARTIAL_THRESHOLD = 0.50  # 文本相似度 50-85% → 部分采纳；<50% → 未采纳


def _text_sim(a: str, b: str) -> float:
    """SequenceMatcher 文本相似度，与 reply_trainer.py 保持一致。"""
    import difflib
    if not a or not b:
        return 1.0 if a == b else 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _sim_tier(sim: float) -> str:
    """将相似度映射为采纳层级标签。"""
    if sim >= _SIM_DIRECT_THRESHOLD:
        return "direct"    # 直接采纳
    if sim >= _SIM_PARTIAL_THRESHOLD:
        return "partial"   # 部分采纳
    return "none"          # 未采纳


def _load_adoption_index() -> dict[str, dict]:
    """
    从 feedback_log.jsonl 建立 issue_key → {tier, sim} 索引。
    采纳标准（基于文本相似度）：
      ≥85% → 直接采纳（direct）
      65-85% → 部分采纳（partial）
      <65% → 未采纳（none）
    同一 issue_key 多条记录取最新一条；最多读最后 5000 行。
    """
    idx: dict[str, dict] = {}
    if not _FEEDBACK_LOG_PATH.exists():
        return {}
    try:
        lines = _FEEDBACK_LOG_PATH.read_text(encoding="utf-8").splitlines()
        for line in lines[-5000:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                key = rec.get("issue_key", "")
                if not key:
                    continue
                ts = rec.get("ts", "")
                if key in idx and ts <= idx[key]["ts"]:
                    continue
                ai_orig = rec.get("ai_original") or ""
                user_fin = rec.get("user_final") or ""
                if ai_orig and user_fin:
                    sim = _text_sim(ai_orig, user_fin)
                    tier = _sim_tier(sim)
                else:
                    # 无文本对 → 回退到 adopted 布尔值
                    sim = 1.0 if rec.get("adopted") else 0.0
                    tier = "direct" if rec.get("adopted") else "none"
                idx[key] = {"ts": ts, "tier": tier, "sim": round(sim, 4)}
            except Exception:
                continue
    except Exception:
        return {}
    return {k: {"tier": v["tier"], "sim": v["sim"]} for k, v in idx.items()}


_ACTION_LABEL = {
    "auto_replied_normal": "AI自动回复",
    "auto_replied_low_risk": "AI低风险回复",
    "auto_moved": "AI自动转交",
    "auto_returned": "AI自动退回",
    "auto_assigned": "AI自动分配",
    "pending_batch_approve": "待批量审批",
    "needs_decision": "人工决策",
    "manual": "人工处理",
}
_AI_ACTIONS = {"auto_replied_normal", "auto_replied_low_risk", "auto_moved", "auto_returned", "auto_assigned"}


def get_processing_log(hours: int = 168) -> list[dict]:
    """
    返回过去 hours 小时内所有处理过的工单记录（每条 issue_key 只保留最新），
    按时间倒序排列，供处理日志表格使用。
    采纳率字段来自 feedback_log.jsonl 真实用户行为数据。
    """
    adoption_idx = _load_adoption_index()
    latest_by_key: dict[str, dict] = {}

    for record in _iter_records(hours):
        key = record.get("issue_key", "")
        if not key:
            continue
        ts_str = record.get("ts", "")
        existing = latest_by_key.get(key)
        if existing is None or ts_str > existing.get("_ts", ""):
            ar = record.get("auto_reply_decision") or {}
            composite = ar.get("composite_score")
            if composite is None:
                composite = record.get("supervisor_score")
            fb = adoption_idx.get(key)  # {"tier": "direct"|"partial"|"none", "sim": float} | None
            latest_by_key[key] = {
                "_ts": ts_str,
                "issue_key": key,
                "ts": ts_str,
                "customer_name": record.get("customer_name") or "",
                "project": record.get("project") or "",
                "actor": record.get("actor") or "system",
                "reply_summary": (record.get("reply_summary") or "")[:120],
                "final_action": record.get("final_action") or "",
                "action_label": _ACTION_LABEL.get(record.get("final_action") or "", record.get("final_action") or ""),
                "is_key_customer": bool(record.get("is_key_customer")),
                "is_ai_processed": record.get("final_action") in _AI_ACTIONS,
                "composite_score": composite,
                "issue_type": record.get("issue_type") or "",
                "adoption_tier": fb["tier"] if fb else None,   # "direct"|"partial"|"none"|None(无记录)
                "adoption_sim": fb["sim"] if fb else None,     # 文本相似度 0-1
            }

    rows = sorted(latest_by_key.values(), key=lambda r: r["_ts"], reverse=True)
    for r in rows:
        del r["_ts"]
    return rows


def get_recent_decision(issue_key: str) -> dict | None:
    """
    返回该 issue_key 的最近一条决策记录，或 None。
    最多扫描最后 10000 行（性能保护），用于回滚场景。
    结果缓存 300 秒（TTL），避免高频看板轮询重复扫描。
    """
    now = time.monotonic()
    cached = _recent_decision_cache.get(issue_key)
    if cached and (now - cached[0]) < _RECENT_DECISION_TTL:
        return cached[1]

    if not _JSONL_PATH_obj.exists():
        return None

    MAX_LINES = 10000
    latest: dict | None = None
    latest_ts: str = ""

    try:
        with open(_JSONL_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        tail = lines[-MAX_LINES:] if len(lines) > MAX_LINES else lines
        for line in tail:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("issue_key") == issue_key:
                    ts_str = record.get("ts", "")
                    if latest is None or ts_str > latest_ts:
                        latest = record
                        latest_ts = ts_str
            except Exception:
                continue
    except Exception as exc:
        print(f"[GateDecisionLog] get_recent_decision failed: {exc}")
        return None

    _recent_decision_cache[issue_key] = (now, latest)
    return latest


def get_reply_gateway_compat(issue_key: str) -> dict | None:
    """返回 reply_gateway 结构；若历史记录是 legacy gate_decisions 字段则现合成。

    用于回复弹窗"回复依据"区与缓存命中分支：需要 v2 形态的 reply_gateway.gates
    才能渲染 G1-G5 卡片。
    """
    record = get_recent_decision(issue_key)
    if not record:
        return None
    rg = record.get("reply_gateway") or {}
    if rg.get("gates"):
        return rg
    gd = record.get("gate_decisions") or {}
    if not gd:
        return None

    def _v(field: str, blocked_verdict: str = "fail") -> str:
        x = gd.get(field)
        if x == "passed":
            return "pass"
        if x == "blocked":
            return blocked_verdict
        return "skipped"

    _legacy_note = "此工单为 v1 时期记录（2026-05-20 前），G2/G3/G4 详情未保存。可触发『重新生成回复』获取完整 v2 分析。"
    gates = {
        "G1_completeness": {
            "verdict": _v("completeness"),
            "missing_fields": record.get("missing_fields") or [],
            "_legacy_note": _legacy_note,
        },
        "G2_classification": {
            "verdict": _v("classification"),
            "_legacy_note": "v1 时期 G2 未启用（reply_gates.yaml classification.enabled=false）",
        },
        "G3_reuse": {
            "verdict": _v("reuse"),
            "composite_score": record.get("reuse_score"),
            "_legacy_note": _legacy_note,
        },
        "G4_specificity": {
            "verdict": _v("specificity"),
            "level": record.get("specificity_level"),
            "_legacy_note": _legacy_note,
        },
        "G5_supervisor": {
            "verdict": _v("supervisor"),
            "score": record.get("supervisor_score"),
            "risk_flags": record.get("risk_flags") or [],
            "rationale": "（v1 记录无 rationale 字段，详见 risk_flags）" if record.get("risk_flags") else "",
        },
    }
    ard = record.get("auto_reply_decision") or {}
    auto_decision = {}
    if ard:
        auto_decision = {
            "composite_confidence": ard.get("composite_score"),
            "threshold_hit": ard.get("action"),
            "action": record.get("final_action", ""),
            "decided_by": "auto_reply_decider",
            "blocked_by": list(record.get("blocked_by") or []),
        }
    return {
        "version": "v1-legacy",
        "gates": gates,
        "final_action": record.get("final_action", ""),
        "auto_decision": auto_decision,
        "_legacy_synthesized": True,
    }


def get_gate_summary(issue_key: str) -> dict | None:
    """Returns compact gate verdicts for board list mini-badge display."""
    record = get_recent_decision(issue_key)
    if not record:
        return None
    rg = record.get("reply_gateway", {})
    gates = rg.get("gates", {})
    if not gates:
        # backward compat: use old gate_decisions field
        gd = record.get("gate_decisions", {})
        if not gd:
            return None
        return {
            "G1": "pass" if gd.get("completeness") == "passed" else ("fail" if gd.get("completeness") == "blocked" else "skipped"),
            "G2": "pass" if gd.get("classification") == "passed" else ("fail" if gd.get("classification") == "blocked" else "skipped"),
            "G3": "pass" if gd.get("reuse") == "passed" else "skipped",
            "G4": "pass" if gd.get("specificity") == "passed" else "skipped",
            "G5": "pass" if gd.get("supervisor") == "passed" else ("fail" if gd.get("supervisor") == "blocked" else "skipped"),
            "final_action": record.get("final_action", ""),
        }
    return {
        "G1": gates.get("G1_completeness", {}).get("verdict", "skipped"),
        "G2": gates.get("G2_classification", {}).get("verdict", "skipped"),
        "G3": gates.get("G3_reuse", {}).get("verdict", "skipped"),
        "G4": gates.get("G4_specificity", {}).get("verdict", "skipped"),
        "G5": gates.get("G5_supervisor", {}).get("verdict", "skipped"),
        "final_action": rg.get("final_action", record.get("final_action", "")),
        "composite_confidence": rg.get("auto_decision", {}).get("composite_confidence"),
        "display_cards": rg.get("display_cards", []),
    }
