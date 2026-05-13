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

### 步骤 2: 获取智能回复

**模块感知路径返回字段**:
| 字段 | 说明 |
|------|------|
| `reply` | 推荐回复内容（主要输出） |
| `module_used` | 实际使用的模块（与请求一致或自动推断） |
| `fallback_used` | 是否降级到全库模式 |
| `kb_refs` | 引用知识库文章列表（含 name / module / score） |
| `cached` | 是否命中缓存 |
| `word_count` | 回复字数 |

**全库融合路径返回字段**:
| 字段 | 说明 |
|------|------|
| `reply_content` | 推荐回复内容（主要输出） |
| `solution_content` | 解决方案建议 |
| `ai_analysis` | AI 分析结果（问题分类、根因、建议） |
| `suggested_reply_method` | 推荐回复方式（远程 / 电话 / 在线） |
| `suggested_issue_type` | 推荐问题确认类型 |
| `kb_sources` | 引用的知识库文章列表 |
| `kb_evidence_count` | 知识库证据数量 |
| `examples_used_count` | 使用的历史回复范例数量 |
| `style_rules_applied` | 是否应用了风格规则 |
| `generation_method` | 生成方法（kb_enhanced / template 等） |

### 如果返回 `status: "warning"`（未找到 AI 分析结果）

执行以下独立降级流程：

1. **用工单概要做语义搜索**，找到历史相似工单：
   ```bash
   curl -s -G "$BASE_URL/api/board/search" --data-urlencode "q=工单概要关键词" -d "top_k=5&min_score=0.2"
   ```

2. **搜索相关知识库文档**：
   ```bash
   curl -s -G "$BASE_URL/api/kb/search" --data-urlencode "q=工单概要关键词" -d "top_k=5"
   ```

3. **由 Claude 直接基于检索结果生成回复建议**（不依赖服务端 AI 分析）。

### 步骤 3: 补充搜索相关工单

```bash
curl -s -G "$BASE_URL/api/board/search" --data-urlencode "q=工单概要关键词" -d "top_k=5&min_score=0.6"
```

### 步骤 4: 渐进式输出（先快后全）

**阶段一（即时，< 2 秒）**：
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  工单智能分析: {issue_key}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  正在分析中...

  [=====     ] 语义搜索  ✓ 找到 {N} 条相关工单
  [==========] 请稍候，正在生成智能回复...
```

**阶段二（语义搜索完成后立即）**：
```
  相关历史工单 ({N} 条):
  1. [{key}] ({score}) {summary}
  2. ...
```

**阶段三（完整分析）**：
```
  AI 分析:
  - 问题分类: {ai_analysis.problem_type}
  - 问题分析: {ai_analysis.problem_analysis}
  - 解决建议: {ai_analysis.solution_suggestion}

  推荐回复:
  {reply_content 或 reply}

  解决方案:
  {solution_content}

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
