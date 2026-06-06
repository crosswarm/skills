"""TDD 单测 — 服务单元文件渲染（纯函数，跨平台可跑）。

验证 launchd plist / systemd unit / Windows task XML 都正确注入
端口、pin 的 APP_AUTH_DB_PATH、uvicorn 单 worker 参数，且 XML 转义无误。
"""
from pathlib import Path

import aiticket_paths as P
import service_manager as SM


HOME = Path("/opt/aiticket-home")
PORT = 18080


def test_uvicorn_args_single_worker():
    args = SM.uvicorn_args(HOME, PORT)
    assert "uvicorn" in args and "main:app" in args
    assert args[args.index("--workers") + 1] == "1"
    assert args[args.index("--port") + 1] == str(PORT)


def test_launchd_plist_pins_auth_db_and_port():
    xml = SM.render_launchd_plist(HOME, PORT)
    assert f"<string>{P.SERVICE_LABEL}</string>" in xml
    assert str(P.auth_db_path(HOME)) in xml
    assert f"<string>{PORT}</string>" in xml
    assert "<key>KeepAlive</key><true/>" in xml
    assert "<key>RunAtLoad</key><true/>" in xml


def test_systemd_unit_has_execstart_and_restart():
    unit = SM.render_systemd_unit(HOME, PORT)
    assert "ExecStart=" in unit
    assert "uvicorn main:app" in unit
    assert "Restart=on-failure" in unit
    assert f"Environment=APP_AUTH_DB_PATH={P.auth_db_path(HOME)}" in unit


def test_windows_task_xml_runs_ctl_start():
    xml = SM.render_windows_task_xml(HOME, PORT, Path("/c/ctl.py"))
    assert "<LogonTrigger>" in xml
    assert "start" in xml
    assert "UTF-16" in xml


def test_xml_escaping_safe():
    # home 含 & < > 时 plist 不应破坏 XML
    weird = Path("/opt/a&b<c>")
    xml = SM.render_launchd_plist(weird, PORT)
    assert "a&amp;b&lt;c&gt;" in xml
    assert "a&b<c>" not in xml


def test_get_manager_returns_some_backend():
    mgr = SM.get_manager(HOME, PORT)
    assert mgr.name in ("launchd", "systemd", "pidfile")
