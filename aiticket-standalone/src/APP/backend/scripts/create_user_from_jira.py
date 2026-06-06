#!/usr/bin/env python3
"""
创建 aiticket 用户（从 Jira 拉 display_name + 随机密码 + 两端同步）

用法:
    python scripts/create_user_from_jira.py lihum lich
    python scripts/create_user_from_jira.py lihum --project YYZJ --module 用户权限 --module 审批流

流程:
    1. getpass 要求输入 qiangxiao 的密码（用作管理员认证 + 调用 Jira API）
    2. 从 qiangxiao 的 jira session binding 拿 JSESSIONID
    3. 对每个目标 username 调 Jira /rest/api/2/user?username=xxx 拉 displayName
    4. secrets.token_urlsafe(12) 生成随机密码
    5. 两端同步 (Mini + QCL via ssh+base64)
    6. 明文密码只在控制台展示一次，不写日志/不入 git
    7. 若指定 --project / --module，同步写入两端的 current_project / project_modules_json
"""
import sys, secrets, getpass, base64, subprocess, json, argparse
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from auth_service import get_auth_service
from jira_service import JiraService


def fetch_display_name(username: str, session_cookies: dict) -> str:
    """调 Jira API 拉真实中文名"""
    try:
        j = JiraService(session_cookies=session_cookies)
        users = j.search_users(username)
        for u in users:
            if u.get("name", "").lower() == username.lower():
                return u.get("displayName") or username
        # fallback: 直接用 username
        return username
    except Exception as e:
        print(f"  ⚠️ 拉取 Jira displayName 失败 ({username}): {e}")
        return username


def create_on_local(svc, username: str, password: str, display_name: str, creator_id: str):
    return svc.create_user(
        username=username, password=password,
        display_name=display_name, role="member", created_by=creator_id,
    )


def create_on_qcl(username: str, password: str, display_name: str,
                  project: str = None, module_json: str = None) -> bool:
    """通过 ssh 在 QCL 上创建同名用户 + 相同密码，可选同步 project/modules"""
    remote_script = r'''
import sys, os
sys.path.insert(0, "/opt/ai-ticket/APP/backend")
os.chdir("/opt/ai-ticket/APP/backend")
from auth_service import get_auth_service
svc = get_auth_service()
uname      = sys.stdin.readline().rstrip("\n")
pw         = sys.stdin.readline().rstrip("\n")
dn         = sys.stdin.readline().rstrip("\n")
project    = sys.stdin.readline().rstrip("\n") or None
module_json= sys.stdin.readline().rstrip("\n") or None
try:
    # 若已存在，改密码和 display_name（幂等）
    from sqlite3 import connect
    conn = connect(svc.db_path)
    existing = conn.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()
    conn.close()
    if existing:
        pw_hash = svc._hash_password(pw)
        from sqlite3 import connect as c2
        conn = c2(svc.db_path)
        conn.execute("UPDATE users SET password_hash=?, display_name=?, updated_at=datetime('now') WHERE id=?",
                     (pw_hash, dn, existing[0]))
        conn.commit(); conn.close()
        print(f"[QCL] updated {uname}")
    else:
        svc.create_user(username=uname, password=pw, display_name=dn, role="member")
        print(f"[QCL] created {uname}")
    if project:
        from sqlite3 import connect as c3
        conn = c3(svc.db_path)
        conn.execute("UPDATE users SET current_project=?, project_modules_json=?, updated_at=datetime('now') WHERE username=?",
                     (project, module_json, uname))
        conn.commit(); conn.close()
        print(f"[QCL] set project={project} modules={module_json}")
except Exception as e:
    print(f"[QCL] ERR: {e}")
    sys.exit(1)
'''
    script_b64 = base64.b64encode(remote_script.encode()).decode()
    # Five stdin lines: username, password, display_name, project (may be empty), module_json (may be empty)
    stdin_payload = (
        f"{username}\n"
        f"{password}\n"
        f"{display_name}\n"
        f"{project or ''}\n"
        f"{module_json or ''}\n"
    )
    try:
        r = subprocess.run(
            ["ssh", "-T", "qcl",
             f"python3 <(echo {script_b64}|base64 -d)"],
            input=stdin_payload,
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            print(f"  ❌ QCL 失败: {r.stderr or r.stdout}")
            return False
        print(f"  {r.stdout.strip()}")
        return True
    except Exception as e:
        print(f"  ❌ QCL 异常: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        prog="create_user_from_jira",
        description="从 Jira 拉 displayName 并在 Mini + QCL 两端创建/重置用户",
    )
    parser.add_argument("usernames", nargs="+", metavar="username",
                        help="要创建的 Jira 用户名，支持多个")
    parser.add_argument("--project", default=None, metavar="PROJECT_KEY",
                        help="为用户设置默认项目（如 YYZJ）；不传则保持系统默认（MYPROJECT）")
    parser.add_argument("--module", dest="modules", action="append", default=[],
                        metavar="MODULE",
                        help="为用户分配的模块权限，可重复使用（如 --module 用户权限 --module 审批流）")
    args = parser.parse_args()

    usernames = args.usernames
    project   = args.project
    modules   = args.modules  # list, may be empty

    print(f"📋 目标用户: {', '.join(usernames)}")
    if project:
        print(f"📁 默认项目: {project}")
    if modules:
        print(f"🧩 分配模块: {', '.join(modules)}")
    print("🔐 请输入管理员 qiangxiao 的密码（不回显）")
    admin_pw = getpass.getpass("密码: ")

    svc = get_auth_service()
    admin = svc.authenticate("qiangxiao", admin_pw)
    if not admin or admin.get("role") != "admin":
        print("❌ 认证失败或非管理员"); sys.exit(1)
    print(f"✅ 管理员认证通过: {admin['display_name']}")

    cookies = svc.get_jira_session_cookies(admin["id"])
    if not cookies:
        print("⚠️ qiangxiao 未绑定 Jira session → displayName 将使用 username (可稍后在系统里手动改)")

    # Build module_json for DB storage: {project: modules} or {} when no project
    module_json: str | None = None
    if project and modules:
        module_json = json.dumps({project: modules}, ensure_ascii=False)
    elif project:
        module_json = json.dumps({project: []}, ensure_ascii=False)

    results = []
    for uname in usernames:
        print(f"\n📍 处理 {uname}...")
        dn = fetch_display_name(uname, cookies) if cookies else uname
        print(f"  display_name: {dn}")
        pw = secrets.token_urlsafe(12)

        # 本机
        uid = None
        try:
            result = create_on_local(svc, uname, pw, dn, admin["id"])
            # create_user may return the new user dict or id
            if isinstance(result, dict):
                uid = result.get("id")
            print(f"  ✅ 本机已创建")
        except ValueError as e:
            print(f"  ⚠️ 本机: {e}（可能已存在，改密码中...）")
            import sqlite3
            conn = sqlite3.connect(svc.db_path)
            existing = conn.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()
            if existing:
                uid = existing[0]
                pw_hash = svc._hash_password(pw)
                conn.execute("UPDATE users SET password_hash=?, display_name=?, updated_at=datetime('now') WHERE id=?",
                             (pw_hash, dn, existing[0]))
                conn.commit()
                print(f"  ✅ 本机密码已重置")
            conn.close()

        # 若 uid 仍未拿到，查一次
        if uid is None:
            import sqlite3
            conn = sqlite3.connect(svc.db_path)
            row = conn.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()
            conn.close()
            if row:
                uid = row[0]

        # 本机 project / modules 写入
        if uid and project:
            try:
                svc.update_current_project(uid, project)
                print(f"  ✅ 本机 current_project → {project}")
            except Exception as e:
                print(f"  ⚠️ 本机 update_current_project 失败: {e}")
        if uid and project and module_json:
            try:
                svc.update_user_modules(uid, {project: modules})
                print(f"  ✅ 本机 project_modules → {module_json}")
            except Exception as e:
                print(f"  ⚠️ 本机 update_user_modules 失败: {e}")

        # QCL
        create_on_qcl(uname, pw, dn, project=project, module_json=module_json)

        results.append((uname, dn, pw))

    # 输出（明文，一次性）
    print("\n" + "=" * 70)
    print("  创建完成 — 请立即复制下列密码发给对应用户")
    print("  关闭终端后密码无法找回（数据库中只存 hash）")
    print("=" * 70)
    for uname, dn, pw in results:
        print(f"\n  用户名: {uname}")
        print(f"  显示名: {dn}")
        print(f"  密码  : {pw}")
        if project:
            print(f"  项目  : {project}")
        if modules:
            print(f"  模块  : {', '.join(modules)}")
    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
