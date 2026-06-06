#!/bin/bash
# 检查所有节点服务状态
# 用法: bash APP/deploy_scripts/check_all_services.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/network_env.sh"

# 颜色输出
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "=========================================="
echo "智能看板集群 - 服务状态检查"
echo "=========================================="
echo ""

# 检查函数
check_service() {
    local host=$1
    local service=$2
    local cmd=$3

    if [ "$host" == "local" ]; then
        result=$(eval "$cmd" 2>&1)
    else
        result=$(ssh -o ConnectTimeout=5 "$host" "$cmd" 2>&1)
    fi

    if echo "$result" | grep -qE "(Running|OK|success|true)"; then
        echo -e "${GREEN}✓${NC} $host $service"
        return 0
    else
        echo -e "${RED}✗${NC} $host $service"
        return 1
    fi
}

# 统计变量
total=0
passed=0

echo "--- qcl (公网服务器) ---"
total=$((total + 1))
check_service "$QCL_HOST" "frps 监听端口" "lsof -nP -iTCP:${FRP_BIND_PORT} -sTCP:LISTEN >/dev/null 2>&1 && lsof -nP -iTCP:${FRP_VHOST_HTTP_PORT} -sTCP:LISTEN >/dev/null 2>&1 && echo Running || echo Stopped" && passed=$((passed + 1))

total=$((total + 1))
check_service "$QCL_HOST" "backend API" "curl -s http://localhost:${QCL_BACKEND_PORT}/api/board 2>/dev/null | grep -q success && echo OK || echo Error" && passed=$((passed + 1))

total=$((total + 1))
check_service "$QCL_HOST" "board diagnose" "curl -s http://localhost:${QCL_BACKEND_PORT}/api/board/diagnose 2>/dev/null | python3 -c 'import sys,json; data=json.load(sys.stdin); print(\"OK\" if data.get(\"fetch_strategy\") == \"${BOARD_FETCH_STRATEGY}\" else \"Error\")'" && passed=$((passed + 1))

total=$((total + 1))
check_service "$QCL_HOST" "mini 节点健康" "curl -s http://localhost:${QCL_BACKEND_PORT}/api/network/metrics 2>/dev/null | python3 -c 'import sys,json; data=json.load(sys.stdin); nodes=data.get(\"data\", {}).get(\"nodes\", []); ok=any(node.get(\"name\") == \"mini\" and node.get(\"healthy\") for node in nodes); print(\"OK\" if ok else \"Error\")'" && passed=$((passed + 1))

echo ""
echo "--- mini (内网桥接) ---"
# mini 是本机，使用 local 检查
total=$((total + 1))
check_service "local" "local backend API" "curl -s http://localhost:${LOCAL_BACKEND_PORT}/api/board/stats 2>/dev/null | grep -q success && echo OK || echo Error" && passed=$((passed + 1))

total=$((total + 1))
if launchctl print gui/$(id -u)/com.aiticket.qcl-tunnel >/dev/null 2>&1; then
    check_service "local" "qcl reverse tunnel" "launchctl print gui/$(id -u)/com.aiticket.qcl-tunnel >/dev/null 2>&1 && echo Running || echo Stopped" && passed=$((passed + 1))
else
    check_service "local" "frpc" "pgrep -f 'frpc.*frpc.ini' > /dev/null && echo Running || echo Stopped" && passed=$((passed + 1))
fi

total=$((total + 1))
check_service "local" "jira proxy" "curl -s http://localhost:${MINI_PROXY_PORT}/proxy/health 2>/dev/null | grep -q success && echo OK || echo Error" && passed=$((passed + 1))

if command -v launchctl >/dev/null 2>&1; then
    total=$((total + 1))
    check_service "local" "launchd local_backend" "launchctl print gui/$(id -u)/com.aiticket.local-backend >/dev/null 2>&1 && echo Running || echo Stopped" && passed=$((passed + 1))

    total=$((total + 1))
    check_service "local" "launchd jira_proxy" "launchctl print gui/$(id -u)/com.aiticket.jira-proxy >/dev/null 2>&1 && echo Running || echo Stopped" && passed=$((passed + 1))

    total=$((total + 1))
    if launchctl print gui/$(id -u)/com.aiticket.qcl-tunnel >/dev/null 2>&1; then
        check_service "local" "launchd qcl_tunnel" "launchctl print gui/$(id -u)/com.aiticket.qcl-tunnel >/dev/null 2>&1 && echo Running || echo Stopped" && passed=$((passed + 1))
    else
        check_service "local" "launchd frpc" "launchctl print gui/$(id -u)/com.aiticket.frpc >/dev/null 2>&1 && echo Running || echo Stopped" && passed=$((passed + 1))
    fi
fi

echo ""
echo "--- lap (备用节点，可选) ---"
if ssh -o ConnectTimeout=3 "$LAP_HOST" "echo online" 2>/dev/null; then
    total=$((total + 1))
    check_service "$LAP_HOST" "jira proxy" "curl -s http://localhost:${MINI_PROXY_PORT}/proxy/health 2>/dev/null | grep -q success && echo OK || echo Error" && passed=$((passed + 1))
else
    echo -e "${YELLOW}⚠${NC} lap 离线 (正常状态)"
fi

echo ""
echo "=========================================="
echo "检查结果: ${GREEN}${passed}/${total}${NC} 正常"
echo "=========================================="

if [ $passed -eq $total ]; then
    echo -e "${GREEN}所有服务运行正常${NC}"
    exit 0
elif [ $passed -ge $((total - 1)) ]; then
    echo -e "${YELLOW}部分服务异常，请检查${NC}"
    exit 1
else
    echo -e "${RED}多个服务异常，需要紧急处理${NC}"
    exit 2
fi
