import requests
import json
import time
import re
import urllib.parse
import base64
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, asdict

from jira_config import JiraConfigParser
import os

# Absolute path to jira_api.md
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JIRA_API_PATH = os.path.join(BASE_DIR, "interface/jira_api.md")

# 本地缓存目录（用于服务器离线模式）
# Demo 沙箱：通过 DEMO_RUNTIME_DIR 隔离，与主站 data_cache 物理分离
CACHE_DIR = os.path.join(os.environ.get("DEMO_RUNTIME_DIR") or BASE_DIR, "data_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

JIRA_CACHE_FILE = os.path.join(CACHE_DIR, "jira_board_data.json")

# 字段选项缓存文件
FIELD_OPTIONS_CACHE_FILE = os.path.join(BASE_DIR, "data", "field_options_cache.json")
os.makedirs(os.path.dirname(FIELD_OPTIONS_CACHE_FILE), exist_ok=True)

@dataclass
class JiraIssue:
    key: str
    summary: str
    status: str
    assignee: str
    reporter: str
    created: str
    updated: str
    due_date: Optional[str]
    priority: str
    issue_type: str
    project_name: str
    description: str = ""
    contact_name: str = ""  # 联系人 (customfield_10404)
    contact_info: str = ""  # 联系方式 (customfield_10405)
    customer_name: str = ""  # 项目名称/客户 (customfield_10725)
    product_version: str = ""  # SOP产品版本 (customfield_13529)
    deploy_mode: str = ""  # 部署模式（从product_version提取）

class JiraService:
    def __init__(
        self,
        config_path: str = JIRA_API_PATH,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_url: Optional[str] = None,
        include_config_cookies: bool = True,
        enable_cache: bool = True,
        cache_namespace: str = "",
        session_cookies: Optional[Dict[str, str]] = None,
    ):
        self.config_parser = JiraConfigParser(config_path)
        self._auth_override = {"username": username or "", "password": password or ""}
        self._base_url_override = base_url or ""
        self._include_config_cookies = include_config_cookies
        self.enable_cache = enable_cache
        self.cache_namespace = cache_namespace.strip()
        # session_cookies 模式：{"JSESSIONID": ..., "xsrf_token": ...}
        # 当提供时，所有请求使用 cookies 认证而非 Basic Auth
        self._session_cookies = dict(session_cookies) if session_cookies else None
        self.reload_config()
        # strict 模式标记：无显式 session_cookies 且无显式 username，意味着使用默认凭据（qiangxiao）
        self._no_default_creds = not bool(username or session_cookies)

    def _build_basic_auth_header(self, username: str, password: str) -> Optional[str]:
        if not username or not password:
            return None
        auth_str = f"{username}:{password}"
        encoded_auth = base64.b64encode(auth_str.encode()).decode()
        return f"Basic {encoded_auth}"

    def reload_config(self):
        """Reload configuration using Basic Auth + Cookies (for backward compatibility)"""
        import os, socket
        self.config_parser.load()

        # Use hardcoded Jira URL (can be moved to config if needed)
        from config.loader import cfg
        _default_jira_url = cfg("jira", "base_url") or os.environ.get("JIRA_BASE_URL", "")
        self.base_url = self._base_url_override or os.environ.get("JIRA_BASE_URL") or _default_jira_url

        # Get Basic Auth configuration (with cookies for backward compatibility)
        auth_config = self.config_parser.get_auth_config(include_cookies=self._include_config_cookies)
        self.headers = auth_config["headers"]
        self.cookies = auth_config["cookies"]

        # 关键：用友 Jira 有反爬虫策略，Python requests 默认 UA 会被识别为自动化
        # 返回 403 "Automated access forbidden"。必须伪装成 Chrome 浏览器
        self.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        )
        self.headers.setdefault("Accept", "application/json, text/plain, */*")
        self.headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
        self.headers.setdefault("Referer", f"{self.base_url}/")

        override_auth_header = self._build_basic_auth_header(
            self._auth_override.get("username", ""),
            self._auth_override.get("password", ""),
        )
        if override_auth_header:
            self.headers["Authorization"] = override_auth_header
            self.cookies = {}

        # session_cookies 模式：强制使用 JSESSIONID cookie，移除 Basic Auth header
        # 优先级高于 Basic Auth（适用于用友内部 Jira 的 MFA 场景）
        if self._session_cookies:
            jsessionid = (self._session_cookies.get("JSESSIONID") or "").strip()
            xsrf = (self._session_cookies.get("xsrf_token") or "").strip()
            if jsessionid:
                # 丢弃 Basic Auth header，避免服务端优先读 header 拒绝
                self.headers.pop("Authorization", None)
                self.cookies = {"JSESSIONID": jsessionid}
                if xsrf:
                    self.cookies["atlassian.xsrf.token"] = xsrf
                    self.headers["X-Atlassian-Token"] = "no-check"
                print("[JiraService] Using session_cookie auth (JSESSIONID)")

        # SSL Verification configuration
        # Priority: 1. Environment variable 2. 内网主机自动禁用 3. Default True
        ssl_verify_env = os.getenv('JIRA_SSL_VERIFY', '').lower()
        if ssl_verify_env in ('false', '0', 'no', 'off'):
            self.ssl_verify = False
            print("[JiraService] SSL verification DISABLED (env)")
        elif ssl_verify_env in ('true', '1', 'yes', 'on'):
            self.ssl_verify = True
            print("[JiraService] SSL verification enabled (env)")
        else:
            # 未设置环境变量时：内网主机(lap)自动禁用，其他主机默认启用
            hostname = socket.gethostname().lower()
            if 'lap' in hostname or hostname.startswith('crosslap'):
                self.ssl_verify = False
                print(f"[JiraService] SSL verification DISABLED (内网主机: {hostname})")
            else:
                # Check for custom CA bundle
                ca_bundle = os.getenv('JIRA_CA_BUNDLE')
                if ca_bundle and os.path.exists(ca_bundle):
                    self.ssl_verify = ca_bundle
                    print(f"[JiraService] Using custom CA bundle: {ca_bundle}")
                else:
                    self.ssl_verify = True
                    print("[JiraService] SSL verification enabled")

        # Cookie control: skip stale browser cookies when Basic Auth is sufficient
        # 例外：session_cookie 模式下必须保留用户 JSESSIONID，否则用户无法通过 session 认证
        skip_cookies = os.getenv('JIRA_SKIP_COOKIES', 'false').lower() in ('true', '1')
        if skip_cookies and not self._session_cookies:
            self.cookies = {}
            print("[JiraService] Cookies disabled via JIRA_SKIP_COOKIES - using Basic Auth only")
        elif skip_cookies and self._session_cookies:
            print("[JiraService] JIRA_SKIP_COOKIES ignored: session_cookie mode requires user JSESSIONID")

        # Check if auth is configured
        if not override_auth_header and not self.config_parser.get_basic_auth_header():
            print("[JiraService] Warning: Basic Auth credentials not found in config")
        if not skip_cookies and not self.cookies and not override_auth_header:
            print("[JiraService] Warning: Cookies not found, Jira authentication may fail")

        # Proxy configuration from environment variables
        self.proxies = self._get_proxy_config()
        if self.proxies:
            print(f"[JiraService] Using proxy: {self.proxies}")

    def _get_proxy_config(self) -> Dict[str, str]:
        """
        Get proxy configuration from environment variables.
        Supports HTTP_PROXY, HTTPS_PROXY, http_proxy, https_proxy.
        """
        import os
        from urllib.parse import urlparse

        # 检查当前 base_url 的 host 是否在 no_proxy 白名单里
        # no_proxy 规则：逗号分隔，支持 .example.com 这种后缀匹配
        no_proxy = os.getenv('no_proxy') or os.getenv('NO_PROXY') or ''
        host = urlparse(self.base_url).hostname or ''
        for entry in [e.strip() for e in no_proxy.split(',') if e.strip()]:
            # 后缀匹配（.example.com 匹配 jira.example.com）
            if entry.startswith('.'):
                if host.endswith(entry) or host == entry.lstrip('.'):
                    return {}
            elif host == entry or host.endswith('.' + entry):
                return {}

        proxies = {}
        http_proxy = os.getenv('HTTP_PROXY') or os.getenv('http_proxy')
        https_proxy = os.getenv('HTTPS_PROXY') or os.getenv('https_proxy')
        if http_proxy:
            proxies['http'] = http_proxy
        if https_proxy:
            proxies['https'] = https_proxy
        return proxies

    def _make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        Wrapper for making HTTP requests with proxy support and error handling.
        """
        from role_guard import is_strict_role, NoUserContextError
        if is_strict_role() and getattr(self, '_no_default_creds', False):
            raise NoUserContextError(
                "JiraService._make_request",
                "default credentials (qiangxiao) blocked in strict mode",
            )
        # Add proxies if configured
        if self.proxies:
            kwargs['proxies'] = self.proxies

        try:
            response = requests.request(method, url, **kwargs)
            return response
        except requests.exceptions.ProxyError as e:
            print(f"[JiraService] Proxy error: {e}")
            raise
        except requests.exceptions.ConnectionError as e:
            print(f"[JiraService] Connection error: {e}")
            # Provide helpful diagnostic information
            self._diagnose_connection(url)
            raise

    def _diagnose_connection(self, url: str):
        """
        Diagnose connection issues and provide troubleshooting tips.
        """
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)

        print(f"[JiraService] Diagnosing connection to {hostname}:{port}...")

        # Check DNS resolution
        try:
            ip = socket.gethostbyname(hostname)
            print(f"[JiraService] DNS resolution OK: {hostname} -> {ip}")
        except socket.gaierror as e:
            print(f"[JiraService] DNS resolution FAILED: {e}")
            print("[JiraService] Troubleshooting: Check your DNS settings or /etc/hosts file")

        # Check if proxy is needed
        if not self.proxies:
            print("[JiraService] No proxy configured. If you're behind a corporate firewall, set HTTP_PROXY/HTTPS_PROXY environment variables.")

    def update_cookies(self, new_cookies: Dict[str, str]):
        """
        Update Jira cookies for authentication
        Note: This is deprecated with Basic Auth, but kept for backward compatibility
        """
        self.cookies.update(new_cookies)

    def _board_cache_file(self) -> Optional[str]:
        if not self.enable_cache:
            return None
        if not self.cache_namespace:
            return JIRA_CACHE_FILE
        safe_namespace = re.sub(r"[^A-Za-z0-9._-]+", "_", self.cache_namespace)
        return os.path.join(CACHE_DIR, f"jira_board_data_{safe_namespace}.json")

    def diagnose_connection(self) -> Dict[str, Any]:
        """独立测试 Jira 直连，返回详细诊断信息（含 cookie 对比测试）。"""
        import socket
        from urllib.parse import urlparse

        url = f"{self.base_url}/rest/api/2/search"
        from config.loader import cfg
        _probe_project = cfg("instance", "primary_project_key") or "TEST"
        params = {"jql": f"project={_probe_project} ORDER BY updated DESC", "maxResults": 1}
        result = {
            "url": url,
            "auth_method": (
                "session_cookie" if self._session_cookies else
                ("basic_auth" if self.headers.get("Authorization") else "none")
            ),
            "has_cookies": bool(self.cookies),
            "cookie_keys": list((self.cookies or {}).keys()),
            "session_cookies_provided": bool(self._session_cookies),
            "ssl_verify": self.ssl_verify,
            "proxies": self.proxies or None,
        }

        # DNS check
        parsed = urlparse(self.base_url)
        hostname = parsed.hostname
        try:
            result["dns_resolved"] = socket.gethostbyname(hostname)
        except socket.gaierror as e:
            result["status"] = "dns_failure"
            result["error"] = str(e)
            return result

        # Test 1: with cookies (current config)
        result["with_cookies"] = self._probe_jira(url, params, use_cookies=True)
        # Test 2: without cookies (Basic Auth only)
        result["without_cookies"] = self._probe_jira(url, params, use_cookies=False)

        # Determine overall status
        wc = result["with_cookies"]
        nc = result["without_cookies"]
        if nc["status"] == "ok" and wc["status"] != "ok":
            result["status"] = "cookie_interference"
            result["recommendation"] = "Set JIRA_SKIP_COOKIES=true to fix"
        elif nc["status"] == "ok":
            result["status"] = "ok"
        elif wc["status"] == "ok":
            result["status"] = "ok"
        else:
            result["status"] = nc["status"]
            result["error"] = nc.get("error", wc.get("error"))

        return result

    def _probe_jira(self, url: str, params: dict, use_cookies: bool) -> Dict[str, Any]:
        """发送一次探测请求，返回状态摘要。"""
        probe = {}
        cookies = self.cookies if use_cookies and self.cookies else None
        start = time.time()
        try:
            resp = requests.get(
                url, headers=self.headers, cookies=cookies, params=params,
                verify=self.ssl_verify, timeout=15, proxies=self.proxies,
            )
            probe["latency_ms"] = round((time.time() - start) * 1000, 1)
            probe["http_status"] = resp.status_code
            if resp.status_code == 200:
                data = resp.json()
                probe["status"] = "ok"
                probe["total_issues"] = data.get("total", 0)
            elif resp.status_code == 401:
                probe["status"] = "auth_failure"
                probe["error"] = "401 Unauthorized"
            elif resp.status_code == 403:
                probe["status"] = "forbidden"
                probe["error"] = "403 Forbidden - CAPTCHA or account locked"
            else:
                probe["status"] = "http_error"
                probe["error"] = f"HTTP {resp.status_code}"
                probe["body_snippet"] = resp.text[:200]
        except requests.exceptions.SSLError as e:
            probe["status"] = "ssl_error"
            probe["error"] = str(e)[:200]
        except requests.exceptions.ConnectionError as e:
            probe["status"] = "connection_error"
            probe["error"] = str(e)[:200]
        except requests.exceptions.Timeout:
            probe["status"] = "timeout"
            probe["error"] = "Request timed out (15s)"
        except Exception as e:
            probe["status"] = "unknown_error"
            probe["error"] = str(e)[:200]
        return probe

    def get_fields(self) -> Dict[str, Any]:
        """
        Get all Jira fields using standard REST API
        Reference: Java example - step 1

        Returns:
            Dict with field information
        """
        url = f"{self.base_url}/rest/api/2/field"

        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[JiraService] Get fields error: {e}")
            return {"error": str(e)}

    # 默认查询字段列表（含客户/项目名称字段 customfield_10725）
    DEFAULT_SEARCH_FIELDS = (
        'summary,status,assignee,reporter,created,updated,priority,issuetype,'
        'project,description,duedate,labels,'
        'customfield_10725,customfield_10729,customfield_10402,customfield_10906,'
        'customfield_11919,customfield_10404,customfield_10405,customfield_13529'
    )

    def search_issues_rest_api(self, jql: str, start_at: int = 0, max_results: int = 50, fields: str = None) -> Dict[str, Any]:
        """
        Search issues using standard REST API (v2)
        Replaces the legacy issueNav/1/issueTable endpoint

        Args:
            jql: JQL query string
            start_at: Starting index for pagination
            max_results: Maximum number of results
            fields: 逗号分隔的字段列表，默认使用 DEFAULT_SEARCH_FIELDS

        Returns:
            Dict with search results containing 'issues' list
        """
        url = f"{self.base_url}/rest/api/2/search"

        params = {
            'jql': jql,
            'startAt': start_at,
            'maxResults': max_results,
            'fields': fields or self.DEFAULT_SEARCH_FIELDS
        }

        try:
            # Use both headers and cookies for authentication
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                params=params,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[JiraService] Search error: {e}")
            return {"error": str(e)}

    def _parse_jira_response(self, response_text: str) -> Tuple[bool, Optional[str]]:
        """
        Parse Jira HTML response to detect errors.
        Returns (success, error_message)
        - success: True if no error found, False otherwise
        - error_message: The error message if error found, None otherwise
        """
        if not response_text:
            return True, None

        # Common error patterns in Jira HTML responses
        error_patterns = [
            # Error message divs
            (r'<div[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE),
            (r'<div[^>]*class="[^"]*aui-message-error[^"]*"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE),
            (r'<span[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</span>', re.DOTALL | re.IGNORECASE),
            # Error messages in specific structures
            (r'<div[^>]*id="[^"]*error[^"]*"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE),
            # Permission errors
            (r'permission[^<]*denied', re.IGNORECASE),
            (r'not[^<]*permitted', re.IGNORECASE),
            # Session errors
            (r'session[^<]*expired', re.IGNORECASE),
            (r'log[^<]*in[^<]*again', re.IGNORECASE),
        ]

        for pattern, flags in error_patterns:
            matches = re.findall(pattern, response_text, flags)
            for match in matches:
                if match and len(match.strip()) > 0:
                    # Clean up the error message
                    error_msg = self._clean_error_message(match)
                    if error_msg and len(error_msg) > 3:  # Avoid single character matches
                        return False, error_msg

        # Check for JSON error responses
        try:
            json_data = json.loads(response_text)
            if 'errorMessages' in json_data and json_data['errorMessages']:
                return False, '; '.join(json_data['errorMessages'])
            if 'errors' in json_data and json_data['errors']:
                errors = json_data['errors']
                if isinstance(errors, dict):
                    error_msgs = [f"{k}: {v}" for k, v in errors.items()]
                    return False, '; '.join(error_msgs)
                elif isinstance(errors, list):
                    return False, '; '.join(str(e) for e in errors)
        except json.JSONDecodeError:
            pass  # Not JSON, continue with HTML parsing

        return True, None

    def _clean_error_message(self, html_text: str) -> str:
        """Clean HTML tags from error message and extract text"""
        if not html_text:
            return ""

        # Remove HTML tags
        text = re.sub(r'<[^>]+>', ' ', html_text)
        # Replace HTML entities
        text = text.replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
        text = text.replace('&nbsp;', ' ')
        # Normalize whitespace
        text = ' '.join(text.split())
        return text.strip()

    def search_issues(self, jql: str, start_index: int = 0, max_results: int = 50) -> Dict[str, Any]:
        """
        Search issues using JQL (now using standard REST API v2)
        Backward compatible with old interface
        """
        return self.search_issues_rest_api(jql, start_index, max_results)

    def assign_issue(self, issue_id: str, assignee: str, comment: str = None) -> dict:
        """
        Assign an issue to a user using standard REST API
        Supports both legacy 'name' field and modern 'accountId' field

        Args:
            issue_id: Issue key (e.g., "MYPROJECT-12345")
            assignee: Assignee username or accountId
            comment: Optional comment to add

        Returns:
            dict: {'success': bool, 'message': str}
        """
        url = f"{self.base_url}/rest/api/2/issue/{issue_id}/assignee"

        # Try modern Jira API format first (accountId)
        # Then fall back to legacy format (name)

        # Strategy 1: Try using accountId (modern Jira 8.0+)
        data_account_id = {'accountId': assignee}
        # Strategy 2: Try using name (legacy)
        data_name = {'name': assignee}

        strategies = [
            ('accountId', data_account_id),
            ('name', data_name)
        ]

        last_error = None
        for strategy_name, data in strategies:
            try:
                print(f"[JiraService] Trying assign with {strategy_name}={assignee}")
                response = requests.put(
                    url,
                    headers=self.headers,
                    cookies=self.cookies if self.cookies else None,
                    json=data,
                    verify=self.ssl_verify,
                    timeout=10,
                    proxies=self.proxies
                )

                # Log response for debugging
                print(f"[JiraService] Assign response status: {response.status_code}")
                if response.status_code != 204 and response.text:
                    print(f"[JiraService] Assign response body: {response.text[:500]}")

                if response.status_code == 204:
                    # Success - 204 No Content
                    # If comment provided, add it separately
                    if comment:
                        comment_result = self.add_comment(issue_id, comment)
                        if not comment_result['success']:
                            return {
                                'success': True,
                                'message': f'分配成功，但评论添加失败: {comment_result["message"]}'
                            }
                        return {
                            'success': True,
                            'message': '分配成功（已添加评论）'
                        }
                    return {'success': True, 'message': '分配成功'}

                # Try to parse error
                try:
                    error_data = response.json()
                    error_msgs = error_data.get('errorMessages', [])
                    errors = error_data.get('errors', {})
                    if error_msgs:
                        last_error = '; '.join(error_msgs)
                    elif errors:
                        last_error = '; '.join([f"{k}: {v}" for k, v in errors.items()])
                    else:
                        last_error = f"HTTP {response.status_code}"
                except:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"

                print(f"[JiraService] Assign failed with {strategy_name}: {last_error}")

            except requests.exceptions.HTTPError as e:
                last_error = f"HTTP错误: {e.response.status_code}"
                print(f"[JiraService] Assign HTTP error for {issue_id} with {strategy_name}: {last_error}")
            except requests.exceptions.Timeout:
                return {'success': False, 'message': '请求超时，请重试'}
            except Exception as e:
                last_error = f'系统错误: {str(e)}'
                print(f"[JiraService] Assign error for {issue_id} with {strategy_name}: {last_error}")

        # All strategies failed
        error_detail = last_error or '未知错误'
        print(f"[JiraService] All assign strategies failed for {issue_id}")
        return {'success': False, 'message': f'分配失败: {error_detail}'}

    def add_comment(self, issue_id: str, comment: str) -> dict:
        """
        Add a comment to an issue using standard REST API

        Args:
            issue_id: Issue key (e.g., "MYPROJECT-12345")
            comment: Comment text (supports Atlassian wiki markup)

        Returns:
            dict: {'success': bool, 'message': str}
        """
        url = f"{self.base_url}/rest/api/2/issue/{issue_id}/comment"

        data = {'body': comment}

        try:
            response = requests.post(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                json=data,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            return {'success': True, 'message': '评论添加成功'}

        except requests.exceptions.HTTPError as e:
            error_msg = f"HTTP错误: {e.response.status_code}"
            print(f"[JiraService] Add comment HTTP error for {issue_id}: {error_msg}")
            return {'success': False, 'message': error_msg}
        except requests.exceptions.Timeout:
            return {'success': False, 'message': '请求超时，请重试'}
        except Exception as e:
            return {'success': False, 'message': f'系统错误: {str(e)}'}

    def upload_attachment(self, issue_id: str, filename: str, file_content: bytes) -> dict:
        """
        Upload a file attachment to a Jira issue.

        Args:
            issue_id: Issue key (e.g., "MYPROJECT-12345")
            filename: Original filename
            file_content: File content as bytes

        Returns:
            dict: {'success': bool, 'message': str, 'attachment': dict|None}
        """
        url = f"{self.base_url}/rest/api/2/issue/{issue_id}/attachments"

        # Jira requires X-Atlassian-Token: no-check for attachment uploads
        headers = dict(self.headers)
        headers['X-Atlassian-Token'] = 'no-check'
        # Remove Content-Type — requests will set multipart/form-data automatically
        headers.pop('Content-Type', None)

        files = {'file': (filename, file_content)}

        try:
            response = requests.post(
                url,
                headers=headers,
                files=files,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify,
                timeout=60,
                proxies=self.proxies
            )
            response.raise_for_status()
            result = response.json()
            attachment_info = result[0] if isinstance(result, list) and result else result
            print(f"[JiraService] Attachment uploaded: {filename} -> {issue_id}")
            return {'success': True, 'message': '附件上传成功', 'attachment': attachment_info}

        except requests.exceptions.HTTPError as e:
            error_detail = ''
            try:
                error_detail = e.response.text[:200]
            except:
                pass
            error_msg = f"HTTP {e.response.status_code}: {error_detail}"
            print(f"[JiraService] Attachment upload failed for {issue_id}: {error_msg}")
            return {'success': False, 'message': f'附件上传失败: {error_msg}', 'attachment': None}
        except requests.exceptions.Timeout:
            return {'success': False, 'message': '上传超时，文件可能过大', 'attachment': None}
        except Exception as e:
            return {'success': False, 'message': f'上传错误: {str(e)}', 'attachment': None}

    def reply_issue(self, issue_id: str, comment: str, custom_fields: Dict = None,
                    action: str = None, close: bool = False,
                    ai_fields: Dict = None) -> dict:
        """
        Add comment to an issue with optional custom fields and close option.
        当 close=True 时，使用"直接回复"转换 API（原子操作，支持转换屏幕字段）。
        当 close=False 时，仅添加评论。

        Args:
            issue_id: The issue key (e.g., "MYPROJECT-12345")
            comment: The comment text
            custom_fields: Optional custom fields dict (solution, reply_method, issue_type_confirmed)
            action: Optional action parameter (deprecated, kept for compatibility)
            close: Whether to close the issue after commenting
            ai_fields: Optional AI metadata fields (smart_result, ai_result, use_agent)

        Returns:
            dict: {'success': bool, 'message': str, ...}
        """
        # 当需要关闭工单时，使用转换 API（原子操作，支持 customfield_10410 等转换屏幕字段）
        if close:
            print(f"[ReplyIssue] 使用转换 API 执行回复并关闭: {issue_id}")
            return self.reply_and_close_via_transition(
                issue_id, comment, custom_fields, ai_fields)

        # 仅回复（不关闭）：添加评论 + 更新可编辑字段
        completed_steps = []
        original_fields = {}  # Store original values for potential rollback

        field_update_error = None
        try:
            # Step 1: Update custom fields if provided (optional, non-blocking)
            if custom_fields:
                print(f"[ReplyIssue] Step 1/2: Updating custom fields for {issue_id}")
                update_result = self.update_issue_fields(issue_id, custom_fields)
                if update_result['success']:
                    completed_steps.append('update_fields')
                    print(f"[ReplyIssue] Step 1 completed: Fields updated")
                else:
                    # Field update failed, log warning but continue
                    field_update_error = update_result['message']
                    print(f"[ReplyIssue] Step 1 warning: Field update failed - {field_update_error}")
                    print(f"[ReplyIssue] Continuing with comment...")

            # Step 2: Add comment (with fallback field info if field update failed)
            print(f"[ReplyIssue] Step 2/3: Adding comment for {issue_id}")

            # If field update failed, append field info to comment
            enhanced_comment = comment
            if field_update_error and custom_fields:
                field_info_lines = ['\n\n---\n字段信息（因字段更新失败而记录）：']
                if custom_fields.get('solution'):
                    field_info_lines.append(f'解决方案：{custom_fields["solution"]}')
                if custom_fields.get('reply_method'):
                    field_info_lines.append(f'回复方式：{custom_fields["reply_method"]}')
                if custom_fields.get('issue_type_confirmed'):
                    field_info_lines.append(f'问题类型：{custom_fields["issue_type_confirmed"]}')
                enhanced_comment += '\n'.join(field_info_lines)
                print(f"[ReplyIssue] Enhanced comment with field info due to field update failure")

            comment_result = self.add_comment(issue_id, enhanced_comment)
            if not comment_result['success']:
                print(f"[ReplyIssue] Step 2 failed: {comment_result['message']}")
                # Attempt to rollback field updates if they were done
                if 'update_fields' in completed_steps:
                    print(f"[ReplyIssue] Attempting to rollback field updates...")
                    # Note: Full rollback may not be possible without original values
                    # Log for manual intervention
                    self._log_partial_failure(issue_id, completed_steps, 'add_comment', custom_fields)

                return {
                    'success': False,
                    'message': f'评论添加失败: {comment_result["message"]}',
                    'partial_success': 'update_fields' in completed_steps,
                    'completed_steps': completed_steps,
                    'failed_step': 'add_comment',
                    'warning': '字段可能已更新但评论未添加，请人工检查'
                }
            completed_steps.append('add_comment')
            print(f"[ReplyIssue] Step 2 completed: Comment added")

            message = '回复成功'
            if field_update_error:
                message += f'（注意：字段更新失败 - {field_update_error}）'
            return {
                'success': True,
                'message': message,
                'partial_success': field_update_error is not None,
                'completed_steps': completed_steps,
                'field_update_error': field_update_error
            }

        except Exception as e:
            # Unexpected error - log for manual intervention
            print(f"[ReplyIssue] Unexpected error: {e}")
            self._log_partial_failure(issue_id, completed_steps, f'exception:{str(e)}', custom_fields)
            return {
                'success': False,
                'message': f'操作异常: {str(e)}',
                'partial_success': len(completed_steps) > 0,
                'completed_steps': completed_steps,
                'failed_step': 'unknown',
                'warning': '操作过程中发生异常，请人工检查工单状态'
            }

    def _log_partial_failure(self, issue_id: str, completed_steps: list,
                            failed_step: str, custom_fields: Dict = None):
        """
        Log partial failure for manual intervention
        """
        import time
        log_entry = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'issue_id': issue_id,
            'completed_steps': completed_steps,
            'failed_step': failed_step,
            'custom_fields': custom_fields,
            'action_required': 'Manual intervention needed'
        }
        print(f"[ReplyIssue] PARTIAL FAILURE LOG: {json.dumps(log_entry, ensure_ascii=False)}")
        # TODO: Could also write to a persistent log file or send alert

    def reply_and_close_via_transition(self, issue_id: str, comment: str,
                                       custom_fields: Dict = None,
                                       ai_fields: Dict = None) -> dict:
        """
        通过"直接回复"工作流转换一次性完成: 字段更新 + 评论 + 状态流转
        这是原子操作，比分步调用更可靠。

        customfield_10410（回复方式）只能在转换屏幕中设置，无法通过常规字段更新 API 修改。

        Args:
            issue_id: 工单编号 (e.g., "MYPROJECT-12345")
            comment: 评论/解决方案内容
            custom_fields: {
                'solution': 解决方案文本 (customfield_10411),
                'reply_method': 回复方式 ID (customfield_10410),
                'issue_type_confirmed': 研发确认问题类型 ID (customfield_10729)
            }
            ai_fields: {
                'smart_result': 智能处理结果 ID (customfield_15703),
                'ai_result': AI处理结果文本 (customfield_15702),
                'use_agent': 使用智能体标记 (customfield_15803)
            }

        Returns:
            dict: {'success': bool, 'message': str, 'transition_used': str}
        """
        try:
            # Step 1: 获取可用转换，查找"直接回复"
            transitions_url = f"{self.base_url}/rest/api/2/issue/{issue_id}/transitions"
            response = requests.get(
                transitions_url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()

            transitions = response.json().get('transitions', [])
            reply_transition = None
            for t in transitions:
                if t.get('name') == '直接回复':
                    reply_transition = t
                    break

            if not reply_transition:
                # 回退：查找目标状态含"完成"的转换
                for t in transitions:
                    to_name = t.get('to', {}).get('name', '')
                    if '完成' in to_name:
                        reply_transition = t
                        break

            if not reply_transition:
                available = [f"{t['name']}(id={t['id']})" for t in transitions]
                return {
                    'success': False,
                    'message': f'未找到"直接回复"转换，可用转换: {", ".join(available)}'
                }

            transition_id = reply_transition['id']
            transition_name = reply_transition['name']
            print(f"[ReplyTransition] 使用转换: {transition_name}(id={transition_id}) for {issue_id}")

            # Step 2: 构建转换请求体（字段 + 评论 一次性提交）
            transition_data = {
                'transition': {'id': transition_id},
                'fields': {},
                'update': {
                    'comment': [{'add': {'body': comment}}]
                }
            }

            # 填充自定义字段
            if custom_fields:
                # 解决方案 (customfield_10411) - 纯文本
                if custom_fields.get('solution'):
                    transition_data['fields']['customfield_10411'] = custom_fields['solution']

                # 回复方式 (customfield_10410) - 单选，需要 {id: "xxx"} 格式
                if custom_fields.get('reply_method'):
                    reply_method_id = self._get_field_id_by_value(
                        'customfield_10410', custom_fields['reply_method'])
                    if reply_method_id:
                        transition_data['fields']['customfield_10410'] = {'id': reply_method_id}
                        print(f"[ReplyTransition] 回复方式: {custom_fields['reply_method']} -> ID {reply_method_id}")

                # 研发确认问题类型 (customfield_10729) - 单选
                if custom_fields.get('issue_type_confirmed'):
                    issue_type_id = self._get_field_id_by_value(
                        'customfield_10729', custom_fields['issue_type_confirmed'])
                    if issue_type_id:
                        transition_data['fields']['customfield_10729'] = {'id': issue_type_id}
                        print(f"[ReplyTransition] 问题类型: {custom_fields['issue_type_confirmed']} -> ID {issue_type_id}")

                # 领域模块 (customfield_10123) - 级联单选，前端传 {id} 字符串
                if custom_fields.get('domain_module'):
                    transition_data['fields']['customfield_10123'] = {'id': str(custom_fields['domain_module'])}
                    print(f"[ReplyTransition] 领域模块: id={custom_fields['domain_module']}")

            # 填充 AI 相关字段
            if ai_fields:
                # 智能处理结果 (customfield_15703) - 单选
                if ai_fields.get('smart_result'):
                    transition_data['fields']['customfield_15703'] = {'id': ai_fields['smart_result']}

                # AI处理结果 (customfield_15702) - 文本
                if ai_fields.get('ai_result'):
                    transition_data['fields']['customfield_15702'] = ai_fields['ai_result']

                # 使用智能体 (customfield_15803) - 文本
                if ai_fields.get('use_agent'):
                    transition_data['fields']['customfield_15803'] = ai_fields['use_agent']

            # 批量读取当前工单所有 transition screen 字段值并透传
            # 优先级：custom_fields 显式传入 > 工单已有值 > 硬编码默认值
            _passthrough_fields = [
                'customfield_15805', 'customfield_10123', 'customfield_10729',
                'customfield_10410', 'customfield_10411', 'customfield_12503',
                'customfield_12502', 'customfield_10725', 'customfield_10439',
                'customfield_10114', 'customfield_10401', 'customfield_10108',
                'customfield_11501', 'customfield_10436', 'customfield_11904',
            ]
            def _norm_field(fk, fv):
                # Cascading selects: strip 'self'/'value' that Jira rejects on POST
                # Keep only {id, child: {id}} format
                if isinstance(fv, dict) and 'id' in fv and 'child' in fv:
                    child = fv.get('child') or {}
                    result = {'id': fv['id']}
                    if child.get('id'):
                        result['child'] = {'id': child['id']}
                    return result
                if isinstance(fv, dict) and 'id' in fv and fk != 'customfield_15805':
                    return {'id': fv['id']}
                return fv

            try:
                _fstr = ','.join(_passthrough_fields)
                _issue_url = f"{self.base_url}/rest/api/2/issue/{issue_id}?fields={_fstr}"
                _issue_resp = requests.get(_issue_url, headers=self.headers,
                                           cookies=self.cookies if self.cookies else None,
                                           verify=self.ssl_verify, timeout=10, proxies=self.proxies)
                if _issue_resp.status_code == 200:
                    _cur = _issue_resp.json().get('fields', {})
                    for _fk in _passthrough_fields:
                        if _fk in transition_data['fields']:
                            continue  # already set by custom_fields above
                        _fv = _cur.get(_fk)
                        if _fv is not None:
                            transition_data['fields'][_fk] = _norm_field(_fk, _fv)
                    print(f"[ReplyTransition] 透传字段: {[k for k in _passthrough_fields if k in transition_data['fields']]}")
            except Exception as e:
                print(f"[ReplyTransition] 批量透传失败(忽略): {e}")

            # 产品模块 (customfield_15805) 兜底默认值
            if 'customfield_15805' not in transition_data['fields']:
                transition_data['fields']['customfield_15805'] = "应用平台|数字化建模|工作流|工作流设计"
                print(f"[ReplyTransition] 产品模块(默认值兜底)")
            # 领域模块 (customfield_10123)：不强制注入默认值，
            # 各项目允许的选项 ID 不同，跨项目硬编码会导致「选项ID无效」错误。
            # 若工单原值非空，透传逻辑已在上方写入；若为空则不发送，
            # Jira 返回 400 时由下方重试逻辑识别「无效选项」并移除后重试。
            # 回复方式 (customfield_10410) 兜底：此字段只能通过 transition 设置，
            # 新工单可能为空，默认用"指导解决"(id=10917)
            if 'customfield_10410' not in transition_data['fields']:
                transition_data['fields']['customfield_10410'] = {'id': '10917'}
                print(f"[ReplyTransition] 回复方式(默认值兜底: 指导解决)")

            print(f"[ReplyTransition] 提交转换请求: fields={list(transition_data['fields'].keys())}")

            # Step 3: 执行转换
            response = requests.post(
                transitions_url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                json=transition_data,
                verify=self.ssl_verify,
                timeout=15,
                proxies=self.proxies
            )

            if response.status_code == 204:
                print(f"[ReplyTransition] 成功: {issue_id} 已通过'{transition_name}'转换")
                return {
                    'success': True,
                    'message': f'回复并关闭成功（转换: {transition_name}）',
                    'transition_used': transition_name
                }

            # 400 error handling — two cases:
            # A) field not on screen → strip and retry
            # B) required field missing → give friendly Chinese error
            _FIELD_NAMES = {
                'customfield_10123': '领域模块',
                'customfield_10729': '研发确认问题类型',
                'customfield_10410': '回复方式',
                'customfield_10411': '解决方案',
                'customfield_15805': '产品模块',
                'customfield_12503': '版本',
                'customfield_12502': '修复版本',
            }
            if response.status_code == 400:
                try:
                    _err_data = response.json()
                    _errors = _err_data.get('errors', {})
                    _bad = [f for f, msg in _errors.items()
                            if 'cannot be set' in msg or 'not on the appropriate screen' in msg
                            or '无效' in msg or 'invalid' in msg.lower()]
                    _required = [f for f, msg in _errors.items()
                                 if 'is required' in msg and f not in _bad]
                    if _bad:
                        print(f"[ReplyTransition] 字段不在转换屏幕，移除后重试: {_bad}")
                        for _bf in _bad:
                            transition_data['fields'].pop(_bf, None)
                        response = requests.post(
                            transitions_url,
                            headers=self.headers,
                            cookies=self.cookies if self.cookies else None,
                            json=transition_data,
                            verify=self.ssl_verify,
                            timeout=15,
                            proxies=self.proxies
                        )
                        if response.status_code == 204:
                            print(f"[ReplyTransition] 重试成功: {issue_id} (已移除 {len(_bad)} 个屏幕外字段)")
                            return {
                                'success': True,
                                'message': f'回复并关闭成功（转换: {transition_name}）',
                                'transition_used': transition_name
                            }
                    if _required and not _bad:
                        _rnames = [_FIELD_NAMES.get(f, f) for f in _required]
                        _rmsg = '、'.join(_rnames)
                        print(f"[ReplyTransition] 必填字段缺失: {_required}")
                        return {
                            'success': False,
                            'message': f'工单缺少必填字段【{_rmsg}】，请先在 Jira 中补填后再关闭',
                            'transition_used': transition_name,
                            'missing_fields': _required,
                        }
                except Exception as _re:
                    print(f"[ReplyTransition] 重试处理异常(忽略): {_re}")

            # 处理错误
            error_detail = f"HTTP {response.status_code}"
            try:
                error_data = response.json()
                error_msgs = error_data.get('errorMessages', [])
                errors = error_data.get('errors', {})
                if error_msgs:
                    error_detail = '; '.join(error_msgs)
                elif errors:
                    error_parts = [f"{field}: {msg}" for field, msg in errors.items()]
                    error_detail = '; '.join(error_parts)
            except:
                if response.text:
                    error_detail = response.text[:300]

            print(f"[ReplyTransition] 失败: {error_detail}")
            return {
                'success': False,
                'message': f'转换执行失败: {error_detail}',
                'transition_used': transition_name
            }

        except requests.exceptions.Timeout:
            return {'success': False, 'message': '请求超时，请重试'}
        except requests.exceptions.HTTPError as e:
            error_msg = f"HTTP错误: {e.response.status_code}"
            if e.response.text:
                error_msg += f" - {e.response.text[:200]}"
            print(f"[ReplyTransition] HTTP error: {error_msg}")
            return {'success': False, 'message': error_msg}
        except Exception as e:
            print(f"[ReplyTransition] 异常: {e}")
            return {'success': False, 'message': f'系统错误: {str(e)}'}

    def _get_field_id_by_value(self, field_id: str, value: str) -> Optional[str]:
        """
        根据字段显示值获取对应的内部ID
        首先尝试将值本身作为ID，然后从缓存和默认值中查找

        Args:
            field_id: 字段ID，如 'customfield_10410'
            value: 显示值，如 '指导解决'

        Returns:
            str: 内部ID，如果找不到则返回原值
        """
        if not value:
            return None

        # 如果值本身就是数字ID，直接使用
        if str(value).isdigit():
            return str(value)

        # 从缓存中查找
        cache = self._load_field_options_cache()
        if cache and cache.get('fields', {}).get(field_id):
            options = cache['fields'][field_id].get('options', [])
            for opt in options:
                if opt['value'] == value:
                    return opt['id']

        # 硬编码默认值映射（数据来源: Jira transitions API 2026-03-28 验证）
        default_mappings = {
            'customfield_10410': {  # 回复方式（仅在"直接回复"转换屏幕可用）
                '紧急补丁/发布': '15953',
                '方案解决': '10916',
                '指导解决': '10917',
                '后续上线解决': '10918',
                '无效问题': '10919',
                '纳入需求库': '15316',
                '无法复现': '15317',
                '提供第三方代码工具包': '15599',
                '退回支持': '15702',
                '合集补丁发布': '27916'
            },
            'customfield_10729': {  # 研发确认问题类型
                '产品错误': '12041',
                '需求问题': '12042',
                '应用操作': '12043',
                '客开问题': '15318',
                '效率问题': '15319',
                '实施问题': '15320',
                '无效问题': '15321',
                '设计问题': '17693',
                'API问题': '17720',
                '环境问题': '17767',
                '安全问题': '17768',
                'UE问题': '20008',
                '升级问题': '25002',
                '运维问题': '28685',
                '数据错误': '28686'
            }
        }

        if field_id in default_mappings and value in default_mappings[field_id]:
            return default_mappings[field_id][value]

        # 找不到映射，返回原值（让Jira API决定如何处理）
        print(f"[JiraService] 警告: 字段 {field_id} 的值 '{value}' 未找到对应ID映射")
        return str(value)

    def update_issue_fields(self, issue_id: str, fields: Dict) -> dict:
        """
        Update issue custom fields using standard REST API
        支持将显示值自动映射为内部ID

        Args:
            issue_id: Issue key (e.g., "MYPROJECT-12345")
            fields: Dict of custom field values:
                - solution: 解决方案 (customfield_10411)
                - reply_method: 回复方式 (customfield_10410) - 可以是显示值或ID
                - issue_type_confirmed: 研发确认问题类型 (customfield_10729) - 可以是显示值或ID

        Returns:
            dict: {'success': bool, 'message': str}
        """
        url = f"{self.base_url}/rest/api/2/issue/{issue_id}"

        # Build update payload
        update_fields = {}

        # 解决方案字段 - 纯文本
        if fields.get('solution'):
            update_fields['customfield_10411'] = fields['solution']

        # 回复方式字段 - 单选，需要ID
        if fields.get('reply_method'):
            reply_method_id = self._get_field_id_by_value('customfield_10410', fields['reply_method'])
            if reply_method_id:
                update_fields['customfield_10410'] = {'id': reply_method_id}
                print(f"[JiraService] 回复方式映射: '{fields['reply_method']}' -> ID '{reply_method_id}'")

        # 研发确认问题类型字段 - 单选，需要ID
        if fields.get('issue_type_confirmed'):
            issue_type_id = self._get_field_id_by_value('customfield_10729', fields['issue_type_confirmed'])
            if issue_type_id:
                update_fields['customfield_10729'] = {'id': issue_type_id}
                print(f"[JiraService] 问题类型映射: '{fields['issue_type_confirmed']}' -> ID '{issue_type_id}'")

        if not update_fields:
            return {'success': True, 'message': '无字段需要更新'}

        data = {'fields': update_fields}

        print(f"[JiraService] 更新工单 {issue_id} 字段: {list(update_fields.keys())}")

        try:
            response = requests.put(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                json=data,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies,
            )

            # 详细记录响应
            print(f"[JiraService] 字段更新响应状态: {response.status_code}")
            if response.status_code != 204 and response.text:
                print(f"[JiraService] 字段更新响应内容: {response.text[:500]}")

            if response.status_code == 204:
                return {'success': True, 'message': '字段更新成功'}

            # 处理错误响应
            try:
                error_data = response.json()
                error_msgs = error_data.get('errorMessages', [])
                errors = error_data.get('errors', {})

                if error_msgs:
                    error_detail = '; '.join(error_msgs)
                elif errors:
                    # 字段特定的错误
                    error_parts = []
                    for field, msg in errors.items():
                        error_parts.append(f"{field}: {msg}")
                    error_detail = '; '.join(error_parts)
                else:
                    error_detail = f"HTTP {response.status_code}"

                print(f"[JiraService] 字段更新验证错误: {error_detail}")
                return {'success': False, 'message': f'字段验证失败: {error_detail}'}

            except:
                return {'success': False, 'message': f'HTTP错误: {response.status_code}'}

        except requests.exceptions.Timeout:
            return {'success': False, 'message': '请求超时，请重试'}
        except Exception as e:
            print(f"[JiraService] 字段更新异常: {e}")
            return {'success': False, 'message': f'系统错误: {str(e)}'}

    def update_issue_labels(self, issue_key: str, add: list = None, remove: list = None) -> dict:
        """通过 Jira REST PUT /rest/api/2/issue/{key} + update.labels 增量改标签."""
        add = [s.strip() for s in (add or []) if s and s.strip()]
        remove = [s.strip() for s in (remove or []) if s and s.strip()]
        if not add and not remove:
            return {'success': True, 'message': '无变更'}
        ops = [{'add': v} for v in add] + [{'remove': v} for v in remove]
        url = f"{self.base_url}/rest/api/2/issue/{issue_key}"
        try:
            response = requests.put(
                url, headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                json={'update': {'labels': ops}},
                verify=self.ssl_verify, timeout=10, proxies=self.proxies,
            )
            if response.status_code == 204:
                return {'success': True, 'message': '标签更新成功'}
            try:
                err = response.json()
                return {'success': False,
                        'message': str(err.get('errorMessages') or err.get('errors') or response.status_code)}
            except Exception:
                return {'success': False, 'message': f'HTTP {response.status_code}'}
        except requests.exceptions.RequestException as e:
            return {'success': False, 'message': str(e)}

    def close_issue(self, issue_id: str) -> dict:
        """
        Close an issue using workflow transition

        Returns:
            dict: {
                'success': bool,
                'message': str  # Success message or error details
            }
        """
        try:
            # Step 1: Get available transitions
            transitions_url = f"{self.base_url}/rest/api/2/issue/{issue_id}/transitions"

            response = requests.get(
                transitions_url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify,
                timeout=10
            )
            response.raise_for_status()

            transitions_data = response.json()
            transitions = transitions_data.get('transitions', [])

            # Find the "Close Issue" or "Closed" transition
            close_transition_id = None
            for transition in transitions:
                name = transition.get('name', '').lower()
                to_status = transition.get('to', {}).get('name', '').lower()
                if any(keyword in name or keyword in to_status for keyword in
                       ['close', '关闭', '完成', 'done', 'resolved', '解决']):
                    close_transition_id = transition.get('id')
                    break

            if not close_transition_id:
                # Try common transition IDs
                close_transition_id = '2'  # Common "Close Issue" transition ID

            # Step 2: Execute the transition
            do_transition_url = f"{self.base_url}/rest/api/2/issue/{issue_id}/transitions"

            transition_data = {
                'transition': {
                    'id': close_transition_id
                }
            }

            response = requests.post(
                do_transition_url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                json=transition_data,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies,
            )
            response.raise_for_status()

            return {
                'success': True,
                'message': '工单已关闭'
            }

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400:
                # Bad request - might be already closed or invalid transition
                error_text = e.response.text
                success, error_msg = self._parse_jira_response(error_text)
                if not success:
                    return {'success': False, 'message': error_msg}
                return {'success': False, 'message': '无法关闭工单，可能已经在关闭状态或权限不足'}
            error_msg = f"HTTP错误: {e.response.status_code}"
            print(f"[JiraService] Close HTTP error for {issue_id}: {error_msg}")
            return {'success': False, 'message': error_msg}
        except requests.exceptions.Timeout:
            error_msg = "请求超时，请重试"
            print(f"[JiraService] Close timeout for {issue_id}")
            return {'success': False, 'message': error_msg}
        except Exception as e:
            error_msg = f"系统错误: {str(e)}"
            print(f"[JiraService] Close error for {issue_id}: {e}")
            return {'success': False, 'message': error_msg}

    def _load_field_options_cache(self) -> Dict:
        """加载字段选项缓存"""
        if not os.path.exists(FIELD_OPTIONS_CACHE_FILE):
            return {}
        try:
            with open(FIELD_OPTIONS_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[JiraService] 加载字段选项缓存失败: {e}")
            return {}

    def _save_field_options_cache(self, cache_data: Dict):
        """保存字段选项缓存"""
        try:
            with open(FIELD_OPTIONS_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            print(f"[JiraService] 字段选项缓存已保存")
        except Exception as e:
            print(f"[JiraService] 保存字段选项缓存失败: {e}")

    def refresh_field_options_cache(self, project_key: str = None, issue_type_id: str = "10001") -> Dict:
        """
        刷新字段选项缓存
        使用createmeta API获取字段选项（不需要具体工单）

        Args:
            project_key: 项目key，如 "MYPROJECT"
            issue_type_id: 工单类型ID，如 "10001" (Support类型)

        Returns:
            dict: 缓存的字段选项数据
        """
        try:
            print(f"[JiraService] 开始刷新字段选项缓存...")

            # 方法1: 使用createmeta API获取创建工单时的字段选项
            url = f"{self.base_url}/rest/api/2/issue/createmeta"
            params = {
                'projectKeys': project_key,
                'issuetypeIds': issue_type_id,
                'expand': 'projects.issuetypes.fields'
            }

            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                params=params,
                verify=self.ssl_verify,
                timeout=15
            )
            response.raise_for_status()

            data = response.json()
            cache_data = {
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'source': 'createmeta',
                'fields': {}
            }

            # 解析字段选项
            projects = data.get('projects', [])
            if projects:
                issue_types = projects[0].get('issuetypes', [])
                if issue_types:
                    fields = issue_types[0].get('fields', {})
                    for field_id, field_meta in fields.items():
                        # 只处理单选/多选字段
                        field_type = field_meta.get('schema', {}).get('type', '')
                        if field_type in ['option', 'array']:
                            allowed_values = field_meta.get('allowedValues', [])
                            options = []
                            for value in allowed_values:
                                if isinstance(value, dict):
                                    option = {
                                        'id': str(value.get('id', '')),
                                        'value': str(value.get('value', value.get('name', '')))
                                    }
                                    if option['id'] and option['value']:
                                        options.append(option)
                            if options:
                                cache_data['fields'][field_id] = {
                                    'name': field_meta.get('name', field_id),
                                    'options': options
                                }

            # 方法2: 用 transitions API 补充 createmeta 缺失的字段（如 customfield_10410）
            # 需要一个真实工单来查 transitions，取最新的"待分析"工单
            try:
                search_url = f"{self.base_url}/rest/api/2/search"
                search_resp = requests.get(
                    search_url,
                    headers=self.headers,
                    cookies=self.cookies if self.cookies else None,
                    params={'jql': f'project={project_key} AND status=待分析 ORDER BY updated DESC',
                            'maxResults': '1', 'fields': 'key'},
                    verify=self.ssl_verify, timeout=10, proxies=self.proxies
                )
                if search_resp.status_code == 200:
                    issues = search_resp.json().get('issues', [])
                    if issues:
                        sample_key = issues[0]['key']
                        t_url = f"{self.base_url}/rest/api/2/issue/{sample_key}/transitions?expand=transitions.fields"
                        t_resp = requests.get(
                            t_url, headers=self.headers,
                            cookies=self.cookies if self.cookies else None,
                            verify=self.ssl_verify, timeout=10, proxies=self.proxies)
                        if t_resp.status_code == 200:
                            for t in t_resp.json().get('transitions', []):
                                for fid, fmeta in t.get('fields', {}).items():
                                    if fid not in cache_data['fields']:
                                        allowed = fmeta.get('allowedValues', [])
                                        options = []
                                        for v in allowed:
                                            if isinstance(v, dict) and not v.get('disabled'):
                                                opt = {
                                                    'id': str(v.get('id', '')),
                                                    'value': str(v.get('value', v.get('name', '')))
                                                }
                                                if opt['id'] and opt['value']:
                                                    options.append(opt)
                                        if options:
                                            cache_data['fields'][fid] = {
                                                'name': fmeta.get('name', fid),
                                                'options': options,
                                                'source': f'transition:{t["name"]}'
                                            }
                            cache_data['source'] = 'createmeta+transitions'
                            print(f"[JiraService] transitions 补充了字段选项，现共 {len(cache_data['fields'])} 个字段")
            except Exception as e:
                print(f"[JiraService] transitions补充缓存失败: {e}")

            # 保存缓存
            self._save_field_options_cache(cache_data)
            print(f"[JiraService] 字段选项缓存刷新完成，共 {len(cache_data['fields'])} 个字段")
            return cache_data

        except Exception as e:
            print(f"[JiraService] 刷新字段选项缓存失败: {e}")
            return {}

    def get_field_options(self, issue_id: str, field_ids: List[str]) -> Dict[str, List[Dict]]:
        """
        获取Jira自定义字段的枚举值选项
        多策略获取：1. editmeta API 2. createmeta API 3. 本地缓存 4. 硬编码默认值

        Args:
            issue_id: 工单编号(用于调用editmeta API)
            field_ids: 字段ID列表，如 ['customfield_10410', 'customfield_10729']

        Returns:
            dict: {
                'field_id': [
                    {'id': 'value1', 'value': '显示值1'},
                    {'id': 'value2', 'value': '显示值2'}
                ]
            }
        """
        result = {}
        cache = self._load_field_options_cache()

        # 策略1: 尝试从editmeta API获取
        try:
            url = f"{self.base_url}/rest/api/2/issue/{issue_id}/editmeta"
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )

            if response.status_code == 200:
                data = response.json()
                fields = data.get('fields', {})

                for field_id in field_ids:
                    field_meta = fields.get(field_id, {})
                    allowed_values = field_meta.get('allowedValues', [])

                    options = []
                    for value in allowed_values:
                        if isinstance(value, dict):
                            option = {
                                'id': str(value.get('id', '')),
                                'value': str(value.get('value', value.get('name', '')))
                            }
                            if option['id'] and option['value']:
                                options.append(option)

                    if options:
                        result[field_id] = options
                        print(f"[JiraService] 从editmeta获取到 {field_id}: {len(options)} 个选项")

        except Exception as e:
            print(f"[JiraService] editmeta获取失败: {e}")

        # 策略1.5: 对于 editmeta 未返回的字段（如 customfield_10410），从 transitions API 获取
        missing_fields = [fid for fid in field_ids if fid not in result]
        if missing_fields:
            try:
                t_url = f"{self.base_url}/rest/api/2/issue/{issue_id}/transitions?expand=transitions.fields"
                t_response = requests.get(
                    t_url, headers=self.headers,
                    cookies=self.cookies if self.cookies else None,
                    verify=self.ssl_verify, timeout=10, proxies=self.proxies)

                if t_response.status_code == 200:
                    for t in t_response.json().get('transitions', []):
                        t_fields = t.get('fields', {})
                        for field_id in missing_fields:
                            if field_id in t_fields and field_id not in result:
                                allowed = t_fields[field_id].get('allowedValues', [])
                                options = []
                                for v in allowed:
                                    if isinstance(v, dict):
                                        opt = {
                                            'id': str(v.get('id', '')),
                                            'value': str(v.get('value', v.get('name', '')))
                                        }
                                        if opt['id'] and opt['value'] and not v.get('disabled'):
                                            options.append(opt)
                                if options:
                                    result[field_id] = options
                                    print(f"[JiraService] 从transitions获取到 {field_id}: {len(options)} 个选项 (转换: {t['name']})")
            except Exception as e:
                print(f"[JiraService] transitions获取失败: {e}")

        # 策略2: 对于未获取到的字段，尝试从缓存获取
        if cache and cache.get('fields'):
            for field_id in field_ids:
                if field_id not in result and field_id in cache['fields']:
                    cached_options = cache['fields'][field_id].get('options', [])
                    if cached_options:
                        result[field_id] = cached_options
                        print(f"[JiraService] 从缓存获取到 {field_id}: {len(cached_options)} 个选项")

        # 策略3: 对于仍未获取到的字段，使用硬编码默认值
        # 数据来源: Jira transitions API (expand=transitions.fields) 2026-03-28 验证
        default_options = {
            'customfield_10410': [  # 回复方式（仅在"直接回复"转换屏幕可用）
                {'id': '15953', 'value': '紧急补丁/发布'},
                {'id': '10916', 'value': '方案解决'},
                {'id': '10917', 'value': '指导解决'},
                {'id': '10918', 'value': '后续上线解决'},
                {'id': '10919', 'value': '无效问题'},
                {'id': '15316', 'value': '纳入需求库'},
                {'id': '15317', 'value': '无法复现'},
                {'id': '15599', 'value': '提供第三方代码工具包'},
                {'id': '15702', 'value': '退回支持'},
                {'id': '27916', 'value': '合集补丁发布'}
            ],
            'customfield_10729': [  # 研发确认问题类型
                {'id': '12041', 'value': '产品错误'},
                {'id': '12042', 'value': '需求问题'},
                {'id': '12043', 'value': '应用操作'},
                {'id': '15318', 'value': '客开问题'},
                {'id': '15319', 'value': '效率问题'},
                {'id': '15320', 'value': '实施问题'},
                {'id': '15321', 'value': '无效问题'},
                {'id': '17693', 'value': '设计问题'},
                {'id': '17720', 'value': 'API问题'},
                {'id': '17768', 'value': '安全问题'},
                {'id': '20008', 'value': 'UE问题'},
                {'id': '25002', 'value': '升级问题'},
                {'id': '28685', 'value': '运维问题'},
                {'id': '28686', 'value': '数据错误'}
            ]
        }

        for field_id in field_ids:
            if field_id not in result and field_id in default_options:
                result[field_id] = default_options[field_id]
                print(f"[JiraService] 使用默认值 {field_id}: {len(default_options[field_id])} 个选项")

        return result

    def parse_issue_table_response(self, response_data: Dict) -> List[JiraIssue]:
        """Parse the specific JSON response from issueTable endpoint"""
        issues = []
        try:
            table_data = response_data.get('issueTable', {}).get('table', [])
            if not isinstance(table_data, str):
                return issues
                
            # Regex parsing of HTML table
            import re
            
            # Use DOTALL to match newlines
            html_content = table_data
            
            # Find all rows with issuerow class
            # Pattern to capture key and content
            # Matches: <tr ... data-issuekey="KEY" ... class="... issuerow ..."> ... </tr>
            # Note: We iterate over finding <tr ...> then finding closing </tr> is hard with regex due to nesting.
            # But these rows are usually top level in tbody.
            # Let's try splitting by <tr class="issuerow" ...
            
            # Robust enough regex for this specific table structure:
            # We look for the data-issuekey attribute which is unique enough for the row start
            
            # We can split the content by 'class="issuerow"' or similar.
            # Or use finditer on the row structure.
            
            row_pattern = re.compile(r'<tr\s+id="issuerow\d+"\s+rel="\d+"\s+data-issuekey="([^"]+)"\s+class="issuerow">', re.DOTALL)
            
            # Be careful, regex cannot easily find the matching </tr>.
            # But we can split the whole string by "class="issuerow"" which gives us chunks starting with a row.
            
            # Let's try a simpler approach invoked in the test script which seemed to work:
            # Assuming standard structure
            
            # Find all chunks that look like rows
            # Since we can't easily match closing tag, we can match until the next <tr or end of string?
            # Or just extract fields using loose regexes from the whole string? No, need to group by issue.
            
            # Let's just find all issue keys first, and then for each, find the snippet?
            # Better: split by `<tr id="issuerow`
            
            chunks = html_content.split('<tr id="issuerow')
            # Skip first chunk (header/pre-table)
            
            for chunk in chunks[1:]:
                # Restore the split delimiter for regex to work if needed, or just parse the chunk
                # chunk starts with `8690764" rel="..." ...`
                
                # Extract key
                key_match = re.search(r'data-issuekey="([^"]+)"', chunk)
                if not key_match:
                    continue
                key = key_match.group(1)
                
                # Extract Summary - <td class="summary">...<a ...>TEXT</a>
                summary_match = re.search(r'<td class="summary">.*?<a[^>]+>(.*?)</a>', chunk, re.DOTALL)
                summary = summary_match.group(1).strip() if summary_match else ""
                
                # Extract Status - <span ... jira-issue-status-lozenge ...>TEXT</span>
                status_match = re.search(r'<span class="[^"]*jira-issue-status-lozenge[^"]*".*?>(.*?)</span>', chunk, re.DOTALL)
                status = status_match.group(1).strip() if status_match else ""
                
                # Extract Assignee - <td class="assignee">...<a ...>TEXT</a>
                assignee_match = re.search(r'<td class="assignee">.*?<a[^>]+>(.*?)</a>', chunk, re.DOTALL)
                assignee = assignee_match.group(1).strip() if assignee_match else ""
                
                # Extract Creator - <td class="creator">...<a ...>TEXT</a>
                reporter_match = re.search(r'<td class="creator">.*?<a[^>]+>(.*?)</a>', chunk, re.DOTALL)
                reporter = reporter_match.group(1).strip() if reporter_match else ""
                
                # Extract Created - <td class="created">...<time ...>TEXT</time>
                # Use datetime attribute for full precision if available, else text
                created_match = re.search(r'<td class="created">.*?<time[^>]*datetime="([^"]+)"', chunk, re.DOTALL)
                created = self._format_date(created_match.group(1)) if created_match else ""
                
                # Extract Due Date - customfield_11919 (based on debug output)
                # <td class="customfield_11919">...<time ... datetime="...">...</time>
                due_match = re.search(r'<td class="customfield_11919">.*?<time[^>]*datetime="([^"]+)"', chunk, re.DOTALL)
                due_date = self._format_date(due_match.group(1)) if due_match else ""
                
                # Extract Project - <td class="project">...<a ...>TEXT</a>
                project_match = re.search(r'<td class="project">.*?<a[^>]+>(.*?)</a>', chunk, re.DOTALL)
                project_name = project_match.group(1).strip() if project_match else ""
                
                # Cleanup HTML entities if needed (e.g. &nbsp;)
                # Basic cleanup
                summary = summary.replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
                
                issues.append(JiraIssue(
                    key=key,
                    summary=summary,
                    status=status,
                    assignee=assignee,
                    reporter=reporter,
                    created=created,
                    updated="", # Not easily available in table
                    due_date=due_date,
                    priority="Normal", # Default
                    issue_type="Support", # Default
                    project_name=project_name, 
                    description=""
                ))
                
        except Exception as e:
            print(f"Parse error: {e}")
        return issues

    def parse_search_response(self, response_data: Dict) -> List[JiraIssue]:
        """
        Parse JSON response from /rest/api/2/search endpoint
        New standard REST API response format

        Args:
            response_data: JSON response from search API

        Returns:
            List of JiraIssue objects
        """
        issues = []
        try:
            issue_list = response_data.get('issues', [])

            for issue_data in issue_list:
                fields = issue_data.get('fields', {})

                key = issue_data.get('key', '')
                summary = fields.get('summary', '')

                # Status
                status_obj = fields.get('status', {})
                status = status_obj.get('name', '')

                # Assignee
                assignee_obj = fields.get('assignee', {})
                assignee = assignee_obj.get('displayName', '') if assignee_obj else ''

                # Reporter
                reporter_obj = fields.get('reporter', {})
                reporter = reporter_obj.get('displayName', '') if reporter_obj else ''

                # Dates
                created = self._format_date(fields.get('created', ''))
                updated = self._format_date(fields.get('updated', ''))
                # 优先使用标准duedate字段，如果不存在则使用自定义到期日字段(customfield_11919)
                due_date_raw = fields.get('duedate') or fields.get('customfield_11919', '')
                due_date = self._format_date(due_date_raw)

                # Priority
                priority_obj = fields.get('priority', {})
                priority = priority_obj.get('name', 'Normal') if priority_obj else 'Normal'

                # Issue Type
                issue_type_obj = fields.get('issuetype', {})
                issue_type = issue_type_obj.get('name', 'Support') if issue_type_obj else 'Support'

                # Project
                project_obj = fields.get('project', {})
                project_name = project_obj.get('name', '') if project_obj else ''

                # Description
                description = fields.get('description', '') or ''

                # Contact Information (custom fields)
                contact_name = fields.get('customfield_10404', '') or ''
                contact_info = fields.get('customfield_10405', '') or ''

                # 客户名称 (customfield_10725 是数组)
                customer_raw = fields.get('customfield_10725', [])
                customer_name = customer_raw[0] if isinstance(customer_raw, list) and customer_raw else (customer_raw or '')

                # 产品版本 (customfield_13529)
                version_raw = fields.get('customfield_13529', '')
                product_version = version_raw if isinstance(version_raw, str) else ''

                # 部署模式（从版本字符串提取）
                deploy_mode = ''
                if product_version:
                    pv = product_version.lower()
                    if '专属' in pv:
                        deploy_mode = '专属云'
                    elif '私有' in pv or '本地' in pv:
                        deploy_mode = '私有化'
                    elif '公有' in pv or '公共' in pv:
                        deploy_mode = '公有云'

                issues.append(JiraIssue(
                    key=key,
                    summary=summary,
                    status=status,
                    assignee=assignee,
                    reporter=reporter,
                    created=created,
                    updated=updated,
                    due_date=due_date,
                    priority=priority,
                    issue_type=issue_type,
                    project_name=project_name,
                    description=description,
                    contact_name=contact_name,
                    contact_info=contact_info,
                    customer_name=customer_name,
                    product_version=product_version,
                    deploy_mode=deploy_mode
                ))

        except Exception as e:
            print(f"[JiraService] Parse search response error: {e}")

        return issues

    def _format_date(self, date_str: str) -> str:
        """Format 2026-02-11T19:31:35+0800 to 2026-02-11 19:31:35"""
        if not date_str:
            return ""
        try:
            # Simple string manipulation to drop timezone and T
            # If standard ISO format
            if 'T' in date_str:
                date_part, time_part = date_str.split('T')
                # Remove timezone +0800
                if '+' in time_part:
                     time_part = time_part.split('+')[0]
                return f"{date_part} {time_part}"
            return date_str
        except:
            return date_str

    def save_board_cache(self, issues: List[JiraIssue]):
        """保存看板数据到本地缓存（用于同步到服务器）"""
        cache_file = self._board_cache_file()
        if not cache_file:
            return
        cache_data = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'count': len(issues),
            'issues': [asdict(issue) for issue in issues]
        }
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            print(f"[JiraService] 已保存 {len(issues)} 条工单到缓存")
        except Exception as e:
            print(f"[JiraService] 保存缓存失败: {e}")

    def load_board_cache(self) -> List[JiraIssue]:
        """从本地缓存加载看板数据（服务器离线模式）"""
        cache_file = self._board_cache_file()
        if not cache_file or not os.path.exists(cache_file):
            print("[JiraService] 缓存文件不存在")
            return []

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            issues = []
            for item in cache_data.get('issues', []):
                issue = JiraIssue(
                    key=item.get('key', ''),
                    summary=item.get('summary', ''),
                    status=item.get('status', ''),
                    assignee=item.get('assignee', ''),
                    reporter=item.get('reporter', ''),
                    created=item.get('created', ''),
                    updated=item.get('updated', ''),
                    due_date=item.get('due_date'),
                    priority=item.get('priority', 'Normal'),
                    issue_type=item.get('issue_type', 'Support'),
                    project_name=item.get('project_name', ''),
                    description=item.get('description', ''),
                    contact_name=item.get('contact_name', ''),
                    contact_info=item.get('contact_info', ''),
                    customer_name=item.get('customer_name', ''),
                    product_version=item.get('product_version', ''),
                    deploy_mode=item.get('deploy_mode', ''),
                )
                issues.append(issue)

            timestamp = cache_data.get('timestamp', 'unknown')
            print(f"[JiraService] 从缓存加载 {len(issues)} 条工单 (更新时间: {timestamp})")
            return issues

        except Exception as e:
            print(f"[JiraService] 加载缓存失败: {e}")
            return []

    def get_cache_info(self) -> Dict:
        """获取缓存信息"""
        cache_file = self._board_cache_file()
        if not cache_file or not os.path.exists(cache_file):
            return {'exists': False}

        try:
            stat = os.stat(cache_file)
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            return {
                'exists': True,
                'timestamp': cache_data.get('timestamp'),
                'timestamp_epoch': stat.st_mtime,
                'count': cache_data.get('count', 0),
                'file_size': stat.st_size
            }
        except Exception as e:
            return {'exists': False, 'error': str(e)}

    # ---- REST 探测方法（字段元数据服务使用） ----

    def get_statuses(self) -> list:
        """GET /rest/api/2/status — 获取所有状态"""
        url = f"{self.base_url}/rest/api/2/status"
        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[JiraService] 获取状态列表失败: {e}")
            return []

    def get_priorities(self) -> list:
        """GET /rest/api/2/priority — 获取所有优先级"""
        url = f"{self.base_url}/rest/api/2/priority"
        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[JiraService] 获取优先级列表失败: {e}")
            return []

    def get_projects(self) -> list:
        """GET /rest/api/2/project — 获取所有项目"""
        url = f"{self.base_url}/rest/api/2/project"
        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[JiraService] 获取项目列表失败: {e}")
            return []

    def get_assignable_users(self, project_key: str = None) -> list:
        """GET /rest/api/2/user/assignable/search?project={key} — 获取可分配用户"""
        url = f"{self.base_url}/rest/api/2/user/assignable/search"
        params = {"project": project_key, "maxResults": 1000}
        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                params=params,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[JiraService] 获取可分配用户失败: {e}")
            return []

    def get_myself(self) -> dict:
        """GET /rest/api/2/myself — 返回当前认证用户信息"""
        try:
            r = requests.get(
                f"{self.base_url}/rest/api/2/myself",
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify, timeout=8, proxies=self.proxies
            )
            if r.ok:
                data = r.json()
                return {"username": data.get("name", ""), "displayName": data.get("displayName", "")}
        except Exception as e:
            print(f"[JiraService] get_myself失败: {e}")
        return {}

    def search_users(self, query: str, max_results: int = 20) -> list:
        """在Jira用户目录搜索用户（支持中文名/用户名模糊匹配）
        优先使用 user/picker（更好的子串模糊匹配），降级到 user/search。
        """
        # Method 1: user/picker — Jira自身搜索UI使用的接口，支持中文名子串匹配
        try:
            r = requests.get(
                f"{self.base_url}/rest/api/2/user/picker",
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                params={"query": query, "maxResults": max_results, "showAvatar": "false"},
                verify=self.ssl_verify, timeout=8, proxies=self.proxies
            )
            if r.ok:
                users = r.json().get("users", [])
                if users:
                    return [
                        {"username": u["name"], "displayName": u.get("displayName", u["name"]), "active": True}
                        for u in users
                    ]
            else:
                print(f"[JiraService] user/picker HTTP {r.status_code}")
        except Exception as e:
            print(f"[JiraService] user/picker失败: {e}")

        # Method 2: user/search fallback
        try:
            r = requests.get(
                f"{self.base_url}/rest/api/2/user/search",
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                params={"username": query, "maxResults": max_results, "includeInactive": "false"},
                verify=self.ssl_verify, timeout=8, proxies=self.proxies
            )
            if r.ok:
                return [
                    {"username": u["name"], "displayName": u.get("displayName", u["name"]),
                     "active": u.get("active", True)}
                    for u in r.json()
                    if u.get("active", True) and not u.get("deleted", False)
                ]
            print(f"[JiraService] user/search HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[JiraService] 用户搜索失败: {e}")
        return []

    def get_jql_autocomplete(self) -> dict:
        """GET /rest/api/2/jql/autocompletedata — 获取JQL自动补全数据（所有可查询字段）"""
        url = f"{self.base_url}/rest/api/2/jql/autocompletedata"
        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[JiraService] 获取JQL自动补全数据失败: {e}")
            return {}

    def get_jql_suggestions(self, field_name: str) -> list:
        """GET /rest/api/2/jql/autocompletedata/suggestions?fieldName={name} — 获取字段值建议"""
        url = f"{self.base_url}/rest/api/2/jql/autocompletedata/suggestions"
        params = {"fieldName": field_name}
        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                params=params,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except requests.exceptions.RequestException as e:
            print(f"[JiraService] 获取字段 '{field_name}' 值建议失败: {e}")
            return []

    def get_server_info(self) -> dict:
        """GET /rest/api/2/serverInfo — 获取服务器信息"""
        url = f"{self.base_url}/rest/api/2/serverInfo"
        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[JiraService] 获取服务器信息失败: {e}")
            return {}

    def get_issue_full(self, issue_key: str) -> dict:
        """GET /rest/api/2/issue/{key}?fields=*all — 获取工单全部字段"""
        url = f"{self.base_url}/rest/api/2/issue/{issue_key}"
        params = {"fields": "*all"}
        try:
            response = requests.get(
                url,
                headers=self.headers,
                cookies=self.cookies if self.cookies else None,
                params=params,
                verify=self.ssl_verify,
                timeout=10,
                proxies=self.proxies
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"[JiraService] 获取工单 '{issue_key}' 全部字段失败: {e}")
            return {}

    def _try_chrome_cookie_refresh(self, domain: str) -> list:
        """从 Chrome Keychain 解密 Jira session cookies（macOS only）。
        DEPRECATED 2026-05-19: 已委派给 JiraSessionRefresher，保留 6 周作回退。"""
        try:
            from services.jira_session_refresher import JiraSessionRefresher
            return JiraSessionRefresher.get_instance()._chrome_decrypt(domain)
        except Exception:
            pass
        # Legacy inline fallback（保留以防 import 失败；仅 macOS）
        import platform as _platform
        if _platform.system() != "Darwin":
            return []
        import shutil, sqlite3, subprocess
        from hashlib import pbkdf2_hmac

        cookies_src = os.path.expanduser(
            "~/Library/Application Support/Google/Chrome/Default/Cookies"
        )
        if not os.path.exists(cookies_src):
            print("[MoveIssue] Chrome Cookies DB 不存在，跳过解密")
            return []

        try:
            from Crypto.Cipher import AES
        except ImportError:
            print("[MoveIssue] 缺少 pycryptodomex，跳过 Chrome 解密")
            return []

        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                print("[MoveIssue] Keychain 无法读取 Chrome Safe Storage")
                return []
            key = pbkdf2_hmac("sha1", r.stdout.strip().encode(), b"saltysalt", 1003, 16)

            import tempfile as _tempfile
            dst = os.path.join(_tempfile.gettempdir(), "chrome_cookies_copy.db")
            shutil.copy2(cookies_src, dst)
            conn = sqlite3.connect(dst)
            rows = conn.execute(
                "SELECT host_key,name,value,encrypted_value,path,"
                "is_secure,is_httponly,expires_utc,samesite "
                "FROM cookies WHERE host_key LIKE '%" + (urlparse(self.base_url).hostname or "").split(".")[-2] + "%'"
            ).fetchall()
            conn.close()
            os.remove(dst)

            def _decrypt(enc):
                if not enc or enc[:3] != b"v10":
                    return None
                ct = enc[3:]
                pt = AES.new(key, AES.MODE_CBC, b" " * 16).decrypt(ct)
                pt = pt[: -pt[-1]]          # strip PKCS7 padding
                # Chrome 130+ macOS: 前 32 字节是 host SHA256，须跳过
                if len(pt) > 32:
                    candidate = pt[32:].decode("utf-8", errors="replace")
                    if all(c.isprintable() or c in "\n\r\t" for c in candidate[:20]):
                        return candidate
                return pt.decode("utf-8", errors="replace")

            samesite_map = {-1: "None", 0: "None", 1: "Lax", 2: "Strict"}
            cookies = []
            for host, name, plain, enc, path, secure, httponly, exp, ss in rows:
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

            print(f"[MoveIssue] Chrome解密获取 {len(cookies)} cookies")
            return cookies
        except Exception as e:
            print(f"[MoveIssue] Chrome解密异常: {e}")
            return []

    def _ensure_browser_session(self) -> str:
        """确保agent-browser session存在。优先验证已有session，fallback到config cookies或Jira session API"""
        import json as _json, time as _t
        from services.host_context import session_path as _session_path
        state_path = _session_path()
        domain = self.base_url.replace("https://", "").replace("http://", "").split("/")[0]

        def _validate_cookies(cookies_list) -> bool:
            """验证cookies是否已登录（用 GET /rest/auth/1/session，返回用户名则有效）"""
            if not cookies_list:
                return False
            cookie_dict = {c["name"]: c["value"] for c in cookies_list}
            try:
                r = requests.get(
                    f"{self.base_url}/rest/auth/1/session",
                    cookies=cookie_dict,
                    headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                    verify=self.ssl_verify, timeout=5,
                    allow_redirects=True,
                    proxies=getattr(self, "proxies", None),
                )
                if r.status_code == 200:
                    data = r.json()
                    # 已登录时返回 {"name": "JSESSIONID", "value": "...", "loginInfo": {...}}
                    return bool(data.get("name") or data.get("loginInfo"))
                return False
            except Exception:
                return False

        # 检查已有session文件（4小时内 + 验证有效性）
        if os.path.exists(state_path):
            age = _t.time() - os.path.getmtime(state_path)
            if age < 14400:
                try:
                    with open(state_path) as f:
                        saved = _json.load(f)
                    existing = saved.get("cookies", [])
                    if _validate_cookies(existing):
                        return state_path
                    print("[MoveIssue] 已有session失效，重新构建")
                except Exception:
                    pass
            else:
                print("[MoveIssue] session文件超过4小时，重新构建")

        # 优先通过 JiraSessionRefresher 刷新（统一入口；Chrome 解密 > REST fallback）
        print("[MoveIssue] 尝试 JiraSessionRefresher 刷新（web UI 需要真实浏览器 session）...")
        try:
            import shutil as _shutil
            from services.jira_session_refresher import JiraSessionRefresher
            _refresher = JiraSessionRefresher.get_instance()
            _refresher.refresh_now()          # 写入全局 session 文件
            from services.host_context import session_path as _session_path
            _global = _session_path()
            if os.path.exists(_global):
                with open(_global) as _f:
                    _saved = _json.load(_f)
                _fresh = _saved.get("cookies", [])
                if _validate_cookies(_fresh):
                    # 若 state_path 是 per-user 路径，同步一份
                    if state_path != _global:
                        _shutil.copy2(_global, state_path)
                    print(f"[MoveIssue] Refresher 刷新成功 ({len(_fresh)} cookies)")
                    return state_path
                print("[MoveIssue] Refresher 刷新后 cookies 仍无效，继续 fallback")
                cookies_list = _fresh
            else:
                cookies_list = []
        except Exception as _re:
            print(f"[MoveIssue] Refresher 调用异常: {_re}，回退 Chrome 解密")
            cookies_list = self._try_chrome_cookie_refresh(domain)

        # Chrome 解密失败时，尝试用当前 JiraService 的 cookies 构建 session
        if not cookies_list and hasattr(self, 'cookies') and self.cookies:
            print("[MoveIssue] Chrome 解密无结果，使用当前 JiraService cookies 构建 session...")
            for name, value in self.cookies.items():
                if value:
                    cookies_list.append({"name": name, "value": value, "domain": domain, "path": "/"})
            if cookies_list and _validate_cookies(cookies_list):
                print(f"[MoveIssue] JiraService cookies 有效: {len(cookies_list)} cookies")
            elif cookies_list:
                print("[MoveIssue] JiraService cookies 验证失败，继续尝试 REST 登录")
                cookies_list = []

        # 仍然失败时，回退 REST 登录（REST JSESSIONID 只能用于 REST API，但聊胜于无）
        if not cookies_list:
            print("[MoveIssue] 回退 REST 登录...")
            username = self._auth_override.get("username") or self.config_parser.username
            password = self._auth_override.get("password") or self.config_parser.password
            if username and password:
                try:
                    r = requests.post(
                        f"{self.base_url}/rest/auth/1/session",
                        json={"username": username, "password": password},
                        headers={"Content-Type": "application/json", "Accept": "application/json",
                                 "User-Agent": "curl/8.7.1"},
                        verify=self.ssl_verify, timeout=10,
                        proxies=getattr(self, "proxies", None),
                    )
                    if r.ok:
                        session_data = r.json().get("session", {})
                        if session_data.get("name") and session_data.get("value"):
                            cookies_list.append({
                                "name": session_data["name"],
                                "value": session_data["value"],
                                "domain": domain, "path": "/",
                            })
                        for name, value in r.cookies.items():
                            if not any(c["name"] == name for c in cookies_list):
                                cookies_list.append({"name": name, "value": value, "domain": domain, "path": "/"})
                        print(f"[MoveIssue] REST 登录获取到 {len(cookies_list)} cookies（注意：不适用于 MoveIssue web UI，仅兜底）")
                    else:
                        print(f"[MoveIssue] REST 登录返回 HTTP {r.status_code}")
                except Exception as e:
                    print(f"[MoveIssue] REST 登录失败: {e}")

        if cookies_list:
            # Playwright storageState format — JSESSIONID must have httpOnly+secure+sameSite for browser to send it
            for c in cookies_list:
                if c.get("name") == "JSESSIONID":
                    c.setdefault("httpOnly", True)
                    c.setdefault("secure", True)
                    c.setdefault("sameSite", "None")
                else:
                    c.setdefault("httpOnly", False)
                    c.setdefault("secure", False)
                    c.setdefault("sameSite", "Lax")
                c.setdefault("expires", -1)
            state = {"cookies": cookies_list, "origins": []}
            with open(state_path, "w") as f:
                _json.dump(state, f)
            print(f"[MoveIssue] 构建browser session: {len(cookies_list)} cookies")
            return state_path
        return ""

    def move_issue(self, issue_id: str, target_project_id: str,
                   issuetype_id: str = "10400", field_values: Dict = None) -> dict:
        """
        通过独立子进程运行 Playwright 操作 Jira MoveIssue 界面移动工单。
        使用子进程避免阻塞 uvicorn 线程池（Playwright sync API 会长时间占用线程）。
        """
        import subprocess as _sp, sys as _sys

        from services.host_context import session_path as _session_path
        state_path = self._ensure_browser_session() or _session_path()

        script = os.path.join(os.path.dirname(__file__), "scripts", "move_issue_playwright.py")
        proxy_url = (getattr(self, "proxies", {}) or {}).get("https") or \
                    (getattr(self, "proxies", {}) or {}).get("http") or ""

        username = getattr(self, '_auth_override', {}).get('username') or getattr(self.config_parser, 'username', '')
        password = getattr(self, '_auth_override', {}).get('password') or getattr(self.config_parser, 'password', '')

        cmd = [_sys.executable, script, issue_id, target_project_id, state_path,
               "--base-url", self.base_url,
               "--ssl-verify", "0" if not self.ssl_verify else "1"]
        if proxy_url:
            cmd += ["--proxy", proxy_url]
        if username:
            cmd += ["--username", username]
        if password:
            cmd += ["--password", password]
        for k, v in (field_values or {}).items():
            cmd += ["--field", f"{k}={v}"]

        try:
            result = _sp.run(cmd, capture_output=True, text=True, timeout=60)
            stdout = result.stdout.strip()
            if not stdout:
                stderr_excerpt = result.stderr.strip()[-200:] if result.stderr else "无输出"
                return {"success": False, "message": f"子进程无输出: {stderr_excerpt}"}
            parsed = json.loads(stdout)
            print(f"[MoveIssue] Playwright结果: success={parsed.get('success')} message={parsed.get('message')} new_key={parsed.get('new_key')}")
            return parsed
        except _sp.TimeoutExpired:
            return {"success": False, "message": "移动工单超时（60s），请检查Jira连通性"}
        except json.JSONDecodeError as e:
            return {"success": False, "message": f"子进程输出解析失败: {result.stdout[:100]}"}
        except Exception as e:
            return {"success": False, "message": f"移动失败: {str(e)}"}

    def get_move_targets(self, issue_id: str) -> dict:
        """获取移动工单可选的目标项目列表"""
        session = requests.Session()
        session.headers.update(self.headers)
        session.cookies.update(self.cookies)
        session.verify = self.ssl_verify

        from services.host_context import session_path as _session_path
        state_path = _session_path()
        try:
            if os.path.exists(state_path):
                import json as _json
                with open(state_path) as f:
                    state = _json.load(f)
                for c in state.get("cookies", []):
                    _jira_host = urlparse(self.base_url).hostname or ""
                    if _jira_host and _jira_host in c.get("domain", ""):
                        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
        except Exception:
            pass

        try:
            proj_r = session.get(f"{self.base_url}/rest/api/2/project", timeout=10)
            if proj_r.ok:
                projects = [{"id": p["id"], "key": p["key"], "name": p["name"]} for p in proj_r.json()]
                return {"success": True, "projects": projects}
            return {"success": False, "message": f"HTTP {proj_r.status_code}"}
        except Exception as e:
            return {"success": False, "message": str(e)}


# Singleton
jira_service = JiraService()
