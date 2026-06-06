import base64
import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cryptography.fernet import Fernet


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.environ.get("APP_AUTH_DB_PATH", os.path.join(BASE_DIR, "data", "app_auth.db"))
DEFAULT_SECRET_PATH = os.environ.get("APP_AUTH_SECRET_PATH", os.path.join(BASE_DIR, "data", "app_auth.key"))
VALID_ROLES = {"admin", "member"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: Optional[datetime] = None) -> str:
    return (value or _utcnow()).isoformat()


class AuthService:
    def __init__(
        self,
        db_path: Optional[str] = None,
        secret_path: Optional[str] = None,
        session_ttl_hours: int = 24,
    ):
        self.db_path = db_path or os.environ.get("APP_AUTH_DB_PATH", DEFAULT_DB_PATH)
        self.secret_path = secret_path or os.environ.get("APP_AUTH_SECRET_PATH", DEFAULT_SECRET_PATH)
        self.session_ttl_hours = session_ttl_hours

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.secret_path), exist_ok=True)

        self._fernet = Fernet(self._load_or_create_secret())
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _load_or_create_secret(self) -> bytes:
        if os.path.exists(self.secret_path):
            with open(self.secret_path, "rb") as handle:
                return handle.read().strip()

        secret = Fernet.generate_key()
        with open(self.secret_path, "wb") as handle:
            handle.write(secret)
        return secret

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_by TEXT,
                    current_project TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    user_agent TEXT NOT NULL DEFAULT '',
                    ip TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS jira_bindings (
                    user_id TEXT PRIMARY KEY,
                    jira_username TEXT NOT NULL,
                    encrypted_token TEXT NOT NULL,
                    jira_base_url TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    auth_type TEXT NOT NULL DEFAULT 'basic_auth',
                    encrypted_xsrf_token TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS pm_bindings (
                    user_id TEXT PRIMARY KEY,
                    encrypted_token TEXT NOT NULL,
                    tenant_info TEXT NOT NULL DEFAULT '0000',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT
                );
                """
            )

            # 幂等迁移：为老库添加 jira_bindings 的 auth_type / encrypted_xsrf_token 列
            for migration_sql in (
                "ALTER TABLE jira_bindings ADD COLUMN auth_type TEXT NOT NULL DEFAULT 'basic_auth'",
                "ALTER TABLE jira_bindings ADD COLUMN encrypted_xsrf_token TEXT NOT NULL DEFAULT ''",
            ):
                try:
                    conn.execute(migration_sql)
                except sqlite3.OperationalError as exc:
                    # 列已存在时 SQLite 报 "duplicate column name" — 安全忽略
                    if "duplicate column name" not in str(exc).lower():
                        raise

            # 幂等迁移：users 表添加 is_demo 标志位（演示账号）
            try:
                conn.execute("ALTER TABLE users ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

            # 幂等迁移：users 表添加 current_project（用户当前工作项目）
            try:
                conn.execute("ALTER TABLE users ADD COLUMN current_project TEXT DEFAULT ''")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

            # 幂等迁移：users 表添加 project_modules_json（用户各项目领域模块归属）
            try:
                conn.execute("ALTER TABLE users ADD COLUMN project_modules_json TEXT NOT NULL DEFAULT '{}'")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

            # 用户级 agent 昵称覆盖表
            conn.execute(
                """CREATE TABLE IF NOT EXISTS user_agent_nicknames (
                  user_id    TEXT NOT NULL,
                  agent_code TEXT NOT NULL,
                  nickname   TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (user_id, agent_code),
                  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )"""
            )

            # Skill 设备令牌表（机器绑定，不可跨机迁移）
            conn.execute(
                """CREATE TABLE IF NOT EXISTS device_tokens (
                  id                 TEXT PRIMARY KEY,
                  user_id            TEXT NOT NULL,
                  client_fingerprint TEXT NOT NULL,
                  token_hash         TEXT NOT NULL UNIQUE,
                  label              TEXT NOT NULL DEFAULT '',
                  created_at         TEXT NOT NULL,
                  last_used_at       TEXT NOT NULL,
                  revoked            INTEGER NOT NULL DEFAULT 0,
                  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                  UNIQUE (user_id, client_fingerprint)
                )"""
            )

            # Skill 令牌表（headless 调用方：MCP server / 瘦客户端，Bearer / X-Skill-Token）
            # 注意：user_id 为 TEXT，与 users.id（TEXT）一致；旧 init_db 曾误建 INTEGER 版
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_tokens (
                  token_hash   TEXT PRIMARY KEY,
                  user_id      TEXT NOT NULL,
                  label        TEXT NOT NULL DEFAULT 'skill',
                  created_at   TEXT NOT NULL,
                  expires_at   TEXT,
                  last_used_at TEXT,
                  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )"""
            )

            # 匿名配额表（每日限制，按 client_id 和 IP 双重计数）
            conn.execute(
                """CREATE TABLE IF NOT EXISTS anon_quota (
                  dim_key  TEXT NOT NULL,
                  day_key  TEXT NOT NULL,
                  count    INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (dim_key, day_key)
                )"""
            )

            # API 请求日志表（智能回复类接口）
            conn.execute(
                """CREATE TABLE IF NOT EXISTS request_logs (
                  id           TEXT PRIMARY KEY,
                  ts           TEXT NOT NULL,
                  interface    TEXT NOT NULL,
                  user_id      TEXT NOT NULL DEFAULT '',
                  display_name TEXT NOT NULL DEFAULT '',
                  is_anon      INTEGER NOT NULL DEFAULT 1,
                  client_id    TEXT NOT NULL DEFAULT '',
                  client_ip    TEXT NOT NULL DEFAULT '',
                  project      TEXT NOT NULL DEFAULT '',
                  issue_key    TEXT NOT NULL DEFAULT '',
                  query_text   TEXT NOT NULL DEFAULT ''
                )"""
            )

            conn.execute(
                """CREATE TABLE IF NOT EXISTS project_index_jobs (
                  project_key TEXT PRIMARY KEY,
                  status      TEXT NOT NULL DEFAULT 'pending',
                  total       INTEGER DEFAULT 0,
                  done        INTEGER DEFAULT 0,
                  started_at  INTEGER,
                  finished_at INTEGER,
                  error       TEXT DEFAULT ''
                )"""
            )

    def _hash_password(self, password: str) -> str:
        salt = secrets.token_bytes(16)
        iterations = 200_000
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return "pbkdf2_sha256${}${}${}".format(
            iterations,
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )

    def _verify_password(self, password: str, password_hash: str) -> bool:
        try:
            algorithm, iterations, salt_b64, digest_b64 = password_hash.split("$", 3)
        except ValueError:
            return False

        if algorithm != "pbkdf2_sha256":
            return False

        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.b64decode(salt_b64),
            int(iterations),
        )
        return secrets.compare_digest(base64.b64encode(digest).decode("ascii"), digest_b64)

    def _sanitize_user_row(self, row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        keys = row.keys()
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "is_active": bool(row["is_active"]),
            "is_demo": bool(row["is_demo"]) if "is_demo" in keys else False,
            "current_project": row["current_project"] if "current_project" in keys else "MYPROJECT",
            "project_modules": json.loads(row["project_modules_json"]) if "project_modules_json" in keys and row["project_modules_json"] else {},
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "created_by": row["created_by"],
        }

    def update_current_project(self, user_id: str, project_key: str) -> bool:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE users SET current_project = ?, updated_at = ? WHERE id = ?",
                (project_key, now, user_id),
            )
            return result.rowcount > 0

    def update_user_modules(self, user_id: str, project_modules: dict) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE users SET project_modules_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(project_modules, ensure_ascii=False), now, user_id),
            )
            return result.rowcount > 0

    def has_users(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            return bool(row["count"])

    def bootstrap_admin(self, username: str, password: str, display_name: str = "管理员") -> dict[str, Any]:
        if self.has_users():
            raise ValueError("Bootstrap already completed")
        return self.create_user(username, password, display_name=display_name, role="admin", created_by=None)

    def create_user(
        self,
        username: str,
        password: str,
        display_name: str = "",
        role: str = "member",
        is_demo: bool = False,
        created_by: Optional[str] = None,
        project_modules: Optional[dict] = None,
    ) -> dict[str, Any]:
        username = (username or "").strip()
        display_name = (display_name or "").strip() or username
        role = (role or "member").strip()
        password = password or ""

        if not username:
            raise ValueError("Username is required")
        if not password:
            raise ValueError("Password is required")
        if role not in VALID_ROLES:
            raise ValueError("Invalid role")

        now = _isoformat()
        user_id = secrets.token_hex(16)
        password_hash = self._hash_password(password)

        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users (id, username, password_hash, display_name, role, is_demo, created_at, updated_at, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, username, password_hash, display_name, role, int(is_demo), now, now, created_by),
                )
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError("Username already exists") from exc

        result = self._sanitize_user_row(row)
        if project_modules:
            self.update_user_modules(user_id, project_modules)
            result["project_modules"] = project_modules
        return result

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [self._sanitize_user_row(row) for row in rows]

    def authenticate(self, username: str, password: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", ((username or "").strip(),)).fetchone()

        if row is None or not self._verify_password(password or "", row["password_hash"]):
            return None
        return self._sanitize_user_row(row)

    def create_session(self, user_id: str, user_agent: str = "", ip: str = "", ttl_hours: int | None = None) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = _utcnow()
        effective_ttl = ttl_hours if ttl_hours is not None else self.session_ttl_hours
        expires_at = now + timedelta(hours=effective_ttl)

        with self._connect() as conn:
            # demo 账号：超出 5 个并发 session 时 FIFO 淘汰最旧
            user_row = conn.execute("SELECT is_demo FROM users WHERE id=?", (user_id,)).fetchone()
            if user_row and user_row["is_demo"]:
                active = [r["id"] for r in conn.execute(
                    "SELECT id FROM sessions WHERE user_id=? AND expires_at>? ORDER BY created_at ASC",
                    (user_id, _isoformat(now)),
                ).fetchall()]
                while len(active) >= 5:
                    conn.execute("DELETE FROM sessions WHERE id=?", (active.pop(0),))

            conn.execute(
                """
                INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at, last_seen_at, user_agent, ip)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    secrets.token_hex(16),
                    user_id,
                    token_hash,
                    _isoformat(now),
                    _isoformat(expires_at),
                    _isoformat(now),
                    user_agent,
                    ip,
                ),
            )

        return token

    def get_user_by_session(self, session_token: str) -> Optional[dict[str, Any]]:
        if not session_token:
            return None

        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        now = _utcnow()
        now_iso = _isoformat(now)

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT users.*
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                  AND sessions.expires_at > ?
                  AND users.is_active = 1
                """,
                (token_hash, now_iso),
            ).fetchone()
            if row is None:
                conn.execute("DELETE FROM sessions WHERE token_hash = ? OR expires_at <= ?", (token_hash, now_iso))
                return None
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now_iso, token_hash),
            )

        return self._sanitize_user_row(row)

    def delete_session(self, session_token: str):
        if not session_token:
            return
        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def update_user_role(self, user_id: str, role: str) -> dict[str, Any]:
        if role not in VALID_ROLES:
            raise ValueError("Invalid role")

        now = _isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                (role, now, user_id),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise ValueError("User not found")
        return self._sanitize_user_row(row)

    def upsert_jira_binding(
        self,
        user_id: str,
        jira_username: str,
        jira_api_token: str,
        jira_base_url: str = "",
    ) -> dict[str, Any]:
        jira_username = (jira_username or "").strip()
        jira_api_token = jira_api_token or ""
        jira_base_url = (jira_base_url or "").strip()

        if not jira_username:
            raise ValueError("Jira username is required")
        if not jira_api_token:
            raise ValueError("Jira API token is required")

        encrypted_token = self._fernet.encrypt(jira_api_token.encode("utf-8")).decode("ascii")
        now = _isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jira_bindings (user_id, jira_username, encrypted_token, jira_base_url, updated_at, auth_type, encrypted_xsrf_token)
                VALUES (?, ?, ?, ?, ?, 'basic_auth', '')
                ON CONFLICT(user_id) DO UPDATE SET
                    jira_username = excluded.jira_username,
                    encrypted_token = excluded.encrypted_token,
                    jira_base_url = excluded.jira_base_url,
                    updated_at = excluded.updated_at,
                    auth_type = 'basic_auth',
                    encrypted_xsrf_token = ''
                """,
                (user_id, jira_username, encrypted_token, jira_base_url, now),
            )

        return self.get_jira_binding_summary(user_id)

    def upsert_jira_session_binding(
        self,
        user_id: str,
        jsessionid: str,
        xsrf_token: str = "",
        jira_base_url: str = "",
    ) -> dict[str, Any]:
        """session_cookie 模式绑定：JSESSIONID 加密存 encrypted_token，xsrf 存 encrypted_xsrf_token。

        - auth_type 置为 'session_cookie'
        - jira_username 保留为空字符串（使用 session cookie 认证时不需要用户名）
        """
        jsessionid = (jsessionid or "").strip()
        xsrf_token = (xsrf_token or "").strip()
        jira_base_url = (jira_base_url or "").strip()

        if not jsessionid:
            raise ValueError("JSESSIONID is required")

        encrypted_jsession = self._fernet.encrypt(jsessionid.encode("utf-8")).decode("ascii")
        encrypted_xsrf = (
            self._fernet.encrypt(xsrf_token.encode("utf-8")).decode("ascii") if xsrf_token else ""
        )
        now = _isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jira_bindings (user_id, jira_username, encrypted_token, jira_base_url, updated_at, auth_type, encrypted_xsrf_token)
                VALUES (?, '', ?, ?, ?, 'session_cookie', ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    jira_username = '',
                    encrypted_token = excluded.encrypted_token,
                    jira_base_url = excluded.jira_base_url,
                    updated_at = excluded.updated_at,
                    auth_type = 'session_cookie',
                    encrypted_xsrf_token = excluded.encrypted_xsrf_token
                """,
                (user_id, encrypted_jsession, jira_base_url, now, encrypted_xsrf),
            )

        return self.get_jira_binding_summary(user_id)

    def get_jira_binding_summary(self, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT jira_username, jira_base_url, updated_at, encrypted_token, auth_type FROM jira_bindings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return {
                "jira_username": "",
                "jira_base_url": "",
                "has_token": False,
                "updated_at": "",
                "auth_type": "basic_auth",
            }
        return {
            "jira_username": row["jira_username"],
            "jira_base_url": row["jira_base_url"],
            "has_token": bool(row["encrypted_token"]),
            "updated_at": row["updated_at"],
            "auth_type": row["auth_type"] or "basic_auth",
        }

    def get_jira_binding_credentials(self, user_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT jira_username, jira_base_url, encrypted_token, auth_type FROM jira_bindings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None

        return {
            "jira_username": row["jira_username"],
            "jira_base_url": row["jira_base_url"],
            "jira_api_token": self._fernet.decrypt(row["encrypted_token"].encode("ascii")).decode("utf-8"),
            "auth_type": row["auth_type"] or "basic_auth",
        }

    def get_jira_session_cookies(self, user_id: str) -> Optional[dict[str, Any]]:
        """session_cookie 模式下返回 {JSESSIONID, xsrf_token}。

        如果未绑定、或绑定是 basic_auth 模式，返回 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT encrypted_token, encrypted_xsrf_token, auth_type, jira_base_url FROM jira_bindings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        if (row["auth_type"] or "basic_auth") != "session_cookie":
            return None

        try:
            jsession = self._fernet.decrypt(row["encrypted_token"].encode("ascii")).decode("utf-8")
        except Exception:
            return None

        xsrf = ""
        enc_xsrf = row["encrypted_xsrf_token"] or ""
        if enc_xsrf:
            try:
                xsrf = self._fernet.decrypt(enc_xsrf.encode("ascii")).decode("utf-8")
            except Exception:
                xsrf = ""

        return {
            "JSESSIONID": jsession,
            "xsrf_token": xsrf,
            "jira_base_url": row["jira_base_url"] or "",
        }

    # ------------------------------------------------------------------
    # PM 绑定
    # ------------------------------------------------------------------

    def upsert_pm_binding(self, user_id: str, pm_token: str, tenant_info: str = "0000") -> dict[str, Any]:
        pm_token = pm_token or ""
        tenant_info = (tenant_info or "0000").strip()

        if not pm_token:
            raise ValueError("PM token is required")

        encrypted = self._fernet.encrypt(pm_token.encode("utf-8")).decode("ascii")
        now = _isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pm_bindings (user_id, encrypted_token, tenant_info, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    encrypted_token = excluded.encrypted_token,
                    tenant_info = excluded.tenant_info,
                    updated_at = excluded.updated_at
                """,
                (user_id, encrypted, tenant_info, now),
            )

        return {"user_id": user_id, "tenant_info": tenant_info, "has_token": True, "updated_at": now}

    def get_pm_binding_token(self, user_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT encrypted_token, tenant_info, updated_at FROM pm_bindings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None

        return {
            "token": self._fernet.decrypt(row["encrypted_token"].encode("ascii")).decode("utf-8"),
            "tenant_info": row["tenant_info"],
            "updated_at": row["updated_at"],
        }

    def get_pm_binding_summary(self, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tenant_info, updated_at FROM pm_bindings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return {"has_binding": False, "tenant_info": None, "updated_at": None}
        return {"has_binding": True, "tenant_info": row["tenant_info"], "updated_at": row["updated_at"]}

    def set_system_setting(self, key: str, value: Any, updated_by: Optional[str] = None):
        now = _isoformat()
        payload = json.dumps(value, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO system_settings (key, value_json, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (key, payload, now, updated_by),
            )

    def get_system_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM system_settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value_json"])

    def list_system_settings(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value_json FROM system_settings ORDER BY key ASC").fetchall()
        return {row["key"]: json.loads(row["value_json"]) for row in rows}

    def log_audit(
        self,
        user_id: Optional[str],
        action: str,
        target_type: str = "",
        target_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ):
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (id, user_id, action, target_type, target_id, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    secrets.token_hex(16),
                    user_id,
                    action,
                    target_type,
                    target_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    _isoformat(),
                ),
            )

    def list_audit_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, action, target_type, target_id, metadata_json, created_at
                FROM audit_logs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "action": row["action"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]


    # ── 用户级 agent 昵称 ───────────────────────────────────────────────

    def get_user_nicknames(self, user_id: str) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_code, nickname FROM user_agent_nicknames WHERE user_id=?",
                (user_id,),
            ).fetchall()
        return {row["agent_code"]: row["nickname"] for row in rows}

    def set_user_nickname(self, user_id: str, agent_code: str, nickname: str) -> None:
        nickname = (nickname or "").strip()
        if not nickname or len(nickname) > 32:
            raise ValueError("昵称长度须在 1-32 字符之间")
        if any(c in nickname for c in ("<", ">", "&", '"', "'")):
            raise ValueError("昵称不可包含 HTML 特殊字符")
        now = _isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO user_agent_nicknames (user_id, agent_code, nickname, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(user_id, agent_code)
                   DO UPDATE SET nickname=excluded.nickname, updated_at=excluded.updated_at""",
                (user_id, agent_code, nickname, now),
            )

    def delete_user_nickname(self, user_id: str, agent_code: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM user_agent_nicknames WHERE user_id=? AND agent_code=?",
                (user_id, agent_code),
            )

    # ── Skill Bearer token ──────────────────────────────────────────────

    def create_skill_token(self, user_id: str, label: str = "skill", ttl_days: int = 90) -> dict[str, Any]:
        raw = secrets.token_urlsafe(40)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        now = _utcnow()
        expires_at = _isoformat(now + timedelta(days=ttl_days))
        created_at = _isoformat(now)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM skill_tokens WHERE user_id=? AND label=?",
                (user_id, label),
            )
            conn.execute(
                """INSERT INTO skill_tokens (token_hash, user_id, label, created_at, expires_at)
                   VALUES (?,?,?,?,?)""",
                (token_hash, user_id, label, created_at, expires_at),
            )
        return {"token": raw, "label": label, "expires_at": expires_at}

    def get_user_by_skill_token(self, raw_token: str) -> Optional[dict[str, Any]]:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now = _isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT u.*, st.expires_at as _st_exp FROM skill_tokens st
                   JOIN users u ON u.id = st.user_id
                   WHERE st.token_hash=? AND u.is_active=1""",
                (token_hash,),
            ).fetchone()
            if not row:
                return None
            if row["_st_exp"] and row["_st_exp"] < now:
                return None
            conn.execute(
                "UPDATE skill_tokens SET last_used_at=? WHERE token_hash=?",
                (now, token_hash),
            )
        return self._sanitize_user_row(row)

    def list_skill_tokens(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT label, created_at, expires_at, last_used_at FROM skill_tokens WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [{"label": r["label"], "created_at": r["created_at"], "expires_at": r["expires_at"], "last_used_at": r["last_used_at"]} for r in rows]

    def revoke_skill_token(self, user_id: str, label: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM skill_tokens WHERE user_id=? AND label=?",
                (user_id, label),
            )
        return cur.rowcount > 0


    # ── Device Token 方法 ──────────────────────────────────────────────────────

    def issue_device_token(self, username: str, password: str, client_fingerprint: str, label: str = "") -> str:
        """校验密码后签发与机器指纹绑定的 device token，DB 只存 sha256 摘要。"""
        user = self.authenticate(username, password)
        if user is None:
            raise ValueError("Invalid credentials")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = _isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO device_tokens (id, user_id, client_fingerprint, token_hash, label, created_at, last_used_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, client_fingerprint) DO UPDATE SET
                     token_hash=excluded.token_hash, label=excluded.label,
                     last_used_at=excluded.last_used_at, revoked=0""",
                (secrets.token_hex(16), user["id"], client_fingerprint, token_hash, label or "", now, now),
            )
        return token

    def verify_device_token(self, token: str, client_fingerprint: str) -> Optional[dict[str, Any]]:
        """token + client_fingerprint 双匹配且未 revoked 时返回 User 字典，否则 None。"""
        if not token or not client_fingerprint:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = _isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT users.* FROM device_tokens
                   JOIN users ON users.id = device_tokens.user_id
                   WHERE device_tokens.token_hash = ?
                     AND device_tokens.client_fingerprint = ?
                     AND device_tokens.revoked = 0
                     AND users.is_active = 1""",
                (token_hash, client_fingerprint),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE device_tokens SET last_used_at=? WHERE token_hash=?",
                    (now, token_hash),
                )
        return self._sanitize_user_row(row)

    def revoke_device_token(self, token: str, client_fingerprint: str) -> bool:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE device_tokens SET revoked=1 WHERE token_hash=? AND client_fingerprint=?",
                (token_hash, client_fingerprint),
            )
        return result.rowcount > 0

    # ── 匿名配额方法 ──────────────────────────────────────────────────────────

    def check_and_increment_anon_quota(self, client_id: str, client_ip: str, limit: int) -> bool:
        """检查并递增匿名配额。返回 True=允许，False=超限。任一维度超限即拒绝。"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn:
            for dim_key in (f"cid:{client_id}", f"ip:{client_ip}"):
                row = conn.execute(
                    "SELECT count FROM anon_quota WHERE dim_key=? AND day_key=?",
                    (dim_key, today),
                ).fetchone()
                if row and row["count"] >= limit:
                    return False
            for dim_key in (f"cid:{client_id}", f"ip:{client_ip}"):
                conn.execute(
                    """INSERT INTO anon_quota (dim_key, day_key, count) VALUES (?, ?, 1)
                       ON CONFLICT(dim_key, day_key) DO UPDATE SET count=count+1""",
                    (dim_key, today),
                )
        return True

    def log_request(
        self,
        interface: str,
        user_id: str = "",
        display_name: str = "",
        is_anon: bool = True,
        client_id: str = "",
        client_ip: str = "",
        project: str = "",
        issue_key: str = "",
        query_text: str = "",
    ) -> str:
        """写入 API 请求日志，返回 log id。失败时静默，不抛异常。"""
        try:
            log_id = secrets.token_hex(16)
            ts = datetime.now(timezone.utc).isoformat()
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO request_logs
                       (id, ts, interface, user_id, display_name, is_anon,
                        client_id, client_ip, project, issue_key, query_text)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (log_id, ts, interface, user_id, display_name, int(is_anon),
                     client_id, client_ip, project, issue_key, query_text[:500]),
                )
            return log_id
        except Exception:
            return ""


def get_auth_service() -> AuthService:
    return AuthService()
