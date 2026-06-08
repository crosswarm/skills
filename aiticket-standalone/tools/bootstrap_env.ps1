# aiticket 一键环境引导（Windows PowerShell）
# 自动检测并安装：git + uv + Python 3.12，然后运行安装器。全程显示进度。
# 用法：powershell -ExecutionPolicy Bypass -File tools\bootstrap_env.ps1 [install.py 的参数…]
$ErrorActionPreference = "Stop"
function Step($m){ Write-Host "`n▶ $m" -ForegroundColor Cyan }
function Ok($m){   Write-Host "  ✓ $m" -ForegroundColor Green }
function Warn($m){ Write-Host "  ⚠ $m" -ForegroundColor Yellow }
$DIR = Split-Path -Parent $MyInvocation.MyCommand.Path

Step "[1/4] 检测系统环境"
Ok "操作系统：Windows"

# ---- git ----
if (Get-Command git -ErrorAction SilentlyContinue) {
  Ok "git 已就绪（$(git --version)）"
} else {
  Step "[2/4] 安装 git（缺失，winget 自动安装中…）"
  winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
  $env:Path = "$env:ProgramFiles\Git\cmd;$env:Path"
  if (Get-Command git -ErrorAction SilentlyContinue) { Ok "git 安装完成" } else { Warn "git 仍不可用（仅 --src 本地安装可不需要 git）" }
}

# ---- uv ----
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
if (Get-Command uv -ErrorAction SilentlyContinue) {
  Ok "uv 已就绪（$(uv --version)）"
} else {
  Step "[3/4] 安装 uv（Python 环境管理器，含 Python 下载；显示进度）"
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Warn "uv 安装失败，请检查网络后重试"; exit 1 }
  Ok "uv 安装完成"
}

# ---- Python 3.12（uv 托管，带下载进度）----
Step "[4/4] 准备 Python 3.12（uv 下载托管版，显示进度）"
uv python install 3.12
$PYBIN = (uv python find 3.12)
if (-not $PYBIN) { Warn "未找到 uv 的 Python 3.12"; exit 1 }
Ok "Python 就绪：$PYBIN"

Step "环境就绪，开始运行 aiticket 安装器（继续显示各步骤进度）"
& $PYBIN "$DIR\install.py" @args
