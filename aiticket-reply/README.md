# aiticket-reply

AITicket 智能分析与回复 — 独立 Claude Code skill

## 功能

| 模式 | 触发方式 | 说明 |
|------|----------|------|
| A. 工单智能回复 | 输入工单号（如 LCZX-61234） | 指定模块时使用模块感知回复，否则全库融合 |
| B. 知识库问答 | 自由提问 | 基于产品知识库语义检索回答 |
| C. 相似工单搜索 | 搜索 + 关键词 | 历史工单语义匹配 |
| D. 智能扩展 | 说「完善方案」 | 基于您修改的内容重跑搜索，给出精准扩展方案 |

触发词：`智能回复`、`分析工单`、`回复建议`、`帮我回复`、`知识库问答`、`相似工单`、`完善方案`、`深化方案`

## 依赖

- Python 3.9+
- `cryptography >= 41`（Fernet 机器绑定加密）

```bash
pip install cryptography
```

## 安装

```bash
cp -r aiticket-reply ~/.claude/skills/
```

## 首次配置

**第一步：配置后端地址**（管理员或首次使用者）

```bash
python3 ~/.claude/skills/aiticket-reply/scripts/setup_config.py --setup
```

输入后端地址（默认 `http://ticket.spux.cn`）。配置以 Fernet 机器绑定加密保存，无法跨机器复用。

**第二步：账号登录**（首次使用智能回复功能时，Claude 会自动引导您完成）

无需手动执行命令——在对话中告诉 Claude 您的 QCL 用户名和密码即可，Claude 会在后台完成登录并安全保存。

> 未登录状态下每天可免费使用 1 次。

## 在 Claude Code 中使用

安装后，直接在对话中使用触发词：

```
> 智能回复 LCZX-61234
> 帮我分析工单 LCZX-61234，流程中心模块
> 云盘账号怎么开通
> 搜索类似工单：薪酬发放异常
> 完善方案，重点关注「字段联动」「事件触发」
```

## 常用运维命令

```bash
# 验证当前登录状态
python3 ~/.claude/skills/aiticket-reply/scripts/setup_config.py --whoami

# 测试后端连通性（验证三个核心 API 端点）
python3 ~/.claude/skills/aiticket-reply/scripts/setup_config.py --test

# 机器迁移后重新加密配置
python3 ~/.claude/skills/aiticket-reply/scripts/setup_config.py --rotate-key
```

## 安全说明

- 账号凭证仅用于换取 device token，不以任何形式明文存储
- Device token 通过 Fernet 机器绑定加密保存，仅在当前机器有效
- `config/config.json` 已在 `.gitignore` 中排除，不会被提交
