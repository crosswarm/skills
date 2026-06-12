#!/usr/bin/python3
"""
auto_setup.py — Zero-interaction PM Insight setup.

Reads Chrome cookies + proxy auth file → generates config.json → verifies.
Designed for first-time users who already have Chrome logged into pm.yyrd.com.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CONFIG_PATH = SKILL_DIR / "config.json"

PROXY_AUTH_PATH = Path.home() / "Library/Scripts/aiticket/pm-proxy-auth.conf"
PM_DOMAINS = [".yyrd.com", "pm.yyrd.com", "pmf.yyrd.com"]

# ANSI colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def check_environment():
    """Verify Python interpreter and dependencies before proceeding."""
    issues = []

    if sys.executable != "/usr/bin/python3" and "Library/Developer" not in sys.executable:
        issues.append(
            f"Python: {sys.executable}\n"
            "    -> 请使用 /usr/bin/python3 执行本脚本，不要用 conda/brew python"
        )

    try:
        import requests  # noqa: F401
    except ImportError:
        issues.append(
            "requests 未安装\n"
            "    -> /usr/bin/python3 -m pip install --user requests"
        )

    try:
        import cryptography  # noqa: F401
    except ImportError:
        issues.append(
            "cryptography 未安装 (Chrome cookie 解密需要)\n"
            "    -> /usr/bin/python3 -m pip install --user cryptography"
        )

    if issues:
        print(f"{RED}{BOLD}环境检查失败:{RESET}\n")
        for i, issue in enumerate(issues, 1):
            print(f"  {RED}{i}.{RESET} {issue}\n")
        print(f"一键安装: {CYAN}/usr/bin/python3 -m pip install --user requests cryptography{RESET}")
        return False
    return True


def banner():
    print(f"""
{CYAN}{BOLD}╔══════════════════════════════════════════════════╗
║          PM Insight — 自动配置向导               ║
║   从 Chrome 自动提取 cookies，零交互完成配置      ║
╚══════════════════════════════════════════════════╝{RESET}
""")


def step(n, total, msg):
    print(f"\n{BOLD}[{n}/{total}]{RESET} {msg}")


def ok(msg):
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}⚠{RESET} {msg}")


def fail(msg):
    print(f"  {RED}✗{RESET} {msg}")


# ── Step 1: Chrome Cookie Extraction (cross-platform) ──

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


def _chrome_cookies_path() -> Path:
    """Return Chrome Cookies DB path for the current OS."""
    if IS_MACOS:
        return Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
    elif IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        for name in ["Cookies", "Network/Cookies"]:
            p = Path(local) / "Google/Chrome/User Data/Default" / name
            if p.exists():
                return p
        return Path(local) / "Google/Chrome/User Data/Default/Network/Cookies"
    else:
        return Path.home() / ".config/google-chrome/Default/Cookies"


def get_chrome_key():
    """Get Chrome decryption key (macOS Keychain or Windows DPAPI)."""
    if IS_MACOS:
        try:
            pw = subprocess.check_output(
                ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
                stderr=subprocess.DEVNULL,
            ).strip()
            return hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", 1003, 16)
        except Exception as e:
            fail(f"无法获取 Chrome Safe Storage 密码: {e}")
            return None
    elif IS_WINDOWS:
        try:
            local_state_path = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data/Local State"
            with open(local_state_path, "r", encoding="utf-8") as f:
                local_state = json.load(f)
            encrypted_key = __import__("base64").b64decode(local_state["os_crypt"]["encrypted_key"])
            encrypted_key = encrypted_key[5:]  # strip DPAPI prefix
            import win32crypt  # type: ignore
            return win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
        except ImportError:
            fail("Windows 需要 pywin32: pip install pywin32")
            return None
        except Exception as e:
            fail(f"无法获取 Chrome 密钥: {e}")
            return None
    else:
        fail("当前仅支持 macOS 和 Windows 的 Chrome cookie 自动获取")
        warn("请使用 --setup 手动配置")
        return None


def decrypt_cookie_value(encrypted_value, key):
    """Decrypt a Chrome encrypted cookie value (cross-platform)."""
    if not encrypted_value:
        return ""

    # macOS: v10 + AES-128-CBC
    if encrypted_value[:3] == b"v10" and IS_MACOS:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            fail("缺少 cryptography: pip install cryptography")
            return ""
        cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(encrypted_value[3:]) + decryptor.finalize()
        pad_len = decrypted[-1]
        if isinstance(pad_len, int) and 0 < pad_len <= 16:
            decrypted = decrypted[:-pad_len]
        if len(decrypted) > 32:
            return decrypted[32:].decode("utf-8", errors="replace")
        return decrypted.decode("utf-8", errors="replace")

    # Windows: v10 + AES-256-GCM (Chrome 80+)
    if encrypted_value[:3] == b"v10" and IS_WINDOWS:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            fail("缺少 cryptography: pip install cryptography")
            return ""
        nonce = encrypted_value[3:15]
        ciphertext = encrypted_value[15:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8", errors="replace")

    # Windows DPAPI fallback (older Chrome)
    if IS_WINDOWS:
        try:
            import win32crypt  # type: ignore
            return win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1].decode("utf-8", errors="replace")
        except Exception:
            pass

    return encrypted_value.decode("utf-8", errors="replace") if encrypted_value else ""


def extract_chrome_cookies(key):
    """Extract and decrypt cookies for yyrd.com domains from Chrome."""
    chrome_db = _chrome_cookies_path()
    if not chrome_db.exists():
        fail(f"Chrome Cookies 数据库不存在: {chrome_db}")
        return {}

    tmp = tempfile.mktemp(suffix=".db")
    shutil.copy2(chrome_db, tmp)

    conn = sqlite3.connect(tmp)
    cursor = conn.cursor()

    all_cookies = {}
    for domain in PM_DOMAINS:
        cursor.execute(
            "SELECT name, encrypted_value FROM cookies WHERE host_key = ?",
            (domain,),
        )
        for name, enc_val in cursor.fetchall():
            val = decrypt_cookie_value(enc_val, key)
            if val:
                all_cookies[name] = {"value": val, "domain": domain}

    conn.close()
    os.unlink(tmp)
    return all_cookies


# ── Step 2: Proxy Auth ──

def read_proxy_auth():
    """Read proxy username:password from pm-proxy-auth.conf."""
    if not PROXY_AUTH_PATH.exists():
        return None, None

    with open(PROXY_AUTH_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                user, passwd = line.split(":", 1)
                return user.strip(), passwd.strip()
    return None, None


# ── Step 3: Generate Config ──

def generate_config(cookies, proxy_user, proxy_pass):
    """Build config.json from extracted data."""
    yht = cookies.get("yht_access_token", {}).get("value", "")
    tenant = cookies.get("tenant_info", {}).get("value", "0000")

    config = {
        "proxy_url": "",
        "proxy_user": "",
        "proxy_pass": "",
        "pm_cookies": {
            "yht_access_token": yht,
            "tenant_info": tenant if len(tenant) >= 4 else "0000",
            "ycap_session": "",
            "extra_cookies": {},
        },
        "line_id": "3058614d-5e02-45b3-8084-33d4c6e6a49b",
        "default_analyst": "",  # 经办人 aid，留空=不按经办人过滤；用户按需填自己的
    }

    # Add ycap cookie with its original name (e.g. ycap_06e6ea000524)
    for name, info in cookies.items():
        if name.startswith("ycap_"):
            config["pm_cookies"]["extra_cookies"][name] = info["value"]
            ok(f"发现 ycap 会话 cookie: {name}")
            break

    return config


# ── Main Flow ──

def main():
    banner()

    if not check_environment():
        return 1

    total_steps = 3

    # Step 1: Chrome cookies
    step(1, total_steps, "从 Chrome 解密 PM cookies...")

    key = get_chrome_key()
    if not key:
        print(f"\n{RED}配置失败: 无法访问 Chrome Keychain{RESET}")
        print("请确保 Chrome 正在运行，且已登录 pm.yyrd.com")
        return 1

    ok("Chrome Safe Storage 密钥获取成功")

    cookies = extract_chrome_cookies(key)
    if not cookies:
        fail("未找到 yyrd.com 域的 cookies")
        print("请先在 Chrome 中打开 https://pm.yyrd.com 并登录")
        return 1

    print(f"\n  发现 {len(cookies)} 个 cookies:")
    for name, info in sorted(cookies.items()):
        val_preview = info["value"][:30] + "..." if len(info["value"]) > 30 else info["value"]
        masked = val_preview[:8] + "****" if len(val_preview) > 12 else val_preview
        print(f"    {CYAN}{info['domain']:20s}{RESET} {name:25s} = {masked}")

    yht = cookies.get("yht_access_token", {}).get("value", "")
    if not yht:
        fail("未找到 yht_access_token — PM 会话可能已过期")
        print("请在 Chrome 中重新登录 pm.yyrd.com")
        return 1
    ok(f"yht_access_token 有效 (长度 {len(yht)})")
    ok("直连模式: 本机直接访问 pmf.yyrd.com (无需代理)")

    # Step 2: Generate config.json
    step(2, total_steps, "生成 config.json...")

    config = generate_config(cookies, None, None)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    ok(f"配置已写入 {CONFIG_PATH}")

    # Show config (masked)
    print(f"\n  配置内容:")
    print(f"    proxy_url:         {config['proxy_url']}")
    print(f"    proxy_user:        {config.get('proxy_user', '')}")
    print(f"    yht_access_token:  {config['pm_cookies']['yht_access_token'][:20]}...")
    print(f"    tenant_info:       {config['pm_cookies']['tenant_info']}")
    print(f"    default_analyst:   {config['default_analyst']}")

    # Step 3: Verify connection
    step(3, total_steps, "验证 PM 连接 (直连模式)...")

    result = subprocess.run(
        ["/usr/bin/python3", str(SCRIPT_DIR / "pm_insight.py"), "--test"],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if "[OK]" in line or "[FAIL]" in line:
            print(f"  {line}")

    if result.returncode == 0:
        print(f"\n{GREEN}{BOLD}配置完成!{RESET}")
        print(f"运行 {CYAN}--overview{RESET} 查看工作概览:")
        print(f"  /usr/bin/python3 {SCRIPT_DIR / 'pm_insight.py'} --overview\n")

        # Auto-show overview
        print(f"{BOLD}─── 工作概览 ───{RESET}\n")
        subprocess.run(
            ["/usr/bin/python3", str(SCRIPT_DIR / "pm_insight.py"), "--overview", "--format", "table"],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    else:
        print(f"\n{RED}连接验证失败{RESET}")
        for line in result.stderr.splitlines():
            print(f"  {line}")
        print("请检查代理和 cookies 配置")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
