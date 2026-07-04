# ticket-insight · 工单深度洞察 Skill

端到端交互式 Jira 工单深度分析：用户登录自己的 Jira 账号 → 选项目/领域模块/时间范围 → 自动聚合问题主题 → **人工确认闸口** → 四维度（产品/研发/实施/客开）同比与月度趋势分析 → 生成 md + html 报告到下载目录。

与 [`ticket-query`](../ticket-query) 的分工：query 管即席查数，**insight 管深度分析 + 正式报告**。

## 能力

- **每用户登录**：per-user Jira 凭证（Fernet 机器绑定加密，config 无明文），认证级联（session 文件 → CDP → 账号密码 → 浏览器 cookie 引导）
- **精准筛选**：指定项目 + 领域模块两级级联（cf10123，createmeta 拉可选值）+ 时间范围（默认 2026 上半年，自动生成去年同期）
- **自动主题聚合 + 人工确认**：内建工单分类 8 原则（权威字段 / 解决方案语义层 / 模板噪音剥离 / 抽样验证 / IPC 复合权重 / 研发镜像产品主题 / 窄化排除 / 叶级主题分类法）；种子库优先、无种子库项目 LLM 归纳并沉淀；「其他/未归类」硬指标 ≤3% 强制收敛循环；主题结构逐批弹窗人工确认后才进入分析
- **四维度分析**：每维度 2025 同比 + 2026 月度趋势 + 工单数/客户数/IPC + 主要问题（主题 Top）+ 典型代表工单 + 重点客户
- **双格式报告**：md（月报结构）+ 单文件 html（ECharts 图表，断网降级表格）；正文不贴全量工单列表，原始 + 加工数据落 `data/` 目录
- **全程体验**：阶段进度横幅、开跑前处理时间计划表、长任务进度行、中断续跑检测

## 安装

1. 将 `ticket-insight/` 目录放到你的 skills 目录：
   - 项目级：`<repo>/.agent/skills/ticket-insight/`
   - 或用户级：`~/.claude/skills/ticket-insight/`
2. 依赖：`pip3 install pyyaml cryptography`
3. 首次配置（密码机器绑定加密，不明文存储）：`python3 scripts/ti_auth.py --setup`
4. 验证登录：`python3 scripts/ti_auth.py --test`
5. 在 Claude Code 里说「用 ticket-insight 分析 XX 项目」即可走全流程。

> 本仓库不含 `config.json`（个人 session）——首次需自行 `--setup`。
> `themes/LCZX/` 是流程中心的主题种子库；其它项目首跑用 LLM 归纳并自动沉淀新种子。

## 目录结构

```
ticket-insight/
├── SKILL.md              五阶段状态机 + 交互文案 + 确认闸口协议 + 中断恢复
├── config.json.example   配置模板（真实 config 由 --setup 生成）
├── scripts/
│   ├── ti_common.py      认证级联 / 退避重试 / JQL / Fernet / 进度横幅
│   ├── ti_auth.py        --setup / --test / --paste-cookie
│   ├── ti_scope.py       项目检索 / cf10123 级联 / 探针 + 执行计划表
│   ├── ti_fetch.py       分页拉取 + 缓存 + 进度 ETA
│   ├── ti_themes.py      8 原则聚合 / ≤3% 门禁 / 抽样表 / 确认批次 / 修订应用
│   ├── ti_analyze.py     四维度同比 + 月度 + IPC + 典型工单 + 重点客户
│   └── ti_report.py      md + ECharts 单文件 html + data 目录
├── themes/LCZX/          流程中心主题种子库（product/impl/kf/rd + LLM 沉淀）
└── references/           报告模板
```

## 五阶段流程

```
S0 登录 → S1 范围(项目+领域模块+时间) → S2 主题聚合 + 🚪人工确认闸口
       → S3 四维度分析 → S4 md + html 报告到下载目录
```

License：见仓库根 [LICENSE](../LICENSE)。
