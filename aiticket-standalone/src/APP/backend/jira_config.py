import re
import os
import base64
from typing import Dict, Any, Optional, Tuple

class JiraConfigParser:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.sections: Dict[str, str] = {}
        self.username: str = ""
        self.password: str = ""
        self.load()

    def load(self):
        """Load and parse the markdown file"""
        if not os.path.exists(self.filepath):
            print(f"Config file not found: {self.filepath}")
            return

        with open(self.filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        # Parse username and password from preamble
        self._parse_credentials(content)

        # Split by ## Headers
        # Format: ## Title\nContent
        parts = re.split(r'^##\s+(.+)$', content, flags=re.MULTILINE)

        # parts[0] is preamble. then title, content, title, content
        self.preamble = parts[0]

        for i in range(1, len(parts), 2):
            title = parts[i].strip()
            section_content = parts[i+1].strip()
            self.sections[title] = section_content

    def _parse_credentials(self, content: str):
        """Parse username and password; env vars take priority over md file."""
        env_user = os.environ.get('JIRA_USERNAME')
        env_pass = os.environ.get('JIRA_PASSWORD')
        if env_user and env_pass:
            self.username = env_user
            self.password = env_pass
            return

        username_match = re.search(r'^username:\s*(.+)$', content, re.MULTILINE | re.IGNORECASE)
        password_match = re.search(r'^password:\s*(.+)$', content, re.MULTILINE | re.IGNORECASE)

        if username_match:
            self.username = username_match.group(1).strip()
        if password_match:
            self.password = password_match.group(1).strip()

    def get_basic_auth_header(self) -> Optional[str]:
        """Generate Basic Auth header value (Base64 encoded)"""
        if not self.username or not self.password:
            return None
        auth_str = f"{self.username}:{self.password}"
        encoded_auth = base64.b64encode(auth_str.encode()).decode()
        return f"Basic {encoded_auth}"

    def save(self):
        """Save changes back to markdown file"""
        with open(self.filepath, 'w', encoding='utf-8') as f:
            f.write(self.preamble)
            for title, content in self.sections.items():
                f.write(f"\n## {title}\n{content}\n")

    def update_section(self, title: str, content: str):
        """Update a specific section"""
        self.sections[title] = content.strip()
        self.save()

    def get_section(self, title: str) -> str:
        return self.sections.get(title, "")

    def parse_curl_command(self, curl_str: str) -> Dict[str, Any]:
        """
        Parse a curl command string to extract url, headers, cookies, data.
        This is a basic parser tailored for the format in jira_api.md
        """
        config = {
            "url": "",
            "method": "GET",
            "headers": {},
            "cookies": {},
            "data": {}
        }

        # Extract URL (first argument usually)
        # curl 'URL' or curl --location ... 'URL'
        url_match = re.search(r"curl\s+(?:--[\w-]+\s+)*'([^']+)'", curl_str)
        if url_match:
            config["url"] = url_match.group(1)
        
        # Extract Headers (-H 'Key: Value' or --header 'Key: Value')
        # Handle multiline
        header_pattern = re.compile(r"(?:-H|--header)\s+'([^:]+):\s*([^']+)'")
        for match in header_pattern.finditer(curl_str):
            key = match.group(1).strip()
            value = match.group(2).strip()
            
            if key.lower() == 'cookie':
                # Parse cookies from header
                cookie_parts = value.split(';')
                for part in cookie_parts:
                    if '=' in part:
                        k, v = part.split('=', 1)
                        config["cookies"][k.strip()] = v.strip()
            else:
                config["headers"][key] = value

        # Extract Cookies from -b or --cookie
        cookie_flag_pattern = re.compile(r"(?:-b|--cookie)\s+'([^']+)'")
        for match in cookie_flag_pattern.finditer(curl_str):
            cookie_str = match.group(1).strip()
            cookie_parts = cookie_str.split(';')
            for part in cookie_parts:
                if '=' in part:
                    k, v = part.split('=', 1)
                    config["cookies"][k.strip()] = v.strip()

        # Determine method (default POST if data present, else GET)
        if "--data" in curl_str or "--data-raw" in curl_str or "--data-urlencode" in curl_str:
            config["method"] = "POST"
            
        # Extract explicit method
        method_match = re.search(r"--request\s+([A-Z]+)", curl_str)
        if method_match:
            config["method"] = method_match.group(1)

        return config

    def get_auth_config(self, include_cookies: bool = False) -> Dict[str, Any]:
        """
        Get authentication configuration using Basic Auth

        Args:
            include_cookies: Whether to include cookies from curl config (for backward compatibility)

        Returns:
            dict with 'headers' and optionally 'cookies'
        """
        config = {
            "headers": {
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            "cookies": {}
        }

        # Add Basic Auth header
        auth_header = self.get_basic_auth_header()
        if auth_header:
            config["headers"]["Authorization"] = auth_header

        # Optionally include cookies: prefer runtime /tmp/jira-session.json over stale file cookies
        if include_cookies:
            runtime_cookies = self._load_runtime_cookies()
            if runtime_cookies:
                config["cookies"] = runtime_cookies
            else:
                # Fallback: legacy curl config in jira_api.md (may be stale)
                search_section = self.get_section("工单查询")
                if search_section:
                    curl_config = self.parse_curl_command(search_section)
                    config["cookies"] = curl_config.get("cookies", {})
                    for key, value in curl_config.get("headers", {}).items():
                        if key.lower() not in ["content-type", "accept", "authorization"]:
                            config["headers"][key] = value

        return config

    def _load_runtime_cookies(self) -> Dict[str, str]:
        """从 session 文件读取最新运行时 cookies（JiraSessionRefresher 维护）。
        若文件不存在或超过 2 小时，返回空 dict 使调用方回落到 jira_api.md。"""
        import json as _json, os as _os, time as _t
        from services.host_context import session_path as _session_path
        path = _session_path()
        if not _os.path.exists(path):
            return {}
        try:
            if _t.time() - _os.path.getmtime(path) > 7200:
                return {}
            with open(path, encoding="utf-8") as f:
                state = _json.load(f)
            return {c["name"]: c["value"] for c in state.get("cookies", []) if c.get("name") and c.get("value")}
        except Exception:
            return {}

    def get_common_config(self) -> Dict[str, Any]:
        """
        DEPRECATED: Use get_auth_config() instead.
        Get common configuration (Headers, Cookies, BaseURL) from '工单查询' section.
        """
        search_section = self.get_section("工单查询")
        if not search_section:
            return {}

        return self.parse_curl_command(search_section)
