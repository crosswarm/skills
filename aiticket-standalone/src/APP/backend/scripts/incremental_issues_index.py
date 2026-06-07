#!/usr/bin/env python3
"""
增量工单向量索引脚本 (MS-4-b)

从 Jira 拉取最近更新的工单，将尚未在 Chroma issues_collection 里的条目补充写入，
避免新工单永远匹配不到的问题。遍历 deployment.yaml 中所有 allowed_project_keys。

用法:
  python3 APP/backend/scripts/incremental_issues_index.py [--days N] [--project KEY] [--dry-run]
"""
import argparse
import os
import sys

BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(BACKEND))

from config.loader import cfg
from _chroma_paths import open_ticket_vector_store
from jira_service import jira_service


def _get_project_keys(project_override: str | None = None) -> list[str]:
    if project_override:
        return [project_override.upper()]
    keys = cfg("instance", "allowed_project_keys") or []
    primary = cfg("instance", "primary_project_key") or ""
    if not keys and primary:
        keys = [primary]
    return [k for k in keys if k]


def run_for_project(project_key: str, days: int = 2, dry_run: bool = False,
                    jira_client=None, progress_cb=None) -> int:
    """拉取最近 `days` 天更新的工单并把未入库的写入向量库。

    jira_client: 显式传入的 JiraService（compact 历史导入用绑定会话的客户端）；
                 不传则用全局 jira_service。
    progress_cb(scanned:int, total:int): 每页回调，供进度上报。
    分页拉全量（12 个月数据可能上千条），逐页写入。
    """
    client = jira_client if jira_client is not None else jira_service
    print(f"[IncrementalIndex] [{project_key}] 拉取最近 {days} 天更新的工单...")

    vs = open_ticket_vector_store(allow_download=False)
    jql = f'project = "{project_key}" AND updated >= -{days}d ORDER BY updated DESC'
    page = 200
    start = 0
    total: int | None = None
    added_total = 0

    while True:
        try:
            result = client.search_issues_rest_api(jql, start_at=start, max_results=page)
        except Exception as e:
            print(f"[IncrementalIndex] [{project_key}] Jira 查询失败(start={start}): {e}")
            break
        if not isinstance(result, dict) or "error" in result:
            print(f"[IncrementalIndex] [{project_key}] Jira 返回错误: {result.get('error') if isinstance(result, dict) else result}")
            break
        if total is None:
            total = int(result.get("total", 0) or 0)
        issues = result.get("issues", [])
        if not issues:
            break

        to_add = []
        for issue in issues:
            key = issue.get("key", "")
            if not key or vs.get_issue_by_key(key):
                continue
            fields = issue.get("fields", {})
            summary = fields.get("summary", "")
            description = (fields.get("description") or summary)[:500]
            to_add.append({
                "key": key, "summary": summary, "description": description,
                "project_key": project_key, "source": "jira_incremental",
            })

        if to_add and not dry_run:
            vs.batch_add_issues(to_add)
        added_total += len(to_add)
        start += len(issues)
        if progress_cb:
            try:
                progress_cb(min(start, total or start), total or start)
            except Exception:
                pass
        print(f"[IncrementalIndex] [{project_key}] 进度 {start}/{total or '?'}，本页新增 {len(to_add)}")
        if (total and start >= total) or len(issues) < page:
            break

    print(f"[IncrementalIndex] [{project_key}] 完成，共新增 {added_total} 条（扫描 {start}/{total or start}）")
    return added_total


def run(days: int = 2, project_override: str | None = None, dry_run: bool = False) -> int:
    project_keys = _get_project_keys(project_override)
    if not project_keys:
        print("[IncrementalIndex] 警告：未配置任何项目（allowed_project_keys 为空），跳过")
        return 0
    total = 0
    for pk in project_keys:
        total += run_for_project(pk, days=days, dry_run=dry_run)
    return total


def run_for_provider(provider, dry_run: bool = False) -> int:
    """Generic incremental sync for non-Jira providers (dist branch).

    Re-fetches all issues from the provider, checks which keys are already
    indexed in Chroma, and adds only the new ones.
    """
    print(f"[IncrementalIndex] [{provider.name}] 拉取全量工单...")
    vs = open_ticket_vector_store(allow_download=False)
    try:
        issues = provider.fetch_all()
    except Exception as e:
        print(f"[IncrementalIndex] [{provider.name}] 拉取失败: {e}")
        return 0

    to_add = [i for i in issues if not vs.get_issue_by_key(i.key)]
    if not to_add:
        print(f"[IncrementalIndex] [{provider.name}] 所有工单均已入库")
        return 0

    print(f"[IncrementalIndex] [{provider.name}] 补充写入 {len(to_add)} 条: "
          f"{[i.key for i in to_add[:5]]}")
    if not dry_run:
        vs.batch_add_generic_issues(to_add)
        print(f"[IncrementalIndex] [{provider.name}] 完成")
    else:
        print(f"[IncrementalIndex] [{provider.name}] dry-run，跳过实际写入")
    return len(to_add)



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2, help="拉取最近 N 天的工单（默认 2）")
    parser.add_argument("--project", type=str, default=None, help="指定单个项目 key（默认遍历所有 allowed_project_keys）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写入 Chroma")
    args = parser.parse_args()
    n = run(days=args.days, project_override=args.project, dry_run=args.dry_run)
    print(f"[IncrementalIndex] 总计新增 {n} 条")
