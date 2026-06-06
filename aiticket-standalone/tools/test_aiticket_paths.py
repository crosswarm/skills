"""TDD 单测 — aiticket_paths 跨平台路径解析（纯函数，可在任一平台跑）。

通过 monkeypatch os.name / 环境变量 模拟 Windows 与 Unix，
验证 venv bin/Scripts、auth.db 路径、端口优先级、service_env 接线。
跑法（仓库根目录）：
    APP/backend/.venv-core/bin/python -m pytest tools/test_aiticket_paths.py -q
"""
import json
import os
from pathlib import Path

import pytest

import aiticket_paths as P


# ---------- home 解析 ----------

def test_default_home_honors_env(monkeypatch):
    monkeypatch.setenv("AITICKET_HOME", "/custom/home")
    assert P.default_home() == Path("/custom/home")


def test_default_home_fallback(monkeypatch):
    monkeypatch.delenv("AITICKET_HOME", raising=False)
    assert P.default_home() == Path.home() / ".aiticket"


# ---------- venv bin / Scripts 跨平台 ----------

def test_venv_bindir_unix(monkeypatch):
    monkeypatch.setattr(P, "_is_windows", lambda: False)
    home = Path("/h")
    assert P.venv_bindir(home) == home / "venv" / "bin"


def test_venv_bindir_windows(monkeypatch):
    # patch _is_windows（而非 os.name）以免污染 pathlib flavour
    monkeypatch.setattr(P, "_is_windows", lambda: True)
    home = Path("/h")
    assert P.venv_bindir(home) == home / "venv" / "Scripts"


def test_venv_python_name(monkeypatch):
    home = Path("/h")
    monkeypatch.setattr(P, "_is_windows", lambda: True)
    assert P.venv_python(home).name == "python.exe"
    monkeypatch.setattr(P, "_is_windows", lambda: False)
    assert P.venv_python(home).name == "python"


# ---------- 数据 / DB 路径 ----------

def test_auth_db_under_data_sqlite():
    home = Path("/h")
    assert P.auth_db_path(home) == home / "data" / "sqlite" / "auth.db"


def test_backend_dir_layout():
    home = Path("/h")
    assert P.backend_dir(home) == home / "src" / "APP" / "backend"


def test_pid_and_log_paths():
    home = Path("/h")
    assert P.pid_file(home) == home / "data" / "aiticket.pid"
    assert P.log_file(home).parent == home / "data" / "logs"


# ---------- 端口优先级：override > env > env.json > default ----------

def test_port_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AITICKET_PORT", raising=False)
    assert P.resolve_port(tmp_path) == P.DEFAULT_PORT


def test_port_env_over_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AITICKET_PORT", "19999")
    assert P.resolve_port(tmp_path) == 19999


def test_port_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AITICKET_PORT", "19999")
    assert P.resolve_port(tmp_path, override=12345) == 12345


def test_port_env_json(monkeypatch, tmp_path):
    monkeypatch.delenv("AITICKET_PORT", raising=False)
    cfg = P.config_dir(tmp_path)
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "env.json").write_text(json.dumps({"port": 18888}), encoding="utf-8")
    assert P.resolve_port(tmp_path) == 18888


# ---------- service_env 接线（修复 auth.db 路径分裂的关键） ----------

def test_service_env_pins_auth_db(monkeypatch):
    monkeypatch.setattr(P, "_is_windows", lambda: False)
    home = Path("/h")
    env = P.service_env(home, port=18080)
    assert env["APP_AUTH_DB_PATH"] == str(P.auth_db_path(home))
    assert env["AITICKET_HOME"] == str(home)
    assert env["AITICKET_PORT"] == "18080"
    # venv bin 必须在 PATH 最前
    assert env["PATH"].split(os.pathsep)[0] == str(P.venv_bindir(home))


def test_service_env_no_proxy_for_localhost():
    env = P.service_env(Path("/h"), port=18080)
    assert "127.0.0.1" in env.get("no_proxy", "")


# ---------- URL 构造 ----------

def test_liveness_url():
    assert P.liveness_url(18080) == "http://127.0.0.1:18080/api/liveness"
