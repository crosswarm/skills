#!/bin/bash
# install_https.sh - HTTPS证书安装脚本
# 在服务器上执行: sudo bash install_https.sh your-domain.com

DOMAIN="${1:-}"

if [ -z "$DOMAIN" ]; then
    echo "用法: sudo bash install_https.sh your-domain.com"
    exit 1
fi

echo "========================================"
echo "为 ${DOMAIN} 安装HTTPS证书"
echo "========================================"

# 安装Certbot
apt install -y certbot python3-certbot-nginx

# 申请证书
certbot --nginx -d ${DOMAIN} --agree-tos --non-interactive --email admin@${DOMAIN}

# 测试自动续期
echo "测试证书自动续期..."
certbot renew --dry-run

# 添加定时任务（每天检查续期）
(crontab -l 2>/dev/null | grep -v certbot; echo "0 3 * * * /usr/bin/certbot renew --quiet") | crontab -

echo "========================================"
echo "✅ HTTPS配置完成！"
echo "访问地址: https://${DOMAIN}/"
echo "========================================"
