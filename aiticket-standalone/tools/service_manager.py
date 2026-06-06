"""aiticket compact — 跨平台服务管理器。

三种后端，按平台 + 可用性自动选择：
  - macOS:  LaunchdManager   — launchd plist，RunAtLoad+KeepAlive（崩溃自重启 + 登录自启）
  - Linux:  SystemdUserManager — systemd --user，Restart=on-failure（崩溃自重启）
  - Windows / 兜底: PidfileManager — 分离子进程 + pidfile（登录自启交 schtasks/startup）

统一接口：
  install_autostart()/uninstall_autostart()  注册/注销开机自启
  start()/stop()                              起停服务
  is_loaded()                                 单元是否已加载/进程是否在
说明：服务"是否健康"以 /api/liveness 为权威（控制器负责探活），
      本模块只管"进程/单元的生命周期"。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import aiticket_paths as P


# ====================================================================
# 单元文件渲染（纯函数，便于单测转义与接线）
# ====================================================================

def uvicorn_args(home: Path, port: int) -> list[str]:
    """uvicorn 启动参数（单 worker 是稳定配置，多 worker 会 Errno 48 崩溃循环）。"""
    return [
        str(P.venv_python(home)),
        "-m", "uvicorn", "main:app",
        "--host", P.DEFAULT_HOST,
        "--port", str(port),
        "--workers", "1",
    ]


def render_launchd_plist(home: Path, port: int) -> str:
    env = P.service_env(home, port)
    args = uvicorn_args(home, port)
    arg_xml = "\n".join(f"    <string>{_xml_escape(a)}</string>" for a in args)
    env_xml = "\n".join(
        f"    <key>{_xml_escape(k)}</key><string>{_xml_escape(v)}</string>"
        for k, v in env.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{P.SERVICE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{arg_xml}
  </array>
  <key>WorkingDirectory</key><string>{_xml_escape(str(P.backend_dir(home)))}</string>
  <key>EnvironmentVariables</key>
  <dict>
{env_xml}
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{_xml_escape(str(P.log_file(home)))}</string>
  <key>StandardErrorPath</key><string>{_xml_escape(str(P.err_log_file(home)))}</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
"""


def render_systemd_unit(home: Path, port: int) -> str:
    env = P.service_env(home, port)
    env_lines = "\n".join(f"Environment={k}={v}" for k, v in env.items())
    exec_start = " ".join(uvicorn_args(home, port))
    return f"""[Unit]
Description=aiticket compact (lite) — 本地 Jira 智能看板
After=network.target

[Service]
Type=simple
WorkingDirectory={P.backend_dir(home)}
ExecStart={exec_start}
Restart=on-failure
RestartSec=3
{env_lines}
StandardOutput=append:{P.log_file(home)}
StandardError=append:{P.err_log_file(home)}

[Install]
WantedBy=default.target
"""


def render_windows_task_xml(home: Path, port: int, ctl_path: Path) -> str:
    """Windows 计划任务：登录时运行 `aiticket_ctl start`（pidfile 起服务）。"""
    py = _xml_escape(str(P.venv_python(home)))
    ctl = _xml_escape(str(ctl_path))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>aiticket compact 登录自启</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{py}</Command>
      <Arguments>"{ctl}" start</Arguments>
      <WorkingDirectory>{_xml_escape(str(P.backend_dir(home)))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# ====================================================================
# 管理器
# ====================================================================

class _BaseManager:
    name = "base"

    def __init__(self, home: Path, port: int):
        self.home = home
        self.port = port

    # 子类实现
    def install_autostart(self) -> None: ...
    def uninstall_autostart(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_loaded(self) -> bool: ...

    # 公共
    def _ensure_logs(self) -> None:
        P.logs_dir(self.home).mkdir(parents=True, exist_ok=True)


class LaunchdManager(_BaseManager):
    name = "launchd"

    def plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{P.SERVICE_LABEL}.plist"

    def _domain_target(self) -> str:
        return f"gui/{os.getuid()}/{P.SERVICE_LABEL}"

    def install_autostart(self) -> None:
        self._ensure_logs()
        pp = self.plist_path()
        pp.parent.mkdir(parents=True, exist_ok=True)
        pp.write_text(render_launchd_plist(self.home, self.port), encoding="utf-8")

    def uninstall_autostart(self) -> None:
        self.stop()
        pp = self.plist_path()
        if pp.exists():
            pp.unlink()

    def start(self) -> None:
        self._ensure_logs()
        pp = self.plist_path()
        if not pp.exists():
            self.install_autostart()
        uid = os.getuid()
        # 优先现代 bootstrap，回退 load -w
        r = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(pp)],
            capture_output=True, text=True,
        )
        if r.returncode != 0 and "already" not in (r.stderr or "").lower():
            subprocess.run(["launchctl", "load", "-w", str(pp)],
                           capture_output=True, text=True)
        subprocess.run(["launchctl", "kickstart", "-k", self._domain_target()],
                       capture_output=True, text=True)

    def stop(self) -> None:
        uid = os.getuid()
        pp = self.plist_path()
        r = subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(pp)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["launchctl", "unload", "-w", str(pp)],
                           capture_output=True, text=True)

    def is_loaded(self) -> bool:
        r = subprocess.run(["launchctl", "print", self._domain_target()],
                           capture_output=True, text=True)
        return r.returncode == 0


class SystemdUserManager(_BaseManager):
    name = "systemd"

    def unit_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / f"{P.SERVICE_NAME}.service"

    def _sc(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True)

    def install_autostart(self) -> None:
        self._ensure_logs()
        up = self.unit_path()
        up.parent.mkdir(parents=True, exist_ok=True)
        up.write_text(render_systemd_unit(self.home, self.port), encoding="utf-8")
        self._sc("daemon-reload")
        self._sc("enable", P.SERVICE_NAME)
        # 允许未登录也常驻
        subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")],
                       capture_output=True, text=True)

    def uninstall_autostart(self) -> None:
        self.stop()
        self._sc("disable", P.SERVICE_NAME)
        up = self.unit_path()
        if up.exists():
            up.unlink()
        self._sc("daemon-reload")

    def start(self) -> None:
        self._ensure_logs()
        if not self.unit_path().exists():
            self.install_autostart()
        self._sc("start", P.SERVICE_NAME)

    def stop(self) -> None:
        self._sc("stop", P.SERVICE_NAME)

    def is_loaded(self) -> bool:
        r = self._sc("is-active", P.SERVICE_NAME)
        return (r.stdout or "").strip() == "active"


class PidfileManager(_BaseManager):
    """Windows 与无 service-manager 的兜底：分离子进程 + pidfile。"""
    name = "pidfile"

    def install_autostart(self) -> None:
        """登录自启：Windows 用 schtasks，其它平台无 service-manager 时写 ~/.config/autostart。"""
        self._ensure_logs()
        if os.name == "nt":
            self._install_schtasks()
        else:
            self._install_xdg_autostart()

    def uninstall_autostart(self) -> None:
        self.stop()
        if os.name == "nt":
            subprocess.run(["schtasks", "/delete", "/tn", P.SERVICE_NAME, "/f"],
                           capture_output=True, text=True)
        else:
            ap = Path.home() / ".config" / "autostart" / f"{P.SERVICE_NAME}.desktop"
            if ap.exists():
                ap.unlink()

    def _ctl_path(self) -> Path:
        return Path(__file__).resolve().parent / "aiticket_ctl.py"

    def _install_schtasks(self) -> None:
        xml = render_windows_task_xml(self.home, self.port, self._ctl_path())
        xml_path = P.config_dir(self.home) / "aiticket-task.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        # Task XML 要求 UTF-16
        xml_path.write_text(xml, encoding="utf-16")
        subprocess.run(
            ["schtasks", "/create", "/tn", P.SERVICE_NAME,
             "/xml", str(xml_path), "/f"],
            capture_output=True, text=True,
        )

    def _install_xdg_autostart(self) -> None:
        ap = Path.home() / ".config" / "autostart"
        ap.mkdir(parents=True, exist_ok=True)
        py = P.venv_python(self.home)
        ctl = self._ctl_path()
        (ap / f"{P.SERVICE_NAME}.desktop").write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=aiticket compact\n"
            f"Exec={py} {ctl} start\n"
            "X-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )

    # ---- 进程控制 ----
    def _read_pid(self) -> int | None:
        pf = P.pid_file(self.home)
        if not pf.exists():
            return None
        try:
            return int(pf.read_text(encoding="utf-8").strip())
        except Exception:
            return None

    def _pid_alive(self, pid: int) -> bool:
        if os.name == "nt":
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True,
            )
            return str(pid) in (r.stdout or "")
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def start(self) -> None:
        self._ensure_logs()
        if self.is_loaded():
            return  # 已在运行，pidfile 守卫防双启
        out = open(P.log_file(self.home), "a", encoding="utf-8")
        err = open(P.err_log_file(self.home), "a", encoding="utf-8")
        env = {**os.environ, **P.service_env(self.home, self.port)}
        kwargs = dict(
            cwd=str(P.backend_dir(self.home)),
            stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env,
        )
        if os.name == "nt":
            DETACHED = 0x00000008
            NO_WINDOW = 0x08000000
            kwargs["creationflags"] = DETACHED | NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(uvicorn_args(self.home, self.port), **kwargs)
        P.pid_file(self.home).write_text(str(proc.pid), encoding="utf-8")

    def stop(self) -> None:
        pid = self._read_pid()
        if pid and self._pid_alive(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True)
            else:
                import signal as _sig
                try:
                    os.kill(pid, _sig.SIGTERM)
                except OSError:
                    pass
        pf = P.pid_file(self.home)
        if pf.exists():
            pf.unlink()

    def is_loaded(self) -> bool:
        pid = self._read_pid()
        return bool(pid and self._pid_alive(pid))


def get_manager(home: Path, port: int) -> _BaseManager:
    """按平台 + 可用性选择管理器。

    可用 AITICKET_SERVICE_BACKEND=pidfile|launchd|systemd 强制覆盖
    （测试 / 无 service-manager 环境 / 不想注册系统服务时的逃生舱）。
    """
    forced = os.environ.get("AITICKET_SERVICE_BACKEND", "").strip().lower()
    if forced == "pidfile":
        return PidfileManager(home, port)
    if forced == "launchd":
        return LaunchdManager(home, port)
    if forced == "systemd":
        return SystemdUserManager(home, port)
    if sys.platform == "darwin" and shutil.which("launchctl"):
        return LaunchdManager(home, port)
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return SystemdUserManager(home, port)
    return PidfileManager(home, port)
