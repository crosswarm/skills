#!/usr/bin/env python3
"""
KB 事实抽取器 — 从已编译的 KB 综合解析中提取 operational facts

运行：
  python extract_facts_from_kb.py            # 全量，使用本地 LLM
  python extract_facts_from_kb.py --dry-run  # 不写入，只打印
  python extract_facts_from_kb.py --llm-only local  # 强制本地（默认）

JobMaster 调度：weekly-fact-extraction.json（周日 03:00）
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent.parent
DB_PATH = PROJECT_ROOT / "data" / "sqlite" / "kb_chunks.db"
PRODUCT_FACTS_PATH = BACKEND_DIR / "data" / "product_facts.md"
LLM_CONFIG_PATH = BACKEND_DIR / "llm_config.json"
LOG_PATH = PROJECT_ROOT / "conclusion" / "facts_extraction_log.jsonl"

EXTRACT_PROMPT = """\
请从以下产品能力解析中提取 3-8 条 operational fact。
要求：客服回复工单时可以直接引用的客观事实，如支持/不支持/默认行为/限制/触发条件/参数范围。

格式：每条以"- "开头，末尾加"（来源：{source_label}）"。
跳过：主观评价、改进建议、使用场景描述、功能介绍性文字、操作步骤指南。

KB内容：
{content}
"""


def _load_llm(provider: str) -> dict:
    cfg = json.loads(LLM_CONFIG_PATH.read_text(encoding="utf-8"))
    p = cfg.get(provider, {})
    return {
        "api_key": p.get("api_key", ""),
        "model": p.get("model_name", ""),
        "base_url": p.get("base_url", ""),
    }


def _call_llm(llm: dict, prompt: str) -> str:
    session = requests.Session()
    base_url = llm["base_url"]
    if "localhost" in base_url or "127.0.0.1" in base_url:
        session.trust_env = False  # bypass Surge SOCKS (all_proxy) for local LLM
    r = session.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json"},
        json={"model": llm["model"], "messages": [{"role": "user", "content": prompt}],
              "max_tokens": 1024, "temperature": 0.1},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _load_existing_hashes() -> set:
    if not PRODUCT_FACTS_PATH.exists():
        return set()
    hashes = set()
    for line in PRODUCT_FACTS_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("- "):
            hashes.add(_sha1(line.strip()))
    return hashes


def _append_facts(topic: str, facts: list[str]) -> int:
    if not facts:
        return 0
    PRODUCT_FACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PRODUCT_FACTS_PATH.exists():
        PRODUCT_FACTS_PATH.write_text(
            "# 产品知识摘要\n\n> 自动生成，由 extract_facts_from_kb.py 维护\n\n",
            encoding="utf-8",
        )
    content = PRODUCT_FACTS_PATH.read_text(encoding="utf-8")
    header = f"\n## {topic}\n"
    entries = "".join(f"- {f}\n" for f in facts)
    if header in content:
        pos = content.index(header) + len(header)
        content = content[:pos] + entries + content[pos:]
    else:
        content = content.rstrip("\n") + f"{header}{entries}"
    PRODUCT_FACTS_PATH.write_text(content, encoding="utf-8")
    return len(facts)


def _load_kb_articles(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        SELECT d.content_id, d.name,
               GROUP_CONCAT(c.chunk_text, '\n') AS full_text
        FROM documents d
        JOIN chunks c ON c.content_id = d.content_id
        WHERE d.source_kind = 'kb_compiled'
        GROUP BY d.content_id, d.name
        ORDER BY d.name
    """)
    rows = cur.fetchall()
    conn.close()
    return [{"content_id": r[0], "name": r[1], "text": r[2] or ""} for r in rows]


def _parse_facts(llm_output: str) -> list[str]:
    facts = []
    for line in llm_output.splitlines():
        line = line.strip()
        if line.startswith("- ") and "（来源：" in line:
            facts.append(line[2:])
    return facts


def run(llm_provider: str = "local", dry_run: bool = False) -> dict:
    print(f"[KB-Facts] 开始抽取，LLM={llm_provider}，dry_run={dry_run}")
    llm = _load_llm(llm_provider)
    if not llm["api_key"]:
        print(f"[KB-Facts] 错误：LLM provider '{llm_provider}' 未配置")
        sys.exit(1)

    articles = _load_kb_articles(DB_PATH)
    print(f"[KB-Facts] 加载 {len(articles)} 篇 kb_compiled 文章")

    existing = _load_existing_hashes()
    total_new = 0
    total_dedup = 0
    total_failed = 0
    t0 = time.time()

    for i, art in enumerate(articles, 1):
        name = art["name"].replace("综合解析：", "").strip()
        source_label = f"KB-{art['content_id'][-8:]}"
        print(f"[KB-Facts] [{i}/{len(articles)}] {name}")

        if not art["text"].strip():
            print(f"  → 跳过（内容为空）")
            continue

        prompt = EXTRACT_PROMPT.format(
            source_label=source_label,
            content=art["text"][:4000],
        )

        if dry_run:
            print(f"  → dry-run，跳过 LLM 调用")
            continue

        try:
            output = _call_llm(llm, prompt)
            facts_raw = _parse_facts(output)
            new_facts = []
            for f in facts_raw:
                h = _sha1(f)
                if h not in existing:
                    existing.add(h)
                    new_facts.append(f)
                else:
                    total_dedup += 1
            written = _append_facts(name, new_facts)
            total_new += written
            print(f"  → +{written} 条（去重 {len(facts_raw) - written} 条）")
        except Exception as e:
            print(f"  → 失败: {e}")
            total_failed += 1

        time.sleep(0.5)

    elapsed = int(time.time() - t0)
    stats = {
        "ts": datetime.now().isoformat(),
        "llm_provider": llm_provider,
        "articles": len(articles),
        "new_facts": total_new,
        "dedup_skipped": total_dedup,
        "failed": total_failed,
        "elapsed_s": elapsed,
        "dry_run": dry_run,
    }

    if not dry_run:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(stats, ensure_ascii=False) + "\n")

    print(f"\n[KB-Facts] 完成 — 新增 {total_new} 条，去重 {total_dedup}，失败 {total_failed}，耗时 {elapsed}s")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KB 事实抽取器")
    parser.add_argument("--llm-only", default="local", metavar="PROVIDER")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(llm_provider=args.llm_only, dry_run=args.dry_run)
