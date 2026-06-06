"""模块感知智能回复 SDK — 供其他模块团队 import 使用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional
import requests


@dataclass
class KBRef:
    name: str
    module: str
    score: float


@dataclass
class ReplyResult:
    text: str
    kb_refs: List[KBRef]
    module_used: Optional[str]
    module_match_score: Optional[float]
    fallback_used: bool
    cached: bool
    word_count: int


class ReplyClient:
    """
    用法::

        from APP.backend.sdk.reply_client import ReplyClient
        client = ReplyClient(base_url="http://localhost:3000")
        result = client.generate(issue_key="MYPROJECT-12345", module="流程中心")
        print(result.text)
        print(result.kb_refs)
        print(result.module_used)

    模块覆盖度查询::

        cov = client.coverage("流程中心")
        # cov["coverage_level"] in ("high", "medium", "low")
    """

    def __init__(self, base_url: str = "http://localhost:3000", api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.trust_env = False   # 绕过 Surge/all_proxy 对 localhost 的拦截
        if api_key:
            self._session.headers.update({"X-API-Key": api_key})

    def generate(
        self,
        issue_key: str,
        module: Optional[str] = None,
        force: bool = False,
        timeout: int = 90,
    ) -> ReplyResult:
        """生成模块感知智能回复。module=None 时自动推断。"""
        resp = self._session.post(
            f"{self.base_url}/api/reply/generate-by-module",
            json={"issue_key": issue_key, "module": module, "force": force},
            timeout=timeout,
        )
        resp.raise_for_status()
        d = resp.json()
        return ReplyResult(
            text=d.get("reply", ""),
            kb_refs=[KBRef(**r) for r in d.get("kb_refs", [])],
            module_used=d.get("module_used"),
            module_match_score=d.get("module_match_score"),
            fallback_used=d.get("fallback_used", True),
            cached=d.get("cached", False),
            word_count=d.get("word_count", 0),
        )

    def coverage(self, module: str, timeout: int = 10) -> dict:
        """查询模块在 KB 中的覆盖度。返回 coverage_level / kb_docs_module / recommendation 等字段。"""
        resp = self._session.get(
            f"{self.base_url}/api/reply/module-coverage",
            params={"module": module},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
