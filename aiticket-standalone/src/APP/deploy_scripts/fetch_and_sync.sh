#!/bin/bash
# fetch_and_sync.sh - 本地拉取Jira数据并同步到服务器
#
# 用法:
#   ./fetch_and_sync.sh              # 拉取数据到本地
#   ./fetch_and_sync.sh --sync       # 拉取并同步到服务器
#   ./fetch_and_sync.sh --sync-only  # 仅同步现有缓存到服务器
#
# 前提条件:
#   1. 本地已连接VPN（如需拉取数据）
#   2. Python环境和依赖已安装
#   3. jira_api.md 中的Cookie有效

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_DIR="$PROJECT_DIR/APP/backend"
SERVER="${REMOTE_HOST:-server}"
QCL_BACKEND_PORT="${QCL_BACKEND_PORT:-18000}"
REMOTE_DIR="/opt/ai-ticket"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}📦 Jira数据拉取与同步工具${NC}"
echo -e "${GREEN}========================================${NC}"

# 解析参数
SYNC_DATA=false
FETCH_DATA=true

for arg in "$@"; do
    case $arg in
        --sync) SYNC_DATA=true ;;
        --sync-only) FETCH_DATA=false; SYNC_DATA=true ;;
    esac
done

# 检查VPN连接
check_vpn() {
    echo -e "${YELLOW}[1/5] 检查VPN连接...${NC}"
    if curl -s --connect-timeout 5 ${JIRA_BASE_URL}/ -o /dev/null 2>/dev/null; then
        echo -e "${GREEN}✓ VPN连接正常${NC}"
        return 0
    else
        echo -e "${RED}✗ 无法连接Jira，请确认VPN已连接${NC}"
        return 1
    fi
}

# 拉取Jira数据
fetch_jira_data() {
    echo -e "${YELLOW}[2/5] 拉取Jira看板数据...${NC}"

    cd "$BACKEND_DIR"

    # 激活虚拟环境
    if [ -d ".venv" ]; then
        source .venv/bin/activate
    fi

    # 运行Python脚本拉取数据
    python3 << 'PYTHON_SCRIPT'
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jira_service import jira_service, JiraIssue
from jira_config import JiraConfigParser

print("[Fetch] 初始化Jira服务...")
jira_service.reload_config()

# 检查配置
config = jira_service.config_parser.get_common_config()
if not config.get('cookies', {}).get('JSESSIONID'):
    print("[Error] Jira Cookie未配置，请检查 jira_api.md")
    sys.exit(1)

# JQL查询 - 获取当前用户的未解决工单
jql = 'project = "云平台-流程中心" AND resolution = Unresolved AND assignee in (currentUser()) ORDER BY due ASC, updated DESC'

print(f"[Fetch] 查询: {jql}")
issues_data = jira_service.search_issues(jql)

if 'error' in issues_data:
    print(f"[Error] Jira查询失败: {issues_data['error']}")
    sys.exit(1)

issues = jira_service.parse_issue_table_response(issues_data)
print(f"[Fetch] 获取到 {len(issues)} 条工单")

if issues:
    # 保存到缓存
    jira_service.save_board_cache(issues)
    print(f"[Fetch] 数据已保存到缓存文件")
else:
    print("[Warning] 没有获取到工单数据")
    sys.exit(1)
PYTHON_SCRIPT

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ 数据拉取完成${NC}"
    else
        echo -e "${RED}✗ 数据拉取失败${NC}"
        exit 1
    fi
}

# 同步到服务器
sync_to_server() {
    echo -e "${YELLOW}[3/5] 同步数据到服务器...${NC}"

    # 检查服务器连接
    if ! ssh -o ConnectTimeout=5 $SERVER "echo '连接成功'" > /dev/null 2>&1; then
        echo -e "${RED}❌ 无法连接到服务器${NC}"
        exit 1
    fi

    # 停止服务（避免数据冲突）
    echo -e "${YELLOW}[4/5] 停止后端服务...${NC}"
    ssh $SERVER "sudo supervisorctl stop ai-ticket" 2>/dev/null || true

    # 同步缓存文件
    echo "  同步 data_cache/jira_board_data.json..."
    rsync -avz "$BACKEND_DIR/data_cache/" "${SERVER}:${REMOTE_DIR}/APP/backend/data_cache/"

    # 同步chroma_db（确保向量库最新）
    echo "  同步 chroma_db..."
    rsync -avz --delete "$BACKEND_DIR/chroma_db/" "${SERVER}:${REMOTE_DIR}/APP/backend/chroma_db/"

    echo -e "${GREEN}✓ 数据同步完成${NC}"
}

# 重启服务并验证
restart_and_verify() {
    echo -e "${YELLOW}[5/5] 重启后端服务...${NC}"
    ssh $SERVER "sudo supervisorctl start ai-ticket"

    echo -e "${YELLOW}等待服务启动...${NC}"
    sleep 5

    # 验证
    echo -e "${YELLOW}验证服务状态...${NC}"
    STATUS=$(ssh $SERVER "curl -s -o /dev/null -w '%{http_code}' http://localhost:${QCL_BACKEND_PORT}/api/board/stats" 2>/dev/null || echo "000")
    if [ "$STATUS" = "200" ]; then
        echo -e "${GREEN}✅ 服务运行正常${NC}"

        # 显示缓存信息
        echo ""
        echo -e "${BLUE}服务器缓存信息:${NC}"
        ssh $SERVER "cd ${REMOTE_DIR}/APP/backend && python3 -c \"
import json
import os
cache_file = 'data_cache/jira_board_data.json'
if os.path.exists(cache_file):
    with open(cache_file, 'r') as f:
        data = json.load(f)
    print(f'  缓存时间: {data.get(\"timestamp\", \"unknown\")}')
    print(f'  工单数量: {data.get(\"count\", 0)} 条')
else:
    print('  缓存文件不存在')
\""
    else
        echo -e "${RED}⚠️ 服务状态异常 (HTTP ${STATUS})${NC}"
    fi
}

# 主流程
if [ "$FETCH_DATA" = true ]; then
    check_vpn || exit 1
    fetch_jira_data
else
    echo -e "${BLUE}[2/5] 跳过数据拉取${NC}"
fi

if [ "$SYNC_DATA" = true ]; then
    sync_to_server
    restart_and_verify
else
    echo -e "${BLUE}[3/5] 跳过数据同步（使用 --sync 推送到服务器）${NC}"
    echo -e "${BLUE}[4/5] 跳过停止服务${NC}"
    echo -e "${BLUE}[5/5] 跳过重启服务${NC}"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✅ 完成！${NC}"
echo -e "${GREEN}========================================${NC}"

if [ "$SYNC_DATA" = true ]; then
    echo ""
    echo "访问地址: http://154.8.231.122/board.html"
fi
