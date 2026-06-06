"""
Create the first admin user — 委托 AuthService，确保密码 hash 与 users.id(TEXT) 与
运行时鉴权完全兼容（不再 raw sqlite + 自定义 hash，避免 seed 出来的账号登不上）。
Usage: python -m bootstrap.seed_admin [--data-dir /data] [--username admin] [--password ...]
"""

import argparse
import sys
from pathlib import Path


def _get_auth_service(data_dir: Path):
    db_path = data_dir / "sqlite" / "auth.db"
    secret_path = data_dir / "sqlite" / "app_auth.key"
    backend_dir = Path(__file__).resolve().parents[1]
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from auth_service import AuthService
    return AuthService(db_path=str(db_path), secret_path=str(secret_path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/data", type=Path)
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--display-name", default="")
    args = parser.parse_args()

    # 仅交互式 TTY 才提示；非交互（安装器/CI）只用 --args
    interactive = sys.stdin.isatty()
    username = args.username or (input("用户名 [admin]: ").strip() if interactive else "") or "admin"
    password = args.password or (input("密码: ").strip() if interactive else "")
    if not password:
        print("ERROR: 密码不能为空（非交互模式请用 --password）")
        sys.exit(1)
    display_name = args.display_name
    if not display_name and interactive:
        display_name = input(f"显示名 [{username}]: ").strip()
    display_name = display_name or username

    auth = _get_auth_service(args.data_dir)
    if not auth.has_users():
        auth.bootstrap_admin(username, password, display_name=display_name)
        print(f"[seed_admin] Admin 用户 '{username}' 创建成功")
        return
    try:
        auth.create_user(username, password, display_name=display_name, role="admin")
        print(f"[seed_admin] Admin 用户 '{username}' 创建成功")
    except ValueError as exc:
        # 用户名已存在等
        print(f"[seed_admin] 跳过：{exc}（用户 '{username}' 可能已存在）")


if __name__ == "__main__":
    main()
