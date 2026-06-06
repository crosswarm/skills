#!/bin/bash
# git_auto_deploy.sh - 服务器端Git自动部署脚本
# 在服务器上执行一次配置

set -e

DEPLOY_DIR="/opt/ai-ticket"
GIT_REPO="${DEPLOY_DIR}/repo.git"
HOOK_SCRIPT="${GIT_REPO}/hooks/post-receive"

echo "========================================"
echo "🚀 配置Git自动部署"
echo "========================================"

# 1. 创建裸仓库
sudo mkdir -p ${GIT_REPO}
cd ${GIT_REPO}
sudo git init --bare
sudo chown -R deploy:deploy ${GIT_REPO}

# 2. 创建post-receive钩子
echo "创建部署钩子..."
sudo tee ${HOOK_SCRIPT} << 'HOOK_EOF'
#!/bin/bash
# Git post-receive hook - 自动部署

echo "========================================"
echo "🚀 收到代码推送，开始自动部署..."
echo "========================================"

DEPLOY_DIR="/opt/ai-ticket"
GIT_WORK_TREE=${DEPLOY_DIR}
GIT_DIR="/opt/ai-ticket/repo.git"

# 检出代码
echo "[1/5] 检出代码..."
git --work-tree=${GIT_WORK_TREE} --git-dir=${GIT_DIR} checkout -f

# 安装依赖
echo "[2/5] 安装依赖..."
cd ${DEPLOY_DIR}/APP/backend
source .venv/bin/activate
pip install -r requirements.txt -q

# 数据迁移（如有需要）
echo "[3/5] 检查数据迁移..."
if [ conclusion/index.md -nt .last_data_load 2>/dev/null ]; then
    echo "检测到数据更新，重新加载向量库..."
    python -c "from search_chroma import SemanticSearchEngine; se = SemanticSearchEngine(); se.reload_data()" || true
    touch .last_data_load
fi

# 重启服务
echo "[4/5] 重启服务..."
sudo supervisorctl restart ai-ticket

# 验证
echo "[5/5] 验证服务..."
sleep 2
QCL_BACKEND_PORT="${QCL_BACKEND_PORT:-18000}"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:${QCL_BACKEND_PORT}/api/board/stats)

if [ "$STATUS" = "200" ]; then
    echo "✅ 部署成功！"
else
    echo "⚠️ 服务状态异常 (HTTP ${STATUS})"
fi

echo "========================================"
echo "部署完成时间: $(date)"
echo "========================================"
HOOK_EOF

sudo chmod +x ${HOOK_SCRIPT}
sudo chown deploy:deploy ${HOOK_SCRIPT}

# 3. 允许deploy用户使用sudo重启服务（无需密码）
echo "配置sudo权限..."
sudo tee /etc/sudoers.d/deploy-supervisor << 'EOF'
deploy ALL=(ALL) NOPASSWD: /usr/bin/supervisorctl restart ai-ticket
EOF
sudo chmod 440 /etc/sudoers.d/deploy-supervisor

echo "========================================"
echo "✅ Git自动部署配置完成！"
echo ""
echo "本地配置步骤:"
echo "1. 添加远程仓库:"
echo "   git remote add deploy ubuntu@154.8.231.122:/opt/ai-ticket/repo.git"
echo ""
echo "2. 推送代码自动部署:"
echo "   git push deploy main"
echo ""
echo "3. 或使用一键部署脚本:"
echo "   ./APP/deploy_scripts/deploy.sh \"更新描述\""
echo "========================================"
