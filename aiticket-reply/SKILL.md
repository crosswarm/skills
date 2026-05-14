---
name: aiticket-reply
description: |
  AITicket 智能分析与回复 — 给定工单号或自由提问，自动生成智能回复内容、
  推荐知识库文章、推荐相关工单、推荐处理团队和回复方式。
  支持模块感知回复（指定模块时走 /api/reply/generate-by-module）。
  支持智能扩展模式（"完善方案"）：基于用户修订内容重跑搜索给出精准方案。
  基于向量语义搜索 + 知识库 + 回复训练器的多源融合智能回复。
  触发词: "智能回复"、"分析工单"、"回复建议"、"帮我回复"、"这个工单怎么处理"、
  "回复工单"、"生成回复"、"知识库问答"、"相似工单"、"完善方案"、"深化方案"
author: 产品管理与应用架构总体部 强骁
version: 1.1.0
date: 2026-05
---

# AITicket 智能分析与回复

## 激活后行为

输出能力卡片：

```text
╔═══════════════════════════════════════════════════════════════════════╗
║                                                                       ║
║    ╔═╗ ╦╔╦╗╦╔═╗╦╔═╔═╗╔╦╗   ╦═╗╔═╗╔═╗╦  ╦ ╦                         ║
║    ╠═╣ ║ ║ ║║  ╠╩╗║╣  ║    ╠╦╝║╣ ╠═╝║  ╚╦╝                         ║
║    ╩ ╩ ╩ ╩ ╩╚═╝╩ ╩╚═╝ ╩    ╩╚═╚═╝╩  ╩═╝ ╩                          ║
║                                                                       ║
║    AITicket 智能分析与回复引擎                                         ║
║    Intelligent Ticket Analysis & Reply Engine                         ║
║                                                                       ║
╠═══════════════════════════════════════════════════════════════════════╣
║                                                                       ║
║    支持模式:                                                          ║
║      A. 工单智能回复  输入工单号 → 生成完整回复建议                   ║
║         · 指定模块  → 模块感知回复（更精准的 KB 召回）               ║
║         · 不指定    → 全库融合回复                                    ║
║      B. 知识库问答    自由提问 → 基于产品知识主题回答                ║
║      C. 相似工单搜索  语义搜索 → 历史工单智能匹配                    ║
║                                                                       ║
║    产品管理与应用架构总体部 强骁 · 应用架构师独立开发                  ║
║    体验:   http://ticket.spux.cn/board.html                           ║
║    v1.0.0 • AITicket Reply • 效能龙 2026                              ║
║                                                                       ║
╚═══════════════════════════════════════════════════════════════════════╝
```

然后根据用户输入判断模式。

---

## 前置准备

### 1. 获取后端地址

```bash
BASE_URL=$(python3 .agent/skills/aiticket-reply/scripts/setup_config.py --get-url)
```

如果返回 `NEED_SETUP`，说明尚未配置，告知用户需要初始化（管理员通过 `--setup` 完成，用户无需关心）。

### 2. 获取认证信息

```bash
# 一次性读取所有认证 Header（Token 行 + Client-Id 行）
HEADERS_RAW=$(python3 .agent/skills/aiticket-reply/scripts/setup_config.py --get-auth-headers)
# 转换为 curl -H 参数（每行一个 Header）
CURL_AUTH=$(echo "$HEADERS_RAW" | awk '{print "-H \""$0"\""}' | tr '\n' ' ')
```

登录状态静默检测：
```bash
LOGIN_STATUS=$(python3 .agent/skills/aiticket-reply/scripts/setup_config.py --whoami)
```
- 返回以 `已登录:` 开头 → 已认证，不计配额，直接进入模式判断
- 返回 `未登录` → **不要让用户手动跑脚本**，直接用自然语言问用户：
  「您还没有登录 QCL 账号，请告诉我您的用户名和密码，我来帮您完成登录」
  用户回复后 Claude 在后台执行：
  ```bash
  python3 .agent/skills/aiticket-reply/scripts/setup_config.py --login <<EOF
  <用户名>
  <密码>
  EOF
  ```
  登录成功后重新获取 `HEADERS_RAW`，继续正常流程。
- 未登录且用户不愿意登录 → 提示「未登录每天可免费使用 1 次，今日额度尚未用完时继续」，进入匿名流程

### 3. 所有 curl 调用格式

认证 Header 从 `$HEADERS_RAW` 逐行注入，模板：
```bash
curl -s -X POST "$BASE_URL/api/reply/generate-by-module" \
  $(echo "$HEADERS_RAW" | awk '{print "-H \""$0"\""}') \
  -H "Content-Type: application/json" \
  -d '{"issue_key":"LCZX-61234","module":"流程中心","force":false}'
```

**URL 编码提示**: 中文查询参数必须 URL 编码：
```bash
# curl --data-urlencode 配合 -G（推荐）
curl -s -G "$BASE_URL/api/board/search" \
  $(echo "$HEADERS_RAW" | awk '{print "-H \""$0"\""}') \
  --data-urlencode "q=中文查询" -d "top_k=5"
```

### 4. 额度用尽的统一处理

当任何接口返回 HTTP 429 时，**不要让用户看到技术性错误信息**，直接以自然语言说：
「您今天的免费额度已经用完了（每天 1 次），请告诉我您的 QCL 用户名和密码，登录后即可无限制使用。」
用户提供账号后，Claude 后台执行 `--login`，登录成功后立即重跑刚才失败的请求。

---

## 模式 A: 工单智能回复

当用户提供工单号（如 LCZX-61234）时执行此流程。

### 路径判断

**用户指令中包含模块名**（如「流程中心」「云盘」「工资薪酬」等）→ 走模块感知路径：

```bash
# 可选：先做模块覆盖度预检
curl -s "$BASE_URL/api/reply/module-coverage?module=流程中心"

# 主回复生成（模块感知）
curl -s -X POST "$BASE_URL/api/reply/generate-by-module" \
  -H "Content-Type: application/json" \
  -d '{"issue_key":"LCZX-61234","module":"流程中心","force":false}'
```

**不指定模块** → 走全库融合路径：

```bash
curl -s -X POST "$BASE_URL/api/board/generate-reply" \
  -H "Content-Type: application/json" \
  -d '{"issue_key":"LCZX-61234","force":false}'
```

### 步骤 1: 模块覆盖度预检（可选，指定模块时执行）

```bash
curl -s "$BASE_URL/api/reply/module-coverage?module=流程中心"
```

**返回字段**:
| 字段 | 说明 |
|------|------|
| `coverage_level` | `high` / `medium` / `low` |
| `kb_docs_module` | 该模块 KB 文档数 |
| `recommendation` | 给 Claude 的使用建议 |

- `coverage_level: low` → 提示用户该模块 KB 覆盖较少，回复质量可能受限，**但仍继续执行**

### 步骤 2: 获取结构化分析数据

> ⚠️ **重要**：`generate-reply` / `generate-by-module` 的返回值中，`reply_content` / `reply` / `solution_content`
> 是服务端 LLM 生成的文本，**不得直接输出给用户**。
> 这些接口的作用是提供**结构化路由数据**（问题分类、推荐团队、置信度、KB 引用列表）；
> 最终回复由 **Claude 本体**基于所有检索证据撰写。

**从返回值中提取的上下文数据**（仅供 Claude 推理使用）:

| 字段 | 用途 |
|------|------|
| `ai_analysis.recommended_team` / `recommended_role` | 路由建议（展示给用户） |
| `ai_analysis.functionality_impact` | 功能影响描述（作为 Claude 写回复的参考） |
| `ai_analysis.solution_suggestion` | 解决方向提示（作为 Claude 写回复的参考） |
| `ai_analysis.confidence` | 置信度（展示给用户） |
| `ai_analysis.similar_issues` | AI 认为相关的工单号（补充搜索用） |
| `suggested_reply_method` | 推荐回复方式（展示给用户） |
| `suggested_issue_type` | 推荐问题类型（展示给用户） |
| `kb_sources` / `kb_refs` | KB 引用列表（展示标题；内容供 Claude 参考） |
| `module_used` / `fallback_used` | 模块信息（展示给用户） |

**如果返回 `status: "warning"`**（无 AI 分析），跳过此步直接进入步骤 3。

### 步骤 3: 并行拉取语义证据

以 `ai_analysis.functionality_impact` 或工单概要的关键词为查询词，**同时执行**：

```bash
# 相似工单
curl -s -G "$BASE_URL/api/board/search" --data-urlencode "q=功能影响或概要关键词" -d "top_k=6&min_score=0.3"

# 知识库文档
curl -s -G "$BASE_URL/api/kb/search" --data-urlencode "q=功能影响或概要关键词" -d "top_k=5"
```

同时将 `ai_analysis.similar_issues` 中的工单号也并入相似工单展示列表。

### 步骤 4: Claude 撰写回复

基于以下所有上下文，由 **Claude 本体**撰写最终回复（不得复制服务端 reply_content）：

- `ai_analysis.functionality_impact`（问题理解）
- `ai_analysis.solution_suggestion`（解决方向）
- 语义搜索返回的相似工单（历史处理参考）
- KB 搜索返回的知识文档（知识依据）

回复风格要求：
- 开头以「您好！」起
- 先复述问题理解，再给出操作建议，最后请对方提供截图或确认
- 控制在 150 字以内，语气专业、简洁

### 步骤 5: 渐进式输出（先快后全）

**阶段一（即时）**：
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  工单智能分析: {issue_key}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  正在分析中...

  [=====     ] 语义搜索  ✓ 找到 {N} 条相关工单
  [==========] 请稍候，正在生成智能回复...
```

**阶段二（搜索完成后）**：
```
  相关历史工单 ({N} 条):
  1. [{key}] ({score}) {summary}
  2. ...
```

**阶段三（完整输出）**：
```
  AI 分析（服务端路由）:
  - 功能影响: {ai_analysis.functionality_impact}
  - 解决方向: {ai_analysis.solution_suggestion}
  - 置信度: {confidence * 100}%
  - 推荐团队: {recommended_team} · {recommended_role}

  推荐回复（Claude 生成）:
  {Claude 撰写的回复文本}

  参考知识库文章 ({kb_evidence_count} 篇):
  {kb_sources 或 kb_refs 列表，每项显示标题}

  相关历史工单:
  {search 结果列表，显示工单号、概要、相似度}

  处理建议:
  - 推荐回复方式: {suggested_reply_method}
  - 推荐问题类型: {suggested_issue_type}
  - 模块: {module_used}
  - 生成方法: {generation_method}
  - 使用范例数: {examples_used_count}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 模式 B: 知识库问答

当用户提出自由文本问题（非工单号）时执行此流程。

### 步骤 1: 调用 KB 问答

```bash
curl -s --max-time 60 -X POST "$BASE_URL/api/kb/qa" \
  -H "Content-Type: application/json" \
  -d '{"query":"用户的问题文本","mode":"qa"}'
```

`mode` 可选值：`qa`（简短回答）、`full`（详细回答）

KB 问答涉及 LLM 调用，响应时间通常 15-45 秒，请耐心等待。

**如果 KB QA 超时（504）或返回 LLM 错误**，降级为 KB 搜索 + Claude 本地汇总：
1. 用 `/api/kb/search?q=问题关键词&top_k=5` 获取知识库相关文档
2. 用 `/api/board/search?q=问题关键词&top_k=5` 获取相关工单
3. 由 Claude 直接基于检索到的 KB 文档和工单数据生成回答

**返回字段**:
| 字段 | 说明 |
|------|------|
| `answer` | LLM 生成的回答 |
| `sources` | 引用的知识库来源列表 |
| `query` | 原始查询 |

### 步骤 2: 补充搜索知识库

```bash
curl -s -G "$BASE_URL/api/kb/search" --data-urlencode "q=用户问题关键词" -d "top_k=5"
```

### 步骤 3: 格式化输出

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  知识库问答
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  问题: {query}

  回答:
  {answer}

  参考来源:
  {sources 列表}

  相关知识库文章:
  {search 结果列表}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 模式 C: 相似工单搜索

当用户要求搜索相似工单时执行。

```bash
curl -s -G "$BASE_URL/api/board/search" --data-urlencode "q=搜索关键词" -d "top_k=10&min_score=0.5"
```

输出搜索结果列表，包含工单号、概要、相似度评分。

---

## 模式 D: 智能扩展（完善方案）

**触发词**: `完善方案` / `深化方案` / `按这个思路再想想` / `refine`

**触发条件**: 当前对话中已有一个最近的 issue_key，且用户对 Claude 上一轮给出的回复进行了手工修改或补充关键词。

### 步骤 1: 提取上下文

从用户最新输入中提取：
- `USER_DRAFT`：用户修改后的方案内容（或补充说明文本）
- `FOCUS_KEYWORDS`：从 USER_DRAFT 中切词，取 2-8 个实意词（2 字以上）

如用户仅说「完善方案」而未给出具体内容，先询问：「请告诉我您想着重完善的方向或关键词，比如「字段联动」「事件触发」等」

### 步骤 2: 调用 refine 端点

```bash
curl -s -X POST "$BASE_URL/api/reply/refine" \
  $(echo "$HEADERS_RAW" | awk '{print "-H \""$0"\""}') \
  -H "Content-Type: application/json" \
  -d "$(python3 -c "
import json, sys
draft = '''$USER_DRAFT'''
keywords = [w for w in draft.replace('，',',').replace('、',',').replace('；',',').split(',') if len(w.strip())>=2][:8]
print(json.dumps({'issue_key':'$ISSUE_KEY','user_draft':draft,'focus_keywords':keywords}))
")"
```

**返回字段**:
| 字段 | 说明 |
|------|------|
| `refined_solution` | 服务端 LLM 基于新搜索结果生成的参考方案 |
| `kb_sources` | 本次搜索命中的知识库文章列表 |
| `similar_issues` | 本次搜索命中的相似工单列表 |
| `search_keywords_used` | 实际用于搜索的关键词 |
| `module_used` | 实际使用的模块分类 |

### 步骤 3: Claude 重写最终回复

⚠️ `refined_solution` 是服务端 LLM 参考输出，**不得直接给用户**。

Claude 本体基于以下材料重写最终回复（格式同模式 A 阶段三，标题改为「方案完善」）：
- `refined_solution`（解决思路参考）
- `similar_issues`（历史工单参考）
- `kb_sources`（知识库依据）
- 用户提供的 `USER_DRAFT`（用户意图核心）

---

## 演示流程

当用户要求演示或体验时，执行以下流程：

1. **查询近期工单**: 用语义搜索获取一批近期工单
   ```bash
   curl -s -G "$BASE_URL/api/board/search" --data-urlencode "q=近期未处理工单" -d "top_k=5&min_score=0.3"
   ```

2. **选取工单**: 从结果中选取一条有代表性的工单（优先选择有描述内容的）

3. **执行智能分析**: 用选取的工单号执行模式 A 的完整流程

4. **对比展示**: 展示 AI 生成的回复与相似历史工单的处理方式对比

---

## 多轮验证

支持用户连续输入多个工单号或问题进行验证，每次独立执行对应模式的完整流程。鼓励用户从不同项目获取工单进行测试。

---

## 严格限制

1. **所有数据通过 API 获取** — 不直接读取本地数据库
2. **不暴露配置内容** — 不在输出中显示 BASE_URL、Token 或任何加密配置内容
3. **异常处理** — API 返回错误时，以友好语言提示用户，不暴露技术细节
4. **用户友好引导** — 登录、额度提示、错误提示全部用自然语言；**禁止让用户自己跑任何命令行脚本**，Claude 代劳所有脚本调用
5. **429 必须转化为登录引导** — 永远不直接向用户输出「HTTP 429」或「额度超限」原始错误，统一转化为友好的登录引导
6. **refined_solution 不直接输出** — 与 reply_content 一样，服务端生成的方案文本只作为 Claude 推理材料，最终回复由 Claude 本体撰写
