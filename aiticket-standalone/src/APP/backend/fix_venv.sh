#!/bin/bash
# fix_venv.sh - 修复虚拟环境
set -e

echo "🔄 修复虚拟环境..."
cd "$(dirname "$0")"

# 备份旧虚拟环境
if [ -d ".venv" ]; then
    echo "📦 备份旧虚拟环境..."
    mv .venv .venv.backup.$(date +%Y%m%d_%H%M%S)
fi

# 创建新虚拟环境（使用系统 Python3）
echo "🐍 创建新虚拟环境..."
python3 -m venv .venv

# 激活并安装依赖
echo "📥 安装依赖..."
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "✅ 虚拟环境修复完成！"
echo ""
echo "启动命令:"
echo "  source .venv/bin/activate && python main.py"
