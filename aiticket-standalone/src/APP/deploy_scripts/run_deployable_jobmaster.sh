#!/bin/bash
# deployable 专用启动器（macOS / Linux）
# 自动注入 AITICKET_DEPLOYABLE=1，跳过周报调度，仅跑月报及其他后台任务
# 用法: bash run_deployable_jobmaster.sh
#       也可由 launchd / systemd 调用

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# network_env.sh 为可选（macOS Mini 专有），Linux 部署时可忽略
if [ -f "$SCRIPT_DIR/network_env.sh" ]; then
    source "$SCRIPT_DIR/network_env.sh"
fi

CONDA_ENV_PATH="${CONDA_ENV_PATH:-$HOME/miniconda3/envs/antigravity}"
PYTHON_BIN="${PYTHON_BIN:-${CONDA_ENV_PATH}/bin/python3}"

# fallback: 系统 python3
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi

if [ ! -x "$PYTHON_BIN" ]; then
    echo "找不到 Python 解释器，请设置 PYTHON_BIN 环境变量" >&2
    exit 1
fi

mkdir -p "$PROJECT_ROOT/APP/backend/logs"
cd "$PROJECT_ROOT/APP/backend"

export HOME="${HOME:-$(eval echo ~$(id -un))}"
export PYTHONUNBUFFERED=1
export RUN_BACKGROUND_JOBS=1
export JIRA_SKIP_COOKIES="${JIRA_SKIP_COOKIES:-true}"
export AITICKET_DEPLOYABLE=1        # 核心门控：跳过 weekly_report 调度

# 清除可能干扰内网请求的代理
unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy
export no_proxy="localhost,127.0.0.1,0.0.0.0,::1"
export NO_PROXY="$no_proxy"

exec "$PYTHON_BIN" -m scripts.local_jobmaster_daemon
