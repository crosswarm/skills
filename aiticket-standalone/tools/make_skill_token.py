#!/usr/bin/env python3
"""为 admin 生成 skill token，写入 <HOME>/config/env.json 的 skill_token 字段。

供 MCP server（AITICKET_SKILL_TOKEN / env.json）与浏览器扩展鉴权使用。
用法：python tools/make_skill_token.py [--home DIR] [--username admin] [--label mcp]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aiticket_paths as P  # noqa: E402


def _find_user_id(db_path: Path, username: str) -> str | None:
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row:
            return row[0]
        # 退而求其次：第一个 admin
        row = con.execute(
            "SELECT id FROM users WHERE role='admin' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="make_skill_token")
    ap.add_argument("--home", default="")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--label", default="mcp")
    args = ap.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else P.default_home()
    db_path = P.auth_db_path(home)
    secret_path = P.auth_secret_path(home)
    if not db_path.exists():
        print(f"ERROR: auth.db 不存在：{db_path}（请先 install）", file=sys.stderr)
        return 1

    uid = _find_user_id(db_path, args.username)
    if not uid:
        print(f"ERROR: 找不到用户 '{args.username}' 或任何 admin", file=sys.stderr)
        return 1

    os.environ["APP_AUTH_DB_PATH"] = str(db_path)
    os.environ["APP_AUTH_SECRET_PATH"] = str(secret_path)
    sys.path.insert(0, str(P.backend_dir(home)))
    from auth_service import AuthService

    auth = AuthService(db_path=str(db_path), secret_path=str(secret_path))
    rec = auth.create_skill_token(uid, label=args.label)
    token = rec.get("token") or rec.get("raw") or rec.get("skill_token") or ""
    if not token:
        # 兜底取首个字符串值
        token = next((v for v in rec.values() if isinstance(v, str) and len(v) > 20), "")
    if not token:
        print("ERROR: 未能取得 token 明文", file=sys.stderr)
        return 1

    # 写入 env.json
    ej = P.env_json_path(home)
    data = {}
    try:
        if ej.exists():
            data = json.loads(ej.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["skill_token"] = token
    ej.parent.mkdir(parents=True, exist_ok=True)
    ej.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
