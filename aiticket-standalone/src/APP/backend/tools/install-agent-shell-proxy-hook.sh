#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SOURCE_HOOK="$ROOT_DIR/APP/backend/tools/agent-shell-proxy-hook.zsh"
INSTALL_DIR="${HOME}/.codex/shell_hooks"
TARGET_HOOK="${INSTALL_DIR}/agent-shell-proxy-hook.zsh"
ZSHRC_PATH="${HOME}/.zshrc"
SOURCE_LINE='source "$HOME/.codex/shell_hooks/agent-shell-proxy-hook.zsh"'

mkdir -p "$INSTALL_DIR"
cp "$SOURCE_HOOK" "$TARGET_HOOK"
chmod 644 "$TARGET_HOOK"

if ! grep -Fq "$SOURCE_LINE" "$ZSHRC_PATH"; then
  {
    printf '\n# Shared agent localhost proxy hook\n'
    printf '%s\n' "$SOURCE_LINE"
  } >> "$ZSHRC_PATH"
fi

printf 'Installed hook to %s\n' "$TARGET_HOOK"
printf 'Updated %s\n' "$ZSHRC_PATH"
