#!/bin/bash
# sync_report_data.sh - 推送报告CSV和JSON数据到QCL
# 用法:
#   ./sync_report_data.sh              # 推送所有报告数据
#   ./sync_report_data.sh --weekly     # 仅推送周报数据
#   ./sync_report_data.sh --monthly    # 仅推送月报数据

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/network_env.sh"

SERVER="${QCL_SSH_TARGET}"
REMOTE_DIR="/opt/ai-ticket"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SYNC_WEEKLY=true
SYNC_MONTHLY=true
for arg in "$@"; do
    case $arg in
        --weekly) SYNC_MONTHLY=false ;;
        --monthly) SYNC_WEEKLY=false ;;
    esac
done

echo -e "${GREEN}📊 推送报告数据到QCL${NC}"

# 检查连接
if ! ssh -o ConnectTimeout=5 "$SERVER" "echo ok" > /dev/null 2>&1; then
    echo -e "${RED}无法连接 $SERVER${NC}"
    exit 1
fi

SYNCED=0

ssh "$SERVER" "mkdir -p ${REMOTE_DIR}/src ${REMOTE_DIR}/conclusion/WeeklyReports ${REMOTE_DIR}/conclusion/MonthlyReports" 2>/dev/null

if [ "$SYNC_WEEKLY" = true ]; then
    # 推送最新周报CSV (只推最新的，避免大量文件传输)
    echo -e "${YELLOW}  推送 最新周数据CSV...${NC}"
    LATEST_WEEKLY_CSV=$(ls -t "$PROJECT_DIR"/src/*周数据*.csv 2>/dev/null | head -1)
    if [ -n "$LATEST_WEEKLY_CSV" ]; then
        scp "$LATEST_WEEKLY_CSV" "${SERVER}:${REMOTE_DIR}/src/" 2>/dev/null && echo "    $(basename "$LATEST_WEEKLY_CSV")"
    fi
    SYNCED=$((SYNCED + 1))

    # 推送周报JSON+MD
    echo -e "${YELLOW}  推送 WeeklyReports/...${NC}"
    rsync -avz "$PROJECT_DIR/conclusion/WeeklyReports/" \
        "${SERVER}:${REMOTE_DIR}/conclusion/WeeklyReports/" 2>/dev/null
    SYNCED=$((SYNCED + 1))
fi

if [ "$SYNC_MONTHLY" = true ]; then
    # 推送最新月报CSV
    echo -e "${YELLOW}  推送 最新月数据CSV...${NC}"
    LATEST_MONTHLY_CSV=$(ls -t "$PROJECT_DIR"/src/*月数据*.csv 2>/dev/null | head -1)
    if [ -n "$LATEST_MONTHLY_CSV" ]; then
        scp "$LATEST_MONTHLY_CSV" "${SERVER}:${REMOTE_DIR}/src/" 2>/dev/null && echo "    $(basename "$LATEST_MONTHLY_CSV")"
    fi
    SYNCED=$((SYNCED + 1))

    # 推送月报JSON+MD
    echo -e "${YELLOW}  推送 MonthlyReports/...${NC}"
    rsync -avz "$PROJECT_DIR/conclusion/MonthlyReports/" \
        "${SERVER}:${REMOTE_DIR}/conclusion/MonthlyReports/" 2>/dev/null
    SYNCED=$((SYNCED + 1))
fi

# 同时推送LLM配置和KPI配置
echo -e "${YELLOW}  推送配置文件...${NC}"
rsync -avz "$PROJECT_DIR/APP/backend/llm_config.json" "${SERVER}:${REMOTE_DIR}/APP/backend/"
rsync -avz "$PROJECT_DIR/APP/backend/config/kpi_config.json" "${SERVER}:${REMOTE_DIR}/APP/backend/config/"

echo -e "${GREEN}✅ 报告数据推送完成 ($SYNCED 项)${NC}"
