#!/bin/bash
# sync_data.sh - 同步向量数据库和其他数据文件到服务器
# 用法: ./sync_data.sh [--full]

set -e

# 配置
SERVER="qcl"
QCL_BACKEND_PORT="${QCL_BACKEND_PORT:-18000}"
REMOTE_DIR="/opt/ai-ticket"
LOCAL_DIR="/Volumes/MacMini/Users/cfone/Documents/用友/AI工单"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}📦 数据同步工具${NC}"
echo -e "${GREEN}========================================${NC}"

# 检查服务器连接
echo -e "${YELLOW}[1/4] 检查服务器连接...${NC}"
if ! ssh -o ConnectTimeout=5 $SERVER "echo '连接成功'" > /dev/null 2>&1; then
    echo -e "${RED}❌ 无法连接到服务器${NC}"
    exit 1
fi
echo -e "${GREEN}✓ 服务器连接正常${NC}"

# 停止服务（避免数据冲突）
echo -e "${YELLOW}[2/4] 停止后端服务...${NC}"
ssh $SERVER "sudo supervisorctl stop ai-ticket" 2>/dev/null || true
echo -e "${GREEN}✓ 服务已停止${NC}"

# 同步数据
echo -e "${YELLOW}[3/4] 同步数据文件...${NC}"

# 同步chroma_db
echo "  同步 chroma_db..."
rsync -avz --delete "${LOCAL_DIR}/APP/backend/chroma_db/" "${SERVER}:${REMOTE_DIR}/APP/backend/chroma_db/"

# 同步conclusion数据（如果指定--full）
if [ "$1" = "--full" ]; then
    echo "  同步 conclusion..."
    rsync -avz --exclude='*.pyc' --exclude='__pycache__' "${LOCAL_DIR}/conclusion/" "${SERVER}:${REMOTE_DIR}/conclusion/"
fi

# 同步LLM配置
echo "  同步 llm_config.json..."
rsync -avz "${LOCAL_DIR}/APP/backend/llm_config.json" "${SERVER}:${REMOTE_DIR}/APP/backend/"

echo -e "${GREEN}✓ 数据同步完成${NC}"

# 重启服务
echo -e "${YELLOW}[4/4] 重启后端服务...${NC}"
ssh $SERVER "sudo supervisorctl start ai-ticket"
sleep 5

# 验证
echo -e "${YELLOW}验证服务状态...${NC}"
STATUS=$(ssh $SERVER "curl -s -o /dev/null -w '%{http_code}' http://localhost:${QCL_BACKEND_PORT}/api/board/stats")
if [ "$STATUS" = "200" ]; then
    echo -e "${GREEN}✅ 服务运行正常${NC}"
else
    echo -e "${RED}⚠️ 服务状态异常 (HTTP ${STATUS})${NC}"
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✅ 数据同步完成！${NC}"
echo -e "${GREEN}========================================${NC}"
