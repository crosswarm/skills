#!/usr/bin/env python3
"""
audit_schedule_registry.py — 调度注册一致性检查

Exit codes:
  0 — 全部通过
  1 — 有警告（无硬错误）
  2 — 有错误（pre-commit 应拒绝）

用法:
  python audit_schedule_registry.py            # 默认：警告+错误都显示
  python audit_schedule_registry.py --strict   # 警告也视为错误（pre-commit 用）
  python audit_schedule_registry.py --json     # 机器可读输出
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent  # project root APP/
BACKEND = ROOT / "backend"
SCHEDULES_DIR = BACKEND / "data" / "schedules"
HB_PATH = BACKEND / "data" / "runtime" / "scheduler_heartbeat.json"
LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

errors: list[str] = []
warnings: list[str] = []


def err(msg: str):
    errors.append(f"[ERROR] {msg}")


def warn(msg: str):
    warnings.append(f"[WARN]  {msg}")


# ── 1. Schedule JSON 字段检查 ─────────────────────────────────────────────
def check_schedule_files():
    jsons = sorted(SCHEDULES_DIR.glob("*.json"))
    if not jsons:
        err(f"data/schedules/ 下没有任何 JSON 文件: {SCHEDULES_DIR}")
        return

    now = datetime.utcnow()
    for f in jsons:
        if f.name.startswith("deferred-") or f.name.startswith("__"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            err(f"{f.name}: JSON 解析失败 — {e}")
            continue

        # task_type=null → 尚未实现的 stub；若已 enabled=false 则静默跳过
        if d.get("task_type") is None and d.get("id") is None:
            if not d.get("enabled", True):
                continue  # 已明确禁用的 stub，不发出 warn
            warn(f"{f.name}: stub 文件（id/task_type 均为 null），尚未实现 — 建议设置 enabled=false")
            continue

        # 允许 'schedule' 作为 'cron' 的历史别名
        has_cron = "cron" in d or "schedule" in d
        # oneshot/manual 不需要 cron
        needs_cron = d.get("trigger_type", "cron") == "cron"

        # 必填字段
        if "id" not in d and "name" not in d:
            err(f"{f.name}: 缺少 'id' 字段")
        if needs_cron and not has_cron:
            err(f"{f.name}: 缺少 'cron' 字段（trigger_type=cron 时必填）")

        # agent_hint 推荐字段（警告）
        if "agent_hint" not in d:
            warn(f"{f.name}: 缺少 'agent_hint' 字段（应声明对应 agent 名称，便于 SCHEDULE_AGENT_MAP 自动派生）")

        # 近期是否有执行记录
        if not d.get("enabled", True):
            continue  # 已停用，跳过

        recent_runs = d.get("recent_runs", [])
        run_count = d.get("run_count", 0)
        last_run = d.get("last_run")

        if run_count == 0 and not recent_runs:
            warn(f"{f.name}: run_count=0，从未执行过")
            continue

        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run.rstrip("Z"))
                age_days = (now - last_dt).days
                # 尝试从 cron 推断周期
                cron = d.get("cron", "")
                parts = cron.split()
                if len(parts) >= 5:
                    # 日周期估算（简化：看 day-of-month 和 day-of-week）
                    _, _, dom, _, dow = parts[:5]
                    if dow != "*" and dom == "*":
                        cadence_days = 7  # 每周
                    elif dom != "*" and dow == "*":
                        cadence_days = 30  # 每月
                    else:
                        cadence_days = 1   # 每日或每小时
                    threshold = cadence_days * 3
                    if age_days > threshold:
                        warn(f"{f.name}: 上次执行距今 {age_days} 天，超过 {threshold} 天阈值（cron: {cron}）")
            except Exception:
                pass


# ── 2. 心跳文件检查 ───────────────────────────────────────────────────────
def check_heartbeat():
    if not HB_PATH.exists():
        warn(f"scheduler 心跳文件不存在: {HB_PATH} — scheduler 可能未启动或尚未写入心跳（Phase 1.3 实施后才有）")
        return

    age_s = time.time() - HB_PATH.stat().st_mtime
    if age_s > 300:
        err(f"scheduler 心跳文件已 {int(age_s)}s 未更新（阈值 300s）— scheduler 很可能已停止")
    elif age_s > 180:
        warn(f"scheduler 心跳文件已 {int(age_s)}s 未更新（阈值 180s）")


# ── 3. LaunchAgent plist ThrottleInterval 检查 ────────────────────────────
def check_launchd_plists():
    aiticket_plists = list(LAUNCHAGENTS_DIR.glob("com.aiticket.*.plist"))
    if not aiticket_plists:
        warn(f"~/Library/LaunchAgents/ 下没有找到 com.aiticket.*.plist — 可能尚未安装或路径不同")
        return

    for plist in aiticket_plists:
        content = plist.read_text(encoding="utf-8", errors="replace")
        if "ThrottleInterval" not in content:
            warn(f"{plist.name}: 缺少 ThrottleInterval，快速崩溃时 launchd 可能永久停止重启")
        if "KeepAlive" not in content:
            warn(f"{plist.name}: 缺少 KeepAlive 配置")


# ── 4. SCHEDULE_AGENT_MAP vs schedule JSON 比对 ───────────────────────────
def check_map_coverage():
    # 优先动态 import task_bridge，获得包含 agent_hint 自动派生的完整 map
    map_keys: set = set()
    try:
        import sys as _sys
        if str(BACKEND) not in _sys.path:
            _sys.path.insert(0, str(BACKEND))
        from services.task_bridge import SCHEDULE_AGENT_MAP as _sam
        map_keys = set(_sam.keys())
    except Exception:
        # 降级：正则解析源码（只匹配硬编码 dict 字面量）
        try:
            import re
            tb_path = BACKEND / "services" / "task_bridge.py"
            src = tb_path.read_text(encoding="utf-8")
            map_keys = set(re.findall(r'"([a-z0-9_-]+)":\s+"[a-z_]+"', src))
            warn("task_bridge 动态 import 失败，回落到正则解析（可能漏报 agent_hint 派生条目）")
        except Exception as e:
            warn(f"无法解析 task_bridge.py SCHEDULE_AGENT_MAP: {e}")
            return

    json_ids = set()
    for f in SCHEDULES_DIR.glob("*.json"):
        if f.name.startswith("deferred-") or f.name.startswith("__"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            sid = d.get("id") or f.stem
            if d.get("enabled", True):
                json_ids.add(sid)
        except Exception:
            pass

    # jobmaster-* 由 JobMaster 内部处理，不需要在 SCHEDULE_AGENT_MAP
    non_jm = {s for s in json_ids if not s.startswith("jobmaster-")}
    unregistered = non_jm - map_keys
    if unregistered:
        for sid in sorted(unregistered):
            warn(f"schedule '{sid}' 未在 task_bridge.py SCHEDULE_AGENT_MAP 中注册（将 fallback 到 'req_analyst'）")


# ── 5. 主入口 ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Schedule registry audit")
    ap.add_argument("--strict", action="store_true", help="警告也视为错误")
    ap.add_argument("--json", dest="as_json", action="store_true", help="机器可读 JSON 输出")
    args = ap.parse_args()

    check_schedule_files()
    check_heartbeat()
    check_launchd_plists()
    check_map_coverage()

    all_messages = errors + warnings
    if args.as_json:
        print(json.dumps({
            "errors": errors,
            "warnings": warnings,
            "has_errors": bool(errors),
            "has_warnings": bool(warnings),
        }, ensure_ascii=False, indent=2))
    else:
        for msg in all_messages:
            print(msg)
        if not all_messages:
            print("✅ 全部检查通过")
        else:
            print(f"\n共 {len(errors)} 个错误，{len(warnings)} 个警告")

    if errors:
        sys.exit(2)
    if args.strict and warnings:
        sys.exit(1)
    if warnings:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
