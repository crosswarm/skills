"""
通用 Webhook 通知服务 — 替代飞书专属 openclaw 实现。

读取 NOTIFICATION_WEBHOOK_URL 环境变量（或 deployment.yaml notifications.webhook_url）。
支持格式：feishu | wecom | dingtalk | generic
未配置时静默 no-op，不影响主流程。
"""
import json
import logging
import os
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_notifier_instance: Optional["WebhookNotifier"] = None
_notifier_lock = threading.Lock()


class WebhookNotifier:
    def __init__(self, webhook_url: str = "", fmt: str = "feishu", chat_id: str = ""):
        self.webhook_url = webhook_url
        self.fmt = fmt        # feishu | wecom | dingtalk | generic
        self.chat_id = chat_id

    # ── 同步发送 ─────────────────────────────────────────────────────────────

    def send_message(self, message: str, chat_id: str = "") -> bool:
        if not self.webhook_url:
            return False
        try:
            payload = self._build_payload(message)
            session = requests.Session()
            session.trust_env = False
            r = session.post(self.webhook_url, json=payload, timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"[WebhookNotifier] 发送失败: {e}")
            return False

    def send_message_async(self, message: str) -> None:
        t = threading.Thread(target=self.send_message, args=(message,), daemon=True)
        t.start()

    def send_rich(self, title: str, body: str) -> bool:
        return self.send_message(f"**{title}**\n\n{body}" if self.fmt != "feishu" else body)

    # ── Payload 构造 ─────────────────────────────────────────────────────────

    def _build_payload(self, message: str) -> dict:
        if self.fmt == "feishu":
            return {
                "msg_type": "text",
                "content": {"text": message},
            }
        if self.fmt == "wecom":
            return {
                "msgtype": "text",
                "text": {"content": message},
            }
        if self.fmt == "dingtalk":
            return {
                "msgtype": "text",
                "text": {"content": message},
            }
        # generic: plain JSON body understood by most webhook endpoints
        return {"text": message, "content": message}

    # ── Null 模式（未配置时） ──────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)


class _NullNotifier:
    """URL 未配置时的无操作占位符。"""
    enabled = False

    def send_message(self, *a, **kw) -> bool:
        return False

    def send_message_async(self, *a, **kw) -> None:
        pass

    def send_rich(self, *a, **kw) -> bool:
        return False


def get_notifier(chat_id: str = "") -> "WebhookNotifier | _NullNotifier":
    global _notifier_instance
    if _notifier_instance is None:
        with _notifier_lock:
            if _notifier_instance is None:
                _notifier_instance = _build_notifier()
    return _notifier_instance


def _build_notifier() -> "WebhookNotifier | _NullNotifier":
    url = os.environ.get("NOTIFICATION_WEBHOOK_URL", "").strip()
    fmt = os.environ.get("NOTIFICATION_WEBHOOK_FORMAT", "feishu").strip()

    if not url:
        try:
            from config.loader import cfg as _cfg
            url = _cfg("notifications", "webhook_url", "")
            fmt = _cfg("notifications", "webhook_format", "feishu")
        except Exception:
            pass

    if not url:
        return _NullNotifier()

    return WebhookNotifier(webhook_url=url, fmt=fmt)
