#!/usr/bin/env python3
"""
采纳反推抽取 — 从 feedback_log.jsonl 中提取客服修改 AI 回复的规律

运行：
  python extract_facts_from_adopted.py            # 全量，本地 LLM
  python extract_facts_from_adopted.py --dry-run  # 不写文件，只打印
  python extract_facts_from_adopted.py --limit 50 # 限制处理条数

输出：APP/backend/data/adopted_facts_pending.json（未审核区，供人工或 jobmaster 合并）
JobMaster 调度：weekly-adopted-extract.json（每日 03:30）
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BACKEND = Path(__file__).resolve().parent.parent
FEEDBACK_PATH = BACKEND / "data" / "reply_trainer" / "feedback_log.jsonl"
OUT_PATH = BACKEND / "data" / "adopted_facts_pending.json"
LLM_CFG_PATH = BACKEND / "llm_config.json"

PROMPT = """\
以下是一条客服采纳了 AI 回复、但做出修改的样例。
请分析客服的修改意图，抽取出一条可复用的 operational guideline（回复规则/偏好/用语习惯）。

工单摘要：{summary}
AI 原始回复：{ai}
客服最终回复：{final}

只输出一条规则，格式严格为：
- <规则正文>（来源：采纳反推 {issue_key}）

不要输出其他内容。"""


def _load_llm(provider: str) -> dict:
    cfg = json.loads(LLM_CFG_PATH.read_text(encoding="utf-8"))
    p = cfg.get(provider, {})
    return {
        "api_key": p.get("api_key", ""),
        "model": p.get("model_name", ""),
        "base_url": p.get("base_url", ""),
    }


def _call_llm(llm: dict, prompt: str) -> str:
    r = requests.post(
        f"{llm['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json"},
        json={"model": llm["model"], "messages": [{"role": "user", "content": prompt}],
              "max_tokens": 256, "temperature": 0.1},
        timeout=30,
        proxies={"http": None, "https": None},
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def load_modified_pairs(limit: int = 200) -> list[dict]:
    """读取 feedback_log.jsonl，只保留 adopted=True 且用户做了修改的条目"""
    pairs = []
    if not FEEDBACK_PATH.exists():
        print(f"[adopted-facts] 数据文件不存在: {FEEDBACK_PATH}", file=sys.stderr)
        return pairs
    lines = FEEDBACK_PATH.read_text(encoding="utf-8").splitlines()
    for line in lines[-limit * 4:]:  # 扫描末尾区段
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("adopted") and d.get("ai_original") and d.get("user_final"):
            if d["ai_original"].strip() != d["user_final"].strip():
                pairs.append(d)
        if len(pairs) >= limit:
            break
    return pairs


def run(llm_provider: str = "local", dry_run: bool = False, limit: int = 50) -> dict:
    print(f"[adopted-facts] 开始抽取，LLM={llm_provider}，dry_run={dry_run}，limit={limit}")
    llm = _load_llm(llm_provider)
    if not llm["api_key"]:
        print(f"[adopted-facts] 错误：LLM provider '{llm_provider}' 未配置", file=sys.stderr)
        sys.exit(1)

    pairs = load_modified_pairs(limit * 4)
    print(f"[adopted-facts] 找到 {len(pairs)} 条修改样例（取前 {limit} 条处理）")
    pairs = pairs[:limit]

    rules = []
    failed = 0
    t0 = time.time()

    for i, p in enumerate(pairs, 1):
        issue_key = p.get("issue_key", f"#{i}")
        print(f"[adopted-facts] [{i}/{len(pairs)}] {issue_key}")

        prompt = PROMPT.format(
            summary=p.get("ticket_summary", "")[:200],
            ai=p.get("ai_original", "")[:400],
            final=p.get("user_final", "")[:400],
            issue_key=issue_key,
        )

        if dry_run:
            print(f"  → dry-run，跳过 LLM 调用")
            continue

        try:
            output = _call_llm(llm, prompt).strip()
            line = next((l.strip() for l in output.splitlines() if l.strip().startswith("- ")), None)
            if line:
                rules.append({
                    "issue_key": issue_key,
                    "rule": line,
                    "ts": p.get("ts"),
                    "issue_type": p.get("issue_type"),
                })
                print(f"  → {line[:80]}")
            else:
                print(f"  → 无有效规则输出")
        except Exception as e:
            print(f"  → 失败: {e}")
            failed += 1

        time.sleep(0.3)

    elapsed = int(time.time() - t0)
    result = {
        "generated_at": datetime.now().isoformat(),
        "source": str(FEEDBACK_PATH.relative_to(BACKEND)),
        "llm_provider": llm_provider,
        "total_pairs": len(pairs),
        "rules_extracted": len(rules),
        "failed": failed,
        "elapsed_s": elapsed,
        "rules": rules,
    }

    if dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[adopted-facts] ✓ 写入 {len(rules)} 条规则 → {OUT_PATH}")

    return result


def main():
    ap = argparse.ArgumentParser(description="采纳反推规则抽取")
    ap.add_argument("--llm-only", dest="llm_provider", default="local",
                    help="LLM provider（默认 local）")
    ap.add_argument("--dry-run", action="store_true", help="不写文件，只打印")
    ap.add_argument("--limit", type=int, default=50, help="最多处理条数（默认 50）")
    args = ap.parse_args()
    result = run(args.llm_provider, args.dry_run, args.limit)
    print(f"[adopted-facts] 完成：{result['rules_extracted']} 条规则，"
          f"失败 {result['failed']} 条，耗时 {result['elapsed_s']}s")


if __name__ == "__main__":
    main()
