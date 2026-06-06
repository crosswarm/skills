#!/bin/bash
# frp 服务端启动脚本 (qcl)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/frp_common.sh"

mkdir -p "$FRP_DIR"
launcher_log="$FRP_DIR/frps-launcher.log"

if pgrep -f "frps.*frps.ini" >/dev/null 2>&1; then
    warn "发现已有 frps 进程，执行无提示重启"
    pkill -f "frps.*frps.ini" || true
    sleep 2
fi

info "启动 frps，日志: $launcher_log"
nohup /bin/bash "$SCRIPT_DIR/run_frps.sh" > "$launcher_log" 2>&1 &
frps_pid=$!

sleep 3

if ps -p "$frps_pid" >/dev/null 2>&1; then
    info "frps 启动成功，PID: $frps_pid"
    echo "查看日志: tail -f $launcher_log"
    exit 0
fi

error "frps 启动失败，请检查日志: $launcher_log"
exit 1
