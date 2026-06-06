"""
ClaudeTaskScanner — 把 Claude Code 原生 bash run_in_background 任务
桥接到 agent_tasks SQLite，使其在 agents.html 中以 agent_name='claude' 出现。

设计文档：_local/conclusion/misc-temp/AGENT-CLAUDE-NATIVE-V1.md（方案 A）

输入：/tmp/claude-501/<sanitized-workspace>/<session-uuid>/tasks/*.output
输出：agent_tasks 表中 agent_name='claude' 的行
扫描周期：3 秒
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from agents.base import AgentStatus, AgentTask
from services.agent_task_store import AgentTaskStore


_SCAN_INTERVAL_SEC = 30
_TAIL_BYTES = 4096
_STALE_HOURS = 48           # 48h 之外的任务不再监控
_RUNNING_MTIME_THRESHOLD = 30   # mtime 在最近 30s 内 → 视为 running

# 默认跳过的 task ID 前缀：Claude Code 内部 hook 脚本，每次工具调用都触发，
# 噪声极大且不是用户关心的"任务"。可通过环境变量 CLAUDE_SCAN_INCLUDE_HOOKS=1 关闭过滤。
_SKIP_PREFIXES = ("hook_",)


def _detect_workspace_root() -> Optional[Path]:
    """从后端进程 cwd 向上找 .git 作为 workspace root。

    aiticket 后端通常运行在 APP/backend，向上找一次即定位到仓库根。
    """
    p = Path(os.getcwd()).resolve()
    for cur in [p, *p.parents]:
        if (cur / ".git").exists():
            return cur
    return p


def _sanitize_path_for_claude(path: Path) -> str:
    """Claude Code 把路径中的 / 替换为 -，作为 /tmp/claude-501/<X> 的目录名。"""
    return "-" + str(path).strip("/").replace("/", "-")


def _safe_read_head_tail(path: Path, head_bytes: int = 4096, tail_bytes: int = _TAIL_BYTES) -> tuple[str, str]:
    """读首尾各 N 字节，避免大日志文件全量加载。"""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "", ""
    try:
        with open(path, "rb") as fh:
            head = fh.read(min(head_bytes, size)).decode("utf-8", errors="ignore")
            if size > head_bytes + tail_bytes:
                fh.seek(size - tail_bytes)
                tail = fh.read(tail_bytes).decode("utf-8", errors="ignore")
            else:
                tail = head[-tail_bytes:] if size <= head_bytes else ""
                if size > head_bytes:
                    fh.seek(head_bytes)
                    tail = fh.read(size - head_bytes).decode("utf-8", errors="ignore")
        return head, tail
    except Exception:
        return "", ""


_PROGRESS_PATTERNS = [
    (re.compile(r"Rendered\s+(\d+)\s*/\s*(\d+)"), "render"),
    (re.compile(r"Encoded\s+(\d+)\s*/\s*(\d+)"), "encode"),
    (re.compile(r"Bundling\s+(\d+)%"), "bundle"),
    (re.compile(r"\b(\d+)\s*%\b"), "percent"),
]


def _infer_progress(tail: str) -> int:
    """从 tail 文本提取进度（0-99）。匹配 Remotion / 通用百分号格式。"""
    if not tail:
        return 0
    last_chunk = tail[-2000:]
    for pattern, kind in _PROGRESS_PATTERNS:
        matches = pattern.findall(last_chunk)
        if not matches:
            continue
        last = matches[-1]
        try:
            if isinstance(last, tuple) and len(last) == 2:
                cur, total = int(last[0]), int(last[1])
                if total <= 0:
                    return 0
                return min(99, int(cur / total * 100))
            elif isinstance(last, str):
                return min(99, int(last))
        except (ValueError, ZeroDivisionError):
            continue
    return 0


_COMPLETION_MARKERS = [
    "Done:", "✓", "Render complete", "Successfully rendered",
    "Build successful", "completed successfully",
]
_FAILURE_MARKERS = [
    "Traceback (most recent call last)",
    "Error:", "ERROR:",
    "non-zero exit", "exited with code",
    "command not found", "Permission denied",
]


def _infer_status(tail: str, mtime_age_sec: float) -> AgentStatus:
    """启发式状态推断：mtime 新 → running；否则看 tail 标记。"""
    if mtime_age_sec < _RUNNING_MTIME_THRESHOLD:
        return AgentStatus.RUNNING
    chunk = tail[-2000:] if tail else ""
    if any(m in chunk for m in _FAILURE_MARKERS):
        return AgentStatus.FAILED
    if any(m in chunk for m in _COMPLETION_MARKERS):
        return AgentStatus.SUCCEEDED
    return AgentStatus.SUCCEEDED


_TITLE_SKIP_PREFIXES = ("compdef:", "━", "─", "Warning:", "DeprecationWarning", "{")
_TITLE_SKIP_EXACT = {"null", "true", "false", "undefined"}
_TRUSTED_JSONL_PREFIX = str(Path.home() / ".claude" / "projects")


def _extract_title_from_lines(text: str, prefix: str = "") -> str:
    """从文本行中提取第一个有意义的标题行。"""
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 4:
            continue
        if line.startswith(_TITLE_SKIP_PREFIXES):
            continue
        if line in _TITLE_SKIP_EXACT:
            continue
        # 跳过纯数字/标点行
        if all(c in "0123456789.-_/\\|+= \t" for c in line):
            continue
        return (prefix + line)[:80]
    return ""


def _extract_prompt_title(prompt: str, tid: str) -> str:
    """从 prompt 字符串（subagent JSONL 的 message.content）提取首句有意义标题。"""
    for line in prompt.splitlines():
        line = line.strip()
        if not line or len(line) < 6:
            continue
        if line.startswith(("#", "//", "```", "---", "===", "***", "<!--")):
            continue
        sentence = re.split(r"[。.！!？?\n]", line)[0].strip()
        if len(sentence) > 4:
            return f"[agent] {sentence[:60]}"
    return f"[agent] subagent {tid[:8]}"


def _read_jsonl_first_line(output_file: Path) -> str:
    """如果 output_file 是 symlink 且指向受信任路径，读取真实 JSONL 首行。"""
    try:
        real = output_file.resolve()
        if not str(real).startswith(_TRUSTED_JSONL_PREFIX):
            return ""
        with open(real, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.readline(65536)
    except Exception:
        return ""


def _infer_title(head: str, tid: str, tail: str = "", output_file: Optional[Path] = None) -> str:
    """从 stdout 前几行提取一个有意义的标题；head 失败时尝试 JSONL symlink 再用 tail。"""
    if not head and not tail:
        if output_file and output_file.is_symlink():
            first_line = _read_jsonl_first_line(output_file)
            if first_line:
                head = first_line
        if not head:
            return f"Claude task {tid[:8]}"

    # JSONL 格式（subagent / local_agent / hook 任务）— 从首行 JSON 提取
    first_line = (head or tail).lstrip().split("\n", 1)[0]
    if first_line.startswith("{") and '"' in first_line:
        try:
            obj = json.loads(first_line)
            if isinstance(obj, dict):
                # hook 文件（hook_event_name 字段）
                if "hook_event_name" in obj:
                    tool = obj.get("tool_name", obj.get("hook_event_name", "hook"))
                    cmd = obj.get("tool_input", {})
                    if isinstance(cmd, dict):
                        cmd_str = (cmd.get("command") or cmd.get("description") or "")[:50]
                        if cmd_str:
                            return f"[hook] {tool}: {cmd_str}"
                    return f"[hook] {tool}"
                # 优先级：description > summary > title
                for key in ("description", "summary", "title"):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        return f"[agent] {val[:60]}"
                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, list) and content:
                        first = content[0]
                        if isinstance(first, dict):
                            text = first.get("text") or first.get("content")
                            if isinstance(text, str) and text.strip():
                                return f"[agent] {text.strip()[:60]}"
                    elif isinstance(content, str) and content.strip():
                        # subagent JSONL 实际格式：message.content 是完整 prompt 字符串
                        return _extract_prompt_title(content, tid)
                if obj.get("isSidechain"):
                    # 尝试从 symlink 真实 JSONL 读一遍（head 可能只截到了首行 JSON）
                    if output_file and output_file.is_symlink():
                        full_first = _read_jsonl_first_line(output_file)
                        if full_first and full_first != first_line:
                            try:
                                full_obj = json.loads(full_first)
                                c = full_obj.get("message", {}).get("content")
                                if isinstance(c, str) and c.strip():
                                    return _extract_prompt_title(c, tid)
                            except Exception:
                                pass
                    return f"[agent] subagent {tid[:8]}"
        except Exception:
            pass

    # 纯日志文本（bash run_in_background 等）
    title = _extract_title_from_lines(head)
    if title:
        return title
    if tail:
        title = _extract_title_from_lines(tail[-2000:])
        if title:
            return title
    return f"Claude task {tid[:8]}"


def _utc_isoformat() -> str:
    return datetime.utcnow().isoformat()


class ClaudeTaskScanner:
    """周期扫描 Claude Code 原生任务，写入 agent_tasks 表。"""

    def __init__(
        self,
        workspace_root: Optional[Path] = None,
        scan_interval: int = _SCAN_INTERVAL_SEC,
    ):
        self._workspace_root = workspace_root or _detect_workspace_root()
        self._scan_interval = scan_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._project_dir: Optional[Path] = None
        self._known_tasks: Dict[str, str] = {}  # task_id → last status
        self._initialized = False

    @property
    def project_dir(self) -> Optional[Path]:
        if self._project_dir is None and self._workspace_root:
            sanitized = _sanitize_path_for_claude(self._workspace_root)
            candidate = Path("/tmp/claude-501") / sanitized
            self._project_dir = candidate if candidate.exists() else None
        return self._project_dir

    def healthy(self) -> bool:
        """是否存在可扫描的 Claude 任务目录。"""
        return self.project_dir is not None and self.project_dir.exists()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="claude-task-scanner",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _loop(self) -> None:
        # 启动时先做一次扫描，把已存在但未入库的任务回填
        try:
            self._scan_once()
        except Exception as e:
            print(f"[ClaudeTaskScanner] initial scan error: {e}")
        while not self._stop_event.wait(self._scan_interval):
            try:
                self._scan_once()
            except Exception as e:
                print(f"[ClaudeTaskScanner] scan error: {e}")

    def _scan_once(self) -> None:
        proj = self.project_dir
        if proj is None or not proj.exists():
            return
        store = AgentTaskStore.get_instance()
        cutoff_ts = time.time() - _STALE_HOURS * 3600
        now_ts = time.time()

        # 遍历所有 session-uuid/tasks/*.output
        for session_dir in proj.iterdir():
            tasks_dir = session_dir / "tasks"
            if not tasks_dir.is_dir():
                continue
            include_hooks = os.environ.get("CLAUDE_SCAN_INCLUDE_HOOKS", "0") == "1"
            for output_file in tasks_dir.glob("*.output"):
                tid = output_file.stem
                if not include_hooks and any(tid.startswith(p) for p in _SKIP_PREFIXES):
                    continue
                # 跳过无 hook_ 前缀但实为 hook 的文件（JSON 首行含 hook_event_name）
                if not include_hooks:
                    try:
                        with open(output_file, "rb") as _fh:
                            _peek = _fh.read(300).decode("utf-8", errors="ignore")
                        _first = _peek.lstrip().split("\n", 1)[0]
                        if _first.startswith("{") and '"hook_event_name"' in _first:
                            continue
                    except Exception:
                        pass
                try:
                    stat = output_file.stat()
                except FileNotFoundError:
                    continue
                if stat.st_mtime < cutoff_ts:
                    continue  # 太老，跳过
                self._upsert_task(store, tid, output_file, stat, now_ts, session_dir.name)

    def _upsert_task(
        self,
        store: AgentTaskStore,
        tid: str,
        output_file: Path,
        stat,
        now_ts: float,
        session_uuid: str,
    ) -> None:
        head, tail = _safe_read_head_tail(output_file)
        mtime_age = now_ts - stat.st_mtime
        status = _infer_status(tail, mtime_age)
        progress = _infer_progress(tail)
        title = _infer_title(head, tid, tail, output_file=output_file)
        log_tail = tail[-2000:] if tail else ""

        try:
            ctime = getattr(stat, "st_birthtime", None) or stat.st_ctime
            started_at = datetime.fromtimestamp(ctime)
        except Exception:
            started_at = datetime.utcnow()

        finished_at = None
        if status in (AgentStatus.SUCCEEDED, AgentStatus.FAILED, AgentStatus.CANCELLED):
            finished_at = datetime.fromtimestamp(stat.st_mtime)

        existing = store.get(tid)
        if existing is None:
            task = AgentTask(
                id=tid,
                agent_name="claude",
                title=title,
                status=status,
                progress=progress if status == AgentStatus.RUNNING else (100 if status == AgentStatus.SUCCEEDED else progress),
                started_at=started_at,
                finished_at=finished_at,
                trigger_src=f"claude_code:session={session_uuid[:8]}",
                payload_json=json.dumps({
                    "output_file": str(output_file),
                    "session_uuid": session_uuid,
                    "size_bytes": stat.st_size,
                }),
                log_tail=log_tail,
                created_at=started_at,
            )
            try:
                store.insert(task)
                self._known_tasks[tid] = status.value
            except Exception as e:
                # 主键冲突等：降级为 update
                try:
                    store.update_status(
                        tid, status,
                        finished_at=finished_at,
                        progress=task.progress,
                    )
                except Exception:
                    print(f"[ClaudeTaskScanner] upsert failed {tid}: {e}")
            # 同步 log_tail
            try:
                store.append_log(tid, "")  # ensure column exists
                # 直接覆盖 log_tail 而不是 append，避免重复
                self._set_log_tail_direct(store, tid, log_tail)
            except Exception:
                pass
        else:
            # 已存在：仅在状态/进度变化时更新
            prev_status = self._known_tasks.get(tid, existing.status.value)
            if prev_status != status.value or existing.progress != progress:
                effective_progress = progress
                if status == AgentStatus.SUCCEEDED:
                    effective_progress = 100
                store.update_status(
                    tid, status,
                    finished_at=finished_at,
                    progress=effective_progress,
                )
                self._known_tasks[tid] = status.value
            # log_tail 总是更新（追踪最新输出）
            try:
                self._set_log_tail_direct(store, tid, log_tail)
            except Exception:
                pass

    @staticmethod
    def _set_log_tail_direct(store: AgentTaskStore, tid: str, log_tail: str) -> None:
        """直接 SET log_tail（绕过 append_log 的累加逻辑）。"""
        with store._write_lock, store._connect() as conn:
            conn.execute(
                "UPDATE agent_tasks SET log_tail=? WHERE id=?",
                (log_tail[-2000:], tid),
            )
