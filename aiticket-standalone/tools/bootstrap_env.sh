#!/usr/bin/env bash
# aiticket 一键环境引导（macOS / Linux）
# 自动检测并安装：git + uv + Python 3.12，然后运行安装器。全程显示进度。
# 用法：bash tools/bootstrap_env.sh [install.py 的参数…]
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
step(){ printf "\n\033[1;36m▶ %s\033[0m\n" "$1"; }
ok(){   printf "  \033[32m✓ %s\033[0m\n" "$1"; }
warn(){ printf "  \033[33m⚠ %s\033[0m\n" "$1"; }

step "[1/4] 检测系统环境"
OS="$(uname -s)"
ok "操作系统：$OS"

# ---- git ----
if command -v git >/dev/null 2>&1; then
  ok "git 已就绪（$(git --version 2>/dev/null)）"
else
  step "[2/4] 安装 git（缺失，自动安装中…）"
  if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then brew install git
    else warn "未装 Homebrew，触发 Xcode 命令行工具安装（弹窗里点『安装』）"; xcode-select --install || true; fi
  elif command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y git
  elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y git
  elif command -v yum >/dev/null 2>&1; then sudo yum install -y git
  elif command -v pacman >/dev/null 2>&1; then sudo pacman -S --noconfirm git
  elif command -v zypper >/dev/null 2>&1; then sudo zypper install -y git
  else warn "无法自动安装 git，请手动安装后重试"; fi
  command -v git >/dev/null 2>&1 && ok "git 安装完成" || warn "git 仍不可用（可继续，仅 --src 本地安装不需要 git）"
fi

# ---- uv（统一管理 Python 与依赖，免去系统 Python 依赖）----
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if command -v uv >/dev/null 2>&1; then
  ok "uv 已就绪（$(uv --version 2>/dev/null)）"
else
  step "[3/4] 安装 uv（Python 环境管理器，含 Python 下载；显示进度）"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 && ok "uv 安装完成" || { warn "uv 安装失败，请检查网络后重试"; exit 1; }
fi

# ---- Python 3.12（uv 托管，带下载进度）----
step "[4/4] 准备 Python 3.12（uv 下载托管版，显示进度）"
uv python install 3.12
PYBIN="$(uv python find 3.12 2>/dev/null || true)"
if [ -z "$PYBIN" ]; then warn "未找到 uv 的 Python 3.12"; exit 1; fi
ok "Python 就绪：$PYBIN"

step "环境就绪，开始运行 aiticket 安装器（继续显示各步骤进度）"
exec "$PYBIN" "$DIR/install.py" "$@"
