---
name: aiticket-reply
description: |
  AITicket 智能分析与回复 — 给定工单号或自由提问，自动生成智能回复内容、
  推荐知识库文章、推荐相关工单、推荐处理团队和回复方式。
  支持模块感知回复（指定模块时走 /api/reply/generate-by-module）。
  基于向量语义搜索 + 知识库 + 回复训练器的多源融合智能回复。
  触发词: "智能回复"、"分析工单"、"回复建议"、"帮我回复"、"这个工单怎么处理"、
  "回复工单"、"生成回复"、"知识库问答"、"相似工单"
author: 产品管理与应用架构总体部 强骁
version: 1.0.0
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

获取后端地址：
```bash
BASE_URL=$(python3 .agent/skills/aiticket-reply/scripts/setup_config.py --get-url)
```

如果返回 `NEED_SETUP`，引导用户运行：
```bash
python3 .agent/skills/aiticket-reply/scripts/setup_config.py --setup
```

所有 API 为公开接口，无需认证，直接 curl 调用。

**URL 编码提示**: 中文查询参数必须 URL 编码：
```bash
# 方法1: python3 编码
Q=$(python3 -c "import urllib.parse; print(urllib.parse.quote('中文查询'))")
curl -s "$BASE_URL/api/board/search?q=$Q&top_k=5"

# 方法2: curl --data-urlencode 配合 -G
curl -s -G "$BASE_URL/api/board/search" --data-urlencode "q=中文查询" -d "top_k=5"
```

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
2. **不暴露配置内容** — 不在输出中显示 BASE_URL 或加密配置的内容
3. **异常处理** — API 返回错误时，提示用户检查后端服务或工单号是否正确
