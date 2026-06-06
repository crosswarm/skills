#!/usr/bin/env python3
"""
统一设置 admin 密码（本机 + QCL）。密码通过 getpass 交互输入，不经过命令行，不写日志。

用法：
  python scripts/set_admin_password.py           # 双端（默认）
  python scripts/set_admin_password.py --local   # 只本机
  python scripts/set_admin_password.py --qcl     # 只 QCL
"""
import sys, os, argparse, getpass, subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))


def update_local_admin(new_password: str) -> bool:
    import sqlite3
    from auth_service import get_auth_service
    svc = get_auth_service()
    conn = sqlite3.connect(svc.db_path)
    rows = conn.execute(
        "SELECT id, username FROM users WHERE role='admin' AND is_active=1"
    ).fetchall()
    if not rows:
        print("  ❌ 本机无管理员用户")
        conn.close()
        return False
    pw_hash = svc._hash_password(new_password)  # 实例方法
    for uid, uname in rows:
        conn.execute(
            "UPDATE users SET password_hash=?, updated_at=datetime('now') WHERE id=?",
            (pw_hash, uid),
        )
        print(f"  ✅ 本机 {uname}")
    conn.commit()
    conn.close()
    return True


def update_qcl_admin(new_password: str) -> bool:
    import base64
    remote_python = r'''
import sys, os, sqlite3
sys.path.insert(0, "/opt/ai-ticket/APP/backend")
os.chdir("/opt/ai-ticket/APP/backend")
from auth_service import get_auth_service
pw = sys.stdin.readline().rstrip("\n")
if not pw:
    print("ERR: empty password"); sys.exit(1)
svc = get_auth_service()
conn = sqlite3.connect(svc.db_path)
rows = conn.execute("SELECT id, username FROM users WHERE role='admin' AND is_active=1").fetchall()
if not rows:
    print("ERR: no admin"); sys.exit(1)
h = svc._hash_password(pw)
for uid, uname in rows:
    conn.execute("UPDATE users SET password_hash=?, updated_at=datetime('now') WHERE id=?", (h, uid))
    print(f"  [OK] QCL {uname}")
conn.commit(); conn.close()
'''
    # 用 base64 编码：脚本和密码一起（避免 stdin 被 ssh 吞）
    encoded_script = base64.b64encode(remote_python.encode()).decode()
    encoded_pwd = base64.b64encode(new_password.encode()).decode()
    try:
        result = subprocess.run(
            ["ssh", "-T", "qcl",
             f"echo {encoded_pwd} | base64 -d | python3 <(echo {encoded_script} | base64 -d)"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"  ❌ QCL 失败: {result.stderr or result.stdout}")
            return False
        print(result.stdout.rstrip())
        return True
    except Exception as e:
        print(f"  ❌ QCL 异常: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--qcl", action="store_true")
    args = parser.parse_args()
    both = not args.local and not args.qcl

    print("🔐 设置 admin 密码（输入不回显）")
    pw1 = getpass.getpass("新密码: ")
    if len(pw1) < 6:
        print("❌ 密码至少 6 位"); sys.exit(1)
    pw2 = getpass.getpass("确认密码: ")
    if pw1 != pw2:
        print("❌ 两次输入不一致"); sys.exit(1)

    if args.local or both:
        print("\n📍 本机:")
        update_local_admin(pw1)
    if args.qcl or both:
        print("\n☁️  QCL:")
        update_qcl_admin(pw1)
    print("\n✅ 完成")


if __name__ == "__main__":
    main()
