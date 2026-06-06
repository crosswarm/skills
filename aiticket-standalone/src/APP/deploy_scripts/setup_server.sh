#!/bin/bash
# setup_server.sh - 服务器环境初始化脚本
# 在服务器上执行: bash setup_server.sh

set -e

echo "========================================"
echo "AI工单系统 - 服务器环境初始化"
echo "========================================"

# 检查root权限
if [ "$EUID" -ne 0 ]; then 
    echo "请使用root权限运行: sudo bash setup_server.sh"
    exit 1
fi

echo "[1/7] 更新系统软件包..."
apt update && apt upgrade -y

echo "[2/7] 安装基础软件..."
apt install -y \
    python3 python3-pip python3-venv \
    git wget curl vim \
    nginx \
    supervisor \
    ufw \
    fail2ban \
    htop iotop

echo "[3/7] 检查Python版本..."
python3 --version

echo "[4/7] 创建部署用户..."
if ! id "deploy" &>/dev/null; then
    adduser --disabled-password --gecos "" deploy
    usermod -aG sudo deploy
    echo "用户 deploy 创建成功"
else
    echo "用户 deploy 已存在"
fi

echo "[5/7] 配置防火墙..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "[6/7] 配置Fail2Ban..."
tee /etc/fail2ban/jail.local > /dev/null << 'EOF'
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 3600
EOF

systemctl restart fail2ban
systemctl enable fail2ban

echo "[7/7] 创建项目目录..."
mkdir -p /opt/ai-ticket
chown deploy:deploy /opt/ai-ticket
mkdir -p /var/log/supervisor
chown deploy:deploy /var/log/supervisor

echo "========================================"
echo "✅ 服务器环境初始化完成！"
echo ""
echo "下一步："
echo "1. 本地执行部署脚本同步代码"
echo "2. 配置Nginx（如有域名）"
echo "========================================"
