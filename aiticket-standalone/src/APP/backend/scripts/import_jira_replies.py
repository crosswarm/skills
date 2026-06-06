#!/usr/bin/env python3
"""
本地 CSV 导入脚本 — 冷启动回复训练器

从 src/ 目录的离线工单 CSV 读取解决方案字段，导入到 ReplyTrainer 的 KB 中。
- 所有经办人的解决方案都导入（作为答案库）
- 指定用户的回复额外标记 style_owner=True（用于风格学习）

用法:
    conda activate antigravity
    cd APP/backend
    python scripts/import_jira_replies.py [--max 0] [--owner qiangxiao] [--dry-run]
"""

import argparse
import csv
import glob
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from reply_trainer import ReplyTrainer

PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "../../.."))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")


def load_csv_replies(src_dir: str, owner: str, max_count: int = 0):
    """从 src/ 目录所有 CSV 读取有解决方案的工单"""
    files = sorted(glob.glob(os.path.join(src_dir, "*.csv")))
    seen_keys = set()
    items = []

    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    key = row.get("问题关键字", "").strip()
                    if not key or key in seen_keys:
                        continue

                    solution = (row.get("自定义字段(解决方案)") or "").strip()
                    if not solution or len(solution) < 20:
                        continue

                    seen_keys.add(key)
                    assignee = row.get("经办人", "").strip()

                    items.append({
                        "issue_key": key,
                        "summary": row.get("概要", "").strip(),
                        "description": "",  # CSV 无详细描述，用概要代替
                        "reply": solution,
                        "reply_method": row.get("自定义字段(回复方式)", "").strip(),
                        "issue_type": row.get("自定义字段(研发确认问题类型)", "").strip(),
                        "assignee": assignee,
                        "is_owner": assignee.lower() == owner.lower(),
                    })

                    if max_count and len(items) >= max_count:
                        return items
        except Exception as e:
            print(f"  跳过 {os.path.basename(f)}: {e}")

    return items


def main():
    parser = argparse.ArgumentParser(description="从本地 CSV 导入解决方案到回复训练器")
    parser.add_argument("--max", type=int, default=0, help="最多导入条数（0=全部）")
    parser.add_argument("--owner", type=str, default="qiangxiao", help="风格学习目标用户")
    parser.add_argument("--dry-run", action="store_true", help="只统计不导入")
    args = parser.parse_args()

    print("=" * 60)
    print("本地 CSV → 回复训练器")
    print(f"数据目录: {SRC_DIR}")
    print(f"风格学习用户: {args.owner}")
    print("=" * 60)

    items = load_csv_replies(SRC_DIR, args.owner, args.max)

    owner_count = sum(1 for i in items if i["is_owner"])
    others_count = len(items) - owner_count

    print(f"\n可导入数据:")
    print(f"  总计: {len(items)} 条（去重后）")
    print(f"  {args.owner} 的回复: {owner_count} 条（标记 style_owner）")
    print(f"  其他人的回复: {others_count} 条（仅作答案参考）")

    if args.dry_run:
        print("\n[dry-run] 前 5 条预览:")
        for item in items[:5]:
            tag = "★" if item["is_owner"] else " "
            print(f"  {tag} {item['issue_key']} ({item['assignee']}): {item['summary'][:50]}")
            print(f"    方案: {item['reply'][:80]}...")
            print(f"    回复方式: {item['reply_method']} | 类型: {item['issue_type']}")
        print(f"\n[dry-run] 结束")
        return

    if not items:
        print("无可导入数据")
        return

    trainer = ReplyTrainer()
    print(f"\n开始导入...")
    chunk_count = trainer.bulk_import(items)
    stats = trainer.get_stats()

    print(f"\n导入完成!")
    print(f"  - 导入回复: {len(items)} 条")
    print(f"  - chunk 数: {chunk_count}")
    print(f"  - 训练器统计: {stats}")


if __name__ == "__main__":
    main()
