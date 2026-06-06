# 快速部署参考

## 一、日常更新（代码变更）

```bash
# 方式1：简单推送（无数据变更）
./APP/deploy_scripts/deploy.sh "更新描述"

# 方式2：代码+数据同步（向量数据有更新时）
./APP/deploy_scripts/deploy.sh "更新描述" --with-data
```

## 二、Jira数据同步（需要VPN）

```bash
# 仅拉取数据到本地（需要连接VPN）
./APP/deploy_scripts/sync_jira_data.sh

# 拉取并推送到服务器
./APP/deploy_scripts/sync_jira_data.sh --push

# 仅推送现有数据（无需VPN）
./APP/deploy_scripts/sync_jira_data.sh --push-only
```

## 三、向量数据同步

```bash
# 同步chroma_db向量数据
./APP/deploy_scripts/sync_data.sh

# 完整同步（包含conclusion数据）
./APP/deploy_scripts/sync_data.sh --full
```

## 四、服务器运维

```bash
# 查看服务状态
./APP/deploy_scripts/server_utils.sh status

# 查看日志
./APP/deploy_scripts/server_utils.sh logs
./APP/deploy_scripts/server_utils.sh logs err    # 错误日志

# 重启服务
./APP/deploy_scripts/server_utils.sh restart

# 健康检查
./APP/deploy_scripts/server_utils.sh health

# 备份数据
./APP/deploy_scripts/server_utils.sh backup

# 检查端口
./APP/deploy_scripts/server_utils.sh ports

# 系统信息
./APP/deploy_scripts/server_utils.sh info
```

## 四、直接SSH操作

```bash
# 登录服务器
ssh qcl

# 查看服务状态
sudo supervisorctl status ai-ticket

# 重启服务
sudo supervisorctl restart ai-ticket

# 查看日志
tail -100 /var/log/supervisor/ai-ticket.out.log
tail -100 /var/log/supervisor/ai-ticket.err.log

# 测试API
curl http://localhost:18000/api/board/stats
```

## 五、访问地址

- 首页（智能看板）：http://154.8.231.122/
- 智能分析：http://154.8.231.122/search.html
- 周报：http://154.8.231.122/report.html
- 需求规划：http://154.8.231.122/requirements.html
- API状态：http://154.8.231.122/api/board/stats

## 六、故障排查

### 服务无法启动
```bash
# 1. 检查端口占用
./APP/deploy_scripts/server_utils.sh ports

# 2. 查看错误日志
./APP/deploy_scripts/server_utils.sh logs err

# 3. 检查向量数据
ssh qcl "ls -la /opt/ai-ticket/APP/backend/chroma_db/"
```

### API返回异常
```bash
# 1. 健康检查
./APP/deploy_scripts/server_utils.sh health

# 2. 重启服务
./APP/deploy_scripts/server_utils.sh restart
```

### 向量数据丢失
```bash
# 重新同步数据
./APP/deploy_scripts/sync_data.sh
```
