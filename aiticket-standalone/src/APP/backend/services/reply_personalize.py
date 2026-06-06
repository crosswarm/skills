"""Gate 3 回复个性化替换：将历史回复中的工单号/日期/版本/客户名替换为当前工单信息。"""
from __future__ import annotations

import re
from datetime import datetime


def personalize_reply(
    reply_text: str,
    new_issue_key: str = "",
    old_issue_key: str = "",
    new_version: str = "",
    new_customer_name: str = "",
) -> str:
    """
    对历史回复进行安全的参数替换，仅替换可识别的动态参数，
    不修改知识库引用、解决步骤等核心内容。
    """
    text = reply_text

    # 1. 工单号替换（旧 issue_key → 新 issue_key）
    if old_issue_key and new_issue_key and old_issue_key != new_issue_key:
        text = text.replace(old_issue_key, new_issue_key)

    # 2. 日期替换：把历史日期替换为今天（仅替换独立的日期表达，不触碰版本号）
    today = datetime.now()
    today_iso = today.strftime("%Y-%m-%d")
    today_cn = today.strftime("%Y年%m月%d日")

    # ISO date: 2025-12-31（不替换版本号格式如 5.0.1.3）
    # 用 lookahead/lookbehind 代替 \b，使其在 CJK 字符旁也能生效
    text = re.sub(
        r'(?<![.\d])(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?![.\d])',
        today_iso,
        text,
    )
    # Chinese date: 2025年12月31日
    text = re.sub(
        r'(20\d{2})年(0?[1-9]|1[0-2])月(0?[1-9]|[12]\d|3[01])日',
        today_cn,
        text,
    )

    # 3. 版本号替换：仅替换独立的版本表达（格式 X.Y.Z.W），不触碰 URL
    if new_version:
        # 匹配独立的多段版本号（前后不是数字/点/斜杠）
        text = re.sub(
            r'(?<![/\d.])(\d+\.\d+\.\d+(?:\.\d+)*)(?![/\d.])',
            new_version,
            text,
        )

    # 4. 客户名替换：仅替换常见的客户名称模式（「客户X」「X客户」「您好，X」开头问候）
    if new_customer_name:
        # 简单的显式感谢/称呼替换（不做模糊匹配，避免误替换内容）
        # 模式: "您好，[客户名]" / "感谢[客户名]" / "[客户名]您好"
        text = re.sub(
            r'(您好[，,]\s*)([^\s，,。！？]{2,10}?)(\s*您好|[，,])',
            lambda m: m.group(1) + (new_customer_name or m.group(2)) + m.group(3),
            text,
            count=1,
        )

    return text
