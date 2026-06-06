"""Gate 1 信息完整性检查：根据项目+工单类型匹配规则，用 LLM 判断必填字段是否存在。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from services.local_llm_lifecycle import shutdown_if_started_by_us, with_fallback_chain, daytime_chain

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


@dataclass
class CompletenessResult:
    passed: bool
    missing_fields: list[str] = field(default_factory=list)
    inquiry_draft: str = ""
    rule_matched: str = ""
    gate_enabled: bool = True
    insufficient_type: str = ""  # "missing_fields" | "invalid_description"


def check(
    issue_key: str,
    project: str,
    issue_type_confirmed: str,
    description: str,
    attachment_texts: list[str],
) -> CompletenessResult:
    try:
        gates_cfg = _load_gates_config()
    except Exception:
        logger.warning("[completeness] failed to load gates config, skipping gate")
        return CompletenessResult(passed=True, gate_enabled=False)

    if not gates_cfg.get("enabled", False):
        return CompletenessResult(passed=True, gate_enabled=False)

    scope_filter = gates_cfg.get("scope_filter", {})
    if isinstance(scope_filter, dict):
        allowed_projects = scope_filter.get("projects", [])
    elif isinstance(scope_filter, list):
        allowed_projects = scope_filter
    else:
        allowed_projects = []
    if allowed_projects and project not in allowed_projects:
        return CompletenessResult(passed=True, gate_enabled=True, rule_matched="scope_skip")

    try:
        schema = _load_schema()
    except Exception:
        logger.warning("[completeness] failed to load schema, skipping gate")
        return CompletenessResult(passed=True, gate_enabled=True)

    rule = _find_rule(schema, project, issue_type_confirmed)
    rule_key = f"{project}/{issue_type_confirmed}" if _has_exact_rule(schema, project, issue_type_confirmed) else "default_fallback"

    required_fields: list[str] = rule.get("required", [])
    if not required_fields:
        return CompletenessResult(passed=True, gate_enabled=True, rule_matched=rule_key)

    content = (description + "\n" + "\n".join(attachment_texts))[:2000]
    provider = with_fallback_chain("completeness_check", daytime_chain("completeness_check"))

    # 描述过短（< 30 字）→ 直接标记为 invalid_description，无需 LLM
    _desc_stripped = (description or "").strip()
    if len(_desc_stripped) < 30:
        return CompletenessResult(
            passed=False,
            missing_fields=["清晰的问题描述"],
            inquiry_draft="您好，当前工单描述过于简短，无法识别具体问题。请补充说明遇到了什么现象或错误，以便快速跟进处理。",
            rule_matched="pre_check:short_description",
            gate_enabled=True,
            insufficient_type="invalid_description",
        )

    missing: list[str] = []
    # default_fallback 检查不通过 → 描述不清晰，属 invalid_description；其他规则属 missing_fields
    _insufficient_type = "missing_fields"
    try:
        from llm_service import LLMService
        llm = LLMService()
        _pcfg = _load_provider_cfg(provider)
        for f in required_fields:
            try:
                prompt = (
                    f'判断以下工单描述中是否包含"{f}"相关信息。\n'
                    f"工单描述：{content}\n"
                    "只回答 YES 或 NO。"
                )
                answer = llm.call_llm(prompt, api_key=_pcfg["api_key"], provider=provider, model_name=_pcfg["model_name"], base_url=_pcfg["base_url"], max_tokens=512, temperature=0.0)
                if _extract_yes_no(answer) == "NO":
                    missing.append(f)
                    if rule_key == "default_fallback":
                        _insufficient_type = "invalid_description"
            except Exception:
                logger.warning("[completeness] LLM call failed for field '%s', treating as present", f)
    finally:
        shutdown_if_started_by_us("completeness_check")

    if not missing:
        return CompletenessResult(passed=True, gate_enabled=True, rule_matched=rule_key)

    missing_list_str = "\n".join(f"- {f}" for f in missing)
    template: str = rule.get("inquiry_template", "请补充以下信息：\n{missing_fields_list}")
    draft = template.replace("{missing_fields_list}", missing_list_str)

    return CompletenessResult(
        passed=False,
        missing_fields=missing,
        inquiry_draft=draft,
        rule_matched=rule_key,
        gate_enabled=True,
        insufficient_type=_insufficient_type,
    )


def _extract_yes_no(raw: str) -> str:
    """从 LLM 回答中提取 YES/NO，兼容 <think>...</think> 推理模型格式。"""
    import re as _re
    text = _re.sub(r'<think>.*?</think>', '', raw, flags=_re.DOTALL).strip()
    if not text:
        text = raw.strip()
    upper = text.upper()
    for line in upper.splitlines():
        line = line.strip()
        if line.startswith("NO"):
            return "NO"
        if line.startswith("YES"):
            return "YES"
    return "YES"  # 无法判断时 fail-open


def _load_provider_cfg(provider_name: str) -> dict:
    try:
        raw = json.loads((PROJECT_ROOT / "llm_config.json").read_text(encoding="utf-8"))
        p = raw.get(provider_name, {})
        return {"api_key": p.get("api_key", ""), "model_name": p.get("model_name", ""), "base_url": p.get("base_url", "")}
    except Exception:
        return {"api_key": "", "model_name": "", "base_url": ""}


def _load_gates_config() -> dict:
    """读 config/reply_gates.yaml，返回 gates.completeness 节。"""
    cfg_path = PROJECT_ROOT / "config" / "reply_gates.yaml"
    with open(cfg_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return raw.get("gates", {}).get("completeness", {})


def _load_schema() -> dict:
    """读 data/completeness_schema.json。"""
    schema_path = PROJECT_ROOT / "data" / "completeness_schema.json"
    with open(schema_path, encoding="utf-8") as fh:
        return json.load(fh)


def _find_rule(schema: dict, project: str, issue_type_confirmed: str) -> dict:
    """精确匹配 (project, issue_type_confirmed)，失败返回 default_fallback。"""
    for rule in schema.get("rules", []):
        if rule.get("project") == project and rule.get("issue_type_confirmed") == issue_type_confirmed:
            return rule
    return schema.get("default_fallback", {})


def _has_exact_rule(schema: dict, project: str, issue_type_confirmed: str) -> bool:
    for rule in schema.get("rules", []):
        if rule.get("project") == project and rule.get("issue_type_confirmed") == issue_type_confirmed:
            return True
    return False


if __name__ == "__main__":
    result = check(
        issue_key="FLOW-999",
        project="流程中心",
        issue_type_confirmed="bug",
        description="点击提交按钮后页面报错，没有截图。",
        attachment_texts=[],
    )
    print(result)
