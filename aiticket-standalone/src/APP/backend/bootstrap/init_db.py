"""
Idempotent DB schema initializer. Safe to run on every startup.
Usage: python -m bootstrap.init_db [--data-dir /data]
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def init_auth_db(data_dir: Path) -> None:
    """委托 AuthService 建规范 auth schema（users/sessions/jira_bindings/pm_bindings/
    skill_tokens/device_tokens 等）。

    历史教训：init_db 曾手搓一套 users.id=INTEGER 的不兼容 schema，与 auth_service 的
    users.id=TEXT + is_active/is_demo 列冲突（CREATE IF NOT EXISTS 不会纠正已存在的表），
    导致登录/skill-token 鉴权静默失效。此处统一由 auth_service 自建，单一真相源。
    """
    db_path = data_dir / "auth.db"
    secret_path = data_dir / "app_auth.key"
    # 让 import auth_service 可用（cwd 通常已是 APP/backend；兜底加路径）
    backend_dir = Path(__file__).resolve().parents[1]
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from auth_service import AuthService

    AuthService(db_path=str(db_path), secret_path=str(secret_path))  # __init__ 建全部 schema
    print(f"[init_db] auth.db schema OK ({db_path})")


def init_jobmaster_db(data_dir: Path) -> None:
    db_path = data_dir / "jobmaster.db"
    con = sqlite3.connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                payload TEXT DEFAULT '{}',
                result TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        con.commit()
        print(f"[init_db] jobmaster.db OK ({db_path})")
    finally:
        con.close()


def init_dirs(data_dir: Path) -> None:
    # 只建 <HOME>/data 下真正使用的目录：sqlite(auth.db/jobmaster.db) + logs。
    # 不再造 chroma/chroma_kb/kb/reply_trainer 空目录——这些运行期数据实际落 src/APP/backend/data，
    # 在此造空目录会误导『数据在 <HOME>/data』（见 aiticket_paths 持久化说明）。
    for sub in ["sqlite", "logs"]:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    print(f"[init_db] data dirs OK ({data_dir})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/data", type=Path)
    args = parser.parse_args()
    data_dir = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    init_dirs(data_dir)
    init_auth_db(data_dir / "sqlite")
    init_jobmaster_db(data_dir / "sqlite")
    print("[init_db] 初始化完成")


if __name__ == "__main__":
    main()
