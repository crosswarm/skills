#!/usr/bin/env bash
# 验证 aiticket 实例健康状态
set -euo pipefail

BASE="${1:-http://127.0.0.1:18000}"
PASS=0
FAIL=0

check() {
    local name="$1" url="$2"
    if curl -fsS --max-time 5 "$url" > /dev/null 2>&1; then
        echo "  ✓ $name"
        PASS=$((PASS+1))
    else
        echo "  ✗ $name  ($url)"
        FAIL=$((FAIL+1))
    fi
}

echo "=== aiticket health check ($BASE) ==="
check "instance config"  "$BASE/api/instance/config"
check "board stats"      "$BASE/api/board/stats"
check "kb manifest"      "$BASE/api/kb/manifest"
check "auth me"          "$BASE/api/auth/me"
check "frontend"         "$BASE/"

echo ""
echo "通过: $PASS  失败: $FAIL"
[ "$FAIL" -eq 0 ] && echo "✅ 健康检查全部通过" || { echo "❌ 有 $FAIL 项未通过"; exit 1; }
