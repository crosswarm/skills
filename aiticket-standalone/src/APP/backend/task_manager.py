"""
后台任务管理器 - 用于异步生成报告
"""

import threading
import time
import uuid
from typing import Dict, Optional, Callable, Any
from datetime import datetime
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BackgroundTask:
    """后台任务对象"""

    def __init__(self, task_id: str, task_type: str, params: Dict):
        self.task_id = task_id
        self.task_type = task_type
        self.params = params
        self.status = TaskStatus.PENDING
        self.progress = 0
        self.message = ""
        self.result: Optional[Any] = None
        self.error: Optional[str] = None
        self.created_at = datetime.now()
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self._cancel_flag = False

    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status.value,
            "progress": self.progress,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class TaskManager:
    """后台任务管理器 - 单例模式"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.tasks: Dict[str, BackgroundTask] = {}
        self._tasks_lock = threading.Lock()
        # 启动清理线程
        self._start_cleanup_thread()

    def create_task(self, task_type: str, params: Dict) -> BackgroundTask:
        """创建新任务"""
        task_id = str(uuid.uuid4())[:8]
        task = BackgroundTask(task_id, task_type, params)
        with self._tasks_lock:
            self.tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[BackgroundTask]:
        """获取任务"""
        with self._tasks_lock:
            return self.tasks.get(task_id)

    def start_task(self, task_id: str, func: Callable, **kwargs) -> bool:
        """启动任务执行"""
        task = self.get_task(task_id)
        if not task or task.status != TaskStatus.PENDING:
            return False

        def run_task():
            try:
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now()

                # 执行任务，传递进度更新回调
                def update_progress(progress: int, message: str = ""):
                    task.progress = min(100, max(0, progress))
                    task.message = message
                    if task._cancel_flag:
                        raise Exception("Task cancelled")

                result = func(
                    progress_callback=update_progress,
                    **kwargs
                )

                task.result = result
                task.progress = 100
                task.status = TaskStatus.COMPLETED
                task.completed_at = datetime.now()

            except Exception as e:
                if "cancelled" in str(e).lower():
                    task.status = TaskStatus.CANCELLED
                else:
                    task.status = TaskStatus.FAILED
                    task.error = str(e)
                task.completed_at = datetime.now()

        thread = threading.Thread(target=run_task, daemon=True)
        thread.start()
        return True

    def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        task = self.get_task(task_id)
        if not task:
            return False
        if task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
            return False

        task._cancel_flag = True
        return True

    def list_tasks(self, task_type: Optional[str] = None) -> list:
        """列出任务"""
        with self._tasks_lock:
            tasks = list(self.tasks.values())
            if task_type:
                tasks = [t for t in tasks if t.task_type == task_type]
            return [t.to_dict() for t in sorted(tasks, key=lambda x: x.created_at, reverse=True)]

    def _start_cleanup_thread(self):
        """启动清理过期任务的线程"""
        def cleanup():
            while True:
                time.sleep(3600)  # 每小时清理一次
                self._cleanup_old_tasks()

        thread = threading.Thread(target=cleanup, daemon=True)
        thread.start()

    def _cleanup_old_tasks(self):
        """清理24小时前的已完成任务"""
        cutoff = datetime.now()
        with self._tasks_lock:
            to_remove = []
            for task_id, task in self.tasks.items():
                if task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
                    if task.completed_at:
                        age = (cutoff - task.completed_at).total_seconds()
                        if age > 86400:  # 24小时
                            to_remove.append(task_id)
            for task_id in to_remove:
                del self.tasks[task_id]


# 全局单例
task_manager = TaskManager()


def get_task_manager() -> TaskManager:
    """获取任务管理器单例"""
    return task_manager