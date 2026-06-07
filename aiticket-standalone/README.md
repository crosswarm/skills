# aiticket-standalone · 本地 Jira 智能工单助手

> 在你自己的电脑上，一条命令装好一个"智能工单看板"：看 Jira 工单、AI 帮你写回复、用你自己的知识库找答案、越用越准。**不用懂技术也能装。**

---

## 这是什么？能帮我做什么？

装好后，浏览器打开一个网页（你自己电脑上的），你会看到：

- **🗂 智能看板**：你负责的 Jira 工单一目了然
- **🤖 智能回复**：点一个工单，AI 结合"知识库 + 历史相似工单"给你一份处理建议，你改改就能用
- **📚 知识库问答**：把你的文档（产品手册、FAQ、方案）丢进一个文件夹，就能语义搜索
- **🧠 自学习**：你采纳/修改回复后，系统会记住，越用越懂你的业务

全部跑在**你自己电脑上**，数据不外传；只在你需要时去读 Jira。

---

## 装它之前，电脑上要有什么？

只需两样（大部分开发电脑都已经有）：

1. **Python 3.11 或更高版本** —— 终端输入 `python3 --version` 能看到 3.11+ 就行
2. **git** —— 输入 `git --version` 能看到版本号就行

> 没有也没关系：去 python.org 装 Python，git 官网装 git，各点"下一步"即可。
> 第一次安装需要联网（下载依赖 + 一个 AI 模型）；之后可离线用。

---

## 第一步：一条命令安装

在终端里，进到本 skill 目录，运行：

```bash
python tools/install.py
```

就这一行。它会自动帮你：建好运行环境 → 装好程序 → 初始化 → **启动服务** → 打印访问网址。
（大约几分钟，第一次稍慢，要下载一个约 120MB 的中文 AI 模型。）

装完你会看到类似：`访问看板 : http://127.0.0.1:18080`，浏览器打开它即可。

### 它已经替你默认好了
- ✅ **不用登录**：本地单用户，打开网页直接进，没有账号密码这一步
- ✅ **Jira 地址已预置**：默认就是公司内网 `https://gfjira.yyrd.com`，不用填
- ✅ **鉴权令牌已生成**：浏览器扩展 / AI 助手接入要用的 token 已自动配好

---

## 第二步：让它看到你的 Jira 工单（抓登录会话）

公司 Jira 有二次验证，程序没法直接登录，所以借用**你浏览器里已登录的会话**。两种办法，挑一个：

**办法 A：装个浏览器小插件（推荐，自动）**
1. Chrome/Edge 地址栏输入 `chrome://extensions` 回车
2. 打开右上角「开发者模式」→ 点「加载已解压的扩展程序」→ 选本 skill 里的 `browser-extension/aiticket-jira-session/` 文件夹
3. 点插件图标，三个框填：Jira 地址（已默认）、本地服务地址（已默认 `http://127.0.0.1:18080`）、令牌（见 `~/.aiticket/config/env.json` 里的 `skill_token`）
4. 确保浏览器里你已登录 Jira，点「立即推送」—— 完成！之后会自动保持。

**办法 B：手动粘贴（备用）**
在看板设置里，把浏览器里的 `JSESSIONID` 粘进去（开发者工具→Application→Cookies 里复制）。

---

## 第三步：设默认项目 → 自动导入近 12 个月历史工单

在配置里填上**你负责的项目**（比如 `LCZX`、`BIP`）。一旦你设好项目**并且**完成了第二步（绑好 Jira 会话），系统会**自动**在后台把这个项目**最近 12 个月**的历史工单拉下来、建好索引——这样 AI 写回复时才能参考"相似的老工单"。

- 进度可查：导入在后台跑，能看到百分比（`/aiticket-import` 命令会显示进度；也可手动重跑）。
- 工单多时要几分钟，跑完即用，不用一直盯着。

> 没绑 Jira 会话就触发导入会提示"请先绑定会话"——先做第二步即可。

---

## 第四步：选你的知识库文件夹 → 自动解析

把你的文档（`.md / .docx / .pdf / .xlsx / .pptx / .txt …`）放进一个文件夹，然后在 skill 里指定这个文件夹。**一选定就自动开始解析**（扫描 → 切片 → 向量化），并显示进度，跑完就能在"知识库"页搜索、AI 回复也会引用它。

```bash
# 在 skill 里指定本地 KB 目录（选定即自动解析 + 显示进度）
/aiticket-kb /你的/知识库/文件夹
```

---

## 平时怎么操作（命令一览）

| 命令 | 作用 |
|------|------|
| `/aiticket-install` | 安装并启动（第一步） |
| `/aiticket-config` | 填默认项目 / 改 Jira 地址 / 可选填 AI key |
| `/aiticket-kb <目录>` | 指定知识库目录并自动解析（看进度） |
| `/aiticket-import [项目]` | 手动导入/重导某项目近 12 个月历史工单（看进度） |
| `/aiticket-start` `stop` `restart` `status` `logs` | 启停/查状态/看日志 |
| `/aiticket-update` | 更新到新版本 |
| `/aiticket-uninstall` | 卸载 |

也可直接用脚本：`python tools/aiticket_ctl.py status`（查健康）、`... logs --err`（看错误日志）。

---

## 进阶：接入 Claude Code / OpenClaw（可选）

本服务能作为"工具"接给 AI 助手，让助手用**它自己的大模型**来生成回复（你这边不用配大模型 key）。先装一次 MCP 依赖：

```bash
uv pip install -r tools/requirements-mcp.txt
```

然后在 Claude Code 的 MCP 配置里加：

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
（Windows 用 `~\.aiticket\venv\Scripts\python.exe`。）

---

## 它装到哪？占多大？

- skill 本体很小（约 9MB 源码）。装到电脑后会多出：运行环境约 250MB + 一个中文 AI 模型约 120–460MB（只下一次，多个项目共用）。这是"本地 AI 检索"的必要成本。
- **想更省空间/不下模型**：把环境变量 `EMBEDDING_PROVIDER` 设为 `api`（用云端向量，需配 key）或 `hash`（离线、质量略低）。
- 你的**管理员/会话数据**在 `~/.aiticket/data`，更新/重装都不会丢；向量/缓存可随时从 Jira/KB 重建。

---

## 遇到问题怎么办？

| 现象 | 怎么办 |
|------|--------|
| 网页打不开 / 服务没起来 | `python tools/aiticket_ctl.py logs --err` 看错误；端口被占就装时加 `--port 别的端口` |
| 看板里没有工单 | Jira 会话没抓到或过期：重新登录 Jira，点插件「立即推送」（第二步） |
| 第一次很慢/卡住 | 在下载 AI 模型（约 120MB+），等它下完；急用可临时设 `EMBEDDING_PROVIDER=hash` |
| 提示"请先绑定 Jira 会话" | 历史导入需要先完成第二步（绑会话）再触发 |
| Mac 上提示扩展加载失败 | 用安装器自带的 Python（它默认用 uv 托管的 Python），别用系统自带的 |

---

## 卸载

```bash
python tools/aiticket_ctl.py stop      # 先停服务（会一并注销 launchd/systemd 自启）
rm -rf ~/.aiticket                      # 删除安装目录（含数据；不想删数据就跳过这步）
```
（Windows 删除 `%USERPROFILE%\.aiticket` 即可。）

---

> 给 AI 助手看的速查在同目录 `SKILL.md`。本 README 面向使用者，怎么装、怎么用，照着做即可。
