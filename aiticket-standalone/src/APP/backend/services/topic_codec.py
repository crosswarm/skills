import re
from functools import lru_cache
from pathlib import Path

_TOPIC_MD = Path(__file__).resolve().parents[1] / "data" / "topic.md"


@lru_cache(maxsize=1)
def load_topic_names() -> dict:
    """Parse topic.md → {dotted_code_without_TOP: chinese_name}.
    Example: {'WF': '工作流', 'WF.ENGINE.RUNTIME': '流转', 'APCOM.SUPPORT.CONFIG': '配置迁移'}
    """
    code2name: dict = {}
    if not _TOPIC_MD.exists():
        return code2name
    with open(_TOPIC_MD, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\s*-\s*\[(TOP-([A-Z._]+))\]\s*(\S.*)", line)
            if m:
                code2name[m.group(2)] = m.group(3).strip()
    return code2name


def resolve_topic_label(topic_l1: str, topic_l2: str = "") -> dict:
    """Return {display, l1_name, l2_name} with Chinese names from topic.md.
    Falls back to dotted-code when not found.
    """
    code2name = load_topic_names()
    l1_name = code2name.get(topic_l1) or topic_l1 or "未分类"
    l2_name = code2name.get(f"{topic_l1}.{topic_l2}") or "" if topic_l2 else ""
    if l2_name and l2_name != topic_l2:
        display = f"{l1_name} · {l2_name}"
    elif topic_l2:
        display = f"{l1_name} · {topic_l2}"
    else:
        display = l1_name
    return {"display": display, "l1_name": l1_name, "l2_name": l2_name or topic_l2}
