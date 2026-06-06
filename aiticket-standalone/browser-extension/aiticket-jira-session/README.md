# aiticket Jira 会话同步（浏览器扩展）

Chrome / Edge MV3 扩展：自动抓取当前浏览器里的 Jira `JSESSIONID`（httpOnly，
JS 读不到），推送到本地 aiticket 服务，省去手动复制 cookie。

## 安装（开发者模式加载）

1. 打开 `chrome://extensions`（Edge: `edge://extensions`）。
2. 右上角打开「开发者模式」。
3. 点「加载已解压的扩展程序」，选择本目录 `aiticket-jira-session/`。

## 使用

1. 点扩展图标，填三项并「保存」：
   - **Jira 地址**：如 `https://jira.example.com`（含协议）
   - **本地服务地址**：默认 `http://127.0.0.1:18080`
   - **Skill Token**：由 `python tools/make_skill_token.py` 生成
2. 在浏览器里正常登录 Jira（确保有有效会话）。
3. 点「立即推送」——把 `JSESSIONID` 推送到本地服务并绑定到你的账号。

之后扩展会在 ① `JSESSIONID` 变化时 ② 每 25 分钟 自动重推，保持看板会话不掉线。

## 权限说明

- `cookies`：读取 Jira 站点的 httpOnly 会话 cookie。
- `host_permissions`：仅本地服务（`127.0.0.1`/`localhost`）固定声明；Jira 站点的
  权限在「保存」时按你填的地址**运行时按需申请**（`optional_host_permissions`）。
- 不收集任何数据，只把会话 POST 到你自己填的本地服务地址。

## 兜底

若不便装扩展，可在看板设置里手动粘贴 JSESSIONID（后端 `/api/settings/jira-session-binding`）。
