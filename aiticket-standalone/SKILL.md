---
name: aiticket-standalone
description: 在本地一条命令安装并运行 aiticket 轻量化服务（跨平台 Win/Mac/Linux）——仅 Jira 数据源的智能看板 + 智能回复 + 知识库(KB)检索 + 自学习，并作为 MCP server 把上下文/证据/回复能力暴露给调用方 Agent（Claude Code / OpenClaw / WorkBuddy）。本 skill 自带安装器/控制器/MCP server/浏览器扩展。触发词："安装 aiticket"、"本地工单服务"、"aiticket standalone"、"aiticket install/config/start/stop/status/logs/update/uninstall"、"配置 aiticket MCP"、"装 Jira 会话扩展"。
---

# aiticket-standalone — 本地轻量化 Jira 智能看板服务

给装了 Claude Code 的开发者：在本地一条命令装好并跑起一套**精简版 aiticket**（仅 Jira 数据源），
保留三大核心 **智能回复 + 知识库(KB)检索 + 自学习**，含 Web 智能看板。跨平台 Win/Mac/Linux 原生。

- **向量层** sqlite-vec + 本地 fastembed（无 ChromaDB，单 SQLite 文件，体积小）。
- **LLM 可选**：默认不配 key = **纯 MCP 委托**（由调用方 Agent 用自己的 LLM 生成回复）；
  配了 key 则服务也能独立出回复。embedding 始终本地（无 key、KB 自包含）。
- **Jira 会话鉴权**：浏览器扩展自动抓 JSESSIONID（推荐）+ 看板内手动粘贴（兜底）。

## 本 skill 自带（bundled）

```
aiticket-standalone/
├── SKILL.md                      ← 本文件
├── tools/                        ← 跨平台 Python 工具（不依赖 bash）
│   ├── install.py                安装器（取源码→uv venv→装依赖→写配置→init_db→seed_admin→注册自启→起服务→探活→生成 skill token）
│   ├── aiticket_ctl.py           控制器（start/stop/restart/status/logs，/api/liveness 探活）
│   ├── aiticket_paths.py         路径唯一真相源（venv bin/Scripts、端口优先级、service_env）
│   ├── service_manager.py        launchd / systemd --user / Windows schtasks 三后端 + pidfile 兜底
│   ├── mcp_server.py             MCP server（薄 HTTP 桥，9 工具）
│   ├── make_skill_token.py       为 admin 生成 skill token 写 env.json
│   ├── requirements-mcp.txt      MCP 依赖（mcp + httpx）
│   └── test_*.py                 路径/服务单元渲染单测（21 项）
├── browser-extension/aiticket-jira-session/   ← Chrome/Edge MV3，自动抓 Jira 会话
└── src/APP/{backend,frontend}/   ← 随 skill 打包的服务源码（已脱敏，零密钥）
```

服务源码（后端 + 静态前端）**随本 skill 打包在 `src/`**，install.py 默认直接用它安装（免 clone，复制到 `<HOME>/src`）；
也可 `--repo/--branch` 改从 git 克隆。源码经 `git archive` 仅导出已跟踪文件并脱敏（无 llm_config / Jira cookie / API key）。

安装布局：`~/.aiticket/{src,venv,data,kb,config}`（`AITICKET_HOME` 可改）。
> 运行工具用 venv 的 python：`<HOME>/venv/bin/python`（Windows: `<HOME>\venv\Scripts\python.exe`）。
> 安装器/控制器自身只需系统 Python 3.11+（仅用 stdlib）。

## 命令 → 动作

| 命令 | 做什么 |
|------|--------|
| `/aiticket-install [--full]` | **优先** `bash tools/bootstrap_env.sh`（macOS/Linux）/ `powershell -ExecutionPolicy Bypass -File tools\bootstrap_env.ps1`（Windows）——它**自动检测并安装 git+uv+Python3.12**（分步进度），再跑 `install.py`（默认随包 `src/` 免 clone；`--full` 加报表依赖）→ 预下载向量模型(约120MB,带进度) → 启动 `http://127.0.0.1:18080` → 自动生成 skill token。若环境已具备可直接 `python tools/install.py`。 |
| `/aiticket-config` | 引导填**默认项目** / Jira 地址(默认 gfjira.yyrd.com) / 可选 LLM key → 写 `config/deployment.yaml`+`env.json`；默认项目经 `PUT /api/user/settings {current_project}` 写入 → 引导装浏览器扩展 |
| `/aiticket-kb <目录>` | 指定本地 KB 目录并**自动解析**：先本机校验目录存在 → `POST /api/config/kb-root {path}`（写配置+切目录+触发解析，返 task_id）→ 轮询 `GET /api/kb/refresh/status/{task_id}` 显示进度（step + source_files/chunk_count）至 done |
| `/aiticket-import [项目]` | 导入某项目近 12 个月历史工单：`POST /api/index/import-history {project_key?,months:12}`（留空用默认项目；需先绑 Jira 会话，否则 409）→ 轮询 `GET /api/index/status?project_key=` 显示百分比至 done |
| `/aiticket-start` `stop` `restart` `status` `logs` | `python tools/aiticket_ctl.py <cmd>` |
| `/aiticket-update` | `git -C <HOME>/src pull` → `uv pip install`（热缓存秒级）→ `init_db`（幂等）→ restart |
| `/aiticket-uninstall [--purge]` | 停服务 + 注销自启；`--purge` 连 data 一起删 |
| `/aiticket-mcp` | 打印 MCP 客户端配置片段（把本服务接入 Claude Code / OpenClaw） |

## 自然语言 → 动作（OpenClaw / WorkBuddy 用户）

用户**几乎全用大白话**，不敲斜杠命令。听到下列意图就执行对应动作（命令见上表）：

| 用户大白话（示例） | 执行 |
|------|------|
| 「帮我装/安装 aiticket / 本地工单助手」 | 跑 `bootstrap_env`（自动装 git/uv/Python）→ install（含模型预下载）→ 报告网址 |
| 「连上 Jira / 抓一下 Jira 会话」 | 引导装浏览器扩展或手动贴 JSESSIONID（POST /api/settings/jira-session-binding） |
| 「我负责 X 项目 / 默认项目设成 X」 | 设默认项目（PUT /api/user/settings current_project=X）；若已绑会话则自动触发历史导入 |
| 「导入/同步近一年历史工单」「重新导入 X 的历史」 | `/aiticket-import [X]`（POST /api/index/import-history）+ 轮询进度播报 |
| 「把 …目录 设成知识库 / 解析我的文档」 | `/aiticket-kb <目录>`（POST /api/config/kb-root）+ 轮询解析进度播报 |
| 「服务还在吗/状态/重启/看日志/更新/卸载」 | 对应 ctl 命令 |
| 「接进 MCP / 给 Claude Code 当工具」 | `/aiticket-mcp` 打印配置 |

## 进度管理（必做）

任何"会让用户等待"的操作，**必须实时把进度播报给用户**，不要让用户黑屏干等：
- **安装环境/依赖/模型**：`bootstrap_env` 与 `install.py` 自带分步进度与下载百分比——把它们的输出原样/概括转述给用户（如"正在下载中文模型 120MB… 45%"）。
- **历史导入**：触发后**轮询** `GET /api/index/status?project_key=`，每隔几秒播报 `percent`（已处理 N/共 M 条）直到 `status=done`。
- **KB 解析**：触发后**轮询** `GET /api/kb/refresh/status/{task_id}`，播报 `step` 与 `sync.source_files`/`chunk_count` 直到 `status=done`。
- 失败要给可读原因 + 下一步建议（如"未绑定 Jira 会话→请先连 Jira"）。

## 安装

> 📖 **面向使用者的完整图文指南见同目录 [`README.md`](README.md)**（前置要求 / 一条命令安装 / 抓 Jira 会话 / 设默认项目自动导入历史 / 选 KB 目录自动解析 / 故障排查 / 卸载）。下面是给 Agent 的速查。
>
> 开箱默认（本轮新增）：① **本地单用户免登录**（localhost 直接进，无登录页；服务仅绑 127.0.0.1）② **Jira 地址默认 `https://gfjira.yyrd.com`** ③ 设默认项目 + 绑会话后 **自动导入近 12 个月历史工单** ④ 选定 KB 目录 **即自动解析 + 进度**。

```bash
# 推荐：一条命令自动装 git/uv/Python 3.12 + 装服务（全程进度）
bash tools/bootstrap_env.sh                                         # macOS / Linux
powershell -ExecutionPolicy Bypass -File tools\bootstrap_env.ps1    # Windows
# 环境已具备时也可直接（免参数：本地免登录 + Jira 默认 gfjira + 自动建本地用户）：
python tools/install.py
```
要点：
- 无需 `--admin-*`/`--jira-url`：默认本地单用户免登录、Jira=gfjira.yyrd.com、自动建本地用户。
- 装完自动**预下载向量模型**（约 120MB，带进度；`--no-warmup` 可跳过；EMBEDDING_PROVIDER=api/hash 自动跳过）。
- 装完自动 `generate_skill_token` 写入 `<HOME>/config/env.json`，MCP / 浏览器扩展开箱即用鉴权。
- 自启：macOS=launchd、Linux=systemd --user、Windows=计划任务；不注册加 `--no-autostart`（改用 pidfile，不写系统单元）。
- 打开 `http://127.0.0.1:18080` 即见登录 → 智能看板。

## 数据持久化

- **auth.db（管理员/会话）外置 `<HOME>/data/sqlite`**，service_env 经 `APP_AUTH_DB_PATH` pin，更新/重装都不丢。
- 向量库/回复训练器/缓存在 `<HOME>/src/APP/backend/data`（git 忽略）：`/aiticket-update`(git pull) 保留；
  `--force` 重装或全新 clone 会清空（可从 Jira/KB 重建）。

## 浏览器扩展（自动抓 Jira 会话）

`browser-extension/aiticket-jira-session/`（Chrome/Edge MV3）：
1. `chrome://extensions` → 开发者模式 → 加载已解压的扩展 → 选该目录。
2. 点扩展图标，填 Jira 地址、本地服务地址（默认 `http://127.0.0.1:18080`）、skill token（install 已生成，见 env.json）。
3. 「立即推送」即把 `JSESSIONID` 推到本地服务；会话变化/定时自动重推。
手动兜底：看板设置粘贴 JSESSIONID（`/api/settings/jira-session-binding`）。

## 接入 MCP（核心：纯委托模式）

服务作 MCP server，调用方用**自己的 LLM** 编排生成。先 `uv pip install -r tools/requirements-mcp.txt`，Claude Code 配置：
```json
{
  "mcpServers": {
    "aiticket": {
      "command": "<HOME>/venv/bin/python",
      "args": ["<此 skill 目录>/tools/mcp_server.py"],
      "env": {
        "AITICKET_HOME": "<HOME>"
      }
    }
  }
}
```
（mcp_server 仅凭 `AITICKET_HOME` 自动从 env.json 读端口 + skill_token。Windows 用 `<HOME>\venv\Scripts\python.exe`。）

MCP 工具：`build_reply_context(issue_key)` 拿证据 + prompt 模板 → 你用自己的 LLM 生成回复正文；
`search_kb` 检索知识库；`run_gates`/`get_reuse_candidates` 看 gate 判定与复用候选；
`list_board`/`get_ticket`/`check_completeness`/`service_health`；`generate_reply` 仅当服务配了 LLM key 时可用。
> `build_reply_context` 是纯只读：跑完非 LLM 的证据收集后即返回，**绝不调 LLM、绝不写 Jira**。

## 跨平台说明

- 路径/服务管理已全跨平台（venv bin/Scripts、launchd/systemd/schtasks、portalocker、tempfile）。
- 端口默认 18080，`--port` 或 `config/env.json` 的 `port` 可改。
- `AITICKET_SERVICE_BACKEND=pidfile` 可强制 pidfile 管理（不注册系统服务，适合临时/测试/CI）。
