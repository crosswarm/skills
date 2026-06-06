"""local_llm_lifecycle — compact 版云-only shim。

完整版负责本地 SuperGemma4(MLX) 的探活/自启/随手关灯 + provider fallback 链。
compact 版砍掉本地模型（纯云 API），只保留 **provider 路由 / fallback 语义**，
让依赖它的核心 gate（completeness_checker G1 / classifier_service G2 /
reply_supervisor G5 / reply_diff_analyzer / main）无改动即可工作。

所有 "local" provider 一律从链中剔除；无 MLX、无 PID 文件、无 os.kill，跨平台。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import List, Optional

logger = logging.getLogger(__name__)

# 云端默认 fallback 链（无 local）。顺序即优先级。
_CLOUD_CHAIN: List[str] = ["zhipu", "minimax", "openai"]


def _default_provider() -> str:
    """当前生效的云 provider：优先读 llm_config.json 的 last_provider，兜底 zhipu。"""
    try:
        import json
        from pathlib import Path
        cfg_path = Path(__file__).resolve().parent.parent / "llm_config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            p = (data.get("last_provider") or data.get("default") or "").strip()
            if p and p != "local":
                return p
    except Exception:
        pass
    return "zhipu"


def _cloud_only(chain: Optional[List[str]]) -> List[str]:
    """剔除 local，去重保序；空则回退默认云链（首项为当前 provider）。"""
    out: List[str] = []
    for p in (chain or []):
        if p and p != "local" and p not in out:
            out.append(p)
    if not out:
        dp = _default_provider()
        out = [dp] + [p for p in _CLOUD_CHAIN if p != dp]
    return out


def is_alive(timeout: float = 3.0) -> bool:
    """本地模型探活——compact 无本地模型，恒 False。"""
    return False


def ensure_running(wait_seconds: int = 120, max_attempts: int = 3) -> bool:
    """本地模型自启——compact 无本地模型，恒 False（调用方据此降级到云）。"""
    return False


def with_fallback(task_name: str) -> str:
    """返回该任务的云 provider（compact：直接给当前默认云 provider）。"""
    return _cloud_only(None)[0]


def with_fallback_chain(task_name: str, chain: list = None, *, use_breaker: bool = False) -> str:
    """按 chain 顺序返回首个可用云 provider（剔除 local）。

    use_breaker=True：跳过熔断 open 的 provider；全 open 抛 RuntimeError（端点转 503）。
    """
    candidates = _cloud_only(chain)

    is_available = None
    if use_breaker:
        try:
            from services.circuit_breaker import is_available as is_available  # noqa
        except Exception:
            is_available = None  # 熔断不可用时 fail-open

    for provider in candidates:
        if is_available is not None and not is_available(provider):
            logger.info("[local_llm] %s: provider %s circuit OPEN, skip", task_name, provider)
            continue
        return provider

    if use_breaker:
        raise RuntimeError(f"all_providers_unavailable chain={candidates}")
    return candidates[-1] if candidates else "zhipu"


def daytime_chain(task_name: str) -> list:
    """compact：云-only，不分时段。返回当前 provider 优先的云链。"""
    return _cloud_only(None)


@contextmanager
def lifecycle(task_name: str, *, required: bool = False):
    """compact：无本地模型，直接 yield 云 provider。required 仅为签名兼容。"""
    yield with_fallback(task_name)


def shutdown_if_started_by_us(task_name: str) -> None:
    """随手关灯——compact 无本地进程，no-op。"""
    return None
