#!/bin/bash
# start_local.sh - 使用 conda antigravity 环境启动本地服务

set -e

# 配置
CONDA_ENV_PATH="/Volumes/MacMini/opt/miniconda3/envs/antigravity"
PYTHON="$CONDA_ENV_PATH/bin/python"
PIP="$CONDA_ENV_PATH/bin/pip"
UVICORN="$CONDA_ENV_PATH/bin/uvicorn"
LOCAL_BACKEND_HOST="${LOCAL_BACKEND_HOST:-0.0.0.0}"
LOCAL_BACKEND_PORT="${LOCAL_BACKEND_PORT:-3000}"
HEALTHCHECK_HOST="${HEALTHCHECK_HOST:-127.0.0.1}"

detect_ip() {
    local pattern="$1"
    ifconfig 2>/dev/null | awk -v p="$pattern" '$1 == "inet" && $2 ~ p { print $2; exit }'
}

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

cd "$(dirname "$0")"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}🚀 AI工单系统本地启动工具${NC}"
echo -e "${GREEN}========================================${NC}"

# 检查 conda 环境
if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}❌ Conda antigravity 环境不存在: $CONDA_ENV_PATH${NC}"
    exit 1
fi

echo -e "${BLUE}Python: $($PYTHON --version)${NC}"
echo -e "${BLUE}环境路径: $CONDA_ENV_PATH${NC}"

# 检查并安装缺失依赖
echo ""
echo -e "${YELLOW}📦 检查依赖...${NC}"

# 从 requirements.txt 检查依赖
while IFS= read -r line || [[ -n "$line" ]]; do
    # 跳过空行和注释
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    
    # 提取包名 (处理 == >= 等版本约束)
    pkg=$(echo "$line" | sed -E 's/([a-zA-Z0-9_-]+).*/\1/')
    
    # 检查是否已安装
    if ! $PIP show "$pkg" > /dev/null 2>&1; then
        echo -e "${YELLOW}  安装缺失包: $line${NC}"
        $PIP install "$line" -q
    fi
done < requirements.txt

echo -e "${GREEN}✓ 依赖检查完成${NC}"

# 停止已存在的服务
echo ""
echo -e "${YELLOW}🛑 检查已有服务...${NC}"
PID=$(lsof -ti :"$LOCAL_BACKEND_PORT" 2>/dev/null || echo "")
if [ -n "$PID" ]; then
    echo -e "${YELLOW}  停止现有服务 (PID: $PID)${NC}"
    kill $PID 2>/dev/null || true
    sleep 2
fi
echo -e "${GREEN}✓ 服务端口已清理${NC}"

# 启动服务
echo ""
echo -e "${YELLOW}🚀 启动后端服务...${NC}"
echo -e "${BLUE}监听地址: ${LOCAL_BACKEND_HOST}:${LOCAL_BACKEND_PORT}${NC}"
echo ""

# --- 数据源配置 ---
# Jira直连: 跳过过期浏览器cookies，仅使用Basic Auth
export JIRA_SKIP_COOKIES=true
# SSL: 如果公司CA不在系统信任链中，取消下行注释
# export JIRA_SSL_VERIFY=false
# Mini代理: 如需启用取消下行注释
# export ENABLE_CACHE_SERVICE=true

# 使用 nohup 启动
export PYTHONPATH="${PWD}:${PYTHONPATH}"
nohup "$UVICORN" main:app --host "$LOCAL_BACKEND_HOST" --port "$LOCAL_BACKEND_PORT" > nohup.out 2>&1 &

# 等待服务启动
sleep 3

# 检查服务状态
if curl -s "http://${HEALTHCHECK_HOST}:${LOCAL_BACKEND_PORT}/api/board/stats" > /dev/null 2>&1; then
    TAILSCALE_IP="$(detect_ip '^100\\.')"
    LAN_IP="$(detect_ip '^192\\.168\\.')"
    echo -e "${GREEN}✅ 服务启动成功！${NC}"
    echo ""
    echo -e "${GREEN}访问地址:${NC}"
    echo "  本机:     http://${HEALTHCHECK_HOST}:${LOCAL_BACKEND_PORT}"
    if [ -n "$TAILSCALE_IP" ] && [ "$TAILSCALE_IP" != "$HEALTHCHECK_HOST" ]; then
        echo "  Tailscale: http://${TAILSCALE_IP}:${LOCAL_BACKEND_PORT}"
    fi
    if [ -n "$LAN_IP" ] && [ "$LAN_IP" != "$HEALTHCHECK_HOST" ] && [ "$LAN_IP" != "$TAILSCALE_IP" ]; then
        echo "  局域网:   http://${LAN_IP}:${LOCAL_BACKEND_PORT}"
    fi
    echo "  智能看板: http://${HEALTHCHECK_HOST}:${LOCAL_BACKEND_PORT}/board.html"
    echo "  知识库:   http://${HEALTHCHECK_HOST}:${LOCAL_BACKEND_PORT}/kb.html"
    echo "  API状态:  http://${HEALTHCHECK_HOST}:${LOCAL_BACKEND_PORT}/api/board/stats"
    echo ""
    echo -e "${YELLOW}日志: tail -f APP/backend/nohup.out${NC}"
else
    echo -e "${RED}⚠️ 服务可能启动失败，请检查日志${NC}"
    tail -20 nohup.out
fi
