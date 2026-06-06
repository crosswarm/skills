"""
reply_diff_analyzer.py — 从人工修改的 AI 回复中提炼产品知识

每次客服修改 AI 回复后，自动分析修改类型：
  - style_fix:      措辞/结构调整，AI 内容正确但表达有问题
  - knowledge_fix:  AI 说了错误或缺失的产品知识，人工纠正了
  - coverage_gap:   AI 回答太模糊，KB 没有检索到正确内容

对于 knowledge_fix，提炼产品事实写入 product_facts.md，形成知识闭环。

LLM 降级链：local (SuperGemma4) → minimax → zhipu
通过 llm_feature_routing.json 的 diff_analyzer 字段路由，未配置时按链序遍历。
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="diff-analyzer")

BACKEND_DIR = Path(__file__).resolve().parent
PRODUCT_FACTS_PATH = BACKEND_DIR / "data" / "product_facts.md"
LLM_CONFIG_PATH = BACKEND_DIR / "llm_config.json"
LLM_ROUTING_PATH = BACKEND_DIR / "llm_feature_routing.json"
KB_GAPS_PATH = BACKEND_DIR.parent.parent / "conclusion" / "kb_gaps.jsonl"

# local 优先：分类任务轻量，本地模型够用；本地不通再走 minimax → zhipu
LLM_FALLBACK_CHAIN = ["local", "minimax", "zhipu"]

# 运行时 LLM 限制（--llm-only 参数设置，None=使用降级链）
_LLM_ONLY_PROVIDER: Optional[str] = None

_CLASSIFY_PROMPT_SYSTEM = (
    "你是工单回复质量分析专家，负责分析客服对 AI 回复的修改原因。"
    "只输出 JSON，不要任何解释或 markdown 格式。"
)

_CLASSIFY_PROMPT_USER = """\
【AI原始回复】
{ai_original}

【人工修改后】
{user_final}

【工单摘要】{ticket_summary}
【问题类型】{issue_type}

判断本次修改的主要原因（只选一个）：
- style_fix：仅措辞/结构调整，AI 的产品信息本身正确
- knowledge_fix：AI 包含错误或缺失的产品知识，人工纠正了具体产品信息
- coverage_gap：AI 回答太模糊笼统，缺少具体操作路径/参数（KB 未检索到）

如果是 knowledge_fix，提炼核心产品事实（1-2 句，直接陈述产品机制/限制/操作方式）。

输出 JSON：
{{"correction_type":"style_fix|knowledge_fix|coverage_gap","product_fact":"（仅knowledge_fix填写）","affected_topic":"领域名(5字以内)","confidence":0.0}}"""

_PRODUCT_FACTS_HEADER = """\
# 产品知识摘要

> 自动生成，来源：客服对 AI 回复的修改中提炼的产品事实
> 每条格式：- 事实内容（来源：工单号，日期）
>
> 此文件由 reply_diff_analyzer.py 维护，请勿手动删除条目

"""


# ── LLM 多层降级 ────────────────────────────────────────────────────────────

def _load_all_providers() -> dict:
    try:
        with open(LLM_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_llm(provider_name: str) -> dict:
    cfg = _load_all_providers()
    p = cfg.get(provider_name, {})
    return {
        "name": provider_name,
        "api_key": p.get("api_key", ""),
        "model": p.get("model_name", ""),
        "base_url": p.get("base_url", ""),
    }


def _routing_chain() -> list[str]:
    """读取 llm_feature_routing.json 中 diff_analyzer 的配置，返回降级链。"""
    if _LLM_ONLY_PROVIDER:
        return [_LLM_ONLY_PROVIDER]
    try:
        with open(LLM_ROUTING_PATH, encoding="utf-8") as f:
            routing = json.load(f)
        preferred = routing.get("diff_analyzer") or routing.get("_default")
        if preferred:
            chain = [preferred] + [p for p in LLM_FALLBACK_CHAIN if p != preferred]
            return chain
    except Exception:
        pass
    return list(LLM_FALLBACK_CHAIN)


def _try_provider(llm: dict, messages: list, max_tokens: int = 800) -> tuple[str, Optional[str]]:
    """单次调用，返回 (content, error_or_None)。"""
    base_url = llm["base_url"] or "https://api.openai.com/v1"
    try:
        r = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json"},
            json={"model": llm["model"], "messages": messages, "max_tokens": max_tokens,
                  "temperature": 0.1},
            timeout=60,
            proxies={"http": None, "https": None},
        )
        if r.status_code != 200:
            return "", f"HTTP {r.status_code}: {r.text[:150]}"
        d = r.json()
        if "error" in d:
            return "", str(d["error"])[:150]
        msg = d.get("choices", [{}])[0].get("message", {})
        content = msg.get("content", "") or msg.get("reasoning_content", "") or ""
        return (content, None) if content else ("", "empty response")
    except Exception as e:
        return "", str(e)[:150]


def _call_llm_with_fallback(user_prompt: str) -> Optional[str]:
    """按降级链依次尝试，返回第一个成功的响应文本。"""
    messages = [
        {"role": "system", "content": _CLASSIFY_PROMPT_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]
    chain = _routing_chain()
    errors = []
    for provider_name in chain:
        if provider_name == "local":
            try:
                from services.local_llm_lifecycle import ensure_running as _ensure_local
                if not _ensure_local():
                    print("[DiffAnalyzer] local LLM 三次自启失败，切到下一个 provider")
                    continue
            except Exception as _e:
                print(f"[DiffAnalyzer] local preflight 异常: {_e}，继续尝试")
        llm = _load_llm(provider_name)
        if not llm["api_key"]:
            continue
        content, err = _try_provider(llm, messages)
        if content:
            if errors:
                print(f"[DiffAnalyzer] 降级成功 → {provider_name} (之前失败: {len(errors)} 个)")
            return content
        errors.append(f"{provider_name}: {err}")
        print(f"[DiffAnalyzer] {provider_name} 失败: {err[:80]}")
    print(f"[DiffAnalyzer] 所有 LLM 均失败: {errors}")
    return None


# ── 结果解析 ────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> Optional[dict]:
    try:
        m = re.search(r"\{.*\}", text or "", re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    return None


# ── product_facts.md 写入 ────────────────────────────────────────────────────

_facts_lock = threading.Lock()


def _append_product_fact(fact: dict, issue_key: str) -> None:
    topic = (fact.get("affected_topic") or "通用").strip()
    text = (fact.get("product_fact") or "").strip()
    if not text:
        return

    date_str = datetime.now().strftime("%Y-%m-%d")
    entry = f"- {text}（来源：{issue_key}，{date_str}）\n"

    with _facts_lock:
        if not PRODUCT_FACTS_PATH.exists():
            PRODUCT_FACTS_PATH.write_text(_PRODUCT_FACTS_HEADER, encoding="utf-8")

        content = PRODUCT_FACTS_PATH.read_text(encoding="utf-8")
        section_header = f"\n## {topic}\n"
        if section_header in content:
            pos = content.index(section_header) + len(section_header)
            content = content[:pos] + entry + content[pos:]
        else:
            content = content.rstrip("\n") + f"\n\n## {topic}\n{entry}"
        PRODUCT_FACTS_PATH.write_text(content, encoding="utf-8")

    print(f"[DiffAnalyzer] 新增产品事实 [{topic}]: {text[:70]}")


# ── 核心分析 ────────────────────────────────────────────────────────────────

def analyze(
    ai_original: str,
    user_final: str,
    issue_key: str = "",
    ticket_summary: str = "",
    issue_type: str = "",
) -> dict:
    """分析一条修改，返回分类结果（同步，供批处理和测试）。"""
    if not ai_original or not user_final:
        return {"correction_type": "style_fix", "confidence": 0.0, "skipped": "no_content"}
    if ai_original.strip() == user_final.strip():
        return {"correction_type": "style_fix", "confidence": 1.0, "skipped": "identical"}

    prompt = _CLASSIFY_PROMPT_USER.format(
        ai_original=ai_original[:800],
        user_final=user_final[:800],
        ticket_summary=(ticket_summary or "")[:200],
        issue_type=issue_type or "",
    )

    raw = _call_llm_with_fallback(prompt)
    result = _parse_json(raw) if raw else None

    if not result:
        return {"correction_type": "style_fix", "confidence": 0.0, "llm_failed": True}

    correction_type = result.get("correction_type", "style_fix")
    confidence = float(result.get("confidence", 0.0))

    if correction_type == "knowledge_fix" and confidence >= 0.6 and result.get("product_fact"):
        _append_product_fact(result, issue_key)
        try:
            from kb_auto_import import get_auto_import
            _auto_import = get_auto_import()
            if _auto_import:
                _auto_import.extract_and_save(
                    result["product_fact"],
                    source_context={
                        "type": "knowledge_correction",
                        "ref_id": issue_key,
                        "issue_type": issue_type,
                    },
                )
        except Exception:
            pass
    elif correction_type == "coverage_gap":
        _log_kb_gap(issue_key, result.get("affected_topic", ""), ai_original, user_final)

    return result


def _log_kb_gap(issue_key: str, topic: str, ai_reply: str, human_reply: str) -> None:
    """记录 coverage_gap 到 kb_gaps.jsonl，供 JobMaster 聚合 KB 缺口报告。"""
    try:
        KB_GAPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps({
            "ts": datetime.now().isoformat(),
            "issue_key": issue_key,
            "topic_inferred": topic,
            "ai_reply": ai_reply[:300],
            "human_reply": human_reply[:300],
        }, ensure_ascii=False)
        with open(KB_GAPS_PATH, "a", encoding="utf-8") as f:
            f.write(record + "\n")
    except Exception as e:
        print(f"[DiffAnalyzer] kb_gaps 写入失败: {e}")


def analyze_async(
    ai_original: str,
    user_final: str,
    issue_key: str = "",
    ticket_summary: str = "",
    issue_type: str = "",
) -> None:
    """异步版本，提交到线程池，不阻塞主流程。"""
    _EXECUTOR.submit(analyze, ai_original, user_final, issue_key, ticket_summary, issue_type)


# ── 模块级单例接口 ───────────────────────────────────────────────────────────

class _ModuleProxy:
    def analyze(self, *args, **kwargs):
        return analyze(*args, **kwargs)

    def analyze_async(self, *args, **kwargs):
        return analyze_async(*args, **kwargs)


_module_proxy = _ModuleProxy()


def get_diff_analyzer():
    return _module_proxy


# ── CLI：批处理 backfill ─────────────────────────────────────────────────────

def _run_backfill(input_path: str, limit: int = 0, dry_run: bool = False) -> dict:
    from collections import Counter, defaultdict

    with open(input_path, encoding="utf-8") as f:
        records = [json.loads(l) for l in f if l.strip()]

    modified = [r for r in records if not r.get("adopted") and r.get("ai_original")]
    if limit:
        modified = modified[:limit]

    print(f"[Backfill] 共 {len(modified)} 条 modified 样本（限制：{limit or '无'}）")

    counter: Counter = Counter()
    topic_facts: defaultdict = defaultdict(list)
    results = []

    for i, rec in enumerate(modified, 1):
        print(f"[Backfill] {i}/{len(modified)} {rec.get('issue_key', '?')}")
        if dry_run:
            result = {"correction_type": "style_fix", "confidence": 0.5, "dry_run": True}
        else:
            result = analyze(
                ai_original=rec.get("ai_original", ""),
                user_final=rec.get("user_final", ""),
                issue_key=rec.get("issue_key", ""),
                ticket_summary=rec.get("ticket_summary", ""),
                issue_type=rec.get("issue_type", ""),
            )

        result["issue_key"] = rec.get("issue_key", "")
        result["ticket_summary"] = rec.get("ticket_summary", "")[:80]
        results.append(result)

        ct = result.get("correction_type", "unknown")
        counter[ct] += 1
        if ct == "knowledge_fix" and result.get("product_fact"):
            topic_facts[result.get("affected_topic", "通用")].append({
                "issue_key": rec.get("issue_key"),
                "fact": result["product_fact"],
                "confidence": result.get("confidence", 0),
            })

    total = len(modified)
    report = {
        "total": total,
        "counts": dict(counter),
        "percentages": {k: round(v / total * 100, 1) for k, v in counter.items()} if total else {},
        "topic_facts": dict(topic_facts),
        "sample_results": results[:20],
    }

    print(f"\n{'='*55}")
    print(f"  Backfill 质量验证报告 — {total} 条样本")
    print(f"{'='*55}")
    for ct, cnt in counter.most_common():
        print(f"  {ct:20s}: {cnt:3d} 条 ({cnt/total*100:.1f}%)" if total else f"  {ct}: {cnt}")
    print(f"\n  提炼的产品事实（按领域）：")
    for topic, facts in topic_facts.items():
        print(f"  [{topic}] {len(facts)} 条")
        for f in facts[:2]:
            print(f"    ↳ {f['fact'][:75]} (置信={f['confidence']:.2f})")
    print(f"{'='*55}\n")

    return report


if __name__ == "__main__":
    import argparse
    import os
    import sys

    sys.path.insert(0, str(BACKEND_DIR))

    parser = argparse.ArgumentParser(description="Reply Diff Analyzer — 批处理工具")
    parser.add_argument("--backfill", action="store_true", help="对 feedback_log.jsonl 跑批")
    parser.add_argument("--input", default="APP/backend/data/reply_trainer/feedback_log.jsonl")
    parser.add_argument("--limit", type=int, default=0, help="限制处理条数（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="不实际调用 LLM，验证流程")
    parser.add_argument("--output", default="", help="报告输出路径（JSON）")
    parser.add_argument("--llm-only", default="", metavar="PROVIDER",
                        help="强制只用指定 LLM（如 local），禁用降级链")
    args = parser.parse_args()

    if args.llm_only:
        import reply_diff_analyzer as _self
        _self._LLM_ONLY_PROVIDER = args.llm_only
        print(f"[DiffAnalyzer] LLM 限定为: {args.llm_only}")

    if args.backfill:
        path = args.input
        if not os.path.isabs(path):
            path = str(BACKEND_DIR.parent.parent / path)
        report = _run_backfill(path, limit=args.limit, dry_run=args.dry_run)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"报告已保存: {args.output}")
    else:
        parser.print_help()
