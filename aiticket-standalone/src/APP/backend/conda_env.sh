#!/bin/bash
# conda_env.sh - Conda antigravity 环境快捷操作
# 用法: source ./conda_env.sh

CONDA_ENV_PATH="/Volumes/MacMini/opt/miniconda3/envs/antigravity"

if [ ! -d "$CONDA_ENV_PATH" ]; then
    echo "❌ Conda antigravity 环境不存在: $CONDA_ENV_PATH"
    return 1
fi

# 设置 PATH 优先使用 conda 环境
export PATH="$CONDA_ENV_PATH/bin:$PATH"

# 设置 Python 相关环境变量
export PYTHON="$CONDA_ENV_PATH/bin/python"
export PIP="$CONDA_ENV_PATH/bin/pip"
export UVICORN="$CONDA_ENV_PATH/bin/uvicorn"

# 验证
echo "✅ 已切换到 conda antigravity 环境"
echo "Python: $($PYTHON --version)"
echo "路径: $CONDA_ENV_PATH"
