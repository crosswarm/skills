"""一次性脚本：清理 reply_cache 中 generation_method='reuse_direct' 且
reuse_score>1.0（Bug A 遗留）或 reply_content 与描述高度重叠（Bug B 遗留）的 entry。
下次访问按修复后的 Gate3 逻辑重算，不动 Jira、不动索引。
用法：
    先停止 uvicorn，再运行：
    cd APP/backend && python3 scripts/patch_reuse_direct_in_cache.py
"""
import difflib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from reply_cache_service import CACHE_FILE, _cache_write_lock  # noqa: E402

BATCH_SIZE = 50  # 防止重启后批量重生成打爆 LLM


def _is_polluted(reply_content: str, description: str) -> bool:
    if not description or not reply_content:
        return False
    return difflib.SequenceMatcher(None, reply_content[:500], description[:500]).ratio() > 0.4


def main() -> None:
    if not os.path.exists(CACHE_FILE):
        print(f"[patch_reuse] cache file not found: {CACHE_FILE}")
        return

    with open(CACHE_FILE, encoding="utf-8") as f:
        cache: dict = json.load(f)

    drop = []
    for key, entry in cache.items():
        if entry.get("generation_method") != "reuse_direct":
            continue
        reuse_score = float(entry.get("reuse_score") or 0)
        content = entry.get("reply_content") or ""
        ai = entry.get("ai_analysis") or {}
        desc = ai.get("issue_description") or ""
        score_bad = reuse_score > 1.0
        polluted = _is_polluted(content, desc)
        if score_bad or polluted:
            drop.append((key, reuse_score, polluted))

    if not drop:
        print("[patch_reuse] nothing to drop (no Bug A/B entries found)")
        return

    print(f"[patch_reuse] will drop {len(drop)} entries in batches of {BATCH_SIZE}")
    for batch_start in range(0, len(drop), BATCH_SIZE):
        batch = drop[batch_start: batch_start + BATCH_SIZE]
        for key, score, polluted in batch:
            print(f"  drop {key}: reuse_score={score:.3f} polluted={polluted}")
            cache.pop(key, None)
        with _cache_write_lock:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"  flushed batch {batch_start // BATCH_SIZE + 1}")

    print(f"[patch_reuse] done. {len(drop)} entries dropped.")


if __name__ == "__main__":
    main()
