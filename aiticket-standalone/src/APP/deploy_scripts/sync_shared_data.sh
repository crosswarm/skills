#!/bin/bash
# sync_shared_data.sh — 双向同步 Mini ↔ QCL 共享数据
#
# 同步目录:
#   conclusion/MonthlyReports/   月报
#   conclusion/WeeklyReports/    周报
#   conclusion/requirements/     需求规划产出
#
# 冲突处理: 两侧同一文件在上次同步后都被修改时 → 保留双份
#   <file>.mine   本地版本
#   <file>.theirs 远端版本
#   conflicts.log 冲突清单，需人工处理
#
# 用法:
#   ./sync_shared_data.sh              # 双向（推送本地改动 + 拉取远端改动）
#   ./sync_shared_data.sh --push       # 只推 Mini→QCL
#   ./sync_shared_data.sh --pull       # 只拉 QCL→Mini
#   ./sync_shared_data.sh --dry-run    # 预览，不实际同步
#   ./sync_shared_data.sh --reset      # 重置 last_sync 时间戳（下次双向全量同步）

set -euo pipefail

# ─── 路径配置 ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

SERVER="qcl"
REMOTE_DIR="/opt/ai-ticket"
LOG_FILE="${REPO_ROOT}/APP/backend/logs/sync_shared_data.log"
LAST_SYNC_FILE="${REPO_ROOT}/APP/backend/data/sync_shared_data_last_sync.txt"
CONFLICT_LOG="${REPO_ROOT}/APP/backend/logs/sync_conflicts.log"

# 需要同步的目录（相对于 REPO_ROOT）
SYNC_DIRS=(
    "conclusion/MonthlyReports"
    "conclusion/WeeklyReports"
    "conclusion/requirements"
    "design/spec"          # 需求规划初稿（spec_generation.py 运行时写入，两侧都可生成）
    "design/template"      # 需求文档模板（requirement_planning.py 运行时读取，用户通过 UI 上传）
)

# ─── 参数解析 ────────────────────────────────────────────────────────────────
MODE="both"   # both | push | pull
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --push)     MODE="push" ;;
        --pull)     MODE="pull" ;;
        --dry-run)  DRY_RUN=true ;;
        --reset)
            rm -f "$LAST_SYNC_FILE"
            echo "Last-sync timestamp reset. Next run will do a full push from Mini."
            exit 0
            ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

# ─── 工具函数 ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

log() { local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"; echo -e "$msg"; echo "$msg" >> "$LOG_FILE" 2>/dev/null || true; }
info()    { log "${GREEN}$*${NC}"; }
warn()    { log "${YELLOW}⚠  $*${NC}"; }
err()     { log "${RED}✗  $*${NC}"; }
section() { log "${CYAN}── $* ──${NC}"; }

dry() { $DRY_RUN && echo -e "${YELLOW}[DRY-RUN]${NC} $*" || true; }

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$LAST_SYNC_FILE")"

# ─── 前置检查 ────────────────────────────────────────────────────────────────
section "Pre-flight"

if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$SERVER" true 2>/dev/null; then
    err "Cannot reach $SERVER — aborting"
    exit 1
fi
info "Connected to $SERVER"

# 获取 last sync 时间戳 (Unix epoch)
if [ -f "$LAST_SYNC_FILE" ]; then
    LAST_SYNC=$(cat "$LAST_SYNC_FILE")
    info "Last sync: $(date -r "$LAST_SYNC" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -d "@$LAST_SYNC" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$LAST_SYNC")"
else
    LAST_SYNC=0
    warn "No last-sync record — first run, will push all local files to QCL"
fi

# ─── 主同步循环 ──────────────────────────────────────────────────────────────
TOTAL_PUSHED=0
TOTAL_PULLED=0
TOTAL_CONFLICTS=0

# 清空本次冲突日志头
echo "# sync_shared_data conflicts — $(date '+%Y-%m-%d %H:%M:%S')" >> "$CONFLICT_LOG"

for SYNC_DIR in "${SYNC_DIRS[@]}"; do
    LOCAL_PATH="${REPO_ROOT}/${SYNC_DIR}/"
    REMOTE_PATH="${SERVER}:${REMOTE_DIR}/${SYNC_DIR}/"

    section "$SYNC_DIR"

    # 确保本地和远端目录存在
    mkdir -p "$LOCAL_PATH"
    ssh "$SERVER" "mkdir -p '${REMOTE_DIR}/${SYNC_DIR}'" 2>/dev/null || true

    if [ "$MODE" = "push" ]; then
        info "Pushing $SYNC_DIR → QCL"
        if $DRY_RUN; then
            dry "rsync -avz --dry-run '$LOCAL_PATH' '$REMOTE_PATH'"
            rsync -avz --dry-run "$LOCAL_PATH" "$REMOTE_PATH" | grep -v "^sending\|^sent\|^total" || true
        else
            COUNT=$(rsync -avz "$LOCAL_PATH" "$REMOTE_PATH" 2>&1 | grep -c "^[^/]*/$\|^[^/]*\." || true)
            TOTAL_PUSHED=$((TOTAL_PUSHED + COUNT))
        fi
        continue
    fi

    if [ "$MODE" = "pull" ]; then
        info "Pulling $SYNC_DIR ← QCL"
        if $DRY_RUN; then
            dry "rsync -avz --dry-run '$REMOTE_PATH' '$LOCAL_PATH'"
            rsync -avz --dry-run "$REMOTE_PATH" "$LOCAL_PATH" | grep -v "^receiving\|^sent\|^total" || true
        else
            COUNT=$(rsync -avz "$REMOTE_PATH" "$LOCAL_PATH" 2>&1 | grep -c "^[^/]*/$\|^[^/]*\." || true)
            TOTAL_PULLED=$((TOTAL_PULLED + COUNT))
        fi
        continue
    fi

    # ── both mode: first run seeds QCL from Mini (Mini = source of truth) ──
    if [ "$LAST_SYNC" -eq 0 ]; then
        info "First run — pushing all of $SYNC_DIR → QCL (Mini is source of truth)"
        if $DRY_RUN; then
            dry "rsync -avz --dry-run '$LOCAL_PATH' '$REMOTE_PATH'"
            rsync -avz --dry-run "$LOCAL_PATH" "$REMOTE_PATH" | grep -v "^sending\|^sent\|^total" || true
        else
            COUNT=$(rsync -avz "$LOCAL_PATH" "$REMOTE_PATH" 2>&1 | grep -c "^[^/]*/$\|^[^/]*\." || true)
            TOTAL_PUSHED=$((TOTAL_PUSHED + COUNT))
        fi
        continue
    fi

    # ── 双向模式：发现冲突后分类处理 ────────────────────────────────────────

    # 在 QCL 找出自 LAST_SYNC 以来被修改的文件
    QCL_CHANGED_RAW=$(ssh "$SERVER" "find '${REMOTE_DIR}/${SYNC_DIR}' -type f -newer /proc/self/fd/0 2>/dev/null" \
        <<< "$(python3 -c "import time; open('/tmp/_anchor','w').write('x'); import os; os.utime('/tmp/_anchor',(${LAST_SYNC},${LAST_SYNC}))" 2>/dev/null; cat /tmp/_anchor 2>/dev/null)" \
        2>/dev/null || \
        ssh "$SERVER" "find '${REMOTE_DIR}/${SYNC_DIR}' -type f -newer <(python3 -c \"import os; os.utime('/dev/stdin',(${LAST_SYNC},${LAST_SYNC}))\" 2>/dev/null) 2>/dev/null" 2>/dev/null || true)

    # Fallback: use find -newer with a temp file approach via SSH
    QCL_CHANGED=$(ssh "$SERVER" bash <<SSHEOF 2>/dev/null || true
python3 -c "import os; open('/tmp/_ssync_anchor','w').close(); os.utime('/tmp/_ssync_anchor',(${LAST_SYNC},${LAST_SYNC}))" 2>/dev/null
find "${REMOTE_DIR}/${SYNC_DIR}" -type f -newer /tmp/_ssync_anchor 2>/dev/null | \
    sed "s|^${REMOTE_DIR}/${SYNC_DIR}/||"
SSHEOF
)

    # 本地找出自 LAST_SYNC 以来被修改的文件
    ANCHOR_FILE="/tmp/_ssync_anchor_local_$$"
    python3 -c "import os; open('${ANCHOR_FILE}','w').close(); os.utime('${ANCHOR_FILE}',(${LAST_SYNC},${LAST_SYNC}))" 2>/dev/null || \
        touch -t "$(date -r $LAST_SYNC '+%Y%m%d%H%M.%S' 2>/dev/null || date -d "@$LAST_SYNC" '+%Y%m%d%H%M.%S' 2>/dev/null)" "$ANCHOR_FILE" 2>/dev/null || \
        touch "$ANCHOR_FILE"

    LOCAL_CHANGED=$(find "$LOCAL_PATH" -type f -newer "$ANCHOR_FILE" 2>/dev/null | \
        sed "s|^${LOCAL_PATH}||" || true)
    rm -f "$ANCHOR_FILE"

    # 分类
    MINI_SET=$(echo "$LOCAL_CHANGED"  | grep -v '^$' | sort || true)
    QCL_SET=$(echo "$QCL_CHANGED"     | grep -v '^$' | sort || true)

    CONFLICTS_LIST=$(comm -12 <(echo "$MINI_SET") <(echo "$QCL_SET") 2>/dev/null || true)
    PUSH_LIST=$(comm -23 <(echo "$MINI_SET") <(echo "$QCL_SET") 2>/dev/null || true)
    PULL_LIST=$(comm -13 <(echo "$MINI_SET") <(echo "$QCL_SET") 2>/dev/null || true)

    # Push Mini-only changes
    if [ -n "$PUSH_LIST" ]; then
        info "Pushing $(echo "$PUSH_LIST" | wc -l | tr -d ' ') file(s) → QCL"
        while IFS= read -r f; do
            [ -z "$f" ] && continue
            LOCAL_F="${LOCAL_PATH}${f}"
            REMOTE_F="${REMOTE_DIR}/${SYNC_DIR}/${f}"
            [ -f "$LOCAL_F" ] || continue
            if $DRY_RUN; then
                dry "push: $SYNC_DIR/$f"
            else
                ssh "$SERVER" "mkdir -p '$(dirname "$REMOTE_F")'" 2>/dev/null || true
                rsync -az "$LOCAL_F" "${SERVER}:${REMOTE_F}"
                TOTAL_PUSHED=$((TOTAL_PUSHED + 1))
            fi
        done <<< "$PUSH_LIST"
    fi

    # Pull QCL-only changes
    if [ -n "$PULL_LIST" ]; then
        info "Pulling $(echo "$PULL_LIST" | wc -l | tr -d ' ') file(s) ← QCL"
        while IFS= read -r f; do
            [ -z "$f" ] && continue
            LOCAL_F="${LOCAL_PATH}${f}"
            REMOTE_F="${REMOTE_DIR}/${SYNC_DIR}/${f}"
            if $DRY_RUN; then
                dry "pull: $SYNC_DIR/$f"
            else
                mkdir -p "$(dirname "$LOCAL_F")"
                rsync -az "${SERVER}:${REMOTE_F}" "$LOCAL_F"
                TOTAL_PULLED=$((TOTAL_PULLED + 1))
            fi
        done <<< "$PULL_LIST"
    fi

    # Handle conflicts
    if [ -n "$CONFLICTS_LIST" ]; then
        COUNT=$(echo "$CONFLICTS_LIST" | grep -c . || true)
        warn "$COUNT conflict(s) in $SYNC_DIR — saving .mine / .theirs"
        while IFS= read -r f; do
            [ -z "$f" ] && continue
            LOCAL_F="${LOCAL_PATH}${f}"
            REMOTE_F="${REMOTE_DIR}/${SYNC_DIR}/${f}"
            [ -f "$LOCAL_F" ] || continue
            if $DRY_RUN; then
                dry "CONFLICT: $SYNC_DIR/$f  (would save .mine/.theirs)"
            else
                cp "$LOCAL_F" "${LOCAL_F}.mine"
                rsync -az "${SERVER}:${REMOTE_F}" "${LOCAL_F}.theirs" 2>/dev/null || \
                    warn "Could not fetch .theirs for $f"
                echo "$(date '+%Y-%m-%d %H:%M:%S') CONFLICT  ${SYNC_DIR}/${f}" >> "$CONFLICT_LOG"
                TOTAL_CONFLICTS=$((TOTAL_CONFLICTS + 1))
            fi
        done <<< "$CONFLICTS_LIST"
        warn "Resolve manually: diff <file>.mine <file>.theirs"
        warn "Then: cp <file>.mine <file>  (or .theirs), delete both, re-sync"
    fi

    if [ -z "$CONFLICTS_LIST" ] && [ -z "$PUSH_LIST" ] && [ -z "$PULL_LIST" ]; then
        info "No changes in $SYNC_DIR"
    fi
done

# ─── 更新 last-sync 时间戳 ──────────────────────────────────────────────────
if ! $DRY_RUN; then
    date +%s > "$LAST_SYNC_FILE"
fi

# ─── 摘要 ────────────────────────────────────────────────────────────────────
section "Summary"
info "Pushed:     ${TOTAL_PUSHED} file(s)"
info "Pulled:     ${TOTAL_PULLED} file(s)"
[ "$TOTAL_CONFLICTS" -gt 0 ] && \
    warn "Conflicts:  ${TOTAL_CONFLICTS} file(s) — see ${CONFLICT_LOG}" || \
    info "Conflicts:  0"
$DRY_RUN && warn "Dry-run mode — no changes made"
info "Done"
