#!/usr/bin/env python3
"""
首部署种子脚本：拉取所有 allowed_project_keys 的近 N 月历史工单写入 Chroma。

用法:
  docker compose exec aiticket-api python -m scripts.seed_projects [--days 180]
  python3 APP/backend/scripts/seed_projects.py [--days 180] [--project KEY]

在首次部署后、用户正式使用前运行，确保智能回复的"相似工单"召回有数据支撑。
运行时间约 5–30 分钟（视 Jira 工单量和网络延迟）。

QCL 守卫：AITICKET_ROLE=qcl 时自动退出（QCL 只读副本，数据由 Mini rsync 填充）。
"""
import argparse
import os
import subprocess
import sys
import time

BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(BACKEND))

if os.environ.get("AITICKET_ROLE", "").lower() == "qcl":
    print("[seed_projects] QCL 是只读副本，无需运行种子脚本（数据由 Mini rsync 填充）")
    sys.exit(0)

from config.loader import cfg
from scripts.incremental_issues_index import run_for_project


def _get_project_keys(override: str | None = None) -> list[str]:
    if override:
        return [override.upper()]
    keys = cfg("instance", "allowed_project_keys") or []
    primary = cfg("instance", "primary_project_key") or ""
    if not keys and primary:
        keys = [primary]
    return [k for k in keys if k]


def main():
    parser = argparse.ArgumentParser(
        description="首部署种子：将 allowed_project_keys 的历史工单写入 Chroma"
    )
    parser.add_argument("--days", type=int, default=180,
                        help="回溯天数（默认 180）")
    parser.add_argument("--project", type=str, default=None,
                        help="只处理指定项目（默认处理所有 allowed_project_keys）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印，不写入 Chroma")
    args = parser.parse_args()

    project_keys = _get_project_keys(args.project)
    if not project_keys:
        print("[seed_projects] 错误：deployment.yaml 未配置 allowed_project_keys 或 primary_project_key")
        print("  请先编辑 APP/backend/config/deployment.yaml 填写项目配置")
        sys.exit(1)

    print(f"[seed_projects] 将处理 {len(project_keys)} 个项目：{project_keys}")
    print(f"[seed_projects] 回溯天数：{args.days}，dry_run={args.dry_run}")
    print()

    # ── preflight：修复 max_seq_id（防止新写入被立即 purge）──────────────────
    # server 模式由 daemon 管理 WAL，跳过直接操作 SQLite
    if os.environ.get("CHROMA_MODE", "").lower() != "server" and not args.dry_run:
        print("[seed_projects] preflight: 检查 ChromaDB max_seq_id...")
        fix_script = os.path.join(os.path.dirname(__file__), "fix_chroma_max_seq_id.py")
        ret = subprocess.run(
            [sys.executable, fix_script, "--fix", "--yes"],
            capture_output=False,
        )
        if ret.returncode not in (0, 2):  # 0=已健康 2=已修复，其他=错误
            print(f"[seed_projects] ❌ max_seq_id 修复失败（exit={ret.returncode}），中止")
            sys.exit(1)
    # ──────────────────────────────────────────────────────────────────────────

    total_added = 0
    failed = []
    for pk in project_keys:
        print(f"{'─' * 50}")
        t0 = time.time()
        try:
            n = run_for_project(pk, days=args.days, dry_run=args.dry_run)
            elapsed = time.time() - t0
            print(f"[seed_projects] [{pk}] 完成：新增 {n} 条，耗时 {elapsed:.1f}s")
            total_added += n
        except Exception as e:
            print(f"[seed_projects] [{pk}] 失败：{e}")
            failed.append(pk)

    print()
    print(f"{'═' * 50}")
    print(f"[seed_projects] 汇总：{len(project_keys)} 个项目，新增 {total_added} 条工单索引")
    if failed:
        print(f"[seed_projects] 失败项目：{failed}（可单独重跑：--project <KEY>）")
    else:
        print("[seed_projects] 全部成功 ✓")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
