"""
离线双盲评估：模块感知智能回复效果

用法：
  python eval_module_aware_reply.py [--since 30d] [--sample 50] [--by-project MYPROJECT]

输出：
  - 控制台报告：by_project / by_module 采纳率对比
  - conclusion/_local/eval/module_aware_eval_<date>.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent.parent
BACKEND = BASE / "APP" / "backend"
sys.path.insert(0, str(BACKEND))

FEEDBACK_LOG = BACKEND / "data" / "reply_trainer" / "feedback_log.jsonl"
OUTPUT_DIR = BASE / "conclusion" / "_local" / "eval"


def parse_since(s: str) -> datetime:
    if s.endswith("d"):
        return datetime.now() - timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return datetime.now() - timedelta(hours=int(s[:-1]))
    return datetime.fromisoformat(s)


def load_feedback(since: datetime, only_project: str = "") -> list[dict]:
    if not FEEDBACK_LOG.exists():
        print(f"[EvalModuleAware] 反馈日志不存在: {FEEDBACK_LOG}")
        return []
    entries = []
    for line in FEEDBACK_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        ts = e.get("ts", "")
        try:
            if datetime.fromisoformat(ts) < since:
                continue
        except Exception:
            continue
        if only_project and e.get("project_key", "") not in (only_project, ""):
            continue
        # Only count live feedback (has ai_original)
        if not e.get("ai_original"):
            continue
        entries.append(e)
    return entries


def bucket_stats(entries: list[dict], key: str) -> dict:
    stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "adopted": 0})
    for e in entries:
        k = e.get(key) or "_unknown"
        stats[k]["total"] += 1
        if e.get("adopted"):
            stats[k]["adopted"] += 1
    result = {}
    for k, v in stats.items():
        t = v["total"]
        a = v["adopted"]
        result[k] = {
            "total": t,
            "adopted": a,
            "adoption_rate": f"{a/t*100:.1f}%" if t else "N/A",
        }
    return dict(sorted(result.items(), key=lambda x: -x[1]["total"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="30d", help="时间窗口，如 30d / 7d / 2026-04-01")
    parser.add_argument("--sample", type=int, default=0, help="限制样本数（0=全部）")
    parser.add_argument("--by-project", default="", dest="by_project", help="只看某个项目，如 MYPROJECT")
    args = parser.parse_args()

    since = parse_since(args.since)
    print(f"[EvalModuleAware] 评估窗口: {since.strftime('%Y-%m-%d')} ~ 现在  过滤项目: {args.by_project or '全部'}")

    entries = load_feedback(since, args.by_project)
    if args.sample and len(entries) > args.sample:
        entries = entries[-args.sample:]

    if not entries:
        print("[EvalModuleAware] 无满足条件的样本，退出")
        return

    print(f"\n共 {len(entries)} 条实时反馈样本\n")

    # By project
    print("=" * 50)
    print("按项目分桶")
    print("=" * 50)
    by_proj = bucket_stats(entries, "project_key")
    for proj, v in by_proj.items():
        print(f"  {proj:12s}  总计={v['total']:4d}  采纳={v['adopted']:3d}  采纳率={v['adoption_rate']}")

    # By module
    module_entries = [e for e in entries if e.get("module_l2")]
    if module_entries:
        print(f"\n{'=' * 50}")
        print("按模块分桶（仅含 module_l2 字段的样本）")
        print("=" * 50)
        by_mod = bucket_stats(module_entries, "module_l2")
        for mod, v in by_mod.items():
            print(f"  {mod:20s}  总计={v['total']:4d}  采纳={v['adopted']:3d}  采纳率={v['adoption_rate']}")
    else:
        print("\n[EvalModuleAware] 暂无 module_l2 数据（新反馈才会带此字段）")

    # Overall
    total = len(entries)
    adopted = sum(1 for e in entries if e.get("adopted"))
    overall_rate = f"{adopted/total*100:.1f}%" if total else "N/A"
    print(f"\n整体采纳率: {overall_rate}  ({adopted}/{total})")
    print(f"基线对比: 2.7%（上线前）→ 目标 4.5%（4 周）→ 目标 8%（12 周）\n")

    # Save
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / f"module_aware_eval_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    report = {
        "generated_at": datetime.now().isoformat(),
        "since": since.isoformat(),
        "sample_count": total,
        "overall_adoption_rate": overall_rate,
        "by_project": by_proj,
        "by_module": bucket_stats(module_entries, "module_l2") if module_entries else {},
    }
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[EvalModuleAware] 报告已写入: {out_file}")


if __name__ == "__main__":
    main()
