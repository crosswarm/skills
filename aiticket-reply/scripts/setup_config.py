#!/usr/bin/env python3
"""
AITicket Reply Skill — 配置向导
产品管理与应用架构总体部 强骁, 2026

用法:
  python3 setup_config.py --setup              # 交互式配置（Fernet 机器绑定加密）
  python3 setup_config.py --login              # 登录 QCL 账号并存储 device token
  python3 setup_config.py --whoami             # 验证当前登录状态
  python3 setup_config.py --logout             # 注销 device token
  python3 setup_config.py --test               # 测试连接（验证最新 API 端点）
  python3 setup_config.py --get-url            # 输出后端 URL（供 curl 使用）
  python3 setup_config.py --get-auth-headers   # 输出认证 Header（供 curl 使用）
  python3 setup_config.py --rotate-key         # 机器迁移时重新加密
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config" / "config.json"
DEFAULT_URL = "http://ticket.spux.cn"
_SALT = b"aiticket-reply-v1"


# ── 机器绑定密钥派生 ─────────────────────────────────────────────────────────

def _machine_key() -> bytes:
    """从机器标识（hostname + home 目录）派生 Fernet 密钥，无需外部密钥文件。"""
    machine_id = f"{socket.gethostname()}:{os.path.expanduser('~')}"
    raw = hashlib.pbkdf2_hmac("sha256", machine_id.encode(), _SALT, 100_000)
    return base64.urlsafe_b64encode(raw)


def _machine_fingerprint() -> str:
    """生成机器指纹用于 device token 绑定（与密钥派生材料一致）。"""
    machine_id = f"{socket.gethostname()}:{os.path.expanduser('~')}"
    return hashlib.sha256((machine_id + "aiticket-fp-v1").encode()).hexdigest()[:32]


def _encrypt(payload: dict) -> str:
    if not _HAS_CRYPTO:
        raise RuntimeError("需要 cryptography 库：pip install cryptography")
    plaintext = json.dumps(payload, ensure_ascii=False)
    return Fernet(_machine_key()).encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> dict:
    if not _HAS_CRYPTO:
        print("✗ 需要 cryptography 库：pip install cryptography", file=sys.stderr)
        sys.exit(1)
    try:
        plaintext = Fernet(_machine_key()).decrypt(ciphertext.encode()).decode()
        return json.loads(plaintext)
    except (InvalidToken, Exception):
        print(
            "✗ 解密失败 — 配置文件绑定到当前机器，换机器后请重新运行 --setup\n"
            "  如需迁移，在新机器上运行: python3 setup_config.py --rotate-key",
            file=sys.stderr,
        )
        sys.exit(1)


# ── 配置读写 ─────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        wrapper = json.load(f)
    if wrapper.get("encrypted"):
        return _decrypt(wrapper["ciphertext"])
    # 兼容旧版明文格式（自动升级）
    return wrapper


def _save_config(payload: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_CRYPTO:
        wrapper = {"version": 1, "encrypted": True, "ciphertext": _encrypt(payload)}
    else:
        wrapper = payload
        print("⚠️  cryptography 未安装，配置以明文保存（建议：pip install cryptography）")
    with open(CONFIG_PATH, "w") as f:
        json.dump(wrapper, f, ensure_ascii=False, indent=2)


# ── HTTP 工具 ─────────────────────────────────────────────────────────────────

def _api_post(base_url: str, path: str, payload: dict) -> dict:
    import json as _json
    url = f"{base_url}{path}"
    data = _json.dumps(payload).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urlopen(req, context=ctx, timeout=15) as resp:
            body = resp.read().decode()
            try:
                return _json.loads(body)
            except Exception:
                return {"_raw": body[:200]}
    except HTTPError as e:
        return {"error": e.read().decode()[:300], "status_code": e.code}
    except Exception as e:
        return {"error": str(e)}


def _api_get(base_url: str, path: str) -> dict:
    url = f"{base_url}{path}"
    req = Request(url, headers={"Accept": "application/json"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urlopen(req, context=ctx, timeout=10) as resp:
            body = resp.read().decode()
            try:
                return json.loads(body)
            except Exception:
                return {"_raw": body[:200]}
    except HTTPError as e:
        return {"error": e.read().decode()[:200], "status_code": e.code}
    except Exception as e:
        return {"error": str(e)}


# ── 子命令实现 ────────────────────────────────────────────────────────────────

def setup_config() -> None:
    print("\n🔧 AITicket Reply Skill — 配置向导")
    print("─" * 40)
    print("  配置通过 Fernet 机器绑定加密存储，无法跨机器复用。\n")

    existing = _load_config() if CONFIG_PATH.exists() else {}
    if existing:
        print(f"  已有配置文件: {CONFIG_PATH}")
        ans = input("  是否覆盖现有配置？[y/N] ").strip().lower()
        if ans != "y":
            print("  已取消")
            return

    default_url = existing.get("backend_url", DEFAULT_URL)
    backend_url = input(f"  后端地址 [{default_url}]: ").strip() or default_url
    backend_url = backend_url.rstrip("/")

    default_project = existing.get("default_project", "LCZX")
    project = input(f"  默认项目 [{default_project}]: ").strip() or default_project

    # 连通性预检（打轻量健康检查端点）
    print(f"\n  正在连接 {backend_url}/health ...")
    result = _api_get(backend_url, "/health")
    if "error" in result:
        print(f"  ⚠️  连接异常: {str(result.get('error', ''))[:100]}")
        ans = input("  仍然保存配置？[y/N] ").strip().lower()
        if ans != "y":
            print("  已取消")
            return
    else:
        print("  ✓ 服务可达")

    payload = {"backend_url": backend_url, "default_project": project}
    _save_config(payload)

    status = "✓ Fernet 机器绑定加密" if _HAS_CRYPTO else "⚠️  明文（建议安装 cryptography）"
    print(f"\n  ✓ 配置已保存: {CONFIG_PATH}")
    print(f"  ✓ 加密状态: {status}")
    print(f"  ✓ 后端地址: {backend_url}")
    print(f"  ✓ 默认项目: {project}")
    print(f"\n  现在可以使用 AITicket Reply Skill 了！\n")


def test_connection() -> None:
    if not CONFIG_PATH.exists():
        print("  ✗ 未找到配置文件，请先运行 --setup")
        sys.exit(1)

    cfg = _load_config()
    base_url = cfg.get("backend_url", DEFAULT_URL)
    print(f"\n  正在测试 {base_url} ...\n")

    # 端点 1：模块覆盖度（最新路由）
    r = _api_get(base_url, "/api/reply/module-coverage?module=" + quote("流程中心"))
    if "error" not in r:
        level = r.get("coverage_level", r.get("data", {}).get("coverage_level", "?"))
        print(f"  ✓ /api/reply/module-coverage  （覆盖度: {level}）")
    else:
        print(f"  ✗ /api/reply/module-coverage  错误: {str(r.get('error', ''))[:80]}")

    # 端点 2：工单语义搜索（主力搜索端点）
    r = _api_get(base_url, "/api/board/search?q=test&top_k=1&min_score=0.3")
    if isinstance(r, dict) and r.get("status") == "success":
        count = len(r.get("results", []))
        print(f"  ✓ /api/board/search           （召回 {count} 条）")
    else:
        print(f"  ✗ /api/board/search           状态异常: {str(r)[:80]}")

    # 端点 3：知识库搜索
    r = _api_get(base_url, "/api/kb/search?q=test&top_k=1")
    if isinstance(r, dict) and ("results" in r or "items" in r or "data" in r or r.get("status") == "success"):
        print(f"  ✓ /api/kb/search              就绪")
    else:
        print(f"  ⚠️ /api/kb/search             状态: {str(r)[:80]}")

    print("\n  连接测试完成！\n")


def get_url() -> None:
    if not CONFIG_PATH.exists():
        print("NEED_SETUP", end="")
        sys.exit(1)
    cfg = _load_config()
    print(cfg.get("backend_url", DEFAULT_URL), end="")


def rotate_key() -> None:
    """机器迁移后重新用新机器密钥加密配置。"""
    if not CONFIG_PATH.exists():
        print("  ✗ 未找到配置文件，请先运行 --setup")
        sys.exit(1)
    if not _HAS_CRYPTO:
        print("  ✗ 需要 cryptography 库：pip install cryptography")
        sys.exit(1)

    print("\n  🔄 密钥轮换 — 用当前机器密钥重新加密配置")
    print("  请输入当前配置的明文后端地址（用于验证）：")
    backend_url = input("  后端地址: ").strip().rstrip("/")
    default_project = input("  默认项目 [LCZX]: ").strip() or "LCZX"

    payload = {"backend_url": backend_url, "default_project": default_project}
    _save_config(payload)
    print(f"  ✓ 已用当前机器密钥重新加密并保存: {CONFIG_PATH}\n")


def login_device(username: str = "", password: str = "") -> None:
    """登录 QCL 账号，签发 device token 并加密存储到 config。"""
    if not CONFIG_PATH.exists():
        print("  ✗ 未找到配置文件，请先运行 --setup", file=sys.stderr)
        sys.exit(1)
    cfg = _load_config()
    base_url = cfg.get("backend_url", DEFAULT_URL)

    if not username:
        username = input("  QCL 用户名: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("  QCL 密码: ")

    fp = _machine_fingerprint()
    result = _api_post(base_url, "/api/auth/device-token", {
        "username": username,
        "password": password,
        "client_fingerprint": fp,
        "label": f"aiticket-reply-skill@{socket.gethostname()}",
    })

    if "error" in result or not result.get("token"):
        print(f"  ✗ 登录失败: {result.get('error', result)}", file=sys.stderr)
        sys.exit(1)

    cfg["device_token"] = result["token"]
    cfg["device_fingerprint"] = fp
    _save_config(cfg)
    os.chmod(str(CONFIG_PATH), 0o600)
    print(f"  ✓ 已登录: {result.get('display_name', username)}")
    print(f"  ✓ Device token 已加密保存（机器绑定）")


def whoami_device(silent: bool = False) -> bool:
    """验证当前 device token 是否有效。返回 True=已登录，False=未登录。"""
    if not CONFIG_PATH.exists():
        if not silent:
            print("未登录（未找到配置文件）", end="")
        return False
    cfg = _load_config()
    token = cfg.get("device_token", "")
    fp = cfg.get("device_fingerprint", "")
    if not token or not fp:
        if not silent:
            print("未登录", end="")
        return False
    base_url = cfg.get("backend_url", DEFAULT_URL)
    result = _api_post(base_url, "/api/auth/device-verify", {
        "token": token,
        "client_fingerprint": fp,
    })
    if result.get("ok"):
        if not silent:
            print(f"已登录: {result.get('display_name', '')}", end="")
        return True
    else:
        if not silent:
            print("未登录（token 已失效，请重新登录）", end="")
        return False


def logout_device() -> None:
    """注销 device token。"""
    if not CONFIG_PATH.exists():
        print("  未找到配置文件")
        return
    cfg = _load_config()
    token = cfg.get("device_token", "")
    fp = cfg.get("device_fingerprint", "")
    if token and fp:
        base_url = cfg.get("backend_url", DEFAULT_URL)
        _api_post(base_url, "/api/auth/device-revoke", {
            "token": token,
            "client_fingerprint": fp,
        })
    cfg.pop("device_token", None)
    cfg.pop("device_fingerprint", None)
    _save_config(cfg)
    print("  ✓ 已注销")


def get_auth_headers() -> None:
    """输出认证 Header 供 curl 使用（每行一个 Header）。"""
    fp = _machine_fingerprint()
    if not CONFIG_PATH.exists():
        print(f"X-AiTicket-Client-Id: {fp}")
        return
    cfg = _load_config()
    token = cfg.get("device_token", "")
    if token:
        print(f"X-AiTicket-Token: {token}")
    print(f"X-AiTicket-Client-Id: {fp}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AITicket Reply Skill 配置")
    parser.add_argument("--setup", action="store_true", help="交互式配置向导")
    parser.add_argument("--login", action="store_true", help="登录 QCL 账号")
    parser.add_argument("--whoami", action="store_true", help="验证当前登录状态")
    parser.add_argument("--logout", action="store_true", help="注销 device token")
    parser.add_argument("--test", action="store_true", help="测试 API 端点连通性")
    parser.add_argument("--get-url", action="store_true", help="输出后端 URL（供 curl 使用）")
    parser.add_argument("--get-auth-headers", action="store_true", help="输出认证 Header（供 curl 使用）")
    parser.add_argument("--rotate-key", action="store_true", help="机器迁移后重新加密配置")
    args = parser.parse_args()

    if args.setup:
        setup_config()
    elif args.login:
        login_device()
    elif args.whoami:
        whoami_device(silent=False)
        print()
    elif args.logout:
        logout_device()
    elif args.test:
        test_connection()
    elif args.get_url:
        get_url()
    elif getattr(args, "get_auth_headers"):
        get_auth_headers()
    elif args.rotate_key:
        rotate_key()
    else:
        parser.print_help()
