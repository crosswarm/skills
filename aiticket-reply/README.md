# aiticket-reply

AITicket 智能分析与回复 — 独立 Claude Code skill

## 功能

| 模式 | 触发方式 | 端点 |
|------|----------|------|
| A. 工单智能回复 | 输入工单号（如 LCZX-61234） | `/api/reply/generate-by-module`（指定模块）/ `/api/board/generate-reply`（默认） |
| B. 知识库问答 | 自由提问 | `/api/kb/qa` |
| C. 相似工单搜索 | 搜索 + 关键词 | `/api/board/search` |

触发词：`智能回复`、`分析工单`、`回复建议`、`帮我回复`、`知识库问答`、`相似工单`

## 依赖

- Python 3.9+
- `cryptography >= 41`（Fernet 加密，推荐安装）

```bash
pip install cryptography
```

## 安装

```bash
# 拷贝到用户全局 skills 目录
cp -r aiticket-reply ~/.claude/skills/

# 或软链（方便同步更新）
ln -s /Users/cfone/Studio/aiticket/.agent/skills/aiticket-reply ~/.claude/skills/aiticket-reply
```

## 首次配置

```bash
python3 ~/.claude/skills/aiticket-reply/scripts/setup_config.py --setup
```

输入后端地址（默认 `http://ticket.spux.cn`）和默认项目（默认 `LCZX`）。配置以 Fernet 机器绑定加密保存，无法跨机器复用。

## 常用命令

```bash
# 测试连接（验证三个核心 API 端点）
python3 ~/.claude/skills/aiticket-reply/scripts/setup_config.py --test

# 查看当前配置的后端地址
python3 ~/.claude/skills/aiticket-reply/scripts/setup_config.py --get-url

# 机器迁移后重新加密
python3 ~/.claude/skills/aiticket-reply/scripts/setup_config.py --rotate-key
```

## 在 Claude Code 中使用

安装后，直接在对话中使用触发词：

```
> 智能回复 LCZX-61234
> 帮我分析工单 LCZX-61234，流程中心模块
> 云盘账号怎么开通
> 搜索类似工单：薪酬发放异常
```

## 发布到 crosswarm/skills

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/cross -o IdentitiesOnly=yes" \
  git clone git@github.com:crosswarm/skills.git /tmp/crosswarm-skills

mkdir -p /tmp/crosswarm-skills/aiticket-reply
rsync -av --exclude 'config/config.json' --exclude '__pycache__' \
  /Users/cfone/Studio/aiticket/.agent/skills/aiticket-reply/ \
  /tmp/crosswarm-skills/aiticket-reply/

cd /tmp/crosswarm-skills
git config user.name "cross" && git config user.email "crosswarm@gmail.com"
git add aiticket-reply/
git commit -m "feat(aiticket-reply): v1.0.0"
GIT_SSH_COMMAND="ssh -i ~/.ssh/cross -o IdentitiesOnly=yes" git push origin main
rm -rf /tmp/crosswarm-skills
```
