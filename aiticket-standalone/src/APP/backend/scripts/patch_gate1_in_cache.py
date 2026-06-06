"""一次性脚本：扫 reply_cache 富 entry，重算 G1 节点写回。
仅对 G1_completeness.error == "'list' object has no attribute 'get'" 的 entry 生效。
不重跑 G2-5，不重生回复正文，零 LLM 调用，秒级完成。
用法：
    cd APP/backend && python3 scripts/patch_gate1_in_cache.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from reply_cache_service import CACHE_FILE, _cache_write_lock  # noqa: E402
from services.reply_gateway import ReplyGateway  # noqa: E402

SENTINEL_ERR = "'list' object has no attribute 'get'"


def _extract_ticket_meta(issue_key: str, entry: dict) -> dict:
    """从 cache entry 中提取 G1 所需的 ticket_meta，无需查 DB。"""
    ai = entry.get("ai_analysis") or {}
    return {
        "project": ai.get("project_name") or ai.get("project", ""),
        "issue_type": ai.get("issue_type", ""),
        "description": ai.get("issue_description", ""),
        "summary": ai.get("issue_title", ""),
        "product_version": ai.get("product_version", ""),
    }


def main() -> None:
    if not os.path.exists(CACHE_FILE):
        print(f"[patch_gate1] cache file not found: {CACHE_FILE}")
        return

    with open(CACHE_FILE, encoding="utf-8") as f:
        cache: dict = json.load(f)

    gw = ReplyGateway()
    patched = 0
    skipped = 0
    errored = 0

    for issue_key, entry in list(cache.items()):
        rg = entry.get("reply_gateway") or {}
        g1 = (rg.get("gates") or {}).get("G1_completeness") or {}
        if g1.get("error") != SENTINEL_ERR:
            skipped += 1
            continue
        try:
            ticket_meta = _extract_ticket_meta(issue_key, entry)
            new_g1 = gw._run_g1(issue_key, ticket_meta)
            entry["reply_gateway"]["gates"]["G1_completeness"] = new_g1
            print(f"  [{issue_key}] G1: {new_g1.get('verdict')} rule={new_g1.get('rule_matched','')}")
            patched += 1
        except Exception as e:
            print(f"  [{issue_key}] error: {e}")
            errored += 1

    if patched == 0 and errored == 0:
        print(f"[patch_gate1] nothing to patch (skipped={skipped})")
        return

    if patched > 0:
        with _cache_write_lock:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)

    print(f"[patch_gate1] Done. patched={patched} skipped={skipped} errored={errored}")


if __name__ == "__main__":
    main()
