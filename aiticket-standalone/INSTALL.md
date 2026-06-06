# aiticket-standalone 安装与使用指南

本地一条命令装好并跑起一套精简版 **aiticket**：Jira 智能看板 + 智能回复 + 知识库(KB)检索 + 自学习。
跨平台 Win / Mac / Linux 原生，向量层用 sqlite-vec + 本地 fastembed（无 ChromaDB），**LLM 可选**。

---

## ⚡ 快速开始（3 步）

```bash
# 1) 安装（默认用随 skill 打包的源码，免 clone；会自动建 venv、初始化、起服务、生成 skill token）
python tools/install.py --admin-user admin --admin-password '你设个密码' --jira-url https://你的jira地址

# 2) 打开看板：浏览器访问
http://127.0.0.1:18080      # 登录 → 智能看板

# 3) 让看板看到真实 Jira 工单：装浏览器扩展抓会话（见下「第 4 步」），或在看板里手动粘贴 JSESSIONID
```

装完终端会打印访问地址、控制命令、以及自动生成的 **skill token**（MCP / 浏览器扩展鉴权用，已写入 `~/.aiticket/config/env.json`）。

---

## 📋 前置要求

| 必需 | 说明 |
|------|------|
| **Python 3.11+** | 运行安装器/控制器（仅用标准库） |
| **git** | 仅当你不用随包源码、要从仓库克隆时才需要 |
| 推荐 **uv**（Astral） | 建 venv + 装依赖，比 pip 快很多。没装也行，安装器会自动回退到 `python -m venv` + pip |
| 一个登录了 Jira 的浏览器（Chrome/Edge） | 抓 Jira 会话用 |
| 联网 | 首次装依赖 + 首次下 embedding 模型需要（之后可离线） |

> 不需要 Docker、不需要 Node/npm、不需要 ChromaDB。

---

## 🔧 详细步骤

### 第 1 步：安装

```bash
python tools/install.py [选项]
```

常用选项：

| 选项 | 作用 |
|------|------|
| `--admin-user` `--admin-password` | 创建管理员账号（不传则跳过，可事后补） |
| `--jira-url https://...` | Jira 地址，写进配置 |
| `--home 目录` | 安装位置（默认 `~/.aiticket`；Windows `%USERPROFILE%\.aiticket`） |
| `--port 18080` | 端口（默认 18080） |
| `--full` | 额外装报表依赖（pandas 等，一般不需要） |
| `--no-autostart` | 不注册开机自启（改用 pidfile 临时管理；适合测试/CI） |
| `--no-start` | 装完不自动起服务 |
| `--repo` `--branch` | 改从 git 仓库克隆源码（默认用随包 `src/`） |

安装做了什么：取源码 → 建 venv → 装 `requirements-core.txt` → 写配置 → 初始化数据库 → 建管理员 → 注册开机自启 → 起服务 → 探活 → 生成 skill token。

> **自启**：macOS=launchd、Linux=systemd --user、Windows=计划任务（登录时拉起）。

### 第 2 步：配置（Jira 地址 / KB 目录 / 可选 LLM key）

配置文件在 `~/.aiticket/config/deployment.yaml`，可直接编辑后 `restart` 生效：

```yaml
jira:
  base_url: "https://你的jira地址"
kb:
  root_dir: "/你的/知识库/目录"     # 把要检索的文档放这里
```

**LLM key（可选）**：
- **不填** = 纯 MCP 委托模式：服务只提供"证据 + prompt 模板"，由调用方 Agent（Claude Code / OpenClaw / WorkBuddy）用**它自己的 LLM** 生成回复。**推荐**。
- **填了**（在 `~/.aiticket/APP/backend/llm_config.json` 或经界面配置）= 服务也能自己出回复。

### 第 3 步：知识库（KB）

把你的文档（产品手册、FAQ、历史方案等）放进 `kb.root_dir` 指定的目录，然后在看板/接口触发索引：
- 接口：`POST /api/kb/refresh`（异步，返回 task_id，可轮询进度）
- 首次索引会下载 embedding 模型（约 120–460 MB，一次性，缓存在 `~/.cache`），耐心等。

### 第 4 步：抓 Jira 会话（让看板显示真实工单）

Jira 用 MFA 挡了程序化登录，所以用**当前浏览器的会话**鉴权。两种方式：

**方式 A：浏览器扩展（推荐，自动）**
1. Chrome/Edge 打开 `chrome://extensions` → 右上「开发者模式」→「加载已解压的扩展程序」→ 选本 skill 的 `browser-extension/aiticket-jira-session/` 目录。
2. 点扩展图标，填：Jira 地址、本地服务地址（默认 `http://127.0.0.1:18080`）、skill token（安装时生成，见 `~/.aiticket/config/env.json` 的 `skill_token`）。
3. 在浏览器里正常登录 Jira，点扩展的「立即推送」。之后会话变化/每 25 分钟自动重推。

**方式 B：手动粘贴（兜底）**
在看板设置里粘贴浏览器里的 `JSESSIONID`（开发者工具 → Application → Cookies 里复制）。

### 第 5 步：用起来

浏览器开 `http://127.0.0.1:18080`：
- **登录** → **智能看板** 显示 Jira 工单
- 点工单 → **回复弹窗**：引用知识库证据、给处理建议、置信度百分制显示
- **知识库** 页：语义检索文档

---

## 🔌 接入 MCP（把能力给 Claude Code / OpenClaw 当工具）

服务作 MCP server，调用方用**自己的 LLM** 编排生成。先装 MCP 依赖：

```bash
uv pip install -r tools/requirements-mcp.txt    # 或 <HOME>/venv/bin/python -m pip install -r ...
```

Claude Code 的 MCP 配置（`~/.claude/mcp.json` 之类）：

```json
{
  "mcpServers": {
    "aiticket": {
      "command": "~/.aiticket/venv/bin/python",
      "args": ["<本 skill 路径>/tools/mcp_server.py"],
      "env": { "AITICKET_HOME": "~/.aiticket" }
    }
  }
}
```
（Windows 用 `~\.aiticket\venv\Scripts\python.exe`。mcp_server 仅凭 `AITICKET_HOME` 自动读端口 + skill token。）

可用工具：`build_reply_context`（拿证据+模板，你自己 LLM 生成回复，**只读、不调 LLM、不写 Jira**）、`search_kb`、`list_board`、`get_ticket`、`check_completeness`、`run_gates`、`get_reuse_candidates`、`service_health`、`generate_reply`（仅服务配了 LLM key 时可用）。

---

## 💾 体积与依赖（重要预期管理）

skill 本体很小（~9 MB 源码），但**装到你机器上后**会有：

| 项 | 体积 | 何时产生 |
|----|------|----------|
| venv 依赖 | ~250 MB | 安装时 pip 装（onnxruntime 67M / jieba 38M / numpy 等） |
| embedding 模型 | ~120–460 MB | 首次 KB 索引/检索时 fastembed 下载到 `~/.cache`（一次，多服务复用） |
| 向量数据 | 随 KB/工单量增长 | 运行时从你的数据生成 |

**想更轻？** 用环境变量切换 embedding 后端（`~/.aiticket/config` 或启动 env）：
- `EMBEDDING_PROVIDER=api` —— 用云 embedding，**不下本地模型、venv 可省 onnxruntime/fastembed**，但每次检索调云 API（需配 embedding 的 API key）。
- `EMBEDDING_PROVIDER=hash` —— 离线哈希向量，**零模型零额外依赖**，但召回质量明显下降（仅兜底/离线场景）。
- 默认 `local` —— 本地 fastembed，质量最好、离线可用，代价是上面的模型/依赖体积。

---

## 🛠 日常运维

```bash
python tools/aiticket_ctl.py status     # 健康检查（/api/liveness）
python tools/aiticket_ctl.py start       # 启动
python tools/aiticket_ctl.py stop        # 停止
python tools/aiticket_ctl.py restart     # 重启
python tools/aiticket_ctl.py logs --lines 80 [--err]   # 看日志
```
更新源码：`git -C ~/.aiticket/src pull && python tools/install.py --force`（auth.db 在 src 外，更新不丢管理员）。

---

## 🔍 故障排查

| 现象 | 处理 |
|------|------|
| 启动后 `/api/liveness` 不通 | `aiticket_ctl logs --err` 看错误；端口被占用换 `--port` |
| 端口 18080 被占 | `--port 别的端口`，或改 `~/.aiticket/config/env.json` 的 `port` 后 restart |
| 看板空 / 显示不出 Jira 工单 | Jira 会话没抓到或过期：重登 Jira → 扩展「立即推送」，或重贴 JSESSIONID |
| 首次检索很慢/卡住 | 在下 embedding 模型（120–460M），等它下完；或临时 `EMBEDDING_PROVIDER=hash` 跳过下载 |
| MCP 调用全 401 | env.json 缺 `skill_token`：跑 `python tools/make_skill_token.py --home ~/.aiticket` 重新生成 |
| Mac 上 sqlite-vec 扩展加载失败 | 用 uv 托管的 Python（安装器默认就用 uv），别用系统 python |
| Windows 不想注册系统服务 | 装时加 `--no-autostart`，用 `AITICKET_SERVICE_BACKEND=pidfile python tools/aiticket_ctl.py ...` 管理 |
| 本机有代理（Surge 等）导致 localhost 卡死 | 服务已自动设 `no_proxy=127.0.0.1`；MCP/控制器也绕过代理，一般无需额外配置 |

---

## 🗑 卸载

```bash
python tools/aiticket_ctl.py stop
# 注销自启 + 删除安装（保留 data 用 install --uninstall 不带 --purge；彻底删带 --purge）
```
auth.db（管理员/会话）在 `~/.aiticket/data/sqlite`，向量/缓存数据可从 Jira/KB 重建。

---

## 数据持久化说明

- **`~/.aiticket/data/sqlite/auth.db` + `app_auth.key`（管理员/会话）外置**，更新/重装都不丢。
- 向量库 / 回复训练器 / 缓存在 `~/.aiticket/src/APP/backend/data`（git 忽略）：`git pull` 更新保留；`--force` 重装会清空（这些可从 Jira/KB 重建）。
