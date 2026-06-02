---
name: product-dev-flow
description: >
  产品开发工作流范式 — 按任务复杂度（L0/L1/L2）自动路由六阶段 SOP（research→design→critique→implement→review→ship），
  内置人机闸口、TDD+SDD+planning-with-files 纪律和 UX 子工作流（UXDever+UXMaster）。
  触发场景：新功能开发 / 多文件改动 / 架构调整 / 重构 / 方案不确定 / plan mode 下的非平凡任务。
version: 1.0.0
author: crosswarm
license: MIT
---

# Product-Dev-Flow · 产品开发工作流范式

> 你（主 Claude 对话主循环）是**唯一编排器**。本 SOP 指导你按用户习惯的流程驱动一次开发。
> 编排对象 = omc subagent（architect/critic/executor）+ skill（grill-me / agent-reach / ralph-loop）+ review 能力（quality_gate 或降级 checklist）。

---

## Prerequisites

在使用本 skill 前，请确认以下依赖已就位：

### 必需

| 依赖 | 说明 | 安装 |
|------|------|------|
| **`oh-my-claudecode`** (omc) | 提供 `architect / critic / executor` subagent 类型 | `npm install -g oh-my-claudecode`，然后在 Claude Code 中启用插件 |
| **`grill-me`** skill | 对抗式设计评审，critique 阶段使用 | 放入 `~/.claude/skills/grill-me/`（来自 [crosswarm/skills](https://github.com/crosswarm/skills) 或 superpowers） |
| **`planning-with-files`** skill | 中间产物落文件的规范 | 同上 |

### 可选（按需）

| 依赖 | 使用场景 | 安装 |
|------|---------|------|
| `agent-reach` skill | L2 任务 research 阶段（联网调研） | 放入 `~/.claude/skills/agent-reach/` |
| `ralph-loop` skill | 长单线 implement 任务（迭代到全绿） | 放入 `~/.claude/skills/ralph-loop/` |
| `code-review` skill | implement 后代码 review | 放入 `~/.claude/skills/code-review/` |
| `design-review` skill | UX 实现后视觉验收 | 放入 `~/.claude/skills/design-review/` |
| `ui-ux-pro-max` skill | UX 子流程中 UXDever 角色 | 放入 `~/.claude/skills/ui-ux-pro-max/` |
| `design-taste-frontend` / `gpt-taste` skill | UXMaster taste 门禁 | 放入对应目录 |
| `saphire` Python 包 | 文档类产出质量门禁（私有包） | 如无法安装，见下方「saphire 降级方案」 |

### 一键安装脚本（可选依赖的快速方式）

```bash
# 克隆 crosswarm/skills 并批量复制所需 skill
git clone https://github.com/crosswarm/skills.git /tmp/cw-skills
for skill in grill-me planning-with-files agent-reach ralph-loop code-review; do
  [ -d "/tmp/cw-skills/$skill" ] && cp -r "/tmp/cw-skills/$skill" ~/.claude/skills/
done
echo "Skills installed to ~/.claude/skills/"
```

---

## 第 0 步（必做）：复杂度自评 + 声明

读完用户需求后，**先自评复杂度**，再用一句话向用户声明走哪几阶段、并说明"可改档"：

| 级别 | 判据 | 走哪些阶段 |
|------|------|-----------|
| **L0 简单** | 单文件 / typo / 明确小任务 / 改配置 | 直接计划 → implement → review（跳过 research/design/critique）|
| **L1 中等** | 多文件 / 新功能 / 需设计 / 一般重构 | design → critique×1 → implement → review |
| **L2 复杂** | 架构级 / 跨系统 / 方案不确定 / 高风险 | research → design+grill-me → critique×3 → 并行 implement → review |

声明示例：「判为 **L1**（多文件新功能），走 design→critique→implement→review。如需调研升 L2、或简化降 L0，说一声即可。」
→ **分级非硬路由，仲裁权交用户**（自我判断 + 用户确认）。

开一个 `run_id`（如 `pdf-<任务短名>`），每阶段起止记 trace（见末尾「Trace 记录」）。

---

## 六阶段 SOP

### ① research（仅 L2）

- **派**：`agent-reach` skill（Exa/GitHub/Jina 调研最佳实践、现有方案、踩坑）
- **产物**：情报落 `conclusion/temp/<run_id>/research.md`
- 简单/中等任务跳过此阶段。
- 若未安装 `agent-reach`：用 Claude 内置的 WebSearch/WebFetch 直接调研，产物格式相同。

### ② design（L1+）

- **派**：`Task(subagent_type="oh-my-claudecode:architect")`，prompt 里要求它**携带 grill-me 对抗心态**深化设计
- **输入**：需求 + research.md（若有）+ 相关代码现状
- **产物**：`conclusion/temp/<run_id>/design.md`（方案 + 文件清单 + 风险）
- **planning-with-files**：设计落文件，不只在对话里

### ② UX 子流程（界面类任务，在 design 内触发）

**触发判据**：任务产物含以下任一 → UI 页面 / 前端组件 / 原型 / 视觉稿 / 交互设计 / 界面改版。
纯后端 / CLI / 数据管道 → 跳过本子流程。

1. **UXDever 设计**：spawn `Task(subagent_type="oh-my-claudecode:executor")`，prompt 明确要求角色扮演 UXDever，携带 `ui-ux-pro-max` + `teach-impeccable` skill（如已安装）。产出高保真界面设计 / 原型 / UI spec，落 `conclusion/temp/<run_id>/ui-spec.md`

2. **UXMaster 评审**（design 完成后、进入 critique 前）：spawn `Task(subagent_type="oh-my-claudecode:critic")`，prompt 要求角色扮演 UXMaster，携带 `grill-me` + `design-taste-frontend`（或 `gpt-taste`，如已安装）。执行高标准设计指导 + taste 门禁评审：
   - 视觉一致性（间距/颜色/字阶）
   - 层次与节奏
   - AI-slop 清理（过度对称、空洞装饰）
   - 品牌对齐
   - 评审不达标 → 回 UXDever 重做，最多 2 轮

---

### ③ critique（L1+）→ 🚪闸口

- **派**：`Task(subagent_type="oh-my-claudecode:critic")`，携 grill-me，多轮鞭打设计（L1×1 轮 / L2×3 轮）
- 复杂方案可同时派 architect 二次深化，与 critic 对抗
- **产物**：`critique.md`（翻案记录 + 修正）
- **🚪 闸口（必停）**：用 `AskUserQuestion` 向用户呈现「修正后方案 + 待确认决策点」，收集补充意见后再继续。**这是流程的核心人机闸口，不可跳过。**

### ④ implement

- **默认**：`Task(subagent_type="oh-my-claudecode:executor")` 分头并行实施（多个独立子任务可并行 spawn）
- **长单线任务**：改用 `/ralph-loop`（JSON 用户故事驱动，迭代到全绿）；若未安装 ralph-loop，改为编排器自己按用户故事逐条循环实施
- **全程纪律（强制）**：
  - **SDD**：先写 `docs/user-stories/<run_id>.json` 验收故事
  - **TDD**：先写测试（红灯）→ 实现（绿灯）→ 重构
  - **planning-with-files**：中间产物落文件
- 共享文件串行改，独立文件并行；每改一批立即回归。

### ④ UX 实现（界面类任务，在 implement 内）

界面类任务的 executor 应以 UXDever 角色执行，携 `ui-ux-pro-max` 指导组件实现（如已安装）。
实现完成后可选触发 `design-review` skill 做视觉验收（确认实现与 ui-spec.md 一致）。

---

### ⑤ review

- **代码类产出** → `code-review` skill 或 spawn `omc:code-reviewer`，查正确性/边界/回归/测试充分性
- **文档类产出** → 优先调 saphire 七项门禁（见下），不可用时走降级 checklist
- **闸口**：有 P0 问题 → 回 implement 修

#### saphire 质量门禁（如已安装）

```bash
saphire review <target>
# 或
python -c "from saphire.pipeline.quality_gate import run_gated_pipeline_sync; ..."
```

#### saphire 降级方案（未安装 saphire 时）

对文档类产出执行以下手动 checklist（spawn `omc:code-reviewer` 逐项检查）：

- [ ] 内容完整性：所有章节均有实质内容，无空占位符
- [ ] 事实准确性：关键数据/API/路径经代码验证
- [ ] 结构清晰度：有目录/标题层级，易于导航
- [ ] 受众适配：术语和深度匹配目标读者
- [ ] 可操作性：示例/命令/步骤可直接复现
- [ ] 无冗余：无重复段落、无 AI-slop 套话
- [ ] 链接有效性：所有引用文件/URL 可访问

全部通过 → 继续；有问题 → 回 implement 修，记录在 `review.md`。

### ⑥ ship → 🚪闸口

- **🚪 闸口（必停）**：向用户确认「变更摘要 + 待提交文件清单」，**审核未提交内容**（扫密钥/运行时垃圾/他人在途工作），获批后再提交
- `git add <精确文件>` → `git commit`（简洁的 commit message）

---

## 反模式铁律

1. **编排器是你（Claude），不是 Python**：不要试图写/调一个 Python 引擎去 spawn omc subagent——物理上做不到（Python 编排的是内部 LLM 不是 subagent）。
2. **验收要真实**：流程跑通 = 真 spawn 了 agent、真停在闸口、真跑了 review——不是"mock 掉 LLM 后测试绿"。
3. **提交前审核**：脏工作区 + 同文件混合他人改动 = 提交雷区，逐项审核；扫密钥（P0 零容忍）；运行时生成物不入库。
4. **复杂度分级是判断不是正则**：读完需求自己判 L0/L1/L2，别用关键词硬匹配。
5. **CLAUDE.md / AGENTS.md 是只读宪法**：不改。

---

## Trace 记录（如已安装 saphire）

```bash
python -c "from saphire.workflow.run_store import record_workflow_run as r; r('<run_id>', '<stage>', '<status>', detail={})"
```

status：`running / done / failed / pending_human`。落 `.saphire/state/workflow_runs/<run_id>.jsonl`。

**未安装 saphire 时**：在 `conclusion/temp/<run_id>/trace.md` 手动记录各阶段起止时间和状态即可。

---

## 与 Track 2 的边界

本 skill 是 **Track 1（交互式开发流程，编排器=你）**。
若任务是「可复用、定时/批量、无人值守的多 agent 任务流」，那是 **Track 2**（Python 引擎 + DSL），不在本 skill——本 skill 只管"人在回路的一次开发"。
