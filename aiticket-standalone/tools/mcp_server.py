#!/usr/bin/env python3
"""aiticket compact — MCP server（薄 stdio↔HTTP 桥）。

把本地 aiticket 服务的只读能力暴露为 MCP 工具，供调用方 Agent
（Claude Code / OpenClaw / WorkBuddy）用**各自的 LLM** 编排与生成回复。
服务侧不强制配 LLM（纯 MCP 委托模式）：本 server 只提供"上下文 + 证据 + prompt 模板"。

它不导入后端重模块，只通过 HTTP 调本机已运行的 uvicorn（复用现有端点 +
context_only 新端点），鉴权用 skill token（X-Skill-Token / Authorization: Bearer）。

配置（按优先级）：
  - 环境变量 AITICKET_BASE_URL（默认 http://127.0.0.1:<env.json port|18080>）
  - 环境变量 AITICKET_SKILL_TOKEN（或 AITICKET_HOME/config/env.json 的 skill_token）

依赖：pip install -r tools/requirements-mcp.txt （mcp + httpx）
运行：python tools/mcp_server.py   （stdio，由 MCP 客户端拉起）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aiticket_paths as P  # noqa: E402

try:
    import httpx
except ImportError:
    print("缺少 httpx：pip install -r tools/requirements-mcp.txt", file=sys.stderr)
    raise

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("缺少 mcp SDK：pip install -r tools/requirements-mcp.txt", file=sys.stderr)
    raise


# ---------- 配置解析 ----------

def _load_env_json() -> dict:
    home = P.default_home()
    ej = P.env_json_path(home)
    try:
        if ej.exists():
            return json.loads(ej.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


_ENV = _load_env_json()


def _base_url() -> str:
    if os.environ.get("AITICKET_BASE_URL"):
        return os.environ["AITICKET_BASE_URL"].rstrip("/")
    port = int(_ENV.get("port") or P.resolve_port(P.default_home()))
    return P.base_url(port)


def _skill_token() -> str:
    return (os.environ.get("AITICKET_SKILL_TOKEN")
            or _ENV.get("skill_token") or "").strip()


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    tok = _skill_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
        h["X-Skill-Token"] = tok
    return h


def _api(method: str, path: str, *, params: dict | None = None,
         json_body: dict | None = None, timeout: float = 30.0) -> dict:
    url = _base_url() + path
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            r = client.request(method, url, params=params, json=json_body, headers=_headers())
        if r.status_code == 401:
            return {"error": "unauthorized", "hint": "未配置或失效的 skill token（AITICKET_SKILL_TOKEN）"}
        if r.status_code >= 400:
            return {"error": f"http_{r.status_code}", "detail": r.text[:300]}
        try:
            return r.json()
        except Exception:
            return {"raw": r.text[:2000]}
    except Exception as e:
        return {"error": "connection_failed", "detail": str(e),
                "hint": f"本地服务未启动？{_base_url()}"}


mcp = FastMCP("aiticket")


# ---------- 工具 ----------

@mcp.tool()
def search_kb(query: str, top_k: int = 5) -> dict:
    """语义检索知识库（KB），返回与 query 最相关的文章/片段及相关度分数。
    用于：为回复/问答找证据。query=自然语言；top_k=返回条数。"""
    return _api("GET", "/api/kb/search", params={"q": query, "top_k": top_k})


@mcp.tool()
def list_board(project_key: str = "") -> dict:
    """列出 Jira 智能看板上的工单（依赖已绑定的 Jira 会话）。
    project_key 留空=当前/全局项目。"""
    hdr_path = "/api/board/issues"
    # project_key 经 X-Project-Key 头传入（中间件解析）
    url = _base_url() + hdr_path
    headers = _headers()
    if project_key:
        headers["X-Project-Key"] = project_key
    try:
        with httpx.Client(timeout=40.0, trust_env=False) as client:
            r = client.get(url, headers=headers)
        return r.json() if r.status_code < 400 else {"error": f"http_{r.status_code}", "detail": r.text[:300]}
    except Exception as e:
        return {"error": "connection_failed", "detail": str(e)}


@mcp.tool()
def get_ticket(issue_key: str) -> dict:
    """获取单个工单的看板信息（从看板工单列表中筛出 issue_key）。"""
    data = list_board()
    if isinstance(data, dict) and data.get("error"):
        return data
    issues = data.get("issues") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    for it in (issues or []):
        if it.get("key") == issue_key or it.get("issue_key") == issue_key:
            return it
    return {"error": "not_found", "issue_key": issue_key}


@mcp.tool()
def check_completeness(issue_key: str) -> dict:
    """Gate 1：检查工单信息是否完整（缺字段/需补充信息），不生成回复、不调用 LLM。"""
    return _api("POST", "/api/board/check-completeness", json_body={"issue_key": issue_key})


@mcp.tool()
def build_reply_context(issue_key: str) -> dict:
    """【核心】为某工单构建回复上下文：KB 证据(kb_hits_scored)、相似历史工单
    (similar_issues_scored)、gate 判定(gate_decisions)、AI 分析、复用候选，以及一份
    可直接交给你自己的 LLM 填充生成回复正文的 prompt 模板(prompt_template)。
    **服务侧不生成正文**——你拿到证据后用自己的 LLM 撰写回复。
    若某 gate 拦截（信息不全/需转派/无证据），返回相应 gate 阻断信息。"""
    return _api("POST", "/api/reply/context", json_body={"issue_key": issue_key}, timeout=60.0)


@mcp.tool()
def get_reuse_candidates(issue_key: str) -> dict:
    """获取该工单的历史复用候选（相似工单 + 分数）与复用评分，便于判断能否直接复用既有回复。"""
    ctx = build_reply_context(issue_key)
    if ctx.get("error"):
        return ctx
    return {
        "issue_key": issue_key,
        "similar_issues_scored": ctx.get("similar_issues_scored", []),
        "reuse_score": ctx.get("reuse_score"),
        "examples_used_count": ctx.get("examples_used_count", 0),
    }


@mcp.tool()
def run_gates(issue_key: str) -> dict:
    """运行回复闸门并返回各 gate 判定（completeness/specificity/reuse 等）+ 接地置信度，
    不生成正文。用于判断该工单是否适合自动回复。"""
    ctx = build_reply_context(issue_key)
    if ctx.get("error"):
        return ctx
    return {
        "issue_key": issue_key,
        "gate": ctx.get("gate"),
        "gate_decisions": ctx.get("gate_decisions", {}),
        "specificity_level": ctx.get("specificity_level"),
        "grounded_confidence": ctx.get("grounded_confidence"),
        "missing_fields": ctx.get("missing_fields", []),
    }


@mcp.tool()
def generate_reply(issue_key: str) -> dict:
    """让【服务侧】直接生成回复正文——仅当服务已配置 LLM key 时可用；
    纯 MCP 委托模式（无 key）下请改用 build_reply_context 自行生成。"""
    return _api("POST", "/api/board/generate-reply", json_body={"issue_key": issue_key}, timeout=90.0)


@mcp.tool()
def service_health() -> dict:
    """检查本地 aiticket 服务是否在线（/api/liveness）。"""
    return _api("GET", "/api/liveness", timeout=5.0)


if __name__ == "__main__":
    mcp.run()
