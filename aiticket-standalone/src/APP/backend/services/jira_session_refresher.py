"""JiraSessionRefresher — 单例，负责周期性从 Chrome 解密 Jira session cookies，
写入 /tmp/jira-session*.json 并可选推送到 QCL。

用法：
    refresher = JiraSessionRefresher.get_instance()
    refresher.configure(jira_base_url="https://gfjira.yyrd.com", ssl_verify=False, proxies={})
    refresher.start_background()          # 30 分钟周期，后台线程
    meta = refresher.refresh_now()        # 立即同步刷新一次（全局）
    meta = refresher.refresh_now("qiangxiao")  # per-user
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional


class JiraSessionRefresher:
    _instance: Optional["JiraSessionRefresher"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._jira_base_url = ""
        self._ssl_verify = True
        self._proxies: dict = {}
        self._push_target: str = ""       # QCL URL, e.g. "https://qcl:18000"
        self._push_token: str = ""        # Bearer token
        self._username: str = ""
        self._password: str = ""
        self._timer: Optional[threading.Timer] = None
        self._interval_sec = 1800         # 30 min

    @classmethod
    def get_instance(cls) -> "JiraSessionRefresher":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def configure(self, jira_base_url: str, ssl_verify: bool = True, proxies: dict = None,
                  push_target: str = "", push_token: str = "",
                  username: str = "", password: str = ""):
        self._jira_base_url = jira_base_url.rstrip("/")
        self._ssl_verify = ssl_verify
        self._proxies = proxies or {}
        self._push_target = (push_target or os.environ.get("JIRA_SESSION_PUSH_TARGET", "")).rstrip("/")
        self._push_token = push_token or os.environ.get("JIRA_SESSION_PUSH_TOKEN", "")
        self._username = username or os.environ.get("JIRA_USERNAME", "")
        self._password = password or os.environ.get("JIRA_PASSWORD", "")

    # ── Public API ───────────────────────────────────────────────────────────

    def refresh_now(self, user: str | None = None) -> dict:
        """同步刷新一次。返回 _meta dict（含 source / refreshed_at / cookie_count）。"""
        state_path = self._session_path(user)
        cookies, source = [], "rest_only"

        if platform.system() == "Darwin":
            domain = self._domain()
            cookies = self._chrome_decrypt(domain)
            if cookies:
                source = "chrome"

        if not cookies:
            cookies = self._rest_session_fallback()
            source = "rest_only"

        meta = {
            "source": source,
            "refreshed_at": int(time.time()),
            "cookie_count": len(cookies),
        }
        if cookies:
            state = {"cookies": cookies, "origins": [], "_meta": meta}
            self._atomic_write(state_path, state)
            print(f"[JiraRefresher] ✓ {state_path} ({source}, {len(cookies)} cookies)")

            if not user and self._push_target and self._push_token:
                self._push_to_remote(state)

        return meta

    def start_background(self, interval_sec: int = 1800):
        self._interval_sec = interval_sec
        self._schedule_next(delay=10)
        print(f"[JiraRefresher] 后台刷新已启动，间隔 {interval_sec}s")

    def stop_background(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def push_remote(self, state: dict, target_url: str, token: str):
        self._push_to_remote(state, target_url=target_url, token=token)

    def receive_push(self, state: dict):
        """QCL 端调用：接收 Mini 推来的 state，落盘到本机 session 文件。"""
        from services.host_context import session_path as _session_path
        meta = state.get("_meta", {})
        self._atomic_write(_session_path(), state)
        print(f"[JiraRefresher] ← 收到 Mini 推送: {meta.get('source')} {meta.get('cookie_count')} cookies")

    # ── Session file helpers ─────────────────────────────────────────────────

    @staticmethod
    def session_path(user: str | None = None) -> str:
        return JiraSessionRefresher._session_path_static(user)

    @staticmethod
    def _session_path_static(user: str | None) -> str:
        from services.host_context import session_path as _session_path
        return _session_path(user=user, prefix="jira")

    def _session_path(self, user: str | None) -> str:
        return self._session_path_static(user)

    # ── Chrome decrypt (macOS only) ──────────────────────────────────────────

    def _domain(self) -> str:
        return self._jira_base_url.replace("https://", "").replace("http://", "").split("/")[0]

    def _chrome_decrypt(self, domain: str) -> list:
        cookies_src = os.path.expanduser(
            "~/Library/Application Support/Google/Chrome/Default/Cookies"
        )
        if not os.path.exists(cookies_src):
            print("[JiraRefresher] Chrome Cookies DB 不存在，跳过解密")
            return []

        try:
            from Crypto.Cipher import AES
        except ImportError:
            print("[JiraRefresher] 缺少 pycryptodomex，跳过 Chrome 解密")
            return []

        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                print("[JiraRefresher] Keychain 无法读取 Chrome Safe Storage")
                return []

            from hashlib import pbkdf2_hmac
            key = pbkdf2_hmac("sha1", r.stdout.strip().encode(), b"saltysalt", 1003, 16)

            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                dst = tmp.name
            shutil.copy2(cookies_src, dst)

            try:
                conn = sqlite3.connect(dst)
                rows = conn.execute(
                    "SELECT host_key,name,value,encrypted_value,path,"
                    "is_secure,is_httponly,expires_utc,samesite "
                    "FROM cookies WHERE host_key LIKE '%yyrd%'"
                ).fetchall()
                conn.close()
            finally:
                try:
                    os.unlink(dst)
                except OSError:
                    pass

            def _decrypt(enc):
                if not enc or enc[:3] != b"v10":
                    return None
                ct = enc[3:]
                pt = AES.new(key, AES.MODE_CBC, b" " * 16).decrypt(ct)
                pt = pt[: -pt[-1]]
                if len(pt) > 32:
                    candidate = pt[32:].decode("utf-8", errors="replace")
                    if all(c.isprintable() or c in "\n\r\t" for c in candidate[:20]):
                        return candidate
                return pt.decode("utf-8", errors="replace")

            samesite_map = {-1: "None", 0: "None", 1: "Lax", 2: "Strict"}
            cookies = []
            for host, name, plain, enc, path, secure, httponly, exp, ss in rows:
                # xsrf 不入 storageState：Playwright 首次 GET 时 Jira 会颁发新的 xsrf
                if name == "atlassian.xsrf.token":
                    continue
                val = plain if plain else _decrypt(enc)
                if not val:
                    continue
                entry = {
                    "name": name, "value": val,
                    "domain": host, "path": path or "/",
                    "secure": bool(secure), "httpOnly": bool(httponly),
                    "sameSite": samesite_map.get(ss, "Lax"),
                    "expires": -1,
                }
                if name == "JSESSIONID":
                    entry.update({"httpOnly": True, "secure": True, "sameSite": "None"})
                cookies.append(entry)

            print(f"[JiraRefresher] Chrome 解密: {len(cookies)} cookies")
            return cookies

        except Exception as e:
            print(f"[JiraRefresher] Chrome 解密异常: {e}")
            return []

    # ── REST fallback (writes REST-only JSESSIONID; no MoveIssue.jspa support) ──

    def _rest_session_fallback(self) -> list:
        if not self._jira_base_url:
            return []
        try:
            import requests as _req
            # username / password come from environment or are injected by caller
            username = self._username or os.environ.get("JIRA_USERNAME", "")
            password = self._password or os.environ.get("JIRA_PASSWORD", "")
            if not (username and password):
                return []
            resp = _req.post(
                f"{self._jira_base_url}/rest/auth/1/session",
                json={"username": username, "password": password},
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         "User-Agent": "curl/8.7.1"},
                verify=self._ssl_verify, timeout=15,
                proxies=self._proxies or None,
            )
            if resp.status_code == 200:
                session = resp.json().get("session", {})
                jsessionid = session.get("value", "")
                if jsessionid:
                    domain = self._domain()
                    return [{"name": "JSESSIONID", "value": jsessionid,
                             "domain": domain, "path": "/",
                             "httpOnly": True, "secure": True,
                             "sameSite": "None", "expires": -1}]
            print(f"[JiraRefresher] REST fallback HTTP {resp.status_code}")
        except Exception as e:
            print(f"[JiraRefresher] REST fallback 异常: {e}")
        return []

    # ── Remote push (Mini → QCL) ─────────────────────────────────────────────

    def _push_to_remote(self, state: dict, target_url: str = "", token: str = ""):
        ssh_host = os.environ.get("JIRA_SESSION_PUSH_SSH_HOST", "")
        remote_path = os.environ.get("JIRA_SESSION_PUSH_SSH_PATH", "/tmp/jira-session.json")
        if ssh_host:
            self._push_via_ssh(state, ssh_host, remote_path)
            return
        # HTTP fallback (requires nginx to allow POST on /internal/)
        url = (target_url or self._push_target)
        tok = (token or self._push_token)
        if not url or not tok:
            return
        try:
            import requests as _req
            r = _req.post(
                f"{url}/internal/jira-session/push-body",
                json=state,
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                timeout=10, verify=False,
            )
            if r.ok:
                print(f"[JiraRefresher] → HTTP 推送到 QCL ({r.status_code})")
            else:
                print(f"[JiraRefresher] → HTTP 推送失败 {r.status_code}: {r.text[:80]}")
        except Exception as e:
            print(f"[JiraRefresher] → HTTP 推送异常: {e}")

    def _push_via_ssh(self, state: dict, ssh_host: str, remote_path: str):
        """通过 scp 将 session state 直接写入远端 /tmp/jira-session.json，绕过 nginx。"""
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
                tmp = f.name
            result = subprocess.run(
                ["scp", "-q", "-o", "StrictHostKeyChecking=no",
                 "-o", "ConnectTimeout=10",
                 tmp, f"{ssh_host}:{remote_path}"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                meta = state.get("_meta", {})
                print(f"[JiraRefresher] → SSH 推送成功: {ssh_host}:{remote_path} "
                      f"({meta.get('source')} {meta.get('cookie_count')} cookies)")
            else:
                print(f"[JiraRefresher] → SSH 推送失败: {result.stderr.strip()}")
        except Exception as e:
            print(f"[JiraRefresher] → SSH 推送异常: {e}")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ── Background timer ─────────────────────────────────────────────────────

    def _schedule_next(self, delay: int | None = None):
        secs = delay if delay is not None else self._interval_sec
        self._timer = threading.Timer(secs, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self):
        try:
            self.refresh_now()
        except Exception as e:
            print(f"[JiraRefresher] 定时刷新异常: {e}")
        finally:
            self._schedule_next()

    # ── File helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _atomic_write(path: str, data: dict):
        import tempfile as _tempfile
        dir_ = os.path.dirname(path) or _tempfile.gettempdir()
        with _tempfile.NamedTemporaryFile("w", dir=dir_, delete=False,
                                          suffix=".tmp", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            tmp = f.name
        os.replace(tmp, path)
