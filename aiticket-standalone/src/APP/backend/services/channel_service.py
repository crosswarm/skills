"""
多用户通道管理服务 (Spec Phase 4)

通道配置存储: APP/backend/data/channels/{user_id}.json
支持: 飞书通知开关、通知类型过滤、个人偏好设置
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from services.feishu_notifier import FeishuNotifier

logger = logging.getLogger(__name__)

CHANNELS_DIR = Path(__file__).parent.parent / "data" / "channels"
CHANNELS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_NOTIFY_TYPES = [
    "requirement_review",   # 需求分析卡片推送
    "prd_ready",            # PRD 生成完成
    "memory_health",        # 记忆健康报告
    "schedule_result",      # 定时任务执行结果
]


def _channel_path(user_id: str) -> Path:
    return CHANNELS_DIR / f"{user_id}.json"


def _default_config(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "display_name": user_id,
        "channels": {
            "feishu": {
                "enabled": True,
                "chat_id": FeishuNotifier.DEFAULT_CHAT_ID,
                "openclaw_user_id": "",
                "notify_types": DEFAULT_NOTIFY_TYPES[:],
            }
        },
        "preferences": {
            "auto_confirm_timeout": 60,      # 分钟：超时自动确认
            "min_score_for_notify": 7,       # 最低价值评分才推送
            "assigned_modules": [],          # 负责的模块列表
        },
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }


class ChannelService:
    """多用户通道管理服务"""

    def get_config(self, user_id: str) -> dict:
        """获取用户通道配置（不存在则返回默认值，不自动创建文件）"""
        path = _channel_path(user_id)
        if not path.exists():
            return _default_config(user_id)
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save_config(self, user_id: str, config: dict) -> dict:
        """保存用户通道配置"""
        config["user_id"] = user_id
        config["updated_at"] = datetime.utcnow().isoformat()
        if "created_at" not in config:
            config["created_at"] = config["updated_at"]
        with open(_channel_path(user_id), "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        logger.info(f"[Channel] 通道配置已保存: {user_id}")
        return config

    def update_config(self, user_id: str, updates: dict) -> dict:
        """部分更新通道配置（深度合并）"""
        current = self.get_config(user_id)
        self._deep_merge(current, updates)
        return self.save_config(user_id, current)

    def _deep_merge(self, base: dict, updates: dict) -> None:
        for k, v in updates.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v

    def list_all(self) -> list:
        """列出所有用户配置（管理员）"""
        result = []
        for path in CHANNELS_DIR.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    result.append(json.load(f))
            except Exception:
                pass
        return result

    def test_channel(self, user_id: str) -> bool:
        """发送测试消息验证通道连通性"""
        config = self.get_config(user_id)
        feishu_cfg = config.get("channels", {}).get("feishu", {})
        if not feishu_cfg.get("enabled", False):
            logger.info(f"[Channel] 用户 {user_id} 飞书通道未启用")
            return False

        chat_id = feishu_cfg.get("chat_id") or FeishuNotifier.DEFAULT_CHAT_ID
        notifier = FeishuNotifier(chat_id=chat_id)
        display = config.get("display_name", user_id)
        return notifier.send_message(
            f"✅ 通道测试成功！\n用户: {display}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def should_notify(self, user_id: str, notify_type: str) -> bool:
        """检查是否应该向该用户推送特定类型的通知"""
        config = self.get_config(user_id)
        feishu_cfg = config.get("channels", {}).get("feishu", {})
        if not feishu_cfg.get("enabled", True):
            return False
        allowed_types = feishu_cfg.get("notify_types", DEFAULT_NOTIFY_TYPES)
        return notify_type in allowed_types

    def get_notifier_for_user(self, user_id: str) -> Optional[FeishuNotifier]:
        """获取用户对应的 FeishuNotifier（使用其配置的 chat_id）"""
        config = self.get_config(user_id)
        feishu_cfg = config.get("channels", {}).get("feishu", {})
        if not feishu_cfg.get("enabled", True):
            return None
        chat_id = feishu_cfg.get("chat_id") or FeishuNotifier.DEFAULT_CHAT_ID
        return FeishuNotifier(chat_id=chat_id)


# 全局单例
_channel_service: Optional[ChannelService] = None


def get_channel_service() -> ChannelService:
    global _channel_service
    if _channel_service is None:
        _channel_service = ChannelService()
    return _channel_service
