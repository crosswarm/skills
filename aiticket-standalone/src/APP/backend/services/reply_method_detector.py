"""
reply_method_detector.py — 统一"纳入需求库"回复方式检测

合并散落的检测逻辑：
  - monthly_analysis.py
  - weekly_analysis.py
"""
from __future__ import annotations

from typing import Any, Dict, List

REPLY_METHOD_INGEST_VALUES: List[str] = ["纳入需求库"]

REPLY_METHOD_FIELD_NAMES: List[str] = [
    "自定义字段(回复方式)",
    "回复方式",
    "reply_method",
    "customfield_10410",
]

# Jira JQL field reference
REPLY_METHOD_JQL_FIELD = "cf[10410]"


def is_ingest_candidate(ticket: Dict[str, Any]) -> bool:
    """
    判断工单是否标记为「纳入需求库」回复方式。

    兼容多种数据来源（Jira API 原始 fields、CSV 导出、本地缓存 dict）。
    """
    for field_name in REPLY_METHOD_FIELD_NAMES:
        value = _extract_field(ticket, field_name)
        if value and _matches(value):
            return True

    # 兼容 Jira REST API 格式 {fields: {customfield_10410: {value: ...}}}
    fields = ticket.get("fields") or {}
    cf = fields.get("customfield_10410")
    if cf:
        value = cf.get("value") or cf if isinstance(cf, str) else None
        if value and _matches(value):
            return True

    return False


def _extract_field(ticket: Dict[str, Any], name: str) -> Any:
    val = ticket.get(name)
    if isinstance(val, dict):
        return val.get("value") or val.get("name")
    return val


def _matches(value: Any) -> bool:
    if isinstance(value, list):
        return any(str(v).strip() in REPLY_METHOD_INGEST_VALUES for v in value)
    return str(value).strip() in REPLY_METHOD_INGEST_VALUES


def build_ingest_jql(
    project: str = "MYPROJECT",
    days_back: int = 365,
    assignees: List[str] | None = None,
    extra_values: List[str] | None = None,
) -> str:
    """
    构建纳入需求库工单的 JQL 查询语句。

    Args:
        project:      Jira 项目 Key（空字符串 = 不限项目）
        days_back:    回溯天数（默认 365 天）
        assignees:    经办人列表（空 = 不限）
        extra_values: 额外的回复方式值（默认只用 REPLY_METHOD_INGEST_VALUES）
    """
    from datetime import datetime, timedelta

    values = REPLY_METHOD_INGEST_VALUES + (extra_values or [])
    value_jql = ", ".join(f'"{v}"' for v in values)

    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    parts = [f'issuetype = "支持问题"']
    if project:
        parts.append(f'project = "{project}"')
    parts.append(f'{REPLY_METHOD_JQL_FIELD} in ({value_jql})')
    parts.append(f'created >= "{since}"')

    if assignees:
        assignee_jql = ", ".join(f'"{a}"' for a in assignees)
        parts.append(f"assignee in ({assignee_jql})")

    parts.append("ORDER BY created DESC")
    return " AND ".join(parts[:-1]) + " " + parts[-1]
