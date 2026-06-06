"""Gate 2 分类正确性：使用 LLM 判断工单是否属于正确的项目和问题类型。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).parent.parent
_MOVE_HISTORY_PATH = _PROJECT_ROOT / "data" / "move_history.json"

# 已知项目列表（LLM 输出必须在此范围内）
_KNOWN_PROJECTS = ["流程中心", "业务流", "消息中心", "开发框架", "元数据", "规则", "公式", "打印", "权限", "组织", "档案和应用", "导入导出"]
_KNOWN_ISSUE_TYPES = ["bug", "需求", "客开", "数据问题", "实施问题", "需求问题"]


def _load_provider_cfg(provider_name: str) -> dict:
    try:
        raw = json.loads((_PROJECT_ROOT / "llm_config.json").read_text(encoding="utf-8"))
        p = raw.get(provider_name, {})
        return {"api_key": p.get("api_key", ""), "model_name": p.get("model_name", ""), "base_url": p.get("base_url", "")}
    except Exception:
        return {"api_key": "", "model_name": "", "base_url": ""}


@dataclass
class ClassificationResult:
    predicted_project: str
    predicted_issue_type: str
    confidence: float
    reasoning: str
    gate_enabled: bool = True


def classify_issue(
    issue_key: str,
    title: str,
    description: str,
    current_project: str = "",
    current_issue_type: str = "",
    issue_type_confirmed: str = "",
) -> ClassificationResult:
    """LLM 判断工单是否属于正确项目/问题类型。"""
    cfg = _load_gate_config()
    if not cfg.get("enabled", False):
        return ClassificationResult(
            predicted_project=current_project,
            predicted_issue_type=issue_type_confirmed or current_issue_type,
            confidence=0.0,
            reasoning="gate disabled",
            gate_enabled=False,
        )

    # 从 move_history.json 取最近 10 次移动作为 few-shot 示例
    few_shot = _build_few_shot_examples(10)

    prompt = (
        "你是一个工单分类专家。请判断以下工单是否被分类到了正确的项目和问题类型。\n\n"
        f"已知项目列表：{', '.join(_KNOWN_PROJECTS)}\n"
        f"已知问题类型：{', '.join(_KNOWN_ISSUE_TYPES)}\n\n"
        f"{few_shot}"
        f"---\n待分类工单：\n"
        f"标题：{title[:200]}\n"
        f"描述：{(description or '')[:500]}\n"
        f"当前项目：{current_project}\n"
        f"当前问题类型：{issue_type_confirmed or current_issue_type}\n\n"
        "请输出纯 JSON（不加代码块标记）：\n"
        "{\"predicted_project\":\"流程中心\",\"predicted_issue_type\":\"bug\","
        "\"confidence\":0.85,\"reasoning\":\"简短说明（30字以内）\"}"
    )

    from services.local_llm_lifecycle import with_fallback, shutdown_if_started_by_us
    provider = with_fallback("classifier")
    raw = ""
    try:
        from llm_service import LLMService
        llm = LLMService()
        _pcfg = _load_provider_cfg(provider)
        raw = llm.call_llm(prompt, api_key=_pcfg["api_key"], provider=provider, model_name=_pcfg["model_name"], base_url=_pcfg["base_url"], max_tokens=256, temperature=0.0)
    except Exception as e:
        logger.warning("[classifier] LLM call failed: %s", e)
        return ClassificationResult(
            predicted_project=current_project,
            predicted_issue_type=issue_type_confirmed or current_issue_type,
            confidence=0.0,
            reasoning=f"LLM 调用失败: {e}",
        )
    finally:
        shutdown_if_started_by_us("classifier")

    return _parse_result(raw, current_project, issue_type_confirmed or current_issue_type)


def _parse_result(raw: str, fallback_project: str, fallback_type: str) -> ClassificationResult:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return ClassificationResult(
            predicted_project=fallback_project,
            predicted_issue_type=fallback_type,
            confidence=0.0,
            reasoning="无法解析分类结果",
        )
    try:
        d = json.loads(m.group(0))
        predicted_project = str(d.get("predicted_project", fallback_project))
        # 约束输出到已知范围
        if predicted_project not in _KNOWN_PROJECTS:
            predicted_project = fallback_project
        predicted_type = str(d.get("predicted_issue_type", fallback_type))
        if predicted_type not in _KNOWN_ISSUE_TYPES:
            predicted_type = fallback_type
        confidence = float(d.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        return ClassificationResult(
            predicted_project=predicted_project,
            predicted_issue_type=predicted_type,
            confidence=confidence,
            reasoning=str(d.get("reasoning", ""))[:200],
        )
    except Exception as e:
        logger.warning("[classifier] parse error: %s", e)
        return ClassificationResult(
            predicted_project=fallback_project,
            predicted_issue_type=fallback_type,
            confidence=0.0,
            reasoning="JSON 解析异常",
        )


def _build_few_shot_examples(limit: int) -> str:
    """从 move_history.json 取最近若干条移动记录作为上下文示例。"""
    try:
        history = json.loads(_MOVE_HISTORY_PATH.read_text(encoding="utf-8"))
        recent = history[-limit:] if isinstance(history, list) else []
        if not recent:
            return ""
        lines = ["历史移动记录（供参考）："]
        for entry in recent:
            lines.append(
                f"  {entry.get('issue_key','?')}: "
                f"{entry.get('source_board','?')} → {entry.get('target_board','?')}"
            )
        return "\n".join(lines) + "\n\n"
    except Exception:
        return ""


def _load_gate_config() -> dict:
    try:
        raw = yaml.safe_load((_PROJECT_ROOT / "config" / "reply_gates.yaml").read_text(encoding="utf-8"))
        return raw.get("gates", {}).get("classification", {})
    except Exception:
        return {}
