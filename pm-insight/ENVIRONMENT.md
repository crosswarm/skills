# PM Insight 运行环境约束

## Python 解释器

### macOS

**强制**: `/usr/bin/python3` (Xcode Command Line Tools 自带)

```bash
/usr/bin/python3 .agent/skills/pm-insight/scripts/pm_insight.py --test
```

不要用 conda/brew 的 python3，经常缺少 `requests`。

### Windows

使用系统 PATH 中的 Python 3.8+：

```cmd
python .agent\skills\pm-insight\scripts\pm_insight.py --test
```

确保是官方 Python（python.org），不要用 Anaconda/conda。

## 依赖包

| 包 | 最低版本 | 用途 | 安装检查 |
|---|---------|------|---------|
| requests | 2.28.0 | HTTP 请求 + CONNECT 代理 | `/usr/bin/python3 -c "import requests"` |
| cryptography | 3.4.0 | Chrome cookie AES 解密 (仅 auto_setup) | `/usr/bin/python3 -c "import cryptography"` |
| urllib3 | 1.26.0 | requests 传递依赖 | 随 requests 安装 |

### 一键安装

```bash
/usr/bin/python3 -m pip install --user requests cryptography
```

> `--user` 安装到 `~/Library/Python/3.9/`，不需要 sudo，不影响系统。

### 已内置 (无需安装)

`argparse`, `csv`, `getpass`, `hashlib`, `io`, `json`, `os`, `shutil`,
`sqlite3`, `subprocess`, `sys`, `tempfile`, `time`, `unicodedata`

## 网络环境

| 目标 | 地址 | 协议 | 说明 |
|------|------|------|------|
| CONNECT 代理 | ticket.spux.cn:13128 | HTTP CONNECT | 所有 PM/TM API 通过此代理 |
| PM API | pmf.yyrd.com:443 | HTTPS | 原始需求 + 协作需求 |
| TM API | tmf.yyrd.com:443 | HTTPS | 缺陷模块 |
| Chrome Keychain | 本地 | - | auto_setup 解密 cookies 需要 |

## 禁止事项

1. **不要** `pip install` 到系统全局 (`sudo pip install`)
2. **不要** 创建虚拟环境 (`venv`, `conda create`) 来运行本 skill
3. **不要** 修改 `#!/usr/bin/python3` shebang 为其他路径
4. **不要** 使用 `python3 -m http.server` 等启动额外服务
5. **不要** 在 auto_setup 之外手动操作 Chrome Cookies 数据库
