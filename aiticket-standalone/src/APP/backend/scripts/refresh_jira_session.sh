#!/bin/bash
# refresh_jira_session.sh — thin wrapper (2026-05-19)
# 已迁移到 JiraSessionRefresher (services/jira_session_refresher.py)
# 本脚本通过 HTTP 触发后端内部刷新，无需 shell 级别 Chrome 解密。
#
# 用法：bash refresh_jira_session.sh [--user <username>]
#   --user 参数保留兼容，但 JiraSessionRefresher.refresh_now() 本地执行时已忽略此参数
#   （Chrome 解密读取的是当前登录 Chrome Default 配置文件，与用户名无关）

set -e

BASE_URL="${JIRA_BACKEND_URL:-http://127.0.0.1:3000}"

echo "触发 JiraSessionRefresher 刷新..."
curl -sf -X POST "${BASE_URL}/api/admin/jira-session/refresh" \
     -H "Content-Type: application/json" \
     --max-time 30 \
     && echo "✅ 刷新已触发" \
     || {
         echo "⚠️  HTTP 触发失败，回退到本地 Python 执行..."
         PYTHON="${PYTHON:-/Volumes/MacMini/opt/miniconda3/envs/antigravity/bin/python}"
         "$PYTHON" -c "
import sys; sys.path.insert(0, '$(dirname "$0")/..')
from services.jira_session_refresher import JiraSessionRefresher
meta = JiraSessionRefresher.get_instance().refresh_now()
print(f'source={meta[\"source\"]} cookies={meta[\"cookie_count\"]}')
"
     }
