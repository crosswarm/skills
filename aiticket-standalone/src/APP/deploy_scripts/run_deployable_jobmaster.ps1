# deployable 专用启动器（Windows PowerShell）
# 自动注入 AITICKET_DEPLOYABLE=1，跳过周报调度，仅跑月报及其他后台任务
# 用法: pwsh -File run_deployable_jobmaster.ps1
#       也可由 Windows Task Scheduler / NSSM 调用

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)

# Python 解释器优先级: PYTHON_BIN 环境变量 → conda env → 系统 python
$PythonBin = if ($env:PYTHON_BIN) {
    $env:PYTHON_BIN
} elseif ($env:CONDA_ENV_PATH) {
    Join-Path $env:CONDA_ENV_PATH "python.exe"
} else {
    "python"
}

if (-not (Get-Command $PythonBin -ErrorAction SilentlyContinue)) {
    Write-Error "找不到 Python 解释器，请设置 PYTHON_BIN 环境变量"
    exit 1
}

$LogDir = Join-Path $ProjectRoot "APP\backend\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Set-Location (Join-Path $ProjectRoot "APP\backend")

$env:PYTHONUNBUFFERED    = "1"
$env:RUN_BACKGROUND_JOBS = "1"
$env:JIRA_SKIP_COOKIES   = if ($env:JIRA_SKIP_COOKIES) { $env:JIRA_SKIP_COOKIES } else { "true" }
$env:AITICKET_DEPLOYABLE = "1"   # 核心门控：跳过 weekly_report 调度

# 清除代理
Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:HTTP_PROXY  -ErrorAction SilentlyContinue
$env:NO_PROXY = "localhost,127.0.0.1,0.0.0.0,::1"

& $PythonBin -m scripts.local_jobmaster_daemon
