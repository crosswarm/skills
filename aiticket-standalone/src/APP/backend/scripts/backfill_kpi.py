#!/usr/bin/env python3
"""
回填历史报告的KPI数据 — 为已生成的周报和月报注入kpi_analysis字段
用法: python backfill_kpi.py [--weekly] [--monthly] [--dry-run]
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from kpi_calculator import KPICalculator

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
WEEKLY_DIR = PROJECT_ROOT / "conclusion" / "WeeklyReports"
MONTHLY_DIR = PROJECT_ROOT / "conclusion" / "MonthlyReports"


def backfill_weekly(dry_run=False):
    """回填周报KPI"""
    kpi = KPICalculator()
    baseline_csv = kpi._find_baseline_csv()
    baseline_df = None
    if baseline_csv:
        baseline_df = pd.read_csv(baseline_csv)
        baseline_df.columns = [c.strip() for c in baseline_df.columns]
        baseline_df["创建日期"] = pd.to_datetime(baseline_df["创建日期"], errors="coerce")
        baseline_df = baseline_df.dropna(subset=["创建日期"])
        baseline_year = kpi.config.get("baseline_year", 2025)
        baseline_df = baseline_df[baseline_df["创建日期"].dt.year == baseline_year]

    json_files = sorted(WEEKLY_DIR.glob("*.json"))
    updated = 0

    for jf in json_files:
        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)

        if data.get("kpi_analysis", {}).get("current", {}).get("per_customer"):
            print(f"  跳过 (已有KPI): {jf.name}")
            continue

        # 找到对应的源CSV
        source_file = data.get("meta", {}).get("source_file", "")
        csv_path = SRC_DIR / source_file if source_file else None

        if csv_path and csv_path.exists():
            df = pd.read_csv(csv_path)
            df.columns = [c.strip() for c in df.columns]
        else:
            # 尝试从ticket_details重建
            all_tickets = data.get("ticket_details", {}).get("all_tickets", [])
            if not all_tickets:
                print(f"  跳过 (无数据源): {jf.name}")
                continue
            df = pd.DataFrame(all_tickets)

        current_kpi = kpi.calculate_period_kpi(df)

        # 同期去年数据
        start = data.get("meta", {}).get("data_start_date", "")
        end = data.get("meta", {}).get("data_end_date", "")
        last_year_kpi = {}
        if baseline_df is not None and start and end:
            try:
                ly_start = pd.to_datetime(start) - pd.DateOffset(years=1)
                ly_end = pd.to_datetime(end) - pd.DateOffset(years=1)
                ly_df = baseline_df[(baseline_df["创建日期"] >= ly_start) & (baseline_df["创建日期"] <= ly_end)]
                if not ly_df.empty:
                    last_year_kpi = kpi.calculate_period_kpi(ly_df)
            except Exception:
                pass

        yoy = kpi.calculate_yoy_kpi(current_kpi, last_year_kpi) if last_year_kpi else {}
        distribution = kpi.get_customer_distribution_bands(df)

        kpi_analysis = {
            "current": {k: v for k, v in current_kpi.items() if k != "customer_breakdown"},
            "last_year_same_week": {k: v for k, v in last_year_kpi.items() if k != "customer_breakdown"} if last_year_kpi else {},
            "yoy_change_pct": yoy.get("change_pct"),
            "target": kpi.target,
            "gap": yoy.get("gap", round(current_kpi.get("per_customer", 0) - kpi.target, 2)),
            "customer_distribution": distribution,
        }

        data["kpi_analysis"] = kpi_analysis

        if dry_run:
            print(f"  [DRY] {jf.name}: per_customer={current_kpi.get('per_customer', 0)}")
        else:
            with open(jf, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"  ✅ {jf.name}: per_customer={current_kpi.get('per_customer', 0)}")

        updated += 1

    print(f"\n周报回填完成: {updated}/{len(json_files)} 份更新")


def backfill_monthly(dry_run=False):
    """回填月报KPI"""
    kpi = KPICalculator()

    json_files = sorted(MONTHLY_DIR.glob("*.json"))
    updated = 0

    for jf in json_files:
        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)

        if data.get("kpi_analysis", {}).get("current", {}).get("per_customer"):
            print(f"  跳过 (已有KPI): {jf.name}")
            continue

        meta = data.get("meta", {})
        year = meta.get("year")
        month = meta.get("month")
        if not year or not month:
            print(f"  跳过 (无年月信息): {jf.name}")
            continue

        # 找源CSV (优先级: 月数据CSV > 源文件 > 周报源CSV合并)
        df = None
        source_type = meta.get("source_type", "")

        # 1. 月数据CSV
        pattern = f"{year}{month:02d}"
        candidates = [f for f in SRC_DIR.iterdir() if "月数据" in f.name and f.suffix == ".csv" and pattern in f.name]
        if candidates:
            df = pd.read_csv(candidates[0])
            df.columns = [c.strip() for c in df.columns]

        # 2. 源文件
        if df is None:
            source_file = meta.get("source_file", "")
            if source_file:
                csv_path = SRC_DIR / source_file
                if csv_path.exists():
                    df = pd.read_csv(csv_path)
                    df.columns = [c.strip() for c in df.columns]

        # 3. 周报聚合模式: 从周报源CSV合并
        if df is None and source_type == "weekly_aggregate":
            week_files = meta.get("week_files", [])
            dfs = []
            for wf in week_files:
                wf_path = WEEKLY_DIR / wf
                if wf_path.exists():
                    with open(wf_path, "r", encoding="utf-8") as wfh:
                        wdata = json.load(wfh)
                    wsrc = wdata.get("meta", {}).get("source_file", "")
                    if wsrc:
                        wsrc_path = SRC_DIR / wsrc
                        if wsrc_path.exists():
                            wdf = pd.read_csv(wsrc_path)
                            wdf.columns = [c.strip() for c in wdf.columns]
                            dfs.append(wdf)
            if dfs:
                df = pd.concat(dfs, ignore_index=True)
                # 去重
                if "问题关键字" in df.columns:
                    df = df.drop_duplicates(subset=["问题关键字"])

        if df is None:
            print(f"  跳过 (无数据源): {jf.name}")
            continue

        current_kpi = kpi.calculate_period_kpi(df)
        ly_df = kpi.load_last_year_same_month(year, month)
        last_year_kpi = kpi.calculate_period_kpi(ly_df) if ly_df is not None else {}
        yoy = kpi.calculate_yoy_kpi(current_kpi, last_year_kpi) if last_year_kpi else {}
        distribution = kpi.get_customer_distribution_bands(df)

        kpi_analysis = {
            "current": {k: v for k, v in current_kpi.items() if k != "customer_breakdown"},
            "last_year_same_month": {k: v for k, v in last_year_kpi.items() if k != "customer_breakdown"} if last_year_kpi else {},
            "yoy_change_pct": yoy.get("change_pct"),
            "target": kpi.target,
            "gap": yoy.get("gap", round(current_kpi.get("per_customer", 0) - kpi.target, 2)),
            "customer_distribution": distribution,
        }

        data["kpi_analysis"] = kpi_analysis

        if dry_run:
            print(f"  [DRY] {jf.name}: per_customer={current_kpi.get('per_customer', 0)}")
        else:
            with open(jf, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"  ✅ {jf.name}: per_customer={current_kpi.get('per_customer', 0)}")

        updated += 1

    print(f"\n月报回填完成: {updated}/{len(json_files)} 份更新")


def main():
    parser = argparse.ArgumentParser(description="回填历史报告KPI数据")
    parser.add_argument("--weekly", action="store_true", help="回填周报")
    parser.add_argument("--monthly", action="store_true", help="回填月报")
    parser.add_argument("--dry-run", action="store_true", help="仅预览不写入")
    args = parser.parse_args()

    if not args.weekly and not args.monthly:
        args.weekly = True
        args.monthly = True

    if args.weekly:
        print("=== 回填周报KPI ===")
        backfill_weekly(dry_run=args.dry_run)

    if args.monthly:
        print("\n=== 回填月报KPI ===")
        backfill_monthly(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
