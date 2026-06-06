"""从周报/月报 KPI non_compliant_customers 中超过 target 的客户动态追加到 customer_tags.json"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_CUSTOMER_TAGS_PATH = _PROJECT_ROOT / "data" / "customer_tags.json"

# 报告目录（WeeklyReports / MonthlyReports 在 conclusion/ 下）
_REPO_ROOT = _PROJECT_ROOT.parent.parent
_REPORTS_WEEKLY_DIR = _REPO_ROOT / "conclusion" / "WeeklyReports"
_REPORTS_MONTHLY_DIR = _REPO_ROOT / "conclusion" / "MonthlyReports"

_DEFAULT_TARGET = 3.37


def _load_tags() -> dict:
    try:
        return json.loads(_CUSTOMER_TAGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tags(tags: dict) -> None:
    _CUSTOMER_TAGS_PATH.write_text(
        json.dumps(tags, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _list_report_jsons(directory: Path, n: int) -> list[Path]:
    """Return up to n most-recent .json files (non-archived) sorted by name desc."""
    if not directory.exists():
        return []
    files = sorted(
        [f for f in directory.glob("*.json") if not f.name.startswith("_")],
        reverse=True,
    )
    return files[:n]


def _parse_period_label(path: Path) -> str:
    """Extract a human-readable period label from the filename."""
    name = path.stem
    # e.g. Weekly_Report_2026-04-27_2026-05-03 → 2026-04-27~2026-05-03
    parts = name.split("_")
    dates = [p for p in parts if len(p) >= 8 and p[0].isdigit()]
    if len(dates) >= 2:
        return f"{dates[0]}~{dates[1]}"
    if len(dates) == 1:
        return dates[0]
    return name


def _collect_exceeding_customers(
    report_path: Path,
) -> tuple[list[dict], float]:
    """
    Returns (non_compliant_customers list, target) from a single report JSON.
    Each item has at least {"customer": str, "issue_count": int}.
    """
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[kpi_seeder] Failed to read %s: %s", report_path.name, exc)
        return [], _DEFAULT_TARGET

    kpi = data.get("kpi_analysis", {})
    target = float(kpi.get("target") or _DEFAULT_TARGET)
    ncc = kpi.get("non_compliant_customers", [])
    return ncc, target


def seed_from_latest_reports(
    *, lookback_weeks: int = 4, lookback_months: int = 3
) -> dict:
    """
    Scans latest N weekly + M monthly reports, finds customers in
    non_compliant_customers, adds them to customer_tags.json with
    source=kpi_seed if not already present.

    Returns {"added": [...], "retained": [...], "suggested_demote": [...]}
    """
    tags = _load_tags()

    # --- collect all periods ---
    weekly_paths = _list_report_jsons(_REPORTS_WEEKLY_DIR, lookback_weeks)
    monthly_paths = _list_report_jsons(_REPORTS_MONTHLY_DIR, lookback_months)
    all_paths = weekly_paths + monthly_paths
    total_periods = len(all_paths)

    if total_periods == 0:
        return {"added": [], "retained": [], "suggested_demote": [], "total_periods": 0}

    # customer → {periods_exceeded, best_period, best_count, best_target, best_gap}
    customer_stats: dict[str, dict] = {}

    for path in all_paths:
        ncc, target = _collect_exceeding_customers(path)
        period_label = _parse_period_label(path)
        for entry in ncc:
            name = entry.get("customer", "")
            if not name:
                continue
            issue_count = int(entry.get("issue_count") or 0)
            gap = round(issue_count - target, 2)
            if name not in customer_stats:
                customer_stats[name] = {
                    "periods_exceeded": 0,
                    "best_period": period_label,
                    "best_count": issue_count,
                    "best_target": target,
                    "best_gap": gap,
                }
            cs = customer_stats[name]
            cs["periods_exceeded"] += 1
            if issue_count > cs["best_count"]:
                cs["best_period"] = period_label
                cs["best_count"] = issue_count
                cs["best_target"] = target
                cs["best_gap"] = gap

    today_str = date.today().isoformat()
    added: list[str] = []
    retained: list[str] = []
    suggested_demote: list[str] = []

    # --- update tags for exceeding customers ---
    for name, cs in customer_stats.items():
        score = round(cs["periods_exceeded"] / total_periods, 4)
        existing = tags.get(name)

        if isinstance(existing, str):
            # manually tagged — don't touch
            retained.append(name)
            continue

        if isinstance(existing, dict):
            src = existing.get("source", "manual")
            if src != "kpi_seed":
                # manually added as dict — don't touch
                retained.append(name)
                continue
            # kpi_seed entry — update evidence
            existing["last_confirmed_at"] = today_str
            existing["evidence"] = {
                "period": cs["best_period"],
                "ticket_count": cs["best_count"],
                "target": cs["best_target"],
                "gap": cs["best_gap"],
                "score": score,
            }
            retained.append(name)
            continue

        # Not in tags yet
        if score >= 0.5:
            tags[name] = {
                "tag": "重点客户",
                "source": "kpi_seed",
                "first_seeded_at": today_str,
                "last_confirmed_at": today_str,
                "evidence": {
                    "period": cs["best_period"],
                    "ticket_count": cs["best_count"],
                    "target": cs["best_target"],
                    "gap": cs["best_gap"],
                    "score": score,
                },
            }
            added.append(name)

    # --- identify suggested_demote: kpi_seed entries NOT in any recent period ---
    for name, entry in list(tags.items()):
        if not isinstance(entry, dict):
            continue
        if entry.get("source") != "kpi_seed":
            continue
        if name in customer_stats:
            score = round(customer_stats[name]["periods_exceeded"] / total_periods, 4)
        else:
            score = 0.0
        if score < 0.25:
            entry["_suggested_demote"] = True
            suggested_demote.append(name)
        else:
            entry.pop("_suggested_demote", None)

    _save_tags(tags)
    logger.info(
        "[kpi_seeder] seed complete: added=%d retained=%d suggested_demote=%d",
        len(added),
        len(retained),
        len(suggested_demote),
    )
    return {
        "added": added,
        "retained": retained,
        "suggested_demote": suggested_demote,
        "total_periods": total_periods,
    }


def list_underperformers(*, period_type: str = "weekly", n: int = 4) -> list[dict]:
    """
    Returns list of {customer_name, ticket_count, target, gap, periods_exceeded, score}
    for customers who appeared in non_compliant_customers in at least 1 of the last N periods.
    Does NOT modify customer_tags.json.
    """
    if period_type not in ("weekly", "monthly"):
        raise ValueError(f"period_type 必须为 weekly 或 monthly，收到: {period_type!r}")
    if period_type == "monthly":
        paths = _list_report_jsons(_REPORTS_MONTHLY_DIR, n)
    else:
        paths = _list_report_jsons(_REPORTS_WEEKLY_DIR, n)

    total_periods = len(paths)
    if total_periods == 0:
        return []

    customer_stats: dict[str, dict] = {}
    for path in paths:
        ncc, target = _collect_exceeding_customers(path)
        for entry in ncc:
            name = entry.get("customer", "")
            if not name:
                continue
            issue_count = int(entry.get("issue_count") or 0)
            gap = round(issue_count - target, 2)
            if name not in customer_stats:
                customer_stats[name] = {
                    "periods_exceeded": 0,
                    "ticket_count": issue_count,
                    "target": target,
                    "gap": gap,
                }
            cs = customer_stats[name]
            cs["periods_exceeded"] += 1
            if issue_count > cs["ticket_count"]:
                cs["ticket_count"] = issue_count
                cs["target"] = target
                cs["gap"] = gap

    result = []
    for name, cs in customer_stats.items():
        score = round(cs["periods_exceeded"] / total_periods, 4)
        result.append(
            {
                "customer_name": name,
                "ticket_count": cs["ticket_count"],
                "target": cs["target"],
                "gap": cs["gap"],
                "periods_exceeded": cs["periods_exceeded"],
                "total_periods": total_periods,
                "score": score,
            }
        )

    result.sort(key=lambda x: (-x["score"], -x["ticket_count"]))
    return result
