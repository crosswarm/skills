"""
飞书通知服务 - 质量审查委员会报告推送
通过OpenClaw发送飞书消息
"""

import os
import shutil
import subprocess
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)


class FeishuNotifier:
    """飞书通知器 - 通过OpenClaw发送消息，支持三层降级：本地→SSH→静默"""

    # Aiticket会话ID (从OpenClaw配置获取)
    DEFAULT_CHAT_ID = "oc_72ef8553bb8b552435cd91b0fb1e86ab"

    # Plan B SSH 配置（环境变量覆盖，便于测试）
    SSH_HOST = os.environ.get("OPENCLAW_SSH_HOST", "cfone.cn")
    SSH_PORT = os.environ.get("OPENCLAW_SSH_PORT", "20000")
    SSH_USER = os.environ.get("OPENCLAW_SSH_USER", "cf")
    SSH_KEY  = os.environ.get("OPENCLAW_SSH_KEY",  "/root/.ssh/cross")

    def __init__(self, chat_id: Optional[str] = None):
        self.chat_id = chat_id or self.DEFAULT_CHAT_ID
        self.channel = "feishu"

    # NVM 安装的 openclaw 全路径（SSH 会话不加载 .zshrc，需显式指定）
    _OPENCLAW_CANDIDATES = [
        "openclaw",
        os.path.expanduser("~/.nvm/versions/node/v24.13.0/bin/openclaw"),
        os.path.expanduser("~/.nvm/versions/node/v22.0.0/bin/openclaw"),
        "/usr/local/bin/openclaw",
    ]

    @classmethod
    def _find_openclaw(cls) -> Optional[str]:
        """找到可执行的 openclaw 路径"""
        for candidate in cls._OPENCLAW_CANDIDATES:
            if shutil.which(candidate) or (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                return candidate
        # 通配 NVM 版本目录
        import glob
        for p in sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/openclaw")), reverse=True):
            if os.access(p, os.X_OK):
                return p
        return None

    def _send_local(self, target: str, message: str) -> bool:
        """优先级1：本地 openclaw subprocess（Mac Mini / 开发环境）"""
        openclaw_bin = self._find_openclaw()
        if not openclaw_bin:
            return False
        # 确保 node 在 PATH 中（NVM 环境不在 uvicorn subprocess 继承的环境里）
        env = os.environ.copy()
        node_dir = os.path.dirname(openclaw_bin)
        env['PATH'] = node_dir + os.pathsep + env.get('PATH', '')
        try:
            result = subprocess.run(
                [openclaw_bin, "message", "send",
                 "--channel", self.channel,
                 "--target", target,
                 "--message", message],
                capture_output=True, text=True, timeout=30,
                env=env,
            )
            if result.returncode == 0:
                logger.info(f"✅ [local] 飞书消息发送成功: {result.stdout.strip()}")
                return True
            logger.error(f"❌ [local] 飞书消息发送失败: {result.stderr}")
            return False
        except subprocess.TimeoutExpired:
            logger.error("❌ [local] 飞书消息发送超时")
            return False

    def _send_via_ssh(self, target: str, message: str) -> bool:
        """优先级2：Plan B — 通过 SSH 到 Mac Mini 执行 openclaw（QCL 生产环境）"""
        safe_msg = message.replace("'", "'\\''")
        safe_target = target.replace("'", "'\\''")
        remote_cmd = (
            f"OC=$(ls ~/.nvm/versions/node/*/bin/openclaw 2>/dev/null | tail -1); "
            f"[ -z \"$OC\" ] && OC=openclaw; "
            f"NODE_BIN=$(dirname \"$OC\" 2>/dev/null); "
            f"[ -n \"$NODE_BIN\" ] && export PATH=\"$NODE_BIN:$PATH\"; "
            f"$OC message send --channel feishu --target '{safe_target}' --message '{safe_msg}'"
        )
        # 修正 SSH key 路径：优先使用 ~/（运行用户 home）
        ssh_key = self.SSH_KEY
        if not os.path.exists(ssh_key):
            fallback = os.path.expanduser("~/.ssh/cross")
            if os.path.exists(fallback):
                ssh_key = fallback
        ssh_cmd = [
            "ssh",
            "-i", ssh_key,
            "-p", self.SSH_PORT,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{self.SSH_USER}@{self.SSH_HOST}",
            remote_cmd,
        ]
        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                logger.info(f"✅ [ssh] 飞书消息发送成功")
                return True
            logger.error(f"❌ [ssh] 飞书消息发送失败: {result.stderr}")
            return False
        except subprocess.TimeoutExpired:
            logger.error("❌ [ssh] 飞书消息发送超时")
            return False
        except Exception as e:
            logger.error(f"❌ [ssh] 飞书消息发送异常: {e}")
            return False

    def send_message(self, message: str, chat_id: Optional[str] = None) -> bool:
        """
        发送纯文本消息，三层降级：本地 openclaw → SSH → 静默入队（等 Plan A 轮询）

        Args:
            message: 消息内容
            chat_id: 会话ID，默认使用Aiticket会话

        Returns:
            bool: 是否发送成功
        """
        target = chat_id or self.chat_id

        # 优先级 1: 本地 openclaw（Mac Mini / 开发）—— 含 NVM 路径探测
        if self._find_openclaw():
            return self._send_local(target, message)

        # 优先级 2: Plan B — SSH 到 Mac Mini
        ssh_key = self.SSH_KEY if os.path.exists(self.SSH_KEY) else os.path.expanduser("~/.ssh/cross")
        if os.path.exists(ssh_key):
            logger.info("[feishu_notifier] 本地无 openclaw，切换 SSH 降级")
            return self._send_via_ssh(target, message)

        # 优先级 3: 静默失败，等 Plan A Mac Mini 轮询捞起
        logger.warning("[feishu_notifier] 无法发送飞书消息（无本地 openclaw 也无 SSH key），等待 Mac Mini 轮询")
        return False
    
    def notify_pending_decision(self, decision: dict) -> bool:
        """发送待决策飞书通知，格式：#ID 描述 / 选项 / 默认 + 超时。"""
        decision_id = decision.get("id", "?")
        description = decision.get("description", "待决策")
        options = decision.get("options") or {}
        default = decision.get("default", "A")
        expires_at = decision.get("expires_at", "")
        hours_left = 3
        try:
            from datetime import datetime as _dt
            exp = _dt.fromisoformat(expires_at)
            hours_left = max(1, int((exp - _dt.utcnow()).total_seconds() / 3600))
        except Exception:
            pass
        if isinstance(options, dict):
            opts_lines = "\n".join(f"  {k}: {v}" for k, v in options.items())
        else:
            opts_lines = "\n".join(f"  {o.get('id','?')}: {o.get('label','')}" for o in options)
        msg = (
            f"🤖 JobMaster 待决策 #{decision_id}\n\n"
            f"{description}\n\n"
            f"{opts_lines}\n\n"
            f"回复『执行 #{decision_id} {default}』确认，或 {hours_left}h 后自动选 {default}"
        )
        return self.send_message(msg)

    def send_quality_review_report(
        self,
        review_target: str,
        overall_rating: str,
        p0_count: int,
        p1_count: int,
        p2_count: int,
        release_recommendation: str,
        details_url: Optional[str] = None
    ) -> bool:
        """
        发送质量审查报告摘要
        """
        rating_emoji = {"A": "🟢", "B": "🔵", "C": "🟡", "D": "🔴"}.get(overall_rating, "⚪")
        release_emoji = {"建议发布": "✅", "条件发布": "⚠️", "不建议发布": "🚫"}.get(release_recommendation, "❓")
        
        message = f"""📋 **质量审查委员会报告**

**评审对象**: {review_target}
**评审时间**: {datetime.now().strftime("%Y-%m-%d %H:%M")}

---

**总体评级**: {rating_emoji} {overall_rating}

**问题统计**:
• P0-阻断级: {p0_count} 个
• P1-严重级: {p1_count} 个  
• P2-重要级: {p2_count} 个

**发布建议**: {release_emoji} {release_recommendation}

---

{f"📄 [查看详细报告]({details_url})" if details_url else "📄 详细报告已保存到本地"}

---
💡 如需讨论，请在此会话中回复
"""
        return self.send_message(message)


# 全局实例
_notifier: Optional[FeishuNotifier] = None


def get_notifier(chat_id: str = ""):
    """获取通知器实例（deployable 版返回通用 WebhookNotifier）"""
    from services.webhook_notifier import get_notifier as _wh_get
    return _wh_get(chat_id=chat_id)


def notify_quality_review_completed(
    review_target: str,
    overall_rating: str,
    p0_count: int,
    p1_count: int,
    p2_count: int,
    release_recommendation: str,
    details_url: Optional[str] = None
) -> bool:
    """
    便捷函数：发送质量审查完成通知
    """
    return get_notifier().send_quality_review_report(
        review_target=review_target,
        overall_rating=overall_rating,
        p0_count=p0_count,
        p1_count=p1_count,
        p2_count=p2_count,
        release_recommendation=release_recommendation,
        details_url=details_url
    )
