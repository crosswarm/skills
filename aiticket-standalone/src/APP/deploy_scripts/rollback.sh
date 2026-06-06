#!/bin/bash
# 一键回退到指定版本
# 用法: bash APP/deploy_scripts/rollback.sh <version> [path]
#
# 参数:
#   version: Git 版本标签 (如 v2026.03.02)
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
    echo "示例: $0 v2026.03.02"
    echo ""
    echo "可用版本:"
    git tag -l | tail -10
    exit 1
fi

echo "=========================================="
echo "智能看板集群 - 紧急回退"
echo "=========================================="
echo -e "${RED}警告: 即将回退到版本 $VERSION${NC}"
echo "项目路径: $PROJECT_PATH"
echo "=========================================="
echo ""

# 确认
read -p "确认回退? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "回退已取消"
    exit 0
fi

# 二次确认
read -p "再次确认? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "回退已取消"
    exit 0
fi

echo ""
echo "开始回退..."

# qcl 回退
echo ""
echo -e "${YELLOW}[1/3] 回退 qcl...${NC}"
ssh "$QCL_HOST" << EOF
cd $PROJECT_PATH
echo "  备份当前状态..."
cp -r APP/backend APP/backend.backup.$(date +%Y%m%d_%H%M%S)

echo "  切换到版本 $VERSION..."
git checkout $VERSION

echo "  重启服务..."
if command -v systemctl &> /dev/null; then
    systemctl restart ai-backend-qcl
    echo "  等待服务启动..."
    sleep 5
elif command -v supervisorctl &> /dev/null; then
    supervisorctl restart ai-backend
    sleep 5
fi

# 检查服务状态
curl -s http://localhost:${QCL_BACKEND_PORT}/health > /dev/null && echo "  ✓ qcl 服务正常" || echo "  ✗ qcl 服务异常"
EOF
echo -e "${GREEN}qcl 回退完成${NC}"

# mini 回退
echo ""
echo -e "${YELLOW}[2/3] 回退 mini...${NC}"
ssh "$MINI_HOST" << EOF
cd $PROJECT_PATH
echo "  备份当前状态..."
cp -r APP/backend APP/backend.backup.$(date +%Y%m%d_%H%M%S)

echo "  切换到版本 $VERSION..."
git checkout $VERSION

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
echo -e "${GREEN}mini 回退完成${NC}"

# lap 回退 (可选)
echo ""
echo -e "${YELLOW}[3/3] 检查 lap...${NC}"
if ssh -o ConnectTimeout=3 "$LAP_HOST" "echo online" 2>/dev/null; then
    echo "  lap 在线，开始回退..."
    ssh "$LAP_HOST" << EOF
cd $PROJECT_PATH
echo "  切换到版本 $VERSION..."
git checkout $VERSION

echo "  重启 jira_proxy..."
pkill -f jira_proxy || true
cd APP/backend
nohup bash ../deploy_scripts/run_jira_proxy.sh > logs/jira_proxy.log 2>&1 &
sleep 3

# 检查服务状态
curl -s http://localhost:${MINI_PROXY_PORT}/proxy/health > /dev/null && echo "  ✓ lap 服务正常" || echo "  ✗ lap 服务异常"
EOF
    echo -e "${GREEN}lap 回退完成${NC}"
else
    echo -e "${YELLOW}  lap 离线，跳过回退${NC}"
fi

# 验证回退
echo ""
echo "=========================================="
echo "验证回退结果..."
echo "=========================================="

bash "$SCRIPT_DIR/check_all_services.sh"

echo ""
echo -e "${GREEN}回退完成！${NC}"
echo "版本: $VERSION"
echo ""
echo "如遇问题，请参考:"
echo "  - _local/design/deployment-handbook.md (部署手册)"
echo "  - _local/design/network-deployment.md (网络部署文档)"
