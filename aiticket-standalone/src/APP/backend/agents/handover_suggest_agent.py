"""
HandoverSuggestAgent — 智能转交 Agent

职责：
- 同步快速路径：基于团队历史操作规则即时给出智能转交对象和跨看板目标
- 异步精修路径：run_task 调用 LLM，将精修结果写回缓存（由端点触发子任务）

数据来源：
  data/handover_patterns.json   — 周聚合产物（first-choice fast-path）
  data/operation_history.jsonl  — 原始事件流（fallback 或首次运行）

冷启动策略：history < 50 条时 transfer_suggestion 返回 null，
move_suggestion 返回 board_config 中的通用候选（不做个性化）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional, TYPE_CHECKING

from agents.base import AgentStatus, AgentTask, BaseAgent
from agents.self_monitor_mixin import AgentSelfMonitorMixin

if TYPE_CHECKING:
    from board_service_chroma import BoardService

_DATA_DIR = os.path.join(os.path.dirname(__file__), "../data")
_JSONL_PATH = os.path.join(_DATA_DIR, "operation_history.jsonl")
_PATTERNS_PATH = os.path.join(_DATA_DIR, "handover_patterns.json")
_CACHE_PATH = os.path.join(_DATA_DIR, "handover_suggestion_cache.json")
_COLD_START_THRESHOLD = 50
_CACHE_TTL_HOURS = 24


class HandoverSuggestAgent(AgentSelfMonitorMixin, BaseAgent):
    expected_run_interval_hours: float = 168
    name = "handover_suggest"
    display_name = "智能转交 Agent"
    description = "基于团队历史操作行为，为看板卡片智能推荐转交对象与目标看板"
    version = "1.0"
    hidden = True
    tags = ["子任务", "智能回复"]
    parent_agent = "reply"

    def __init__(self, board_service: "BoardService"):
        self._svc = board_service

    # ── BaseAgent 合约 ────────────────────────────────────────────────

    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "capabilities": self.list_capabilities(),
        }

    def list_capabilities(self) -> List[str]:
        return ["rule-aggregate", "llm-refine", "feature-match", "cold-start-fallback"]

    def health_check(self) -> dict:
        event_count = self._count_events()
        patterns_exist = os.path.exists(_PATTERNS_PATH)
        cold = event_count < _COLD_START_THRESHOLD
        return {
            "healthy": True,
            "detail": f"事件 {event_count} 条 | 聚合规则={'已就绪' if patterns_exist else '待生成'} | {'冷启动模式' if cold else '正常模式'}",
        }

    def run_task(self, task: AgentTask) -> Optional[dict]:
        """异步 LLM 精修任务。支持批量（payload.issues 列表）和单条（payload.issue_key）两种模式。"""
        import json as _j
        payload = _j.loads(task.payload_json or "{}")

        # 批量模式：来自看板预加载端点
        issues_list = payload.get("issues", [])
        if issues_list:
            return self._run_batch(task, issues_list)

        # 单条模式（兼容手动触发）
        issue_key: str = payload.get("issue_key", "")
        issue_meta: dict = payload.get("issue_meta", {})
        if not issue_key:
            return {"error": "missing issue_key"}

        self.report_progress(task.id, 10, "加载历史规则")
        patterns = self._load_patterns()
        rule = self._rule_based_suggest(issue_meta, patterns)

        self.report_progress(task.id, 40, "LLM 精修中")
        try:
            llm_cfg = self._get_llm_config()
            if llm_cfg.get("provider", "none") != "none":
                result = self._llm_refine(issue_key, issue_meta, rule, llm_cfg)
                result["stage"] = "llm"
            else:
                result = rule
                result["stage"] = "rule"
        except Exception as exc:
            self.append_log(task.id, f"LLM 精修失败，使用规则结果: {exc}")
            result = rule
            result["stage"] = "rule"

        content_hash = _content_hash(issue_meta)
        self._set_cache(issue_key, content_hash, result)
        self.report_progress(task.id, 100, "完成")
        return result

    def _run_batch(self, task: AgentTask, issues_list: list) -> dict:
        """批量精修：逐条 LLM 调用，结果写缓存，最终以 {items:[...]} 返回。"""
        self.report_progress(task.id, 5, "加载历史规则")
        patterns = self._load_patterns()
        llm_cfg = self._get_llm_config()
        has_llm = llm_cfg.get("provider", "none") != "none"

        items = []
        total = len(issues_list)
        for i, issue_data in enumerate(issues_list):
            issue_key = issue_data.get("key", "")
            if not issue_key:
                continue
            rule = self._rule_based_suggest(issue_data, patterns)
            if has_llm:
                try:
                    refined = self._llm_refine(issue_key, issue_data, rule, llm_cfg)
                    refined["stage"] = "llm"
                    result = refined
                except Exception as exc:
                    self.append_log(task.id, f"{issue_key} LLM失败: {exc}")
                    result = {**rule, "stage": "rule"}
            else:
                result = {**rule, "stage": "rule"}
            result["issue_key"] = issue_key
            self._set_cache(issue_key, _content_hash(issue_data), result)
            items.append(result)
            self.report_progress(task.id, int((i + 1) / total * 100), f"已处理 {i+1}/{total}")

        return {"items": items}

    # ── 公开 API（端点直接调用）──────────────────────────────────────

    def get_suggestion_sync(self, issue_key: str, issue_meta: dict) -> dict:
        """规则即时路径，不阻塞。约 < 50ms。"""
        content_hash = _content_hash(issue_meta)
        cached = self._get_cache(issue_key, content_hash)
        if cached:
            return cached

        patterns = self._load_patterns()
        result = self._rule_based_suggest(issue_meta, patterns)
        result["stage"] = "rule"
        result["issue_key"] = issue_key
        # 规则结果不写缓存（等 LLM 任务完成后再写）
        return result

    # ── 规则聚合 ─────────────────────────────────────────────────────

    def _load_patterns(self) -> dict:
        """优先读聚合文件，没有则实时扫 JSONL（首次运行兜底）。"""
        if os.path.exists(_PATTERNS_PATH):
            try:
                with open(_PATTERNS_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return _aggregate_jsonl_live(_JSONL_PATH)

    def _rule_based_suggest(self, issue_meta: dict, patterns: dict) -> dict:
        """
        按 (module, customer, product_version, project_key) 四维特征在历史模式中匹配，
        返回最高频的移交对象 + 跨项目目标。
        """
        event_count = patterns.get("_event_count", 0)
        if event_count < _COLD_START_THRESHOLD:
            return _cold_start_result(event_count)

        key = _feature_key(issue_meta)
        transfer_data = patterns.get("transfer", {}).get(key) or {}
        move_data = patterns.get("move_jira", {}).get(key) or {}

        transfer_suggestion = _pick_top(transfer_data, field="to_assignee")
        move_suggestion = _pick_top(move_data, field="to_project_key",
                                    meta=patterns.get("project_names", {}))
        return {
            "transfer_suggestion": transfer_suggestion,
            "move_suggestion": move_suggestion,
        }

    # ── LLM 精修 ─────────────────────────────────────────────────────

    def _llm_refine(self, issue_key: str, issue_meta: dict, rule_baseline: dict, llm_cfg: dict) -> dict:
        prompt = _build_refine_prompt(issue_key, issue_meta, rule_baseline)
        raw = self._svc.llm_service.call_llm(
            prompt,
            api_key=llm_cfg.get("api_key", ""),
            provider=llm_cfg.get("provider", "minimax"),
            model_name=llm_cfg.get("model_name", ""),
            base_url=llm_cfg.get("base_url", ""),
            max_tokens=512,
        )
        return _parse_llm_result(raw, rule_baseline)

    def _get_llm_config(self) -> dict:
        try:
            # 延迟导入避免循环依赖
            from main import resolve_feature_llm_runtime
            return resolve_feature_llm_runtime("handover_suggest")
        except Exception:
            return {"provider": "none"}

    # ── 缓存 ─────────────────────────────────────────────────────────

    def _get_cache(self, issue_key: str, content_hash: str) -> Optional[dict]:
        try:
            if not os.path.exists(_CACHE_PATH):
                return None
            with open(_CACHE_PATH, encoding="utf-8") as f:
                cache = json.load(f)
            entry = cache.get(issue_key)
            if not entry:
                return None
            if entry.get("hash") != content_hash:
                return None
            ts = datetime.fromisoformat(entry["ts"])
            if datetime.utcnow() - ts > timedelta(hours=_CACHE_TTL_HOURS):
                return None
            return entry["data"]
        except Exception:
            return None

    def _set_cache(self, issue_key: str, content_hash: str, data: dict):
        try:
            cache = {}
            if os.path.exists(_CACHE_PATH):
                with open(_CACHE_PATH, encoding="utf-8") as f:
                    cache = json.load(f)
            cache[issue_key] = {
                "hash": content_hash,
                "ts": datetime.utcnow().isoformat(),
                "data": data,
            }
            # 保留最近 500 条
            if len(cache) > 500:
                oldest = sorted(cache.items(), key=lambda x: x[1].get("ts", ""))
                cache = dict(oldest[-500:])
            os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
            with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[HandoverSuggestAgent] cache write failed: {exc}")

    def _count_events(self) -> int:
        if not os.path.exists(_JSONL_PATH):
            return 0
        try:
            with open(_JSONL_PATH, encoding="utf-8") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0


# ── 模块级工具函数 ───────────────────────────────────────────────────

def _feature_key(issue_meta: dict) -> str:
    parts = [
        (issue_meta.get("module") or "").strip(),
        (issue_meta.get("customer") or "").strip(),
        (issue_meta.get("product_version") or "").strip(),
        (issue_meta.get("project_key") or issue_meta.get("key", "").split("-")[0]).strip(),
    ]
    return "||".join(parts)


def _content_hash(issue_meta: dict) -> str:
    s = json.dumps(issue_meta, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(s.encode()).hexdigest()[:12]


def _cold_start_result(event_count: int) -> dict:
    return {
        "transfer_suggestion": None,
        "move_suggestion": None,
        "cold_start": True,
        "event_count": event_count,
        "message": f"数据积累中（{event_count}/{_COLD_START_THRESHOLD} 条）",
    }


def _pick_top(freq_dict: dict, *, field: str, meta: dict = None) -> Optional[dict]:
    if not freq_dict:
        return None
    top = max(freq_dict.items(), key=lambda kv: kv[1]["count"])
    val, stats = top
    total = sum(v["count"] for v in freq_dict.values())
    confidence = round(stats["count"] / total, 2) if total else 0.0
    if confidence < 0.3:
        return None
    result = {field: val, "confidence": confidence, "count": stats["count"]}
    if meta and val in meta:
        result["display_name"] = meta[val]
    return result


def _aggregate_jsonl_live(path: str) -> dict:
    """实时扫描 JSONL，返回与 handover_patterns.json 相同结构（首次运行兜底）。"""
    transfer: dict = {}
    move_jira: dict = {}
    project_names: dict = {}
    count = 0
    if not os.path.exists(path):
        return {"_event_count": 0, "transfer": {}, "move_jira": {}, "project_names": {}}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                count += 1
                key = "||".join([
                    ev.get("module", ""), ev.get("customer", ""),
                    ev.get("product_version", ""), ev.get("from_project_key", ""),
                ])
                etype = ev.get("event_type", "")
                if etype == "transfer":
                    target = ev.get("to_assignee", "")
                    if target:
                        transfer.setdefault(key, {})
                        transfer[key].setdefault(target, {"count": 0})
                        transfer[key][target]["count"] += 1
                elif etype == "move_jira":
                    target = ev.get("to_project_key", "")
                    if target:
                        move_jira.setdefault(key, {})
                        move_jira[key].setdefault(target, {"count": 0})
                        move_jira[key][target]["count"] += 1
    except Exception:
        pass
    return {
        "_event_count": count,
        "transfer": transfer,
        "move_jira": move_jira,
        "project_names": project_names,
    }


def _build_refine_prompt(issue_key: str, issue_meta: dict, rule_baseline: dict) -> str:
    t = rule_baseline.get("transfer_suggestion") or {}
    m = rule_baseline.get("move_suggestion") or {}
    return f"""你是一个工单路由助手。请根据下列工单信息，优化"移交建议"和"移动建议"。

【工单信息】
- key: {issue_key}
- 标题: {issue_meta.get('summary', '')}
- 模块: {issue_meta.get('module', '')}
- 客户: {issue_meta.get('customer', '')}
- 版本: {issue_meta.get('product_version', '')}

【规则基线】
- 移交建议: {json.dumps(t, ensure_ascii=False)}
- 移动建议: {json.dumps(m, ensure_ascii=False)}

请严格返回如下 JSON（不要 Markdown 代码块标记）：
{{
  "transfer_suggestion": {{"assignee": "姓名", "confidence": 0.85, "reason": "简短理由（20字内）"}},
  "move_suggestion": {{"project_key": "KEY", "project_name": "项目名", "confidence": 0.80, "reason": "简短理由（20字内）"}}
}}
如果无法给出建议，对应字段填 null。"""


def _parse_llm_result(raw: str, fallback: dict) -> dict:
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return fallback
        data = json.loads(m.group(0))
        return {
            "transfer_suggestion": data.get("transfer_suggestion"),
            "move_suggestion": data.get("move_suggestion"),
        }
    except Exception:
        return fallback
