"""
通用会话保活管理器 SessionKeepAliveManager

任何需要定期刷新 session token 的系统（PM / Jira / BIP 等）
都可以注册一个刷新函数，由此统一调度和监控。

使用方式（以 PM 为例）:
    mgr = get_keepalive_manager()
    mgr.register(
        name="pm_original_demand",
        label="PM 原始需求",
        refresh_fn=pm_svc._refresh_token,   # callable -> bool
        interval_minutes=25,
    )

以 Jira 为例:
    mgr.register(
        name="jira",
        label="Jira",
        refresh_fn=lambda: jira_svc.refresh_jira_session(),
        interval_minutes=30,
    )

状态查询（供 /api/health 或管理 API 使用）:
    mgr.get_status()  -> [{name, label, last_refresh, last_ok, next_refresh, consecutive_failures}]
"""

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional


class _SessionEntry:
    def __init__(
        self,
        name: str,
        label: str,
        refresh_fn: Callable[[], bool],
        interval_minutes: int,
        validate_fn: Optional[Callable[[], bool]] = None,
        alert_fn: Optional[Callable[[str, str, int], None]] = None,
        alert_after_failures: int = 2,
    ):
        self.name = name
        self.label = label
        self.refresh_fn = refresh_fn
        self.interval_minutes = interval_minutes
        self.validate_fn = validate_fn           # 额外有效性验证（可选）
        self.alert_fn = alert_fn                 # 失败告警回调(label, reason, consecutive_failures)
        self.alert_after_failures = alert_after_failures
        self.last_refresh: Optional[datetime] = None
        self.last_ok: Optional[bool] = None
        self.last_validate_ok: Optional[bool] = None
        self.consecutive_failures: int = 0
        self.next_refresh: datetime = datetime.now() + timedelta(minutes=interval_minutes)
        self._alerted: bool = False              # 防止重复告警

    def to_dict(self) -> Dict[str, Any]:
        now = datetime.now()
        overdue = self.next_refresh < now - timedelta(minutes=2)  # 超过2分钟未刷新视为异常
        return {
            "name": self.name,
            "label": self.label,
            "interval_minutes": self.interval_minutes,
            "last_refresh": self.last_refresh.isoformat() if self.last_refresh else None,
            "last_ok": self.last_ok,
            "last_validate_ok": self.last_validate_ok,
            "next_refresh": self.next_refresh.isoformat(),
            "consecutive_failures": self.consecutive_failures,
            "overdue": overdue,
            "status": (
                "error" if self.consecutive_failures >= 1 else
                "warning" if overdue else
                "ok" if self.last_ok else
                "pending"
            ),
        }


class SessionKeepAliveManager:
    """
    全局会话保活调度器。
    内部维护一个后台线程，每 30 秒检查一次是否有到期的刷新任务。
    """

    def __init__(self):
        self._sessions: Dict[str, _SessionEntry] = {}
        self._lock = threading.Lock()
        self._started = False

    def register(
        self,
        name: str,
        label: str,
        refresh_fn: Callable[[], bool],
        interval_minutes: int = 25,
        validate_fn: Optional[Callable[[], bool]] = None,
        alert_fn: Optional[Callable[[str, str, int], None]] = None,
        alert_after_failures: int = 2,
    ) -> None:
        """
        注册一个会话保活任务。
        - name: 唯一标识（如 "pm", "jira", "bip"）
        - label: 显示名称
        - refresh_fn: 无参可调用，返回 True 表示刷新成功
        - interval_minutes: 刷新间隔（分钟）
        - validate_fn: 可选，额外验证 token 是否真正有效（如发一个轻量 API 请求）
        - alert_fn: 可选，失败告警回调 (label, reason, consecutive_failures)
        - alert_after_failures: 连续失败多少次后触发告警（默认 2）
        """
        with self._lock:
            if name in self._sessions:
                entry = self._sessions[name]
                entry.refresh_fn = refresh_fn
                entry.interval_minutes = interval_minutes
                if validate_fn is not None:
                    entry.validate_fn = validate_fn
                if alert_fn is not None:
                    entry.alert_fn = alert_fn
            else:
                self._sessions[name] = _SessionEntry(
                    name, label, refresh_fn, interval_minutes,
                    validate_fn=validate_fn, alert_fn=alert_fn,
                    alert_after_failures=alert_after_failures,
                )
            print(f"[SessionKeepAlive] 注册会话: {label} ({name})，间隔 {interval_minutes} 分钟")

        self._ensure_running()

    def refresh_now(self, name: str) -> bool:
        """立即触发指定会话刷新（供 API 调用）"""
        with self._lock:
            entry = self._sessions.get(name)
        if not entry:
            return False
        return self._do_refresh(entry)

    def get_status(self) -> List[Dict[str, Any]]:
        """返回所有注册会话的状态列表"""
        with self._lock:
            return [e.to_dict() for e in self._sessions.values()]

    def get_session_status(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._sessions.get(name)
        return entry.to_dict() if entry else None

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _ensure_running(self) -> None:
        if self._started:
            return
        self._started = True
        t = threading.Thread(target=self._scheduler_loop, daemon=True, name="session-keepalive")
        t.start()
        print("[SessionKeepAlive] 后台调度线程已启动")

    def _scheduler_loop(self) -> None:
        """每 30 秒检查一次是否有到期的刷新任务"""
        while True:
            time.sleep(30)
            now = datetime.now()
            with self._lock:
                due = [e for e in self._sessions.values() if e.next_refresh <= now]
            for entry in due:
                self._do_refresh(entry)

    def _do_refresh(self, entry: _SessionEntry) -> bool:
        # Step 1: 执行刷新脚本
        try:
            ok = bool(entry.refresh_fn())
        except Exception as exc:
            print(f"[SessionKeepAlive] {entry.label} 刷新异常: {exc}")
            ok = False

        # Step 2: 验证 token 有效性
        validate_ok = None
        if entry.validate_fn is not None:
            try:
                validate_ok = bool(entry.validate_fn())
            except Exception as exc:
                print(f"[SessionKeepAlive] {entry.label} token 验证异常: {exc}")
                validate_ok = False

            if ok and not validate_ok:
                print(f"[SessionKeepAlive] ⚠️  {entry.label} 脚本成功但 token 验证失败")
                ok = False
            elif not ok and validate_ok:
                # 脚本失败（如 Chrome cookie 丢失）但缓存 token 仍然有效 → 视为成功
                print(f"[SessionKeepAlive] ℹ️  {entry.label} 刷新脚本失败但缓存 token 仍有效，跳过告警")
                ok = True

        now = datetime.now()
        should_alert = False
        alert_reason = ""
        with self._lock:
            entry.last_refresh = now
            entry.last_ok = ok
            entry.last_validate_ok = validate_ok
            entry.next_refresh = now + timedelta(minutes=entry.interval_minutes)
            if ok:
                entry.consecutive_failures = 0
                entry._alerted = False  # 恢复正常后重置告警状态
                print(f"[SessionKeepAlive] ✅ {entry.label} 刷新成功"
                      f"{' (token验证✓)' if validate_ok else ''}，"
                      f"下次: {entry.next_refresh.strftime('%H:%M:%S')}")
            else:
                entry.consecutive_failures += 1
                n = entry.consecutive_failures
                print(f"[SessionKeepAlive] ⚠️  {entry.label} 刷新失败（连续 {n} 次）")
                # 达到告警阈值且本轮未已告警
                if n >= entry.alert_after_failures and not entry._alerted and entry.alert_fn:
                    should_alert = True
                    alert_reason = f"连续失败 {n} 次，请检查 Chrome 是否已登录"
                    entry._alerted = True

        # Step 3: 在锁外执行告警（避免死锁）
        if should_alert and entry.alert_fn:
            try:
                entry.alert_fn(entry.label, alert_reason, entry.consecutive_failures)
            except Exception as exc:
                print(f"[SessionKeepAlive] 告警发送失败: {exc}")

        return ok


# ------------------------------------------------------------------
# 全局单例
# ------------------------------------------------------------------
_manager: Optional[SessionKeepAliveManager] = None
_manager_lock = threading.Lock()


def get_keepalive_manager() -> SessionKeepAliveManager:
    """获取全局 SessionKeepAliveManager 单例"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = SessionKeepAliveManager()
    return _manager
