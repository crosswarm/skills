#!/usr/bin/env bash
# product-dev-flow install script
# Usage: bash scripts/install.sh [--hook-only | --claude-md-only | --dry-run]
#
# Effects:
#   1. Copies this skill to ~/.claude/skills/product-dev-flow/
#   2. Appends activation rule to ~/.claude/CLAUDE.md  (deduped)
#   3. Adds PostToolUse:EnterPlanMode hook to ~/.claude/settings.json  (deduped)
#
# The hook injects a system-reminder into Claude's context every time plan mode
# is entered, ensuring the skill is invoked for non-trivial dev tasks.

set -euo pipefail

SKILL_NAME="product-dev-flow"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
SKILLS_DIR="$CLAUDE_DIR/skills"
CLAUDE_MD="$CLAUDE_DIR/CLAUDE.md"
SETTINGS_JSON="$CLAUDE_DIR/settings.json"

DRY_RUN=false
HOOK_ONLY=false
CLAUDE_MD_ONLY=false

for arg in "$@"; do
  case $arg in
    --dry-run)      DRY_RUN=true ;;
    --hook-only)    HOOK_ONLY=true ;;
    --claude-md-only) CLAUDE_MD_ONLY=true ;;
  esac
done

log()  { echo "  $*"; }
ok()   { echo "✓ $*"; }
skip() { echo "– $*  (already present)"; }
dry()  { echo "[dry-run] $*"; }

echo ""
echo "product-dev-flow installer"
echo "──────────────────────────"

# ── Step 1: Copy skill ─────────────────────────────────────────────────
if ! $HOOK_ONLY && ! $CLAUDE_MD_ONLY; then
  TARGET="$SKILLS_DIR/$SKILL_NAME"
  if $DRY_RUN; then
    dry "cp -r $SKILL_DIR → $TARGET"
  else
    mkdir -p "$SKILLS_DIR"
    cp -r "$SKILL_DIR" "$TARGET"
    ok "Skill copied to $TARGET"
  fi
fi

# ── Step 2: CLAUDE.md activation rule ─────────────────────────────────
CLAUDE_MD_LINE="进入 plan mode 处理非平凡开发任务（新功能 / 多文件改动 / 架构调整 / 重构 / 方案不确定）时，默认走 \`/product-dev-flow\` 范式：先自评复杂度 L0/L1/L2，再按 design→critique→implement→review→ship 编排。琐碎任务（单行/typo/问答）直接做，无需流程。"

if ! $HOOK_ONLY; then
  if $DRY_RUN; then
    dry "Append product-dev-flow activation rule to $CLAUDE_MD"
  else
    mkdir -p "$(dirname "$CLAUDE_MD")"
    touch "$CLAUDE_MD"
    if grep -qF "product-dev-flow" "$CLAUDE_MD" 2>/dev/null; then
      skip "CLAUDE.md already contains product-dev-flow rule"
    else
      printf '\n## product-dev-flow\n\n%s\n' "$CLAUDE_MD_LINE" >> "$CLAUDE_MD"
      ok "Activation rule appended to $CLAUDE_MD"
    fi
  fi
fi

# ── Step 3: settings.json PostToolUse hook ────────────────────────────
# The hook fires after EnterPlanMode and prints a reminder into Claude's
# context (stdout of hooks appears as system-reminder in the conversation).
HOOK_MARKER="product-dev-flow-hook"
HOOK_CMD="echo 'SKILL_REMINDER [$HOOK_MARKER]: Invoke /product-dev-flow BEFORE planning. Rate complexity: L0 (trivial→skip), L1 (multi-file/feature→design+critique), L2 (arch/risky→research+design+critique×3). Stop at both gate points (critique + ship).'"

if ! $CLAUDE_MD_ONLY; then
  if $DRY_RUN; then
    dry "Inject PostToolUse:EnterPlanMode hook into $SETTINGS_JSON"
  else
    # Use python3 to safely merge into existing JSON
    python3 - "$SETTINGS_JSON" "$HOOK_MARKER" "$HOOK_CMD" << 'PY'
import json, sys, os, shutil

settings_path, marker, hook_cmd = sys.argv[1], sys.argv[2], sys.argv[3]

# Load or init
if os.path.exists(settings_path):
    with open(settings_path) as f:
        cfg = json.load(f)
else:
    cfg = {}

# Check if already present
hooks = cfg.setdefault("hooks", {})
post_hooks = hooks.setdefault("PostToolUse", [])

for entry in post_hooks:
    for h in entry.get("hooks", []):
        if marker in h.get("command", ""):
            print(f"– settings.json hook already present  (already present)")
            sys.exit(0)

# Add new hook entry
new_entry = {
    "matcher": "EnterPlanMode",
    "hooks": [{"type": "command", "command": hook_cmd}]
}
post_hooks.append(new_entry)

# Backup and write
if os.path.exists(settings_path):
    shutil.copy(settings_path, settings_path + ".bak")
with open(settings_path, "w") as f:
    json.dump(cfg, f, indent=4, ensure_ascii=False)
print(f"✓ PostToolUse:EnterPlanMode hook added to {settings_path}")
PY
  fi
fi

echo ""
echo "Installation complete."
echo ""
echo "How the enforcement works:"
echo "  • ~/.claude/CLAUDE.md  → global instruction loaded into every conversation"
echo "  • settings.json hook   → system-reminder injected each time plan mode is entered"
echo "  • Both mechanisms reinforce each other; either alone is usually sufficient."
echo ""
echo "To verify: open Claude Code, type a dev task, enter plan mode — you should see"
echo "  a system-reminder citing product-dev-flow."
echo ""
