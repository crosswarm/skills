#!/usr/bin/env python3
"""
MS-4-c: 全量重建 issues 向量集合（含 description 嵌入）

将 Jira 缓存中的工单重新写入 issues_collection，
此次包含真实 description（修复原先 conclusion 来源只嵌入标题的问题）。
建议夜间运行，约 12110 条耗时 ~30-60 min。

用法:
  python3 scripts/rebuild_issues_v2.py [--limit N] [--dry-run]
"""
import argparse
import os
import sys
import time

BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(BACKEND))

from _chroma_paths import open_ticket_vector_store
from jira_cache_service import get_jira_cache_service


def run(limit: int = 0, dry_run: bool = False):
    print("[RebuildIssuesV2] 开始全量重建 issues 向量集合（含 description）...")
    vs = open_ticket_vector_store(allow_download=True)

    # 自检：确认路径和 embedding 维度
    ef = vs.embedding_func
    if ef is not None:
        try:
            dim = len(ef(["test"])[0])
            print(f"[RebuildIssuesV2] persist_directory={vs.persist_directory}  embedding_dim={dim}")
            assert dim == 384, f"embedding 维度 {dim} != 384，终止"
        except AssertionError:
            raise
        except Exception as e:
            print(f"[RebuildIssuesV2] ⚠️ 维度自检跳过: {e}")
    else:
        print(f"[RebuildIssuesV2] persist_directory={vs.persist_directory}  embedding_func=None")
    svc = get_jira_cache_service()

    start_at = 0
    batch_size = 100
    total_added = 0
    total_skipped = 0
    total_updated = 0

    while True:
        jql = "project=MYPROJECT ORDER BY updated DESC"
        try:
            result = svc.search_issues(jql, start_at=start_at, max_results=batch_size)
        except Exception as e:
            print(f"[RebuildIssuesV2] Jira 查询失败 (start_at={start_at}): {e}")
            break

        issues = result.get("issues", []) if isinstance(result, dict) else []
        if not issues:
            break

        to_add = []
        for issue in issues:
            key = issue.get("key", "")
            if not key:
                continue
            fields = issue.get("fields", {})
            summary = fields.get("summary", "")
            description = (fields.get("description") or "")[:800]

            existing = vs.get_issue_by_key(key)
            if existing:
                existing_doc = existing.get("documents", [""])[0] if isinstance(existing, dict) else ""
                if description and description not in existing_doc:
                    # description 更丰富，值得更新
                    to_add.append({
                        "key": key,
                        "summary": summary,
                        "description": description,
                        "source": "jira_rebuild_v2",
                    })
                    total_updated += 1
                else:
                    total_skipped += 1
            else:
                to_add.append({
                    "key": key,
                    "summary": summary,
                    "description": description or summary,
                    "source": "jira_rebuild_v2",
                })
                total_added += 1

        if to_add and not dry_run:
            vs.batch_add_issues(to_add)
            print(f"[RebuildIssuesV2] batch start={start_at}: +{total_added} new / {total_updated} updated / {total_skipped} skip")

        start_at += batch_size
        if limit and start_at >= limit:
            break

        time.sleep(0.5)  # 限流 Jira

    print(f"[RebuildIssuesV2] 完成：新增 {total_added}，更新 {total_updated}，跳过 {total_skipped}")
    return total_added + total_updated


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条（0=全量）")
    parser.add_argument("--dry-run", action="store_true", help="不写入，只统计")
    args = parser.parse_args()
    run(limit=args.limit, dry_run=args.dry_run)
