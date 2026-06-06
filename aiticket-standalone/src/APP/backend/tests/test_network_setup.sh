#!/bin/bash
# 智能看板网络环境测试脚本

echo "==================================="
echo "智能看板网络环境测试"
echo "==================================="

# 配置参数
qcl_IP="${qcl_IP:-localhost}"
PROXY_URL="${PROXY_URL:-http://${qcl_IP}:8080}"
CACHE_URL="${CACHE_URL:-http://${qcl_IP}:8000}"

# 颜色输出
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 测试计数器
PASS_COUNT=0
FAIL_COUNT=0
TOTAL_COUNT=0

# 测试函数
test_connection() {
    local name=$1
    local url=$2
    TOTAL_COUNT=$((TOTAL_COUNT + 1))

    echo -n "测试 $name ... "
    if curl -s -f --max-time 10 "$url" > /dev/null 2>&1; then
        echo -e "${GREEN}✓ PASS${NC}"
        PASS_COUNT=$((PASS_COUNT + 1))
        return 0
    else
        echo -e "${RED}✗ FAIL${NC}"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return 1
    fi
}

test_api() {
    local name=$1
    local url=$2
    local field=$3
    TOTAL_COUNT=$((TOTAL_COUNT + 1))

    echo -n "测试 $name ... "
    response=$(curl -s --max-time 30 "$url" 2>&1)
    if echo "$response" | grep -q "$field"; then
        echo -e "${GREEN}✓ PASS${NC}"
        PASS_COUNT=$((PASS_COUNT + 1))
        return 0
    else
        echo -e "${RED}✗ FAIL${NC}"
        echo "  响应: $response"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return 1
    fi
}

test_api_with_value() {
    local name=$1
    local url=$2
    local expected_value=$3
    TOTAL_COUNT=$((TOTAL_COUNT + 1))

    echo -n "测试 $name ... "
    response=$(curl -s --max-time 30 "$url" 2>&1)
    if echo "$response" | grep -q '"status":"success"'; then
        echo -e "${GREEN}✓ PASS${NC}"
        PASS_COUNT=$((PASS_COUNT + 1))
        return 0
    else
        echo -e "${RED}✗ FAIL${NC}"
        echo "  响应: $response"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        return 1
    fi
}

# 1. frp 隧道测试
echo ""
echo "[1] frp 隧道测试"
echo "-----------------------------------"

# 检查 frps 进程
if ps aux 2>/dev/null | grep -v grep | grep -q "frps"; then
    echo -e "frps 服务端运行: ${GREEN}✓ PASS${NC}"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo -e "frps 服务端运行: ${YELLOW}⚠ SKIP (本地环境)${NC}"
fi

# 测试 frp 管理面板
test_connection "frp 管理面板" "http://${qcl_IP}:7500"

# 测试代理服务（通过 frp 隧道）
test_connection "代理服务健康检查" "${PROXY_URL}/proxy/health"

# 2. 代理 API 测试
echo ""
echo "[2] Jira 代理 API 测试"
echo "-----------------------------------"

test_api "字段列表接口" "${PROXY_URL}/proxy/jira/fields" '"id"'
test_api "工单搜索接口" "${PROXY_URL}/proxy/jira/search?jql=project=MYPROJECT&maxResults=1" '"issues"'

# 3. 缓存层测试
echo ""
echo "[3] qcl 缓存层测试"
echo "-----------------------------------"

test_connection "缓存服务健康检查" "${CACHE_URL}/api/health"
test_api "看板数据接口" "${CACHE_URL}/api/jira/board_data" '"issues"'

# 4. 缓存性能测试
echo ""
echo "[4] 缓存性能测试"
echo "-----------------------------------"

# 首次请求（无缓存）
echo -n "首次请求（无缓存）... "
start=$(python3 -c "import time; print(int(time.time() * 1000))")
curl -s --max-time 30 "${CACHE_URL}/api/jira/board_data" > /dev/null
end=$(python3 -c "import time; print(int(time.time() * 1000))")
duration=$((end - start))
echo "${duration}ms"

# 二次请求（有缓存）
echo -n "二次请求（有缓存）... "
start=$(python3 -c "import time; print(int(time.time() * 1000))")
curl -s --max-time 5 "${CACHE_URL}/api/jira/board_data" > /dev/null
end=$(python3 -c "import time; print(int(time.time() * 1000))")
duration=$((end - start))
echo "${duration}ms"

if [ $duration -lt 100 ]; then
    echo -e "缓存命中: ${GREEN}✓ PASS${NC} (< 100ms)"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo -e "缓存命中: ${YELLOW}⚠ WARNING${NC} (>= 100ms)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi
TOTAL_COUNT=$((TOTAL_COUNT + 1))

# 5. 监控接口测试
echo ""
echo "[5] 监控接口测试"
echo "-----------------------------------"

test_connection "缓存指标接口" "${CACHE_URL}/api/network/metrics"
test_connection "监控摘要接口" "${CACHE_URL}/api/network/summary"

# 总结
echo ""
echo "==================================="
echo "测试完成"
echo "==================================="
echo -e "通过: ${GREEN}${PASS_COUNT}${NC}/${TOTAL_COUNT}"
echo -e "失败: ${RED}${FAIL_COUNT}${NC}/${TOTAL_COUNT}"

if [ $FAIL_COUNT -eq 0 ]; then
    echo -e "\n${GREEN}所有测试通过!${NC}"
    exit 0
else
    echo -e "\n${RED}部分测试失败，请检查配置和服务状态${NC}"
    exit 1
fi
