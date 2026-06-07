"""aiticket compact — 跨平台路径与环境解析（纯函数，无副作用）。

唯一真相源：安装器(install.py)、控制器(aiticket_ctl.py)、服务模板共用同一套路径
与 service_env，避免 auth.db 路径分裂（init_db / seed_admin / auth_service 三方对齐）。

布局（AITICKET_HOME，默认 ~/.aiticket；Windows: %USERPROFILE%\\.aiticket）：
    <HOME>/
    ├── src/    git checkout（后端在 src/APP/backend）
    ├── venv/   uv venv（bin/ 或 Scripts/）
    ├── data/   sqlite/auth.db + app_auth.key（管理员/会话，src 外，git pull 与 --force 重装都不丢）；
    │           jobmaster.db、logs/、aiticket.pid 亦在此
    ├── kb/     默认 KB 源目录（deployment.yaml kb.root_dir 可改指任意目录）
    └── config/ deployment.yaml + env.json（跨平台路径/端口/skill_token）

数据持久化说明（与上一致）：
  - **auth.db（管理员/会话）外置在 <HOME>/data/sqlite**，service_env 经 APP_AUTH_DB_PATH
    pin，任何更新/重装都不丢——这是唯一硬保证的持久数据。
  - 向量库(tickets.db/kb_chunks)、回复训练器(reply_trainer，含随仓库发布的种子)、缓存
    (data_cache/chroma_db) 等运行期数据位于 src/APP/backend/data（git 忽略）：
    /aiticket-update(git pull) 会保留；--force 重装或全新 clone 会清空。这些数据可从
    Jira/KB 重建，故不外置（外置会与 shipped 种子文件冲突）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PORT = 18080
DEFAULT_HOST = "127.0.0.1"

# 服务标识：launchd 用反域名 label，systemd/schtasks 用短名
SERVICE_LABEL = "com.aiticket.compact"   # launchd
SERVICE_NAME = "aiticket-compact"        # systemd --user / Windows 计划任务


# ---------- 平台判定（独立 helper，测试 patch 此处而非全局 os.name，
#            以免污染 pathlib 的 flavour 检测） ----------

def _is_windows() -> bool:
    return os.name == "nt"


# ---------- HOME ----------

def default_home() -> Path:
    env = os.environ.get("AITICKET_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".aiticket"


# ---------- venv（bin/ vs Scripts/ 跨平台） ----------

def venv_dir(home: Path) -> Path:
    return home / "venv"


def venv_bindir(home: Path) -> Path:
    # Windows 用 Scripts/，POSIX 用 bin/
    return venv_dir(home) / ("Scripts" if _is_windows() else "bin")


def venv_python(home: Path) -> Path:
    return venv_bindir(home) / ("python.exe" if _is_windows() else "python")


# ---------- 源码 / 后端 ----------

def src_dir(home: Path) -> Path:
    return home / "src"


def backend_dir(home: Path) -> Path:
    return src_dir(home) / "APP" / "backend"


# ---------- 数据 / 配置 ----------

def data_dir(home: Path) -> Path:
    return home / "data"


def sqlite_dir(home: Path) -> Path:
    return data_dir(home) / "sqlite"


def auth_db_path(home: Path) -> Path:
    return sqlite_dir(home) / "auth.db"


def auth_secret_path(home: Path) -> Path:
    return sqlite_dir(home) / "app_auth.key"


def kb_dir(home: Path) -> Path:
    return home / "kb"


def config_dir(home: Path) -> Path:
    return home / "config"


def env_json_path(home: Path) -> Path:
    return config_dir(home) / "env.json"


def deployment_yaml_path(home: Path) -> Path:
    return config_dir(home) / "deployment.yaml"


def logs_dir(home: Path) -> Path:
    return data_dir(home) / "logs"


def log_file(home: Path) -> Path:
    return logs_dir(home) / "aiticket.out.log"


def err_log_file(home: Path) -> Path:
    return logs_dir(home) / "aiticket.err.log"


def pid_file(home: Path) -> Path:
    return data_dir(home) / "aiticket.pid"


# ---------- 端口：override > env AITICKET_PORT > env.json > default ----------

def resolve_port(home: Path, override: int | None = None) -> int:
    if override:
        return int(override)
    env = os.environ.get("AITICKET_PORT")
    if env and env.strip().isdigit():
        return int(env.strip())
    ej = env_json_path(home)
    try:
        if ej.exists():
            data = json.loads(ej.read_text(encoding="utf-8"))
            if data.get("port"):
                return int(data["port"])
    except Exception:
        pass
    return DEFAULT_PORT


# ---------- 服务运行环境（被安装器写进模板、被控制器注入子进程） ----------

def service_env(home: Path, port: int, extra: dict | None = None) -> dict:
    """构造服务进程环境变量。最关键：pin APP_AUTH_DB_PATH 消除路径分裂。"""
    bindir = str(venv_bindir(home))
    base_path = os.environ.get("PATH", "")
    env = {
        "AITICKET_HOME": str(home),
        "AITICKET_PORT": str(port),
        # 本地单用户免登录：localhost 请求自动以唯一本地用户身份通过（服务仅绑 127.0.0.1）
        "AITICKET_LOCAL_MODE": "1",
        # 修复 🔴 auth.db 分裂：init_db/seed_admin 写 <data>/sqlite/auth.db，
        # 此处让 auth_service 读同一文件
        "APP_AUTH_DB_PATH": str(auth_db_path(home)),
        # Fernet 密钥（加密 jira/pm token），与 auth.db 同目录，跟随 db 一起持久化
        "APP_AUTH_SECRET_PATH": str(auth_secret_path(home)),
        # config.loader._find_config_file() 优先认 CONFIG_FILE → 读我们写的 deployment.yaml
        # （内含 kb.root_dir 指向 <home>/kb、jira.base_url 等）
        "CONFIG_FILE": str(deployment_yaml_path(home)),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        # 本机直连，绕过任何代理（与 main.py:9 的 no_proxy 自举一致）
        "no_proxy": "localhost,127.0.0.1,0.0.0.0,::1",
        "NO_PROXY": "localhost,127.0.0.1,0.0.0.0,::1",
        # venv bin 置于 PATH 最前
        "PATH": bindir + os.pathsep + base_path if base_path else bindir,
    }
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


# ---------- URL ----------

def base_url(port: int, host: str = DEFAULT_HOST) -> str:
    return f"http://{host}:{port}"


def liveness_url(port: int, host: str = DEFAULT_HOST) -> str:
    return base_url(port, host) + "/api/liveness"


def health_url(port: int, host: str = DEFAULT_HOST) -> str:
    return base_url(port, host) + "/health"
