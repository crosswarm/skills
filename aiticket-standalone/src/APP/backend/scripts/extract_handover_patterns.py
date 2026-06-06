#!/usr/bin/env python3
"""
weekly 聚合脚本：扫描 operation_history.jsonl → 写 handover_patterns.json

按 (module, customer, product_version, from_project_key) 四维特征汇总
transfer / move_jira 事件的目标频率。
"""
from __future__ import annotations

import json
import os
import sys

# 从脚本目录向上找到 data/
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JSONL = os.path.join(_BASE, "data", "operation_history.jsonl")
_OUT   = os.path.join(_BASE, "data", "handover_patterns.json")


def run():
    transfer: dict  = {}  # feature_key → {to_assignee → {count}}
    move_jira: dict = {}  # feature_key → {to_project_key → {count}}
    project_names: dict = {}  # project_key → display_name (if available)
    total = 0

    if not os.path.exists(_JSONL):
        print(f"[extract_handover_patterns] JSONL 不存在: {_JSONL}，跳过。")
        return

    with open(_JSONL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue

            total += 1
            fkey = "||".join([
                ev.get("module", ""), ev.get("customer", ""),
                ev.get("product_version", ""), ev.get("from_project_key", ""),
            ])
            etype = ev.get("event_type", "")

            if etype == "transfer":
                target = ev.get("to_assignee", "")
                if target:
                    transfer.setdefault(fkey, {})
                    transfer[fkey].setdefault(target, {"count": 0})
                    transfer[fkey][target]["count"] += 1

            elif etype == "move_jira":
                target = ev.get("to_project_key", "")
                if target:
                    move_jira.setdefault(fkey, {})
                    move_jira[fkey].setdefault(target, {"count": 0})
                    move_jira[fkey][target]["count"] += 1

    result = {
        "_event_count": total,
        "_generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "transfer":  transfer,
        "move_jira": move_jira,
        "project_names": project_names,
    }

    os.makedirs(os.path.dirname(_OUT), exist_ok=True)
    with open(_OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    transfer_keys = sum(len(v) for v in transfer.values())
    move_keys = sum(len(v) for v in move_jira.values())
    print(
        f"[extract_handover_patterns] 处理 {total} 条事件 | "
        f"移交模式 {len(transfer)} 特征/{transfer_keys} 候选 | "
        f"移动模式 {len(move_jira)} 特征/{move_keys} 候选 → {_OUT}"
    )


if __name__ == "__main__":
    run()
