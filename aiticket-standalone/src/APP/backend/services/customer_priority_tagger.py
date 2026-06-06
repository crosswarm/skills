"""客户重要度标签读取服务。从 data/customer_tags.json 读取客户标签。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).parent.parent
_TAGS_PATH = _PROJECT_ROOT / "data" / "customer_tags.json"


def get_customer_tag(customer_name: str) -> str:
    """
    根据客户名返回重要度标签。
    返回 "重点客户" 或 "普通客户"（默认）。
    同时支持旧版字符串格式和新版 dict 格式。
    """
    if not customer_name:
        return "普通客户"
    try:
        tags = json.loads(_TAGS_PATH.read_text(encoding="utf-8"))
        v = tags.get(customer_name)
        if isinstance(v, str):
            return v if v else "普通客户"
        elif isinstance(v, dict):
            return v.get("tag", "普通客户")
        return "普通客户"
    except Exception:
        return "普通客户"


def is_key_customer(customer_name: str) -> bool:
    return get_customer_tag(customer_name) == "重点客户"
