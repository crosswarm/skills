"""
采纳事实消费者：将 adopted_facts_pending.json 中待采纳的规则写入 product_facts.md 并触发重索引。
幂等：已有 consumed_at 字段则跳过。
"""
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKEND = Path(__file__).resolve().parent.parent
_PENDING_PATH = _BACKEND / "data" / "adopted_facts_pending.json"


def consume_pending_facts() -> int:
    """读取 adopted_facts_pending.json，将 rules 写入 product_facts.md，添加 consumed_at 墓碑。

    Returns:
        int: 实际消费的规则条数（0 表示已消费或无新规则）
    """
    if not _PENDING_PATH.exists():
        logger.info("[AdoptedConsumer] %s 不存在，跳过", _PENDING_PATH)
        return 0

    data = json.loads(_PENDING_PATH.read_text(encoding="utf-8"))

    # 幂等检查
    if data.get("consumed_at"):
        logger.info("[AdoptedConsumer] 已消费（%s），跳过", data["consumed_at"])
        return 0

    rules = data.get("rules") or []
    if not rules:
        logger.info("[AdoptedConsumer] rules 为空，添加墓碑后跳过")
        _write_tombstone(data)
        return 0

    # 写入 product_facts.md
    from reply_diff_analyzer import _append_product_fact
    consumed = 0
    for rule in rules:
        if not isinstance(rule, str) or not rule.strip():
            continue
        try:
            _append_product_fact(
                {"affected_topic": "采纳规则", "product_fact": rule.strip()},
                issue_key="adopted_facts_consumer",
                _skip_reindex=True,
            )
            consumed += 1
        except Exception as e:
            logger.warning("[AdoptedConsumer] 写入规则失败: %s | rule: %.80s", e, rule)

    logger.info("[AdoptedConsumer] 写入 %d 条规则到 product_facts.md", consumed)

    # 写墓碑
    _write_tombstone(data)

    # 触发重索引（_append_product_fact 已触发，这里是保险）
    if consumed > 0:
        try:
            from product_facts_indexer import reindex as _reindex
            _reindex(force=True)
        except Exception as e:
            logger.warning("[AdoptedConsumer] reindex 失败: %s", e)

    return consumed


def _write_tombstone(data: dict) -> None:
    data["consumed_at"] = datetime.now().isoformat()
    _PENDING_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
