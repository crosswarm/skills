#!/bin/bash
# server_utils.sh - 服务器运维工具集
# 用法: ./server_utils.sh <command>
#
# 命令列表:
#   status    - 查看服务状态
#   logs      - 查看最近日志
#   restart   - 重启服务
#   stop      - 停止服务
#   start     - 启动服务
#   health    - 健康检查
#   backup    - 备份数据
#   ports     - 检查端口占用
#   cleanup   - 清理旧日志和缓存

set -e

SERVER="${REMOTE_HOST:-server}"
REMOTE_DIR="/opt/ai-ticket"
QCL_BACKEND_PORT="${QCL_BACKEND_PORT:-18000}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

show_help() {
    echo "AI工单系统运维工具"
    echo ""
    echo "用法: $0 <command>"
    echo ""
    echo "命令列表:"
    echo "  status    查看服务状态"
    echo "  logs      查看最近日志 (可选: logs err 查看错误日志)"
    echo "  restart   重启后端服务"
    echo "  stop      停止后端服务"
    echo "  start     启动后端服务"
    echo "  health    健康检查"
    echo "  backup    备份chroma_db数据"
    echo "  ports     检查端口占用"
    echo "  cleanup   清理旧日志和缓存"
    echo "  info      显示系统信息"
}

cmd_status() {
    echo -e "${BLUE}=== 服务状态 ===${NC}"
    ssh $SERVER "sudo supervisorctl status ai-ticket"

    echo ""
    echo -e "${BLUE}=== API状态 ===${NC}"
    ssh $SERVER "curl -s http://localhost:${QCL_BACKEND_PORT}/api/board/stats | python3 -m json.tool 2>/dev/null || echo 'API无响应'"

    echo ""
    echo -e "${BLUE}=== Nginx状态 ===${NC}"
    ssh $SERVER "sudo systemctl is-active nginx"
}

cmd_logs() {
    LOG_TYPE=${1:-"out"}
    LINES=${2:-100}

    if [ "$LOG_TYPE" = "err" ]; then
        echo -e "${BLUE}=== 最近 ${LINES} 行错误日志 ===${NC}"
        ssh $SERVER "tail -${LINES} /var/log/supervisor/ai-ticket.err.log"
    else
        echo -e "${BLUE}=== 最近 ${LINES} 行输出日志 ===${NC}"
        ssh $SERVER "tail -${LINES} /var/log/supervisor/ai-ticket.out.log"
    fi
}

cmd_restart() {
    echo -e "${YELLOW}重启后端服务...${NC}"
    ssh $SERVER "sudo supervisorctl restart ai-ticket"
    sleep 3
    cmd_status
}

cmd_stop() {
    echo -e "${YELLOW}停止后端服务...${NC}"
    ssh $SERVER "sudo supervisorctl stop ai-ticket"
    echo -e "${GREEN}✓ 服务已停止${NC}"
}

cmd_start() {
    echo -e "${YELLOW}启动后端服务...${NC}"
    ssh $SERVER "sudo supervisorctl start ai-ticket"
    sleep 3
    cmd_status
}

cmd_health() {
    echo -e "${BLUE}=== 健康检查 ===${NC}"

    # 检查API
    echo -n "API: "
    STATUS=$(ssh $SERVER "curl -s -o /dev/null -w '%{http_code}' http://localhost:${QCL_BACKEND_PORT}/api/board/stats")
    if [ "$STATUS" = "200" ]; then
        echo -e "${GREEN}✓ 正常 (HTTP ${STATUS})${NC}"
    else
        echo -e "${RED}✗ 异常 (HTTP ${STATUS})${NC}"
    fi

    # 检查向量库
    echo -n "向量库: "
    COUNT=$(ssh $SERVER "curl -s http://localhost:${QCL_BACKEND_PORT}/api/board/stats | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"stats\"][\"vector_stats\"][\"issues_count\"])' 2>/dev/null || echo 0")
    if [ "$COUNT" -gt 0 ]; then
        echo -e "${GREEN}✓ ${COUNT} 条记录${NC}"
    else
        echo -e "${RED}✗ 无数据${NC}"
    fi

    # 检查磁盘空间
    echo -n "磁盘空间: "
    ssh $SERVER "pct=\$(df -h /opt | tail -1 | awk '{print \$5}'); if [ \${pct%\%} -lt 80 ]; then echo -e '${GREEN}✓ 正常 ('\"\$pct\"')${NC}'; else echo -e '${YELLOW}⚠ 警告 ('\"\$pct\"')${NC}'; fi"
}

cmd_backup() {
    BACKUP_DIR="/opt/ai-ticket/backups"
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)

    echo -e "${YELLOW}备份数据...${NC}"
    ssh $SERVER "mkdir -p ${BACKUP_DIR}"
    ssh $SERVER "tar -czf ${BACKUP_DIR}/chroma_db_${TIMESTAMP}.tar.gz -C ${REMOTE_DIR}/APP/backend chroma_db"
    ssh $SERVER "tar -czf ${BACKUP_DIR}/config_${TIMESTAMP}.tar.gz -C ${REMOTE_DIR}/APP/backend llm_config.json"

    # 保留最近7个备份
    ssh $SERVER "ls -t ${BACKUP_DIR}/chroma_db_*.tar.gz | tail -n +8 | xargs rm -f 2>/dev/null || true"
    ssh $SERVER "ls -t ${BACKUP_DIR}/config_*.tar.gz | tail -n +8 | xargs rm -f 2>/dev/null || true"

    echo -e "${GREEN}✓ 备份完成: ${BACKUP_DIR}/${NC}"
    ssh $SERVER "ls -lh ${BACKUP_DIR}/*.tar.gz | tail -4"
}

cmd_ports() {
    echo -e "${BLUE}=== 端口占用检查 ===${NC}"
    echo "端口 ${QCL_BACKEND_PORT} (后端):"
    ssh $SERVER "sudo lsof -i :${QCL_BACKEND_PORT} 2>/dev/null | head -5 || echo '  未占用'"
    echo ""
    echo "端口 80 (Nginx):"
    ssh $SERVER "sudo lsof -i :80 2>/dev/null | head -5 || echo '  未占用'"
}

cmd_cleanup() {
    echo -e "${YELLOW}清理旧日志和缓存...${NC}"
    ssh $SERVER "
        # 清理Python缓存
        find ${REMOTE_DIR} -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
        find ${REMOTE_DIR} -type f -name '*.pyc' -delete 2>/dev/null || true

        # 截断过大日志 (>10MB)
        for log in /var/log/supervisor/ai-ticket.*.log; do
            if [ -f \"\$log\" ] && [ \$(stat -f%z \"\$log\" 2>/dev/null || stat -c%s \"\$log\") -gt 10485760 ]; then
                sudo truncate -s 5M \"\$log\"
            fi
        done
    "
    echo -e "${GREEN}✓ 清理完成${NC}"
}

cmd_info() {
    echo -e "${BLUE}=== 系统信息 ===${NC}"
    ssh $SERVER "
        echo '服务器: $(hostname) ($(cat /etc/os-release | grep PRETTY_NAME | cut -d'\"' -f2))'
        echo '内核: $(uname -r)'
        echo 'CPU: $(nproc) 核心'
        echo '内存: $(free -h | grep Mem | awk '{print \$2}')'
        echo '磁盘: $(df -h /opt | tail -1 | awk '{print \$4}') 可用'
        echo ''
        echo 'Python: $(python3 --version)'
        echo '项目目录: ${REMOTE_DIR}'
        echo '向量库大小: $(du -sh ${REMOTE_DIR}/APP/backend/chroma_db 2>/dev/null | cut -f1)'
    "
}

# 主命令
case "$1" in
    status)   cmd_status ;;
    logs)     cmd_logs "$2" "$3" ;;
    restart)  cmd_restart ;;
    stop)     cmd_stop ;;
    start)    cmd_start ;;
    health)   cmd_health ;;
    backup)   cmd_backup ;;
    ports)    cmd_ports ;;
    cleanup)  cmd_cleanup ;;
    info)     cmd_info ;;
    *)        show_help ;;
esac
