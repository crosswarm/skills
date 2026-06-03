---
name: requirement-revision-guard
description: 安装并执行一套可复用的需求变更记录治理机制。适用于任何 AI Agent 产品或工程项目：当用户要求每次新增需求、变更需求、新建需求文档、修改需求文档时都必须在 docs/spec/revision 下生成记录；当需要为 PRD、规格说明、设计文档、验收标准、限制条件建立可追溯变更日志；当需要在 AGENTS.md、CLAUDE.md、GEMINI.md、Cursor 规则、Windsurf 规则或其他 Agent 入口中加入强制规则；当需要用脚本阻止“需求文档已变更但没有对应变更记录”的情况。
---

# 需求变更记录守门

## 核心规则

把任何新需求、需求变更、需求澄清、验收标准变化、限制条件变化、需求文档编辑，都视为必须可追溯的事件。任务结束前，目标项目必须新增一份 `docs/spec/revision/YYYYMMDD-HHMMSS-变更标题.md` 记录，并写清：

- `变更内容`
- `变更原因`
- `已完成任务`
- `未完成后续计划`

如果没有后续任务，必须在“未完成后续计划”中写明“暂无”以及原因。正式记录中不得保留 `待补充`、`请补充`、`TODO`、`TBD` 等占位内容。

## 适配范围

这是通用 AI Agent 工作流，不绑定某一个产品。安装后应尽量让多个 Agent 入口同时生效，包括但不限于：

- Codex / OpenAI Agents：`AGENTS.md`
- Claude Code：`CLAUDE.md`
- Gemini CLI：`GEMINI.md`
- Cursor：`.cursor/rules/requirement-revision-guard.mdc`
- Windsurf：`.windsurfrules`
- 其他 Agent：把同一段规则复制到该产品会自动读取的项目级规则文件中

机器可识别的 skill 目录名仍为 `requirement-revision-guard`，这是跨产品分发时的稳定标识；面向人的说明必须使用中文。

## 安装流程

当用户要求把机制安装到某个项目、复用到其他项目、或强制记录需求变更时，按以下流程执行：

1. 检查目标项目是否已有 Agent 规则文件、`package.json`、`docs/spec`、`docs/design` 或其他需求文档目录。
2. 从目标项目根目录运行本 skill 内置安装器：

```bash
node "$CODEX_HOME/skills/requirement-revision-guard/scripts/install_revision_guard.js" --project .
```

3. 如果项目的需求文档目录不是默认路径，显式传入目录：

```bash
node "$CODEX_HOME/skills/requirement-revision-guard/scripts/install_revision_guard.js" --project . --requirement-dirs docs/product,docs/requirements,docs/design --exclude-dirs docs/design/assets
```

4. 如需控制写入哪些 Agent 规则入口，显式传入文件列表：

```bash
node "$CODEX_HOME/skills/requirement-revision-guard/scripts/install_revision_guard.js" --project . --agent-files AGENTS.md,CLAUDE.md,GEMINI.md,.cursor/rules/requirement-revision-guard.mdc,.windsurfrules
```

5. 检查生成或更新的文件，只在目标项目有更强本地规范时调整措辞。
6. 有 `package.json` 时运行 `npm run revision:check`；没有 `package.json` 时运行 `node scripts/revision-guard.js --check`。
7. 在最终回复中说明安装文件、适配的 Agent 入口和验证结果。

安装器可重复运行，会更新受控规则块，创建 `docs/spec/revision/README.md`、`_TEMPLATE.md`、`scripts/revision-guard.js`，并在存在 `package.json` 时加入 npm 命令。

## 日常使用流程

当当前任务引入或改变需求时，按以下流程执行：

1. 先创建变更记录草稿：

```bash
npm run revision:new -- --title "变更标题" --reason "变更原因"
```

2. 修改相关需求文档、规格文档、设计文档或实现代码。
3. 回填变更记录，写清变更内容、变更原因、已完成任务、后续计划和验证结果。
4. 最终回复前运行 `npm run revision:check`。
5. 如果任务包含暂存或提交，还要运行 `npm run revision:check:staged`。
6. 最终回复必须说明新增的变更记录路径和检查结果。

## 守门脚本行为

生成的守门脚本默认检查工作区，传入 `--staged` 时检查暂存区。它会把配置的需求目录下发生变化的 Markdown 文件视为需求文档变更，同时排除 `docs/spec/revision` 和配置的资产目录。

脚本在以下情况失败：

- 需求文档已变更，但没有新增正式变更记录。
- 正式变更记录被删除。
- 变更记录文件名不符合 `YYYYMMDD-HHMMSS-变更标题.md`。
- 必填章节缺失或为空。
- 正式变更记录仍包含占位内容。

## 适配建议

- 把通用规则写入多个 Agent 入口；不同产品只要读取其中一个入口，就能触发同一套约束。
- 目标项目有 CI 或 Git hook 时，优先把 `npm run revision:check:staged` 接入提交或合并检查。
- 目标项目不是 npm 项目时，保留 `scripts/revision-guard.js`，直接用 Node 执行。
- 目标 Agent 不支持 skill 格式时，仍可复制本目录并直接运行安装器；安装后的项目规则和守门脚本不依赖特定 Agent 产品。
