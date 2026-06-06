"""
定时调度服务 (Spec Phase 3)
轻量实现: threading.Timer + JSON 配置 + croniter 表达式解析

支持:
  1. Cron 定时触发    (cron: "0 9 * * 1")
  2. 事件条件触发     (trigger_type: "event", condition: "new_requirement_count >= 5")
  3. 冷却期保护       (cooldown_hours: 24)

调度文件存放: APP/backend/data/schedules/{schedule_id}.json
"""

import json
import os
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Optional

from croniter import croniter

logger = logging.getLogger(__name__)

SCHEDULES_DIR = Path(__file__).parent.parent / "data" / "schedules"
SCHEDULES_DIR.mkdir(parents=True, exist_ok=True)

# 任务类型 → 可调用处理器注册表（在 main.py 中注册）
_task_handlers: Dict[str, Callable] = {}


def register_task_handler(task_type: str, handler: Callable) -> None:
    """注册任务类型处理器（在 main.py 启动时调用）"""
    _task_handlers[task_type] = handler
    logger.info(f"[Scheduler] 注册任务处理器: {task_type}")


def _load_schedule(path: Path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[Scheduler] 加载调度配置失败 {path}: {e}")
        return None


def _save_schedule(schedule: dict) -> None:
    path = SCHEDULES_DIR / f"{schedule['id']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(schedule, f, ensure_ascii=False, indent=2)


def _load_all_schedules() -> list:
    schedules = []
    for path in SCHEDULES_DIR.glob("*.json"):
        s = _load_schedule(path)
        if s and isinstance(s, dict):
            schedules.append(s)
    return schedules


def _is_in_cooldown(schedule: dict) -> bool:
    """检查是否在冷却期内（上次执行到现在不足 cooldown_hours）"""
    cooldown_hours = schedule.get("cooldown_hours", 0)
    if not cooldown_hours:
        return False
    last_run = schedule.get("last_run")
    if not last_run:
        return False
    last_dt = datetime.fromisoformat(last_run)
    return datetime.utcnow() < last_dt + timedelta(hours=cooldown_hours)


def _cron_last_expected(cron_expr: str, now: datetime) -> Optional[datetime]:
    """返回 cron 在 now 时刻或之前最近一次应该触发的时间"""
    try:
        return croniter(cron_expr, now + timedelta(seconds=1)).get_prev(datetime)
    except Exception:
        return None


def _cron_is_due(schedule: dict, now: datetime) -> bool:
    """检查 Cron 定时任务是否到期（±60s 窗口 + missed-run 补跑）

    Scenarios:
      A. 正常运行：now=01:00:30, cron 0 1 * * * → 窗口命中，触发
      B. 宕机2小时重启：now=03:05, last_run=昨天01:00 → 补跑，触发
      C. 已跑过：now=01:30, last_run=今天01:00 → last_expected=today01:00=last_run → 不重复触发
      D. 超过补跑窗口：last_expected=3天前 → age>catchup_window_hours → 跳过
      E. 从未运行(last_run=None)：仅走窗口触发，不补跑（避免全量立即触发）
    """
    cron_expr = schedule.get("cron")
    if not cron_expr:
        return False
    try:
        # 1. 标准 ±60s 窗口
        if croniter(cron_expr, now - timedelta(seconds=60)).get_next(datetime) <= now:
            return True

        # 2. Missed-run 补跑（仅对有 last_run 记录的任务）
        last_run_str = schedule.get("last_run")
        if not last_run_str:
            return False

        last_run = datetime.fromisoformat(str(last_run_str).replace("Z", "").split("+")[0])
        last_expected = _cron_last_expected(cron_expr, now)
        if not last_expected or last_expected <= last_run:
            return False

        # 上次期望时间 > 实际 last_run → 有漏跑，检查补跑窗口
        catchup_hours = schedule.get("catchup_window_hours", 24)
        if (now - last_expected).total_seconds() <= catchup_hours * 3600:
            logger.info(
                f"[Scheduler] 补跑 {schedule.get('id')}: "
                f"期望={last_expected.isoformat()}, last_run={last_run.isoformat()}"
            )
            return True

        return False
    except Exception as e:
        logger.error(f"[Scheduler] cron 表达式解析失败: {e}")
        return False


def _event_is_triggered(schedule: dict) -> bool:
    """事件条件触发检查。当前版本始终返回 False（事件驱动调度未启用）。"""
    return False


class SchedulerService:
    """
    轻量定时调度服务。
    启动后每 60s 扫描一次所有调度配置，判断是否需要执行。
    """

    CHECK_INTERVAL = 60   # 每 60s 检查一次

    def __init__(self):
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """后端启动时调用，启动调度循环"""
        if self._running:
            return
        self._running = True
        self._schedule_next()
        logger.info("[Scheduler] 调度服务已启动")

    def stop(self) -> None:
        self._running = False
        if self._timer:
            self._timer.cancel()
        logger.info("[Scheduler] 调度服务已停止")

    def _schedule_next(self) -> None:
        if not self._running:
            return
        self._timer = threading.Timer(self.CHECK_INTERVAL, self._tick)
        self._timer.daemon = True
        self._timer.start()

    _tick_count: int = 0
    _HB_PATH = Path(__file__).parent.parent / "data" / "runtime" / "scheduler_heartbeat.json"

    def _tick(self) -> None:
        """每 60s 执行一次的主循环"""
        try:
            self._check_and_execute()
        except Exception as e:
            logger.error(f"[Scheduler] tick 异常: {e}")
        finally:
            try:
                self._HB_PATH.parent.mkdir(parents=True, exist_ok=True)
                self._HB_PATH.write_text(json.dumps({
                    "ts": time.time(),
                    "tick_count": self._tick_count,
                    "last_check_at": datetime.utcnow().isoformat(),
                }), encoding="utf-8")
                self._tick_count += 1
            except Exception:
                pass
            self._schedule_next()

    def _check_and_execute(self) -> None:
        now = datetime.utcnow()
        schedules = _load_all_schedules()

        for schedule in schedules:
            if not schedule.get("enabled", True):
                continue
            if _is_in_cooldown(schedule):
                continue

            # deployable worktree 禁用周报（只跑月报）
            if (os.environ.get("AITICKET_DEPLOYABLE") == "1"
                    and schedule.get("task_type") == "weekly_report"):
                logger.info(f"[Scheduler] skip weekly_report on deployable worktree: {schedule['id']}")
                continue

            trigger_type = schedule.get("trigger_type", "cron")
            should_run = False

            if trigger_type == "cron":
                should_run = _cron_is_due(schedule, now)
            elif trigger_type == "event":
                should_run = _event_is_triggered(schedule)

            if should_run:
                self._execute(schedule, now)

    def _execute(self, schedule: dict, now: datetime) -> None:
        """执行一个调度任务"""
        task_type = schedule.get("task_type")
        handler = _task_handlers.get(task_type)

        if not handler:
            logger.warning(f"[Scheduler] 未找到任务处理器: {task_type}")
            return

        logger.info(f"[Scheduler] 执行任务: {schedule['id']} ({task_type})")

        # 更新 last_run（先保存，防止重入）
        # Fix C2: 保存执行前旧值，失败时在 finally 里还原，
        # 避免 cooldown 从失败时刻起算锁死重试（月报 600h≈25天）
        _prev_last_run = schedule.get("last_run")
        schedule["last_run"] = now.isoformat()
        _save_schedule(schedule)

        # 写 agent_tasks 入库（让 history_count 能统计到 cron 任务）
        try:
            from services.task_bridge import notify_trigger, notify_done
            _task_id = notify_trigger(
                schedule_id=schedule["id"],
                title=schedule.get("name") or schedule["id"],
            )
        except Exception:
            _task_id = None

        import time as _time
        _start = _time.monotonic()
        status = "success"
        error_msg = ""
        try:
            params = dict(schedule.get("params") or {})
            # 注入 schedule 上下文，供 script 类 handler 读取 command / id
            params.setdefault("_schedule_id", schedule.get("id", ""))
            params.setdefault("_schedule_command", schedule.get("command", []))
            handler(**params)
            logger.info(f"[Scheduler] 任务完成: {schedule['id']}")
        except Exception as e:
            logger.error(f"[Scheduler] 任务执行失败 {schedule['id']}: {e}")
        finally:
            duration_s = round(_time.monotonic() - _start, 1)
            # 持久化运行历史（run_count / last_status / recent_runs）
            try:
                fresh = _load_schedule(SCHEDULES_DIR / f"{schedule['id']}.json") or schedule
                fresh["run_count"] = fresh.get("run_count", 0) + 1
                fresh["last_status"] = status
                if error_msg:
                    fresh["last_error"] = error_msg
                elif "last_error" in fresh:
                    del fresh["last_error"]
                # Fix C2: 失败时还原 last_run，让 cooldown/catchup 可在下一 tick 重试
                if status == "failed":
                    if _prev_last_run is not None:
                        fresh["last_run"] = _prev_last_run
                    else:
                        fresh.pop("last_run", None)
                run_record = {"ts": now.isoformat(), "status": status, "duration_s": duration_s}
                runs = fresh.get("recent_runs", [])
                runs.append(run_record)
                fresh["recent_runs"] = runs[-10:]  # 最多保留 10 条
                _save_schedule(fresh)
            except Exception as _e:
                logger.warning(f"[Scheduler] 运行历史写入失败 {schedule['id']}: {_e}")
            # 同步更新 agent_tasks 状态
            try:
                if _task_id:
                    from services.task_bridge import notify_done
                    notify_done(_task_id, success=(status == "success"), msg=error_msg or "ok")
            except Exception:
                pass

    # ─── CRUD API ────────────────────────────────────────────────────────────

    def create_schedule(self, data: dict) -> dict:
        """创建调度任务"""
        import uuid
        if not data.get("id"):
            data["id"] = str(uuid.uuid4())[:8]
        data.setdefault("enabled", True)
        data.setdefault("last_run", None)
        data.setdefault("created_at", datetime.utcnow().isoformat())
        _save_schedule(data)
        logger.info(f"[Scheduler] 创建调度: {data['id']}")
        return data

    def get_schedule(self, schedule_id: str) -> Optional[dict]:
        path = SCHEDULES_DIR / f"{schedule_id}.json"
        return _load_schedule(path) if path.exists() else None

    def list_schedules(self) -> list:
        return _load_all_schedules()

    def update_schedule(self, schedule_id: str, updates: dict) -> Optional[dict]:
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return None
        schedule.update(updates)
        _save_schedule(schedule)
        return schedule

    def delete_schedule(self, schedule_id: str) -> bool:
        path = SCHEDULES_DIR / f"{schedule_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def trigger_now(self, schedule_id: str) -> bool:
        """立即触发（用于测试）"""
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return False
        self._execute(schedule, datetime.utcnow())
        return True


# 全局单例
_scheduler_instance: Optional[SchedulerService] = None


def get_scheduler() -> SchedulerService:
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = SchedulerService()
    return _scheduler_instance


def start_scheduler() -> SchedulerService:
    svc = get_scheduler()
    svc.start()
    return svc
