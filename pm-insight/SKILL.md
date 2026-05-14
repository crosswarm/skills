---
name: pm-insight
description: PM 系统原始需求智能分析 CLI
trigger_phrases:
  - PM分析
  - 原始需求
  - PM统计
  - 应用服务过滤
  - 批量暂缓
  - PM报告
  - 待分析需求
  - 待规划需求
  - 缺陷
  - bug
  - 验证子任务
  - 产品验证
  - 工作概览
  - 协作需求
---

```
 ____  __  __   ___              _       _     _
|  _ \|  \/  | |_ _|_ __   ___ (_) __ _| |__ | |_
| |_) | |\/| |  | || '_ \ / __|| |/ _` | '_ \| __|
|  __/| |  | |  | || | | |\__ \| | (_| | | | | |_
|_|   |_|  |_| |___|_| |_||___/|_|\__, |_| |_|\__|
                                   |___/        v0.3
	应用与开发平台产品规划部 强骁，2026-04
```

**PM 多模块智能分析** -- 数据管道 + Agent 推理

| 能力 | 说明 |
|------|------|
| 统一工作概览 | 4 模块计数卡片: 原始需求 / 协作需求 / 缺陷 / 验证子任务 |
| 看板概览 | 按状态统计指定模块需求分布 |
| 智能分析 | 按应用/服务分组、TOP10 高频产品、主题聚类 |
| 智能评分 | 对每条需求打 0-100 分，相似需求自动加权 |
| 缺陷分析 | TM 系统缺陷查询、按版本/状态过滤 |
| 批量操作 | 低分需求一键暂缓 |

---

## 1. 配置指南

CLI 路径: `.agent/skills/pm-insight/scripts/pm_insight.py`
配置文件: `.agent/skills/pm-insight/config.json`
配置模板: `.agent/skills/pm-insight/config.json.example`

> **必须使用 `/usr/bin/python3`** 执行（系统 Python 自带 `requests`，`python3` 可能指向缺少依赖的 conda 环境）。

### 1.1 首次配置（推荐：自动模式）

自动从 Chrome 提取 cookies，零交互完成配置（直连模式，无需代理）：

```bash
# macOS
/usr/bin/python3 .agent/skills/pm-insight/scripts/auto_setup.py

# Windows
python .agent\skills\pm-insight\scripts\auto_setup.py
```

> 前提：Chrome 已登录 pm.yyrd.com，本机通过 VPN 可直连 pmf.yyrd.com。

如果自动模式不可用，使用手动向导：

```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --setup
```

向导分 4 步:

| 步骤 | 内容 | 获取方式 |
|------|------|----------|
| 1. PM Cookies | `yht_access_token` + `tenant_info` | Chrome 自动解密（推荐）或 DevTools 手动复制 |
| 3. 产品线 ID | `line_id` | 默认已填，一般无需改动 |
| 4. 默认经办人 | `default_analyst` | 可选，用于过滤 |

### 1.2 验证连接

```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --test
```

成功输出示例:
```
[OK] 连接成功！当前产线共 326 条原始需求。
     最新: [OD-20260418-0012] XXX功能优化建议
```

### 1.3 config.json 结构

```json
{
  "proxy_url": "",
  "proxy_user": "",
  "proxy_pass": "",
  "pm_cookies": {
    "yht_access_token": "<token>",
    "tenant_info": "0000",
    "extra_cookies": {}
  },
  "line_id": "3058614d-5e02-45b3-8084-33d4c6e6a49b",
  "default_analyst": ""
}
```

---

## 2. 命令参考

所有命令前缀:
```
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py
```

### 2.1 核心命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `--setup` | 交互式配置向导 | `--setup` |
| `--test` | 验证 PM 连接 | `--test` |
| `--overview` | 统一工作概览（4 模块计数卡片） | `--overview` |
| `--dashboard` | 按状态统计概览 | `--dashboard --entity defect` |
| `--list` | 列出需求（默认: 待分析+待规划） | `--list --format json` |
| `--detail <AID>` | 查看单条需求详情 | `--detail 1234567890` |
| `--batch-hang` | 批量暂缓低分需求 | `--batch-hang --product 工作流 --below 40 --yes` |
| `--hang-progress [ID]` | 查询批量暂缓执行进度 | `--hang-progress latest` |

### 2.2 实体类型（`--entity`）

| 值 | 说明 | API 来源 | 默认状态 |
|------|------|----------|----------|
| `original` | 原始需求（默认） | pmf.yyrd.com | 待分析+待规划 |
| `demand` | 协作需求 | pmf.yyrd.com | 待分析 |
| `defect` | 缺陷（TM 系统） | tmf.yyrd.com | 待审核+打开 |
| `verification` | 产品验证子任务 | pmf.yyrd.com | 待处理（API 待调通） |

示例: `--entity defect --list --format markdown`

### 2.3 过滤参数（与 `--list` 组合使用）

| 参数 | 说明 | 示例 |
|------|------|------|
| `--status <状态>` | 按状态过滤（中文或英文代码） | `--status 待分析` |
| `--product <名称>` | 按产品/应用过滤（模糊匹配） | `--product 工作流` |
| `--assignee <ID>` | 按经办人过滤���`all` 跳过过滤） | `--assignee all` |
| `--all` | 翻页获取全部结果 | `--list --all` |
| `--top N` | 限制输出前 N 条 | `--list --top 20` |
| `--max-results N` | 单页大小（默认 50） | `--max-results 100` |

### 2.4 输出参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--format json\|markdown\|table\|csv` | 输出格式（默认 json） | `--format markdown` |
| `--export <PATH>` | 导出到文件（按扩展名自动选格式） | `--export report.md` |

### 2.5 可用状态值

**原始需求** (`--entity original`):

| 中文 | 英文代码 |
|------|----------|
| 待分析 | WAIT_ANALYSIS |
| 待规划 | ASSIGNING |
| 实现中 | PROCESSING |
| 已方案解决 | SOLUTION_RESOLVED |
| 已实现 | IMPLEMENTED |
| 暂缓 | HANG |
| 已拒绝 | REJECTED |

**缺陷** (`--entity defect`, 来源 tmf.yyrd.com):

| 中文 | 英文代码 |
|------|----------|
| 待审核 | ONAPPR |
| 打开 | OPEN |
| 已修复 | FIXED |
| 已关闭 | CLOSED |
| 已拒绝 | REJECTED |
| 已挂起 | HANG |
| 重新打开 | REOPEN |

---

## 3. AI 分析工作流

> CLI 是纯数据管道，不含任何 LLM 调用。以下所有"智能"能力由 Agent 自身推理完成。

### 3.1 智能分析（Smart Analysis）

**触发**: 用户要求"PM 分析"、"需求概览"、"分析一下原始需求"等。

**步骤**:

1. 调用 `--dashboard --format json` 获取状态分布。
2. 调用 `--list --all --format json` 获取全量需求列表。
3. Agent 按 `productId_title` 字段（应用/服务名称）对需求分组。
4. Agent 统计每个产品的需求数量，排序得出 **TOP10 高频产品**。
5. Agent 对标题和描述做关键词提取，识别**高频主题**（如"导出"、"权限"、"性能"等）。
6. Agent 生成结构化报告:

```markdown
## PM 原始需求分析报告
**生成时间**: YYYY-MM-DD HH:mm
**数据范围**: 全量 / 指定状态

### 总体概况
- 总需求数: N 条
- 待分析: X | 待规划: Y | 实现中: Z | 暂缓: W

### TOP10 高频应用/服务
| 排名 | 应用/服务 | 需求数 | 占比 |
|------|----------|--------|------|
| 1    | ...      | ...    | ...  |

### 高频主题聚类
| 主题关键词 | 出现次数 | 典型需求 |
|-----------|---------|---------|

### 关键发现
- ...
```

### 3.2 智能评分（Smart Scoring）

**触发**: 用户要求"需求评分"、"哪些需求质量低"、"评估一下需求"等。

**步骤**:

1. 调用 `--list --all --format json` 获取全量需求。
2. Agent 对每条需求按以下维度打分（满分 100）:

| 维度 | 分值 | 评判标准 |
|------|------|----------|
| 描述质量 | 30 分 | 有明确场景描述得 10 分；有复现步骤得 10 分；有期望结果得 10 分 |
| 影响范围 | 20 分 | 影响多用户/多产品得 20 分；单一场景得 5 分 |
| 紧迫程度 | 20 分 | 有截止时间且临近得 20 分；无截止时间得 0 分 |
| 信息完整度 | 30 分 | 有产品/分类/提出人/经办人各 7.5 分 |

3. **相似需求加权**（关键规则 -- 标题关键词重叠检测）:
   - 与 >2 条需求标题高度相似 -> 得分 x 1.5
   - 与 >3 条需求标题高度相似 -> 得分 x 2.0
   - 与 >5 条需求标题高度相似 -> 得分 x 4.0
   - **上限封顶 100 分**

4. Agent 输出排序表:

```markdown
## 需求评分报告

### 高分需求（建议优先处理）
| 编号 | 标题 | 得分 | 相似数 | 应用/服务 |
|------|------|------|--------|----------|

### 低分需求（建议暂缓或拒绝）
| 编号 | 标题 | 得分 | 扣分原因 |
|------|------|------|---------|
```

### 3.3 按应用分组总结（Group by Application）

**触发**: 用户要求"按应用分析"、"XX 应用的需求"、"分产品看需求"等。

**步骤**:

1. 调用 `--list --product <应用名> --all --format json`（若指定应用）。
   或调用 `--list --all --format json` 后按 `productId_title` 分组（若未指定）。
2. Agent 对每个产品内的需求按主题归类（如"功能缺陷"、"体验优化"、"新功能"）。
3. Agent 输出每产品的总结:

```markdown
## 应用需求分组报告

### [应用名称 A]（共 N 条）
**主题分布**:
- 功能缺陷 (X 条): 简述共性问题
- 体验优化 (Y 条): 简述共性诉求
- 新功能 (Z 条): 简述典型需求

**建议**: ...
```

### 3.4 导出报告（Export Report）

**触发**: 用户要求"导出 PM 报告"、"生成报告文件"等。

**步骤**:

1. Agent 先执行上述任一分析工作流，生成 Markdown 内容。
2. 调用 `--export <path>` 保存，或 Agent 直接将分析结果写入文件。
3. 推荐导出路径: `conclusion/temp/pm_insight_report_YYYYMMDD.md`

---

## 4. 常用操作速查

### 统一工作概览（推荐首次使用）
```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --overview
```

### 查看全局概况
```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --dashboard --format json
```

### 查看缺陷概况（TM 系统）
```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --entity defect --dashboard --format json
```

### 列出我的缺陷
```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --entity defect --list --format markdown
```

### 获取全量待分析需求（JSON，供 Agent 分析）
```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --list --status 待分析 --all --format json
```

### 按产品筛选需求
```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --list --product 工作流 --all --format json
```

### 查看单条需求详情
```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --detail <AID>
```

### 批量暂缓低分需求
```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --batch-hang --product 工作流 --below 40 --yes
```

### 导出为 Markdown 表格
```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --list --all --format markdown --export report.md
```

---

## 5. 严格约束

1. **用户隔离**: 每个用户通过自己的 Chrome cookies 直连 PM（VPN 内网），互不干扰。proxy 字段仅供远程服务器场景使用。
2. **CLI 唯一数据源**: Agent 不得直接调用 PM API (`pmf.yyrd.com`)，必须通过本 CLI 获取数据。
3. **Agent 自主推理**: 所有分析、评分、分组、总结均由 Agent 自身完成，CLI 不包含任何 LLM 调用。
4. **凭据保密**: `config.json` 中的密码、token、cookie 值绝不允许出现在 Agent 输出中。泄露即为严重事故。
5. **Python 解释器**: 固定使用 `/usr/bin/python3`，不要使用 `python3` 或 `python`。

---

## 6. 故障排查

| 现象 | 原因 | 修复 |
|------|------|------|
| `[FAIL] 网络连接失败` | VPN 未连接或 PM 服务不可达 | 检查 iNode VPN 连接，确认浏览器能打开 pm.yyrd.com |
| `[ERROR] HTTP 401` | PM cookies 过期 | 重新运行 `auto_setup.py` 或在 Chrome 重新登录后 `--setup` |
| `ModuleNotFoundError: requests` | 使用了错误的 Python | 改用 `/usr/bin/python3` |
| 产品模糊匹配无结果 | 名称不一致 | 先 `--list --top 5 --format json` 查看实际 `productId_title` 值 |
| `--batch-hang` 部分失败 | 需求状态已变更 | 检查 `--hang-progress` 查看详情 |
| `processConvert` 一律返回 400 | 参数放错位置（Body 而非 Query） | 见第 7 节：所有工作流参数必须放 Query String |

---

## 7. 内部 API 备忘（processConvert 暂缓接口）

> 通过逆向前端 JS bundle（`pmf.yyrd.com/app.*.js`）确认，2026-05-14 实测有效。

### 正确调用格式

```
POST https://pmf.yyrd.com/rest/v1/workflow/processConvert
  ?lineId=<cfg.line_id>
  &entityType=ORIGINAL_DEMAND
  &operation=WAIT_PROCESS
  &currentStatus=WAIT_ANALYSIS
  &tenant_info=<tenant>

Body: {"fieldData": {"aids": ["<aid>"]}}
```

**关键陷阱**：`lineId`、`entityType`、`operation`、`currentStatus` 必须作为 **Query 参数**传入，请求体只放 `fieldData`。把这些字段放进 Body 会得到 `HTTP 400`（空响应体），不报具体错误。

### 状态码与操作码速查

| 状态名 | code | 说明 |
|--------|------|------|
| 待分析 | `WAIT_ANALYSIS` | 新建未分配状态 |
| 待规划 | `ASSIGNING` | 已分配经办人 |
| 实现中 | `DEVING` | 排入迭代 |
| 暂缓处理 | `WAIT_PROCESS` | 暂缓目标状态，也是 operation 名 |
| 拒绝 | `REFUSED` | 拒绝目标状态 |

### 前端 JS 来源（逆向依据）

```javascript
// pmf.yyrd.com/app.026cc9b69d2e2350cd18.js 第 4161593 字节附近
this.$http.post(
  `/rest/v1/workflow/processConvert?lineId=${lineId}&entityType=${y}&operation=${operationCode}&currentStatus=${currentStatus}`,
  { fieldData: { aids: [demandId] } },
  { timeout: 30000 }
)
```
