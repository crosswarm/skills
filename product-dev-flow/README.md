# product-dev-flow

> Claude Code skill — 产品开发工作流范式

一个面向 Claude Code 的结构化开发 SOP，覆盖从需求到上线的完整流程。自动按任务复杂度（L0/L1/L2）路由不同的 agent 编排策略，内置人机闸口、TDD/SDD 纪律和 UX 子工作流。

## 特性

- **三档复杂度**：L0 简单直做 / L1 设计+评审 / L2 调研+多轮对抗
- **六阶段 SOP**：research → design → critique → implement → review → ship
- **两个人机闸口**：critique 后 + ship 前，必停等用户确认
- **UX 子流程**：界面类任务自动触发 UXDever 设计 + UXMaster 评审
- **TDD + SDD + planning-with-files**：implement 阶段强制执行

## 安装

使用内置安装脚本（推荐）：

```bash
git clone https://github.com/crosswarm/skills.git /tmp/crosswarm-skills
bash /tmp/crosswarm-skills/product-dev-flow/scripts/install.sh
```

安装脚本会自动完成三件事：
1. 将 skill 复制到 `~/.claude/skills/product-dev-flow/`
2. 在 `~/.claude/CLAUDE.md` 追加激活规则（全局对话生效）
3. 在 `~/.claude/settings.json` 注入 `PostToolUse:EnterPlanMode` hook

> **Plan mode 强制触发机制**：hook 在每次进入 plan mode 时向 Claude 注入一条 `system-reminder`，提示检查复杂度分级并调用本 skill。CLAUDE.md 和 hook 双重保险，任一即可生效。

也可单独操作：
```bash
bash install.sh --claude-md-only   # 只改 CLAUDE.md
bash install.sh --hook-only        # 只改 settings.json
bash install.sh --dry-run          # 预览，不实际修改
```

## Prerequisites

详见 `SKILL.md` 中的 `## Prerequisites` 节。核心依赖：

| 依赖 | 类型 | 安装方式 |
|------|------|---------|
| `oh-my-claudecode` | Claude Code 插件 | `npm install -g oh-my-claudecode` |
| `grill-me` | skill（必需） | 来自 [crosswarm/skills](https://github.com/crosswarm/skills) 或 superpowers |
| `planning-with-files` | skill（必需） | 同上 |
| `agent-reach` | skill（L2 可选） | 同上 |
| `ralph-loop` | skill（长任务可选） | 同上 |

## 使用

在 Claude Code 中输入 `/product-dev-flow` 或在 CLAUDE.md 中配置自动触发：

```markdown
进入 plan mode 处理非平凡开发任务时，默认走 `/product-dev-flow` 范式
```

## License

MIT
