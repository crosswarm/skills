#!/bin/bash
# reset_demo.sh — 还原 Demo 沙箱到 baseline 快照
# 必须在 QCL /opt/ai-ticket-demo/ 目录下运行，或以 root 执行
# 用途：cron（每天 08:00）+ POST /api/admin/reset-demo 手动触发

set -euo pipefail

DEMO_ROOT="${DEMO_ROOT:-/opt/ai-ticket-demo}"
BASELINE="$DEMO_ROOT/baseline"
DATA="$DEMO_ROOT/data"
LOG_FILE="/var/log/aiticket-demo-reset.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "==============================="
log "Demo reset 开始"
log "==============================="

# ── 验证基线存在 ─────────────────────────────────────────────────
if [ ! -d "$BASELINE" ]; then
    log "ERROR: baseline 目录不存在: $BASELINE"
    exit 1
fi

# ── 停服务 ──────────────────────────────────────────────────────
log "停止 aiticket-demo 服务..."
systemctl stop aiticket-demo 2>&1 | while read -r l; do log "  $l"; done || true

# ── 原子替换 data ───────────────────────────────────────────────
log "替换 data 目录..."
if [ -d "$DATA" ]; then
    mv "$DATA" "${DATA}.tombstone-$(date +%Y%m%d_%H%M%S)"
fi
cp -r "$BASELINE" "$DATA"
log "  data 目录已还原"

# ── 清理 >7 天的 tombstone ───────────────────────────────────────
find "$DEMO_ROOT" -maxdepth 1 -name "data.tombstone-*" -mtime +7 \
    -exec rm -rf {} + 2>/dev/null || true

# ── 重启服务 ────────────────────────────────────────────────────
log "启动 aiticket-demo 服务..."
systemctl start aiticket-demo 2>&1 | while read -r l; do log "  $l"; done || true

log "==============================="
log "Demo reset 完成 ✓"
log "==============================="
