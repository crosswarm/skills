"""AgentSelfMonitorMixin — stub for compact deployable (监控已剥离)。"""
from typing import ClassVar, Optional


class AgentSelfMonitorMixin:
    expected_run_interval_hours: ClassVar[Optional[float]] = None
    self_monitor_grace_factor: ClassVar[float] = 2.0

    def self_check_last_run(self) -> dict:
        return {"status": "stub", "alert": False}
