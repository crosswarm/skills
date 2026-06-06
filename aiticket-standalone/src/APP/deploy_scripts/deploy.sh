#!/bin/bash
# deploy.sh - 本地一键部署脚本
# 用法:
#   ./deploy.sh                    # 只推送（无更改时）
#   ./deploy.sh "commit信息"        # 提交并推送
#   ./deploy.sh --with-data        # 同时同步chroma_db数据
#   ./deploy.sh "commit信息" --with-data  # 提交+推送+同步数据

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
REMOTE_NAME="deploy"
BRANCH="main"
SYNC_DATA=false
QCL_BACKEND_PORT="${QCL_BACKEND_PORT:-18000}"

# 解析参数
COMMIT_MSG=""
for arg in "$@"; do
    case $arg in
        --with-data) SYNC_DATA=true ;;
        *) COMMIT_MSG="$arg" ;;
    esac
done

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}🚀 AI工单系统部署工具${NC}"
echo -e "${GREEN}========================================${NC}"

cd "$PROJECT_DIR"

# 检查是否有未提交的更改
if [ -n "$(git status --porcelain)" ]; then
    if [ -z "$COMMIT_MSG" ]; then
        echo -e "${RED}❌ 有未提交的更改，请提供commit信息${NC}"
        echo "用法: ./deploy.sh \"commit信息\""
        exit 1
    fi

    echo -e "${YELLOW}[1/3] 提交更改...${NC}"
    git add .
    git commit -m "$COMMIT_MSG"
    echo -e "${GREEN}✓ 提交完成${NC}"
else
    echo -e "${BLUE}[1/3] 无待提交更改${NC}"
fi

# 推送代码
echo -e "${YELLOW}[2/3] 推送到部署服务器...${NC}"
git push $REMOTE_NAME $BRANCH
echo -e "${GREEN}✓ 代码推送完成${NC}"

# 同步数据（如果需要）
if [ "$SYNC_DATA" = true ]; then
    echo -e "${YELLOW}[3/3] 同步向量数据...${NC}"
    "$SCRIPT_DIR/sync_data.sh"
else
    echo -e "${BLUE}[3/3] 跳过数据同步（使用 --with-data 同步向量数据）${NC}"
fi

# 验证服务
echo ""
echo -e "${YELLOW}验证服务状态...${NC}"
sleep 3
STATUS=$(ssh ${REMOTE_HOST:-server} "curl -s -o /dev/null -w '%{http_code}' http://localhost:${QCL_BACKEND_PORT}/api/board/stats" 2>/dev/null || echo "000")
if [ "$STATUS" = "200" ]; then
    echo -e "${GREEN}✅ 服务运行正常 (HTTP ${STATUS})${NC}"
else
    echo -e "${RED}⚠️ 服务状态异常 (HTTP ${STATUS})，请检查日志${NC}"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✅ 部署完成！${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "访问地址: http://154.8.231.122/"
echo ""
echo "常用命令:"
echo "  查看状态: ./server_utils.sh status"
echo "  查看日志: ./server_utils.sh logs"
echo "  健康检查: ./server_utils.sh health"
