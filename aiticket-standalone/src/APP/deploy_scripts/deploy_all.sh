#!/bin/bash
# 一键部署到所有节点
# 用法: bash APP/deploy_scripts/deploy_all.sh <version> [path]
#
# 参数:
#   version: Git 版本标签或分支名 (如 v2026.03.03)
#   path: 项目路径 (默认 /path/to/AI工单)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/network_env.sh"

VERSION=$1
PROJECT_PATH=${2:-/path/to/AI工单}

# 颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [ -z "$VERSION" ]; then
    echo "用法: $0 <version> [path]"
    echo "示例: $0 v2026.03.03"
    exit 1
fi

echo "=========================================="
echo "智能看板集群 - 一键部署"
echo "=========================================="
echo "版本: $VERSION"
echo "项目路径: $PROJECT_PATH"
echo "=========================================="
echo ""

# 确认
read -p "确认部署版本 $VERSION? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "部署已取消"
    exit 0
fi

# qcl 部署
echo ""
echo -e "${YELLOW}[1/3] 部署 qcl...${NC}"
ssh "$QCL_HOST" << EOF
cd $PROJECT_PATH
echo "  拉取代码..."
git fetch --all
git checkout $VERSION
git pull origin $VERSION

echo "  安装依赖..."
cd APP/backend
pip install -r requirements.txt --quiet

echo "  重启服务..."
# 使用 systemctl 或 supervisor
if command -v systemctl &> /dev/null; then
    systemctl restart ai-backend-qcl
    echo "  等待服务启动..."
    sleep 5
elif command -v supervisorctl &> /dev/null; then
    supervisorctl restart ai-backend
    echo "  等待服务启动..."
    sleep 5
else
    echo "  警告: 未找到 systemctl 或 supervisorctl，请手动重启"
fi

# 检查服务状态
curl -s http://localhost:${QCL_BACKEND_PORT}/health > /dev/null && echo "  ✓ qcl 服务正常" || echo "  ✗ qcl 服务异常"
EOF
echo -e "${GREEN}qcl 部署完成${NC}"

# mini 部署
echo ""
echo -e "${YELLOW}[2/3] 部署 mini...${NC}"
ssh "$MINI_HOST" << EOF
cd $PROJECT_PATH
echo "  拉取代码..."
git fetch --all
git checkout $VERSION
git pull origin $VERSION

echo "  安装依赖..."
cd APP/backend
pip install -r requirements.txt --quiet

echo "  重启 frpc..."
pkill -f 'frpc.*frpc.ini' || true
cd ../..
bash APP/deploy_scripts/start_frpc.sh

echo "  重启 jira_proxy..."
pkill -f jira_proxy || true
cd APP/backend
nohup bash ../deploy_scripts/run_jira_proxy.sh > logs/jira_proxy.log 2>&1 &
sleep 3

# 检查服务状态
curl -s http://localhost:${MINI_PROXY_PORT}/proxy/health > /dev/null && echo "  ✓ mini 服务正常" || echo "  ✗ mini 服务异常"
EOF
echo -e "${GREEN}mini 部署完成${NC}"

# lap 部署 (可选)
echo ""
echo -e "${YELLOW}[3/3] 检查 lap...${NC}"
if ssh -o ConnectTimeout=3 "$LAP_HOST" "echo online" 2>/dev/null; then
    echo "  lap 在线，开始部署..."
    ssh "$LAP_HOST" << EOF
cd $PROJECT_PATH
echo "  拉取代码..."
git fetch --all
git checkout $VERSION
git pull origin $VERSION

echo "  安装依赖..."
cd APP/backend
pip install -r requirements.txt --quiet

echo "  重启 jira_proxy..."
pkill -f jira_proxy || true
nohup bash ../deploy_scripts/run_jira_proxy.sh > logs/jira_proxy.log 2>&1 &
sleep 3

# 检查服务状态
curl -s http://localhost:${MINI_PROXY_PORT}/proxy/health > /dev/null && echo "  ✓ lap 服务正常" || echo "  ✗ lap 服务异常"
EOF
    echo -e "${GREEN}lap 部署完成${NC}"
else
    echo -e "${YELLOW}  lap 离线，跳过部署 (正常状态)${NC}"
fi

# 验证部署
echo ""
echo "=========================================="
echo "验证部署结果..."
echo "=========================================="

bash "$SCRIPT_DIR/check_all_services.sh"

echo ""
echo -e "${GREEN}部署完成！${NC}"
echo "版本: $VERSION"
