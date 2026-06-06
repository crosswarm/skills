#!/usr/bin/env python3
"""
智能回复相似度评分器
新评分体系：
  - 直接采纳：similarity ≥ 85%（AI回复基本正确，轻微润色）
  - 部分采纳：60% ≤ similarity < 85%（AI方向正确，人工补充/调整）
  - 未采纳：similarity < 60%（AI回复不可用，人工重写）

目标：直接采纳 ≥ 15%，部分采纳 ≥ 50%（含直接采纳）

运行：
  python reply_similarity_scorer.py          # 全量历史分析
  python reply_similarity_scorer.py --update # 分析 + 更新 reply_feedback.json
"""
import sys, os, json, argparse
from pathlib import Path
from difflib import SequenceMatcher
from datetime import datetime
from collections import defaultdict

BACKEND_DIR = Path(__file__).resolve().parent.parent
FEEDBACK_LOG = BACKEND_DIR / "data" / "reply_trainer" / "feedback_log.jsonl"
REPLY_FEEDBACK = BACKEND_DIR / "data" / "reply_feedback.json"

DIRECT_THRESHOLD = 0.85       # 直接采纳
PARTIAL_THRESHOLD = 0.60      # 部分采纳
TARGET_DIRECT = 0.15          # 目标：直接采纳 ≥ 15%
TARGET_PARTIAL_ONLY = 0.50    # 目标：部分采纳（仅60-85%，不含直接采纳）≥ 50%


def similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    a, b = a.strip(), b.strip()
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def classify(sim: float) -> str:
    if sim >= DIRECT_THRESHOLD:
        return "直接采纳"
    elif sim >= PARTIAL_THRESHOLD:
        return "部分采纳"
    else:
        return "未采纳"


def load_records():
    if not FEEDBACK_LOG.exists():
        return []
    records = []
    with open(FEEDBACK_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    return records


def analyze(records):
    results = []
    monthly = defaultdict(lambda: {"direct": 0, "partial_only": 0, "none": 0, "total": 0})

    for rec in records:
        ai = rec.get("ai_original", "")
        human = rec.get("user_final", "")
        sim = similarity(ai, human)
        label = classify(sim)
        ts = rec.get("ts", "")
        month = ts[:7] if ts else "unknown"

        results.append({
            "issue_key": rec.get("issue_key", ""),
            "ts": ts,
            "month": month,
            "sim": round(sim, 4),
            "label": label,
            "ai_len": len(ai),
            "human_len": len(human),
        })

        monthly[month]["total"] += 1
        if label == "直接采纳":
            monthly[month]["direct"] += 1
        elif label == "部分采纳":
            monthly[month]["partial_only"] += 1

    return results, monthly


def print_report(results, monthly):
    n = len(results)
    if n == 0:
        print("无记录"); return

    direct = sum(1 for r in results if r["label"] == "直接采纳")
    partial_only = sum(1 for r in results if r["label"] == "部分采纳")
    total_partial = direct + partial_only  # 直接 + 部分
    none_count = sum(1 for r in results if r["label"] == "未采纳")

    direct_rate = direct / n
    total_partial_rate = total_partial / n
    avg_sim = sum(r["sim"] for r in results) / n

    print("=" * 60)
    print("  智能回复质量评分报告（新评分体系）")
    print(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    print(f"\n📊 全量汇总（共 {n} 条记录）")
    print(f"  平均相似度:   {avg_sim:.1%}")
    partial_only_rate = partial_only / n
    print(f"  直接采纳(≥85%): {direct:3d} 条 / {direct_rate:.1%} {'✅' if direct_rate >= TARGET_DIRECT else '❌ 目标15%'}")
    print(f"  部分采纳(60-85%): {partial_only:3d} 条 / {partial_only_rate:.1%} {'✅' if partial_only_rate >= TARGET_PARTIAL_ONLY else '❌ 目标50%'}")
    print(f"  合计采纳(≥60%): {total_partial:3d} 条 / {total_partial_rate:.1%}")
    print(f"  未采纳(<60%):  {none_count:3d} 条 / {none_count/n:.1%}")

    print(f"\n📅 月度趋势")
    print(f"  {'月份':<10} {'总数':>4} {'直接%':>7} {'合计采纳%':>10} {'达标'}")
    print(f"  {'-'*10} {'-'*4} {'-'*7} {'-'*10} {'-'*4}")
    for month in sorted(monthly.keys()):
        m = monthly[month]
        t = m["total"]
        dr = m["direct"] / t
        pr = m["partial_only"] / t
        ok_d = "✅" if dr >= TARGET_DIRECT else "❌"
        ok_p = "✅" if pr >= TARGET_PARTIAL_ONLY else "❌"
        print(f"  {month:<10} {t:>4} {dr:>6.1%}  {pr:>9.1%}  {ok_d}{ok_p}")

    print(f"\n📋 相似度分布")
    bins = [(0.9, 1.01, "≥90%"), (0.85, 0.9, "85-90%"),
            (0.7, 0.85, "70-85%"), (0.6, 0.7, "60-70%"),
            (0.4, 0.6, "40-60%"), (0, 0.4, "<40%")]
    for lo, hi, label in bins:
        cnt = sum(1 for r in results if lo <= r["sim"] < hi)
        bar = "█" * int(cnt / n * 40)
        print(f"  {label:>8}: {cnt:3d} {bar}")

    print(f"\n📌 结论")
    if direct_rate >= TARGET_DIRECT and partial_only_rate >= TARGET_PARTIAL_ONLY:
        print("  ✅ 直接采纳和部分采纳均达标")
    else:
        if direct_rate < TARGET_DIRECT:
            gap = int((TARGET_DIRECT - direct_rate) * n)
            print(f"  ❌ 直接采纳缺口: 需再提高 {gap} 条（当前{direct_rate:.1%}，目标15%）")
        if partial_only_rate < TARGET_PARTIAL_ONLY:
            gap = int((TARGET_PARTIAL_ONLY - partial_only_rate) * n)
            print(f"  ❌ 部分采纳缺口: 需再提高 {gap} 条（当前{partial_only_rate:.1%}，目标50%）")

    return {
        "total": n, "direct": direct, "partial_only": partial_only,
        "total_partial": total_partial, "none": none_count,
        "direct_rate": round(direct_rate, 4),
        "partial_only_rate": round(partial_only_rate, 4),
        "total_partial_rate": round(total_partial_rate, 4),
        "avg_similarity": round(avg_sim, 4),
    }


def update_feedback_json(stats):
    data = {}
    if REPLY_FEEDBACK.exists():
        with open(REPLY_FEEDBACK, encoding="utf-8") as f:
            data = json.load(f)

    data["sim_total"] = stats["total"]
    data["sim_direct"] = stats["direct"]
    data["sim_partial_only"] = stats["partial_only"]
    data["sim_total_partial"] = stats["total_partial"]
    data["sim_none"] = stats["none"]
    data["sim_direct_rate"] = f"{stats['direct_rate']:.1%}"
    data["sim_partial_only_rate"] = f"{stats['partial_only_rate']:.1%}"
    data["sim_total_partial_rate"] = f"{stats['total_partial_rate']:.1%}"
    data["sim_avg_similarity"] = f"{stats['avg_similarity']:.1%}"
    data["sim_target_direct"] = "15%"
    data["sim_target_partial_only"] = "50%"
    data["sim_last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(REPLY_FEEDBACK, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已更新 {REPLY_FEEDBACK.name}")


def main():
    parser = argparse.ArgumentParser(description="智能回复相似度分析")
    parser.add_argument("--update", action="store_true", help="更新 reply_feedback.json")
    args = parser.parse_args()

    records = load_records()
    print(f"加载 {len(records)} 条回复记录…\n")
    results, monthly = analyze(records)
    stats = print_report(results, monthly)

    if args.update and stats:
        update_feedback_json(stats)


if __name__ == "__main__":
    main()
