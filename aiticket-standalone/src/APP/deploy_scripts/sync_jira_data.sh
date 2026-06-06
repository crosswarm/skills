#!/bin/bash
# sync_jira_data.sh - 从Jira拉取数据并同步到服务器
#
# 用法:
#   ./sync_jira_data.sh              # 仅拉取数据到本地
#   ./sync_jira_data.sh --push       # 拉取并推送到服务器
#   ./sync_jira_data.sh --push-only  # 仅推送现有数据到服务器
#
# 前提条件:
#   1. 本地已连接VPN
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
echo -e "${GREEN}📦 Jira数据同步工具${NC}"
echo -e "${GREEN}========================================${NC}"

# 解析参数
PUSH_DATA=false
PULL_DATA=true

for arg in "$@"; do
    case $arg in
        --push) PUSH_DATA=true ;;
        --push-only) PULL_DATA=false; PUSH_DATA=true ;;
    esac
done

# 检查VPN连接
check_vpn() {
    echo -e "${YELLOW}[1/4] 检查VPN连接...${NC}"
    if curl -s --connect-timeout 5 ${JIRA_BASE_URL}/ -o /dev/null 2>/dev/null; then
        echo -e "${GREEN}✓ VPN连接正常${NC}"
        return 0
    else
        echo -e "${RED}✗ 无法连接Jira，请确认VPN已连接${NC}"
        return 1
    fi
}

# 拉取Jira数据
pull_jira_data() {
    echo -e "${YELLOW}[2/4] 拉取Jira数据...${NC}"

    cd "$BACKEND_DIR"

    # 激活虚拟环境
    if [ -d ".venv" ]; then
        source .venv/bin/activate
    fi

    # 运行Python脚本拉取数据
    python3 << 'PYTHON_SCRIPT'
import os
import json
import requests
from datetime import datetime
from jira_config import JiraConfigParser

# 配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JIRA_API_PATH = os.path.join(BASE_DIR, "interface/jira_api.md")
SRC_DIR = os.path.normpath(os.path.join(BASE_DIR, "../../src"))

# 确保src目录存在
os.makedirs(SRC_DIR, exist_ok=True)

# 加载配置
config_parser = JiraConfigParser(JIRA_API_PATH)
config = config_parser.get_common_config()

headers = config.get("headers", {})
cookies = config.get("cookies", {})
base_url = "${JIRA_BASE_URL}"

# 设置必要headers
headers['X-Atlassian-Token'] = 'no-check'

def fetch_issues(jql, label=""):
    """拉取工单数据"""
    url = f"{base_url}/rest/issueNav/1/issueTable"
    all_issues = []
    start = 0
    batch_size = 100

    print(f"  正在拉取: {label}")
    while True:
        data = {
            'startIndex': start,
            'jql': jql,
            'layoutKey': 'list-view',
            'filterId': -1,
            'fields': 'key,summary,status,assignee,reporter,created,updated,duedate,priority,issuetype,project,description'
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                cookies=cookies,
                data=data,
                verify=False,
                timeout=30
            )
            result = response.json()

            issues = result.get('issues', [])
            if not issues:
                break

            all_issues.extend(issues)
            print(f"    已获取 {len(all_issues)} 条...")

            if len(issues) < batch_size:
                break
            start += batch_size

        except Exception as e:
            print(f"    错误: {e}")
            break

    return all_issues

# JQL查询
jql_queries = {
    "flow_center": 'project = "云平台-流程中心" ORDER BY updated DESC',
    "weekly": 'project = "云平台-流程中心" AND created >= -7d ORDER BY created DESC',
    "my_issues": 'assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC'
}

# 拉取数据
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
results = {}

for name, jql in jql_queries.items():
    issues = fetch_issues(jql, name)
    results[name] = {
        "total": len(issues),
        "issues": issues,
        "updated": timestamp
    }
    print(f"  {name}: {len(issues)} 条")

# 保存到文件
output_file = os.path.join(SRC_DIR, f"jira_data_{timestamp}.json")
with open(output_file, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

# 同时更新latest文件
latest_file = os.path.join(SRC_DIR, "jira_data_latest.json")
with open(latest_file, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\n✓ 数据已保存:")
print(f"  - {output_file}")
print(f"  - {latest_file}")
PYTHON_SCRIPT

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ 数据拉取完成${NC}"
    else
        echo -e "${RED}✗ 数据拉取失败${NC}"
        exit 1
    fi
}

# 同步到服务器
push_to_server() {
    echo -e "${YELLOW}[3/4] 同步数据到服务器...${NC}"

    # 同步src目录
    rsync -avz --progress "${PROJECT_DIR}/src/" "${SERVER}:${REMOTE_DIR}/src/"

    echo -e "${GREEN}✓ 数据同步完成${NC}"
}

# 触发服务器重新加载
trigger_reload() {
    echo -e "${YELLOW}[4/4] 触发向量库更新...${NC}"

    ssh $SERVER "curl -s -X POST http://localhost:${QCL_BACKEND_PORT}/analyze" 2>/dev/null || true

    echo -e "${GREEN}✓ 已触发数据更新${NC}"
}

# 主流程
if [ "$PULL_DATA" = true ]; then
    check_vpn || exit 1
    pull_jira_data
else
    echo -e "${BLUE}[2/4] 跳过数据拉取${NC}"
fi

if [ "$PUSH_DATA" = true ]; then
    push_to_server
    trigger_reload
else
    echo -e "${BLUE}[3/4] 跳过推送（使用 --push 推送到服务器）${NC}"
    echo -e "${BLUE}[4/4] 跳过重新加载${NC}"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✅ 同步完成！${NC}"
echo -e "${GREEN}========================================${NC}"

if [ "$PUSH_DATA" = true ]; then
    echo ""
    echo "访问地址: http://154.8.231.122/"
fi
