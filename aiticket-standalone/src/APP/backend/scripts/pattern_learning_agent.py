"""
移动/分配特征学习反馈 Agent

分析 move_history.json + auto_move_log.json（近 90 天），
提取路由关键词模式、swimlane 信号、assignee 规律，
结果写入 data/learned_patterns.json（暂存区），人工审核后可写入路由表。

用法：
  python pattern_learning_agent.py
  python pattern_learning_agent.py --dry-run   # 只打印，不写文件
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"
sys.path.insert(0, str(BACKEND_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("pattern_learning_agent")

MOVE_HISTORY_PATH = DATA_DIR / "move_history.json"
AUTO_MOVE_LOG_PATH = DATA_DIR / "auto_move_log.json"
LEARNED_PATTERNS_PATH = DATA_DIR / "learned_patterns.json"
GATE2_ROUTING_PATH = DATA_DIR / "gate2_routing.json"
LLM_CONFIG_PATH = BACKEND_DIR / "llm_config.json"
LLM_ROUTING_PATH = BACKEND_DIR / "llm_feature_routing.json"

COLD_START_MIN = 100        # 少于此数跳过 LLM 调用
BATCH_SIZE = 50             # 每批最多 N 张票
LOOKBACK_DAYS = 90


# ── LLM 工具 ─────────────────────────────────────────────────────────────────

def _load_llm(provider_name: str) -> dict:
    cfg = json.loads(LLM_CONFIG_PATH.read_text(encoding="utf-8"))
    provider = cfg.get(provider_name, {})
    return {
        "name": provider_name,
        "api_key": provider.get("api_key", ""),
        "model": provider.get("model_name", ""),
        "base_url": provider.get("base_url", ""),
    }


def _try_llm_call(llm: dict, messages: list, max_tokens: int = 3000) -> tuple:
    """返回 (ok: bool, text: str)。"""
    try:
        import requests as _req
        session = _req.Session()
        if "localhost" in llm.get("base_url", "") or "127.0.0.1" in llm.get("base_url", ""):
            session.trust_env = False  # 绕过 Surge 代理
        r = session.post(
            f"{llm['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {llm['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": llm["model"],
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
            timeout=120,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        return True, text
    except Exception as exc:
        logger.warning("[llm] %s 调用失败: %s", llm.get("name"), exc)
        return False, ""


def _pick_provider() -> str:
    """根据 llm_feature_routing.json 或时间窗选 provider。"""
    routing: dict = {}
    if LLM_ROUTING_PATH.exists():
        try:
            routing = json.loads(LLM_ROUTING_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    provider = routing.get("pattern_learning") or routing.get("_default") or ""
    if not provider:
        # 夜间用 local，白天降级到 minimax
        hour = datetime.now().hour
        provider = "local" if (hour < 9 or hour >= 21) else "minimax"
    return provider


def _call_with_fallback(messages: list, max_tokens: int = 3000) -> str:
    """按 daytime_chain 依次尝试 LLM，返回响应文本；全部失败返回空串。"""
    hour = datetime.now().hour
    chain = ["local", "zhipu", "minimax"] if (hour < 9 or hour >= 21) else ["minimax", "zhipu", "local"]

    # preflight for local
    if chain[0] == "local":
        try:
            from services.local_llm_lifecycle import ensure_running, is_alive
            if not is_alive():
                logger.info("[preflight] 启动 SuperGemma4…")
                if not ensure_running():
                    logger.warning("[preflight] 本地模型启动失败，降级")
                    chain = [p for p in chain if p != "local"] + ["local"]
        except Exception:
            chain = [p for p in chain if p != "local"] + ["local"]

    for provider in chain:
        llm = _load_llm(provider)
        if not llm.get("base_url"):
            continue
        ok, text = _try_llm_call(llm, messages, max_tokens)
        if ok and text.strip():
            return text
    return ""


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def _load_json_safe(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取 %s 失败: %s", path, exc)
        return default


def _load_move_entries() -> list[dict]:
    """合并 move_history + auto_move_log，过滤近 90 天，去重。"""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    seen: set[str] = set()
    entries: list[dict] = []

    raw_history = _load_json_safe(MOVE_HISTORY_PATH, [])
    raw_auto = _load_json_safe(AUTO_MOVE_LOG_PATH, {})

    # auto_move_log 可能是 dict 或 list
    if isinstance(raw_auto, dict):
        raw_auto = list(raw_auto.values()) if raw_auto else []

    for item in (raw_history or []) + (raw_auto or []):
        key = item.get("id") or item.get("issue_key", "")
        ts_str = item.get("timestamp") or item.get("ts") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            ts = datetime.now(tz=timezone.utc)

        if ts < cutoff:
            continue
        dedup_key = f"{item.get('issue_key','')}_{item.get('source_board','')}_{item.get('target_board','')}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        entries.append(item)

    return entries


# ── Jira 补全 ─────────────────────────────────────────────────────────────────

def _enrich_with_jira(entries: list[dict]) -> list[dict]:
    """
    对缺少 summary/description 的条目，尝试从 Jira 补全。
    失败则跳过（不阻断整体流程）。
    """
    need_fetch = [e for e in entries if not e.get("summary")]
    if not need_fetch:
        return entries

    try:
        from jira_service import JiraService
        cfg_path = BACKEND_DIR / "jira_config.json"
        if not cfg_path.exists():
            logger.info("[jira] jira_config.json 不存在，跳过补全")
            return entries
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        # 取第一个可用账号
        account = next(iter(cfg.values())) if isinstance(cfg, dict) else cfg
        jira = JiraService(
            base_url=account.get("base_url", ""),
            username=account.get("username", ""),
            api_token=account.get("api_token") or account.get("password", ""),
        )
        for entry in need_fetch:
            issue_key = entry.get("issue_key", "")
            if not issue_key:
                continue
            try:
                issue = jira.get_issue_full(issue_key)
                fields = issue.get("fields", {})
                entry["summary"] = fields.get("summary", "")
                entry["description"] = (fields.get("description") or "")[:300]
                entry["assignee"] = (fields.get("assignee") or {}).get("displayName", "")
            except Exception as exc:
                logger.debug("[jira] %s 补全失败: %s", issue_key, exc)
    except Exception as exc:
        logger.warning("[jira] 初始化失败，跳过补全: %s", exc)

    return entries


# ── LLM 分析 ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是一个工单路由分析专家。给你一批已人工移动过的工单记录，
每条记录包含 issue_key、source_board（来源看板）、target_board（目标看板）、summary（标题）、description（描述节选）。

请分析这批记录，提取：
1. 预测路由的关键词模式
2. swimlane / 子模块信号
3. assignee 分配规律

以如下 JSON 格式输出（不要输出任何其他文字）：
{
  "patterns": [
    {
      "trigger_keywords": ["关键词1", "关键词2"],
      "predicted_project": "模块名称（中文）",
      "target_board_id": "",
      "target_swimlane": "子模块名",
      "default_assignee": "",
      "support_count": <整数，此批中支持该规律的条目数>,
      "confidence": <0.0-1.0>,
      "evidence_tickets": ["ISSUE-KEY-1", "ISSUE-KEY-2"]
    }
  ],
  "classification_few_shot": [
    {
      "snippet": "FLOW-123: 业务流 → 流程中心",
      "reason": "近 14 天高频误判示例",
      "confidence": 0.88
    }
  ]
}
"""

def _build_batch_text(batch: list[dict]) -> str:
    lines = []
    for e in batch:
        parts = [
            f"issue_key={e.get('issue_key','')}",
            f"source={e.get('source_board','')}",
            f"target={e.get('target_board','')}",
        ]
        summary = (e.get("summary") or "").strip()
        desc = (e.get("description") or "").strip()[:200]
        if summary:
            parts.append(f"summary={summary}")
        if desc:
            parts.append(f"desc={desc}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _parse_llm_output(text: str) -> dict:
    """从 LLM 响应中提取 JSON。"""
    text = text.strip()
    # 尝试去除 markdown code fences
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(text)
    except Exception:
        # 尝试找到第一个 { ... } 块
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
    logger.warning("[llm] 无法解析 JSON 响应: %s", text[:200])
    return {}


def _merge_patterns(existing: list[dict], new_batch: list[dict]) -> list[dict]:
    """
    合并新 batch 的模式到已有列表：
    - 同 trigger_keywords 集合 → 合并 support_count，取较高 confidence
    - 否则追加
    """
    merged = list(existing)
    for new in new_batch:
        kw_set = frozenset(new.get("trigger_keywords", []))
        matched = None
        for m in merged:
            if frozenset(m.get("trigger_keywords", [])) == kw_set:
                matched = m
                break
        if matched:
            matched["support_count"] = matched.get("support_count", 0) + new.get("support_count", 0)
            matched["confidence"] = max(matched.get("confidence", 0.0), new.get("confidence", 0.0))
            for ek in new.get("evidence_tickets", []):
                if ek not in matched.setdefault("evidence_tickets", []):
                    matched["evidence_tickets"].append(ek)
        else:
            merged.append(new)
    return merged


# ── 主流程 ────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    logger.info("=== pattern_learning_agent 开始 ===")
    started_at = datetime.now().isoformat()

    entries = _load_move_entries()
    logger.info("近 %d 天移动记录: %d 条", LOOKBACK_DAYS, len(entries))

    # 冷启动保护
    if len(entries) < COLD_START_MIN:
        logger.warning(
            "记录数 %d < 阈值 %d，跳过 LLM 分析，写空结果",
            len(entries), COLD_START_MIN,
        )
        if not dry_run:
            _write_output({
                "version": 1,
                "last_run": started_at,
                "gate2_routing_suggestions": [],
                "gate2_prompt_few_shot_suggestions": [],
                "classification_keyword_patterns": [],
                "assignee_drift_alerts": [],
                "blacklist": _load_blacklist(),
                "_cold_start_skipped": True,
                "_entry_count": len(entries),
            })
        _notify(f"[pattern_learning] 冷启动：记录数 {len(entries)} < {COLD_START_MIN}，已写空结果")
        return

    # Jira 补全缺失摘要
    entries = _enrich_with_jira(entries)

    # 分批 LLM 分析
    all_patterns: list[dict] = []
    all_few_shot: list[dict] = []
    total_batches = (len(entries) + BATCH_SIZE - 1) // BATCH_SIZE
    llm_failures = 0

    for batch_start in range(0, len(entries), BATCH_SIZE):
        batch = entries[batch_start: batch_start + BATCH_SIZE]
        batch_text = _build_batch_text(batch)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"以下是工单移动记录（共 {len(batch)} 条）：\n\n{batch_text}\n\n请按 JSON 格式输出分析结果。"},
        ]
        logger.info("批次 %d-%d / %d — 调用 LLM…", batch_start + 1, batch_start + len(batch), len(entries))
        response_text = _call_with_fallback(messages)
        if not response_text:
            llm_failures += 1
            logger.warning("批次 %d-%d LLM 无响应，跳过", batch_start + 1, batch_start + len(batch))
            continue
        parsed = _parse_llm_output(response_text)
        batch_patterns = parsed.get("patterns", [])
        batch_few_shot = parsed.get("classification_few_shot", [])
        all_patterns = _merge_patterns(all_patterns, batch_patterns)
        all_few_shot.extend(batch_few_shot)
        logger.info("本批提取模式: %d 条，few-shot: %d 条", len(batch_patterns), len(batch_few_shot))

    all_failed = llm_failures > 0 and llm_failures == total_batches
    if all_failed:
        logger.warning("[pattern_learning] 所有批次 LLM 均无响应")

    # 去重 few-shot
    seen_snippets: set[str] = set()
    deduped_few_shot = []
    for fs in all_few_shot:
        snippet = fs.get("snippet", "")
        if snippet and snippet not in seen_snippets:
            seen_snippets.add(snippet)
            deduped_few_shot.append(fs)

    blacklist = _load_blacklist()
    output = {
        "version": 1,
        "last_run": started_at,
        "gate2_routing_suggestions": all_patterns,
        "gate2_prompt_few_shot_suggestions": deduped_few_shot,
        "classification_keyword_patterns": all_patterns,
        "assignee_drift_alerts": [],
        "blacklist": blacklist,
        "_entry_count": len(entries),
        "_all_batches_failed": all_failed,
    }

    if dry_run:
        logger.info("[dry-run] 结果预览:\n%s", json.dumps(output, ensure_ascii=False, indent=2)[:2000])
    else:
        _write_output(output)
        logger.info("已写入 %s", LEARNED_PATTERNS_PATH)

    if all_failed:
        summary = (
            f"[pattern_learning] LLM 全量失败 — "
            f"{total_batches} 个批次均无响应，"
            f"基于 {len(entries)} 条移动记录"
        )
    else:
        summary = (
            f"[pattern_learning] 分析完成 — "
            f"路由建议: {len(all_patterns)} 条，"
            f"few-shot: {len(deduped_few_shot)} 条，"
            f"基于 {len(entries)} 条移动记录"
        )
    logger.info(summary)
    _notify(summary)

    # 随手关灯
    try:
        from services.local_llm_lifecycle import shutdown_if_started_by_us
        shutdown_if_started_by_us("pattern_learning")
    except Exception:
        pass

    logger.info("=== pattern_learning_agent 完成 ===")


def _load_blacklist() -> list:
    data = _load_json_safe(LEARNED_PATTERNS_PATH, {})
    return data.get("blacklist", []) if isinstance(data, dict) else []


def _write_output(data: dict) -> None:
    LEARNED_PATTERNS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _notify(msg: str) -> None:
    try:
        from services.feishu_notifier import get_notifier
        get_notifier().send_message(msg)
    except Exception:
        logger.debug("[notify] 飞书通知失败（非致命）")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="移动/分配特征学习反馈 Agent")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    args = parser.parse_args()
    try:
        run(dry_run=args.dry_run)
    except Exception:
        logger.error("未捕获异常:\n%s", traceback.format_exc())
        sys.exit(1)
