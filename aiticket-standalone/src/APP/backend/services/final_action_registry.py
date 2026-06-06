"""
final_action 动作注册表：从 data/final_action_schema.json 读取动作定义。
提供统一的动作查找、渲染、校验接口。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
_SCHEMA_PATH = _PROJECT_ROOT / "data" / "final_action_schema.json"
_BUILTIN_KEYS = {  # system_builtin=true 的动作 key 不可删除
    "auto_returned", "auto_moved", "auto_assigned",
    "auto_replied_normal", "auto_replied_low_risk",
    "pending_batch_approve", "manual_review", "manual_with_steps", "needs_decision"
}


def _load_schema() -> list[dict]:
    try:
        return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")).get("actions", [])
    except Exception as e:
        logger.warning("[final_action_registry] failed to load schema: %s", e)
        return []


def _save_schema(actions: list[dict]) -> None:
    current = {}
    try:
        current = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    current["actions"] = actions
    _SCHEMA_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def list_actions() -> list[dict]:
    return _load_schema()


def get_action(key: str) -> dict | None:
    for a in _load_schema():
        if a.get("key") == key:
            return a
    return None


def render_notification(action_key: str, ctx: dict) -> str:
    action = get_action(action_key)
    if not action:
        return f"工单 {ctx.get('issue_key', '?')} 执行了 {action_key}"
    template = action.get("notification_template", "")
    try:
        return template.format_map({k: v for k, v in ctx.items()})
    except Exception:
        return template


def render_operation_steps(action_key: str, ctx: dict) -> list[str]:
    action = get_action(action_key)
    if not action:
        return []
    steps = action.get("operation_steps_template", [])
    result = []
    for step in steps:
        try:
            result.append(step.format_map({k: v for k, v in ctx.items()}))
        except Exception:
            result.append(step)
    return result


def is_valid_key(key: str) -> bool:
    return any(a.get("key") == key for a in _load_schema())


def upsert_action(action_data: dict) -> dict:
    """新增或更新动作。返回最终保存的动作。"""
    actions = _load_schema()
    key = action_data.get("key", "")
    if not key:
        raise ValueError("action key 不能为空")
    for i, a in enumerate(actions):
        if a.get("key") == key:
            # 保留 system_builtin 标记，不可被覆盖为 False
            if a.get("system_builtin"):
                action_data["system_builtin"] = True
            actions[i] = {**a, **action_data}
            _save_schema(actions)
            return actions[i]
    # 新增
    action_data.setdefault("system_builtin", False)
    actions.append(action_data)
    _save_schema(actions)
    return action_data


def delete_action(key: str) -> bool:
    """删除动作。内置动作不可删除，返回 False。"""
    if key in _BUILTIN_KEYS:
        return False
    actions = _load_schema()
    new_actions = [a for a in actions if a.get("key") != key]
    if len(new_actions) == len(actions):
        return False
    _save_schema(new_actions)
    return True


def toggle_action(key: str, enabled: bool) -> dict | None:
    actions = _load_schema()
    for i, a in enumerate(actions):
        if a.get("key") == key:
            actions[i] = {**a, "enabled": enabled}
            _save_schema(actions)
            return actions[i]
    return None
