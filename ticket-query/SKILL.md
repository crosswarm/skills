---
name: ticket-query
description: |
  智能工单查询 — 自然语言查询 Jira 工单，自动构建 JQL 执行，输出 Markdown 和 CSV。
  零后端依赖，直接调 Jira REST API。
  触发词: "查工单"、"搜工单"、"帮我查下工单"、"我的工单数"、"统计下工单"、
  "工单查询"、"工单列表"、"帮我找工单"、"查一下"（工单相关）、
  "ticket query"、"search tickets"。
author: BIP应用与开发平台产品规划部 qiangxiao
version: 3.2.0
date: 2026
---

# 智能工单查询

## 激活后行为

**第一步**: 输出欢迎卡片：

```
╔═══════════════════════════════════════════════╗
║  智能工单查询 v3.2                             ║
║  BIP应用与开发平台产品规划部 qiangxiao, 2026          ║
╠═══════════════════════════════════════════════╣
║  支持能力:                                     ║
║    - 自然语言查询工单                           ║
║    - 按月/按客户/按字段聚合统计                  ║
║    - CSV 文件导出                              ║
║    - 跨年对比、趋势分析                         ║
╚═══════════════════════════════════════════════╝
```

**第二步**: 检查配置文件：
```bash
test -f .agent/skills/ticket-query/config.json && echo "CONFIGURED" || echo "NEED_SETUP"
```

**如果 NEED_SETUP**: 进入引导式配置（见下方"首次配置流程"）。
**如果 CONFIGURED**: 直接进入查询模式，处理用户请求。

---

## 首次配置流程

**密码安全说明**：配置向导会用机器专属密钥（Fernet）加密存储密码，config.json 中不会出现明文密码。加密结果绑定当前机器，不可跨机器复制。

引导用户运行配置向导（**一条命令完成**，密码输入不可见）：

```bash
python3 .agent/skills/ticket-query/scripts/jira_query.py --setup
```

向导会交互式询问：Jira 地址、用户名、密码（隐藏输入），自动生成加密 config.json。

如果用户提到了主要项目，向导完成后可用 Write 工具更新 `default_project` 字段：
- 从下方"已知项目映射"中查找对应 Key；找不到就用 `--discover-fields` 搜索

验证连接：
```bash
python3 .agent/skills/ticket-query/scripts/jira_query.py --test-connection
```

连接成功后输出：
```
配置完成！你的账号已验证通过，密码已加密存储。
现在可以直接问我工单相关的问题了，比如：
  - "帮我查下上周的高优先级工单"
  - "统计下今年每月的工单数"
  - "我的未解决工单有多少"
```

---

## 严格限制

1. **禁止创建任何 .py / .sh / .js 文件** — 所有查询必须通过 jira_query.py 完成
2. **禁止修改 SKILL 目录下的任何文件**（config.json 除外）
3. 如果 jira_query.py 不支持某个功能：
   - 先告诉用户"当前脚本不支持此操作"
   - 问用户是否需要在他自己的环境生成辅助脚本
   - 用户确认后才可生成，且生成到 `conclusion/temp/` 目录
4. **一次查询 = 一条命令** — 不要拆成多个命令分步执行
5. 不要单独执行 `date` 命令 — 在构建 JQL 时直接用 Python `datetime` 或 JQL 函数

## 响应策略：先快后深

1. **简单查询**（总数、列表）→ 一条命令直接出结果
2. **复杂分析**（聚合、对比、趋势）→ 分两步：
   - 第一步：先用 `--max-results 1` 快速获取 total，直接回答核心数据
   - 第二步：用户追问"详细看看"或"展开"时，再执行 `--all` 全量查询
3. **示例**：
   - 用户: "今年工单有多少？" → 一条命令 `--max-results 1`，回答"共 N 条"
   - 用户追问"每月趋势呢？" → 再执行 `--all --group-by-month`

## 查询工作流

1. 理解用户查询意图
2. 获取当前日期（在同一条命令中: `date +%Y-%m-%d && python3 ...`）
3. 构建 JQL 并调用脚本 — **一条命令完成**
4. 展示结果（脚本已内置 Markdown 输出）

## 查询命令

脚本路径: `.agent/skills/ticket-query/scripts/jira_query.py`

```bash
# 快速查总数（先快后深第一步）
python3 .agent/skills/ticket-query/scripts/jira_query.py \
  --jql 'project = LCZX AND issuetype = "支持问题" AND created >= "2026-01-01"' \
  --max-results 1

# 查询并直接输出 Markdown 表格
python3 .agent/skills/ticket-query/scripts/jira_query.py \
  --jql '...' --max-results 20 --format markdown

# 全量获取 + 概要统计（一条命令）
python3 .agent/skills/ticket-query/scripts/jira_query.py \
  --jql '...' --all --summary

# 客户 TOP 10（内置，不需要外部脚本）
python3 .agent/skills/ticket-query/scripts/jira_query.py \
  --jql '...' --all --top-customers 10

# 经办人 TOP 10
python3 .agent/skills/ticket-query/scripts/jira_query.py \
  --jql '...' --all --top-assignees 10

# 按月聚合
python3 .agent/skills/ticket-query/scripts/jira_query.py \
  --jql '...' --all --group-by-month

# 按月 + 按字段交叉统计
python3 .agent/skills/ticket-query/scripts/jira_query.py \
  --jql '...' --all --group-by-month --group-by-field customfield_10402

# 导出 CSV
python3 .agent/skills/ticket-query/scripts/jira_query.py \
  --jql '...' --all --csv conclusion/temp/result.csv

# 导出周报兼容 CSV
python3 .agent/skills/ticket-query/scripts/jira_query.py \
  --jql '...' --all --report-csv src/工作流-周数据-xxx.csv

# 验证连接
python3 .agent/skills/ticket-query/scripts/jira_query.py --test-connection

# 发现字段元数据
python3 .agent/skills/ticket-query/scripts/jira_query.py --discover-fields
```

**关键**: 所有分析都通过参数组合完成，不要创建额外脚本。

## JQL 语法参考

### 基础语法
```
field = value                    field != value
field in ("val1", "val2")        field not in ("val1", "val2")
field ~ "keyword"                # 文本模糊搜索（summary, description）
field is EMPTY                   field is not EMPTY
field >= "2026-01-01"            field < "2026-04-01"
```

### 优先级 (priority) — 中文值
`"紧急"` | `"高"` | `"中"` | `"低"`

### 状态 (status) — 常用值
`"待处理"` `"处理中"` `"已解决"` `"已关闭"` `"待分配"` `"支持确认完成"` `"待开始"` `"打开"` `"关闭"` `"挂起"`
"未关闭" → `status not in ("已关闭", "关闭")`
"未解决" → `resolution = Unresolved`

### 标准字段
| 字段 | JQL 名 | 说明 |
|------|--------|------|
| 项目 | `project` | |
| 经办人 | `assignee` | Jira 用户名，`currentUser()` = 当前用户 |
| 报告人 | `reporter` | |
| 问题类型 | `issuetype` | 默认 "支持问题" |
| 标题 | `summary` | 用 `~` 模糊搜索 |
| 描述 | `description` | 用 `~` 模糊搜索 |
| 标签 | `labels` | **仅精确匹配** |
| 创建/更新/到期 | `created` `updated` `due` | 日期函数 |

### 自定义字段
| 字段 | JQL | 字段ID | 说明 |
|------|-----|--------|------|
| **项目名称（=客户）** | `cf[10725]` | customfield_10725 | 口语"客户/用户/公司"对应此字段 |
| 客户问题类型 | `cf[10402]` | customfield_10402 | 数据错误, 技术问题, 实施问题 等 |
| 研发确认问题类型 | `cf[10729]` | customfield_10729 | |
| 解决方式 | `"解决方式" ~ "值"` | customfield_10906 | **文本字段，不支持 `=`，只能用 `~`**。指导解决, 远程解决 等 |
| 解决方案 | `cf[10411]` | customfield_10411 | 文本 |
| 回复方式 | `cf[10410]` | customfield_10410 | |
| 客户属性 | `cf[13211]` | customfield_13211 | |
| 重点客户类型 | `cf[14301]` | customfield_14301 | |
| 所属伙伴 | `cf[11910]` | customfield_11910 | |
| 所属大区 | `cf[11908]` | customfield_11908 | |
| 机构 | `cf[11909]` | customfield_11909 | |
| 需求负责人 | `cf[10401]` | customfield_10401 | |
| 项目领域模块 | `cf[11942]` | customfield_11942 | |
| SOP产品版本 | `cf[13529]` | customfield_13529 | |
| 领域模块 | `cf[10123]` | customfield_10123 | |
| 冲刺标签 | `cf[15200]` | customfield_15200 | |
| 联系人 | `cf[10404]` | customfield_10404 | |
| 联系方式 | `cf[10405]` | customfield_10405 | |
| **初始领域** | `cf[13308]` | customfield_13308 | 用于查转出工单 |
| 初始模块 | `cf[13309]` | customfield_13309 | |
| 初始项目 | `cf[11935]` | customfield_11935 | |

### 常用指标查询方法

#### 转出工单
初始项目是流程中心，但当前已转到其他项目：
```
issuetype = "支持问题" AND cf[11935] = "云平台-流程中心" AND project != LCZX AND created >= "2026-01-01"
```

#### 转入工单
初始项目不是流程中心，但当前在 LCZX 项目中：
```
project = LCZX AND issuetype = "支持问题" AND cf[11935] != "云平台-流程中心" AND cf[11935] is not EMPTY AND created >= "2026-01-01"
```

关键字段: `cf[11935]`（初始项目，array 类型），值为项目全名如 `"云平台-流程中心"`。
本项目总数直接用 `project = LCZX`，转入/转出按需单独查询。

#### 找人类问题统计
在标题或描述中搜索以下关键词（用 `summary ~` 或全量拉取后文本匹配）：
```
找不到人, 找不到审批, 审批人不对, 审批人错, 参与人不对, 参与人错,
分支不对, 分支错, 走错, 选不到人, 找不到参与, 找人, 审批人为空,
没有审批人, 无审批人, 人员不对, 指派错, 分派错
```
由于关键词多，建议全量拉取后用 Python 文本匹配，不用 JQL `summary ~`（JQL 不支持 OR 超过 5 个条件）。

### 标签查询策略
JQL `labels` 不支持前缀匹配。先采样发现实际标签名，再用 `labels in (...)` 查询。

### 日期函数
`startOfDay()` `endOfDay()` `startOfWeek()` `endOfWeek()` `startOfMonth()` `endOfMonth()` `startOfYear()`
偏移: `startOfWeek(-1)` = 上周一, `endOfMonth(-1)` = 上月末, `"-3d"` = 3天前

### 排序
`ORDER BY created DESC` | `ORDER BY due ASC` | `ORDER BY priority ASC` | `ORDER BY updated DESC`

## 聚合分析策略

JQL 不支持 GROUP BY。用 `--all` 全量拉取 + `--group-by-month` / `--group-by-field` 本地聚合。

**客户聚合**: 字段 `customfield_10725`，提取 `fields.customfield_10725[0]`

## 已知项目映射
| 说法 | Key | 全名 |
|------|-----|------|
| 流程中心 | LCZX | 云平台-流程中心 |
| 消息平台 | YWZT | 云平台-消息平台 |
| 云打印 | YDY | 云平台-云打印 |
| 大模型平台 | AI | 云平台-大模型平台 |
| 智能机器人 | AIIM | 云平台-智能机器人 |
| YonBuilder | DDMPT | 云平台-YonBuilder |
| 云ESB | ESB | 云平台-云ESB |
| 公共技术 | GGJS | 云平台-公共技术 |
| 核心档案 | HXDA | 云平台-核心档案 |
| 基础档案 | JCDA | 云平台-基础档案 |
| YonLinker | KFPT | 云平台-YonLinker |
| 零代码 | LDM | 云平台-零代码 |
| 前端框架 | QDKJ | 云平台-前端框架 |
| 智能体 | VPA | 云平台-智能体 |
| 信创适配 | XC | 云平台-信创适配 |
| 友户通 | YHT | 云平台-友户通 |
| 中间件 | YMS | 云平台-中间件 |
| 应用框架 | YYZJ | 云平台-应用框架 |
| 主数据 | ZSJ | 云平台-主数据 |

**未知项目**: 用 `--discover-fields` 搜索。绝不猜测 Key。

## 时间范围与日期处理

### 获取当前日期
构建 JQL 前，**必须先获取用户系统当前日期**：
```bash
date +%Y-%m-%d
```
用返回的日期（如 `2026-03-26`）推算所有相对时间。

### 默认时间范围
- **用户未指定时间** → 默认**今年至今**: `created >= "YYYY-01-01"`（YYYY = 当前年份）
- 所有时间过滤一律用 `created` 字段

### 相对时间计算（基于系统日期）
| 用户说法 | 计算方式 | JQL 示例（假设今天 2026-03-26） |
|----------|----------|------|
| 今年 / 未指定 | 当前年份 1月1日 | `created >= "2026-01-01"` |
| 去年 | 上一年 | `created >= "2025-01-01" AND created < "2026-01-01"` |
| 本月 | 当月1日 | `created >= "2026-03-01"` |
| 上月 | 上月1日 ~ 本月1日 | `created >= "2026-02-01" AND created < "2026-03-01"` |
| 本周 | 本周一日期 | `created >= "2026-03-23"` |
| 上周 | 上周一 ~ 本周一 | `created >= "2026-03-16" AND created < "2026-03-23"` |
| 最近N天 | 今天 - N | `created >= "2026-03-23"` (最近3天) |
| 今天 | 当天 | `created >= "2026-03-26"` |
| 同期对比 | 去年同日期范围 | 2025-01-01 ~ 2025-03-26 vs 2026-01-01 ~ 2026-03-26 |

**规则**: 优先用具体日期字符串（`"2026-03-26"`），而非 JQL 函数（`startOfYear()`），确保跨年/同期对比时日期精确。

## 注意事项
- **默认 `issuetype = "支持问题"`**: "工单"/"问题"默认指支持问题，除非用户明确指定其他类型
- **未指定时间默认今年**: 自动加 `created >= "YYYY-01-01"`
- **默认日期用 `<`**: 查"2025全年"用 `created >= "2025-01-01" AND created < "2026-01-01"`
- 如果 config.json 有 `default_project`，JQL 自动加 `project = XXX`；如果为空，不限项目
- 用户说"我的工单"时，用 `assignee = "config中的username"`
- 默认排序 `ORDER BY created DESC`
- 默认不加 `resolution = Unresolved`，除非用户说"未解决"
- 优先级/状态值是**中文**
- max_results 默认 50，"全部"用 `--all`
- CSV 导出到 `conclusion/temp/`
