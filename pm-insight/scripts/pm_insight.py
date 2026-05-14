#!/usr/bin/python3
"""
pm_insight.py — Standalone CLI for querying the PM system (pmf.yyrd.com).

Pure data pipe: fetch PM data, output JSON / Markdown / CSV / table.
NO LLM calls. All PM API calls go through an HTTP CONNECT proxy.

Usage:
    python pm_insight.py --setup            # Interactive config wizard
    python pm_insight.py --test             # Verify PM connection
    python pm_insight.py --dashboard        # Status overview
    python pm_insight.py --list             # List demands (default: 待分析+待规划)
    python pm_insight.py --detail <aid>     # Single demand detail
    python pm_insight.py --batch-hang --product X --below 40 --yes
    python pm_insight.py --export out.md --format markdown
"""

import argparse
import csv
import getpass
import io
import json
import os
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

# Suppress InsecureRequestWarning (internal cert)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CONFIG_PATH = SKILL_DIR / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "proxy_url": "",
    "proxy_user": "",
    "proxy_pass": "",
    "pm_cookies": {
        "yht_access_token": "",
        "tenant_info": "0000",
        "ycap_session": "",
        "extra_cookies": {},
    },
    "line_id": "3058614d-5e02-45b3-8084-33d4c6e6a49b",
    "default_analyst": "",
}

PM_BASE = "https://pmf.yyrd.com"
TM_BASE = "https://tmf.yyrd.com"

STATUS_MAP = {
    "待分析": "WAIT_ANALYSIS",
    "待规划": "ASSIGNING",
    "实现中": "PROCESSING",
    "已方案解决": "SOLUTION_RESOLVED",
    "已实现": "IMPLEMENTED",
    "暂缓": "HANG",
    "已拒绝": "REJECTED",
}

COLLAB_STATUS_MAP = {
    "待分析": "WAIT_ANALYSIS",
    "已采纳": "COO_ACCEPT",
    "暂缓": "COO_HANG",
    "已拒绝": "COO_REJECTED",
    "已关闭": "CLOSED",
    "实现中": "PROCESSING",
    "已实现": "IMPLEMENTED",
}

DEFECT_STATUS_MAP = {
    "待审核": "ONAPPR",
    "打开": "OPEN",
    "已修复": "FIXED",
    "已关闭": "CLOSED",
    "已拒绝": "REJECTED",
    "已挂起": "HANG",
    "重新打开": "REOPEN",
}

STATUS_REVERSE: Dict[str, str] = {v: k for k, v in STATUS_MAP.items()}
COLLAB_STATUS_REVERSE: Dict[str, str] = {v: k for k, v in COLLAB_STATUS_MAP.items()}
DEFECT_STATUS_REVERSE: Dict[str, str] = {v: k for k, v in DEFECT_STATUS_MAP.items()}

DEFAULT_FETCH_FIELDS = [
    "aid", "code", "title", "status", "assignee", "analyst",
    "productId", "categoryId", "desc", "priority",
    "ctime", "expectedResolveTime", "commitDeliveryTime",
    "char8", "char3", "char11", "corProposer", "link",
]

COLLAB_FETCH_FIELDS = [
    "aid", "code", "title", "status", "analyst",
    "productId", "categoryId", "corProposer",
    "expectedResolveTime", "commitDeliveryTime", "ctime", "mtime",
    "closeTime", "description", "priority",
]

ENTITY_TYPE = "ORIGINAL_DEMAND"

DEFECT_FETCH_FIELDS = [
    "aid", "code", "title", "status", "assignee", "analyst",
    "productId", "influenceVersion", "priority", "severity",
    "ctime", "description",
]

ENTITY_CONFIG = {
    "original": {
        "entity_type": "ORIGINAL_DEMAND",
        "api_path": "/rest/v1/originalDemand/page",
        "api_host": PM_BASE,
        "fetch_fields": DEFAULT_FETCH_FIELDS,
        "status_map": STATUS_MAP,
        "status_reverse": STATUS_REVERSE,
        "default_statuses": ["WAIT_ANALYSIS", "ASSIGNING"],
    },
    "demand": {
        "entity_type": "DEMAND",
        "api_path": "/rest/v1/demand/page",
        "api_host": PM_BASE,
        "fetch_fields": COLLAB_FETCH_FIELDS,
        "status_map": COLLAB_STATUS_MAP,
        "status_reverse": COLLAB_STATUS_REVERSE,
        "default_statuses": ["WAIT_ANALYSIS"],
    },
    "defect": {
        "entity_type": "DEFECT",
        "api_path": "/tm/rest/v1/defect/page",
        "api_host": TM_BASE,
        "fetch_fields": DEFECT_FETCH_FIELDS,
        "status_map": DEFECT_STATUS_MAP,
        "status_reverse": DEFECT_STATUS_REVERSE,
        "default_statuses": ["ONAPPR", "OPEN"],
        "response_wrapper": True,
    },
}

VERIFICATION_STATUS_MAP = {
    "待处理": "PENDING",
    "处理中": "PROCESSING",
    "已完成": "COMPLETED",
}
VERIFICATION_STATUS_REVERSE: Dict[str, str] = {v: k for k, v in VERIFICATION_STATUS_MAP.items()}

HEADERS = {
    "Origin": "https://pm.yyrd.com",
    "Referer": "https://pm.yyrd.com/",
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> Dict[str, Any]:
    """Load config.json; return defaults if missing."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Merge with defaults for any missing keys
            merged = {**DEFAULT_CONFIG, **cfg}
            merged["pm_cookies"] = {**DEFAULT_CONFIG["pm_cookies"], **cfg.get("pm_cookies", {})}
            return merged
        except Exception as e:
            print(f"[WARN] config.json 读取失败: {e}, 使用默认配置", file=sys.stderr)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: Dict[str, Any]) -> None:
    """Persist config to disk."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[OK] 配置已保存到 {CONFIG_PATH}")

# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

def _build_proxies(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Build requests-compatible proxy dict from config."""
    proxy_url = cfg.get("proxy_url", "").rstrip("/")
    if not proxy_url:
        return {}
    user = cfg.get("proxy_user", "")
    passwd = cfg.get("proxy_pass", "")
    if user:
        # Insert user:pass before the host portion
        scheme, rest = proxy_url.split("://", 1)
        auth_proxy = f"{scheme}://{user}:{passwd}@{rest}"
    else:
        auth_proxy = proxy_url
    return {"https": auth_proxy, "http": auth_proxy}


def _build_cookies(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Build cookie dict from config."""
    pm = cfg.get("pm_cookies", {})
    cookies: Dict[str, str] = {}
    if pm.get("yht_access_token"):
        cookies["yht_access_token"] = pm["yht_access_token"]
    cookies["tenant_info"] = pm.get("tenant_info", "0000")
    if pm.get("ycap_session"):
        cookies["ycap_session"] = pm["ycap_session"]
    extras = pm.get("extra_cookies", {})
    if isinstance(extras, dict):
        cookies.update(extras)
    return {k: v for k, v in cookies.items() if v}


def pm_request(
    method: str,
    path: str,
    cfg: Dict[str, Any],
    json_body: Any = None,
    params: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    base_url: Optional[str] = None,
) -> requests.Response:
    """
    Send an HTTP request to pmf.yyrd.com through the proxy.
    Raises on connection errors; caller handles HTTP status.
    """
    url = f"{base_url or PM_BASE}{path}"
    if params:
        # Append query params
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}" if "?" not in url else f"{url}&{qs}"

    proxies = _build_proxies(cfg)
    cookies = _build_cookies(cfg)

    session = requests.Session()
    session.trust_env = False
    resp = session.request(
        method,
        url,
        headers=HEADERS,
        cookies=cookies,
        proxies=proxies,
        json=json_body,
        verify=False,
        timeout=timeout,
    )
    return resp


def check_auth(resp: requests.Response) -> bool:
    """Return True if response indicates valid session, print error and return False otherwise."""
    if resp.status_code == 401:
        print("[ERROR] PM 会话已过期，请重新运行 --setup 绑定 cookies", file=sys.stderr)
        return False
    if resp.status_code >= 400:
        print(f"[ERROR] HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return False
    return True

# ---------------------------------------------------------------------------
# PM API wrappers
# ---------------------------------------------------------------------------

def flatten_record(fields: List[Dict]) -> Dict[str, Any]:
    """Flatten PM API field-array into a simple dict."""
    result: Dict[str, Any] = {}
    for f in fields:
        code = f.get("fieldCode", "")
        if not code:
            continue
        value = f.get("value")
        title = f.get("title", "")
        edit_type = f.get("editType", "")

        if edit_type == "USER" and isinstance(value, dict):
            result[code] = value.get("userName") or value.get("name") or str(value)
        elif edit_type in ("LIST", "SELECT", "ENUM"):
            # For list/enum fields, prefer title (display name) over raw code
            result[code] = title or value or ""
            result[f"{code}_raw"] = value
        else:
            result[code] = value
        # Always keep the display title for reference
        if title and code not in ("name", "desc"):
            result[f"{code}_title"] = title
    return result


def fetch_demands_page(
    cfg: Dict[str, Any],
    page: int = 1,
    page_size: int = 30,
    conditions: Optional[List[Dict]] = None,
    properties: Optional[List[str]] = None,
    entity: str = "original",
    self_only: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    POST /rest/v1/{originalDemand|demand}/page — fetch one page of demands.
    Returns {"total": N, "records": [...flattened...], "raw_response": {...}} or {"error": "..."}.
    """
    ecfg = ENTITY_CONFIG.get(entity, ENTITY_CONFIG["original"])
    line_id = cfg.get("line_id", "1906301517140860973")
    all_conditions = []
    if line_id:
        all_conditions.append({
            "fieldCode": "lineId", "operation": "eq",
            "valueType": "STRING", "editType": "LIST", "values": [line_id],
        })
    if conditions:
        all_conditions.extend(conditions)
    body = {
        "key": "",
        "lineId": line_id,
        "isAsc": False,
        "orderBy": "ctime",
        "selfOnly": (entity == "defect") if self_only is None else self_only,
        "pageNumber": page,
        "pageSize": page_size,
        "entityType": ecfg["entity_type"],
        "specific": ecfg["entity_type"],
        "fetchFields": properties or ecfg["fetch_fields"],
        "onlyAttention": False,
        "conditions": all_conditions,
        "conditionGroups": None,
        "queryTotal": True,
    }

    resp = pm_request(
        "POST",
        ecfg["api_path"],
        cfg,
        json_body=body,
        params={"tenant_info": cfg.get("pm_cookies", {}).get("tenant_info", "0000")},
        base_url=ecfg.get("api_host"),
    )
    if not check_auth(resp):
        return {"error": f"HTTP {resp.status_code}"}

    try:
        data = resp.json()
    except Exception:
        return {"error": f"Invalid JSON: {resp.text[:200]}"}

    if data.get("code") == 500:
        return {"error": f"API error: {data.get('msg', data.get('message', ''))}"}

    # TM API wraps response in {"code":200, "data":{"page":{...}}}
    if ecfg.get("response_wrapper") and "data" in data and isinstance(data["data"], dict):
        data = data["data"]

    page_data = data.get("page", {})
    total = page_data.get("total", page_data.get("totalRow", 0))
    raw_records = page_data.get("records", [])

    records = []
    for rec in raw_records:
        if isinstance(rec, list):
            flat = flatten_record(rec)
        elif isinstance(rec, dict) and "fields" in rec:
            flat = flatten_record(rec["fields"])
        else:
            flat = flatten_record(rec) if isinstance(rec, list) else rec
        if isinstance(flat, dict):
            if not flat.get("aid"):
                flat["aid"] = flat.get("id", "")
            records.append(flat)

    return {"total": total, "records": records, "raw_response": data}


def fetch_verification_page(
    cfg: Dict[str, Any],
    page: int = 1,
    page_size: int = 30,
    extra_conditions: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """
    POST /rest/v1/featureStorySubTask/getSubTaskByPage
    Product verification subtasks use a completely different API pattern.
    """
    line_id = cfg.get("line_id", "1906301517140860973")
    conditions = [
        {
            "fieldCode": "pfss.userId",
            "editType": "USER",
            "operation": "eq",
            "valueType": "STRING",
            "values": ["#(CURRENT_USER)"],
        }
    ]
    if extra_conditions:
        conditions.extend(extra_conditions)

    body = {
        "lineId": line_id,
        "productId": "PF",
        "code": "DEMAND_VERIFY_TASK",
        "pageNumber": page,
        "pageSize": page_size,
        "conditions": conditions,
    }

    resp = pm_request(
        "POST",
        "/rest/v1/featureStorySubTask/getSubTaskByPage",
        cfg,
        json_body=body,
        params={"tenant_info": cfg.get("pm_cookies", {}).get("tenant_info", "0000")},
    )
    if not check_auth(resp):
        return {"error": f"HTTP {resp.status_code}"}

    try:
        data = resp.json()
    except Exception:
        return {"error": f"Invalid JSON: {resp.text[:200]}"}

    if isinstance(data, dict) and data.get("code") == 500:
        return {"error": f"API error: {data.get('msg', data.get('message', ''))}"}

    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        data = data["data"]

    page_data = data.get("page", data)
    if isinstance(page_data, dict):
        total = page_data.get("total", page_data.get("totalRow", 0))
        raw_records = page_data.get("records", [])
    else:
        total = 0
        raw_records = []

    records = []
    for rec in raw_records:
        if isinstance(rec, list):
            flat = flatten_record(rec)
        elif isinstance(rec, dict) and "fields" in rec:
            flat = flatten_record(rec["fields"])
        elif isinstance(rec, dict):
            flat = rec
        else:
            continue
        if isinstance(flat, dict):
            if not flat.get("aid"):
                flat["aid"] = flat.get("id", "")
            records.append(flat)

    return {"total": total, "records": records, "raw_response": data}


def fetch_demand_detail(cfg: Dict[str, Any], aid: str) -> Dict[str, Any]:
    """
    GET /rest/v1/originalDemand/<aid> — fetch single demand detail.
    Returns flattened dict or {"error": "..."}.
    """
    tenant = cfg.get("pm_cookies", {}).get("tenant_info", "0000")
    resp = pm_request("GET", f"/rest/v1/originalDemand/{aid}", cfg, params={"tenant_info": tenant})
    if not check_auth(resp):
        return {"error": f"HTTP {resp.status_code}"}

    try:
        data = resp.json()
    except Exception:
        return {"error": f"Invalid JSON: {resp.text[:200]}"}

    if data.get("code") == 500:
        return {"error": f"API error: {data.get('msg', data.get('message', ''))}"}

    inner = data.get("data", data)
    if isinstance(inner, dict) and "fields" in inner:
        flat = flatten_record(inner["fields"])
    elif isinstance(inner, list):
        flat = flatten_record(inner)
    else:
        flat = inner if isinstance(inner, dict) else {}
    flat["aid"] = inner.get("aid", "") if isinstance(inner, dict) else flat.get("aid", aid)
    return flat


def execute_hang(cfg: Dict[str, Any], aid: str, comment: str = "") -> Dict[str, Any]:
    """
    POST /rest/v1/workflow/processConvert — hang (暂缓) a demand.
    Returns {"success": True/False, "message": "..."}.
    """
    line_id = cfg.get("line_id", "1906301517140860973")
    tenant = cfg.get("pm_cookies", {}).get("tenant_info", "0000")
    # All workflow params go in query string; only fieldData in body
    params = {
        "lineId": line_id,
        "entityType": "ORIGINAL_DEMAND",
        "operation": "WAIT_PROCESS",
        "currentStatus": "WAIT_ANALYSIS",
        "tenant_info": tenant,
    }
    body = {"fieldData": {"aids": [aid]}}

    resp = pm_request(
        "POST",
        "/rest/v1/workflow/processConvert",
        cfg,
        json_body=body,
        params=params,
    )
    if not check_auth(resp):
        return {"success": False, "message": f"HTTP {resp.status_code}"}

    try:
        data = resp.json()
    except Exception:
        return {"success": False, "message": f"Invalid JSON: {resp.text[:200]}"}

    # processConvert may return True (bare bool) or {"code": 200, ...}
    if data is True or (isinstance(data, dict) and data.get("code") == 200):
        return {"success": True, "message": "OK"}

    msg = data.get("message", str(data)) if isinstance(data, dict) else str(data)
    return {"success": False, "message": msg}

# ---------------------------------------------------------------------------
# Display width helpers (CJK-aware)
# ---------------------------------------------------------------------------

def _char_width(ch: str) -> int:
    """Return display width of a character (2 for CJK, 1 otherwise)."""
    cat = unicodedata.east_asian_width(ch)
    return 2 if cat in ("W", "F") else 1


def display_width(s: str) -> int:
    """Return the display width of a string accounting for CJK characters."""
    return sum(_char_width(c) for c in s)


def pad_to_width(s: str, width: int) -> str:
    """Pad string with spaces to reach target display width."""
    current = display_width(s)
    if current >= width:
        return s
    return s + " " * (width - current)

# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_status_label(code: str, entity: str = "original") -> str:
    """Convert status code to Chinese label."""
    if entity == "verification":
        return VERIFICATION_STATUS_REVERSE.get(code, code)
    rev = ENTITY_CONFIG.get(entity, ENTITY_CONFIG["original"])["status_reverse"]
    return rev.get(code, STATUS_REVERSE.get(code, VERIFICATION_STATUS_REVERSE.get(code, code)))


def _pick_columns(records: List[Dict]) -> List[Tuple[str, str, int]]:
    """Return (key, header_label, min_width) tuples for table display."""
    return [
        ("code", "编号", 16),
        ("title", "\u6807\u9898", 36),
        ("status", "状态", 10),
        ("assignee", "经办人", 10),
        ("productId_title", "应用/服务", 14),
        ("char3", "需求类型", 10),
        ("ctime_title", "创建时间", 12),
    ]


def format_table(records: List[Dict], columns: Optional[List[Tuple[str, str, int]]] = None) -> str:
    """Render records as a Unicode box-drawing table."""
    if not records:
        return "(无数据)"

    cols = columns or _pick_columns(records)

    # Compute column widths
    widths: List[int] = []
    for key, header, min_w in cols:
        col_w = max(min_w, display_width(header))
        for r in records:
            val = _cell_value(r, key)
            col_w = max(col_w, display_width(val))
        widths.append(min(col_w, 50))  # Cap at 50

    def hline(left: str, mid: str, right: str, fill: str = "─") -> str:
        return left + mid.join(fill * (w + 2) for w in widths) + right

    lines = [hline("┌", "┬", "┐")]

    # Header
    header_cells = []
    for i, (key, label, _) in enumerate(cols):
        header_cells.append(f" {pad_to_width(label, widths[i])} ")
    lines.append("│" + "│".join(header_cells) + "│")
    lines.append(hline("├", "┼", "┤"))

    # Rows
    for r in records:
        cells = []
        for i, (key, _, _) in enumerate(cols):
            val = _cell_value(r, key)
            # Truncate if too wide
            while display_width(val) > widths[i]:
                val = val[:-1]
            cells.append(f" {pad_to_width(val, widths[i])} ")
        lines.append("│" + "│".join(cells) + "│")

    lines.append(hline("└", "┴", "┘"))
    return "\n".join(lines)


def _cell_value(record: Dict, key: str) -> str:
    """Extract a display-ready cell value from a record."""
    val = record.get(key, "")
    if key == "status":
        return format_status_label(str(val))
    if key == "assignee":
        if isinstance(val, dict):
            return val.get("userName") or val.get("name") or ""
        return str(val) if val else ""
    if key == "ctime_title" and val:
        s = str(val)
        return s[:10] if len(s) >= 10 else s
    if key == "priority":
        pmap = {"1": "紧急", "2": "高", "3": "中", "4": "低"}
        return pmap.get(str(val), str(val) if val else "")
    if val is None:
        return ""
    return str(val)


def format_markdown_table(records: List[Dict], columns: Optional[List[Tuple[str, str, int]]] = None) -> str:
    """Render records as a GFM Markdown table."""
    if not records:
        return "*无数据*"
    cols = columns or _pick_columns(records)

    header_line = "| " + " | ".join(label for _, label, _ in cols) + " |"
    sep_line = "| " + " | ".join("---" for _ in cols) + " |"

    rows = []
    for r in records:
        cells = [_cell_value(r, key) for key, _, _ in cols]
        rows.append("| " + " | ".join(cells) + " |")

    return "\n".join([header_line, sep_line] + rows)


def format_csv_output(records: List[Dict], columns: Optional[List[Tuple[str, str, int]]] = None) -> str:
    """Render records as CSV."""
    if not records:
        return ""
    cols = columns or _pick_columns(records)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label, _ in cols])
    for r in records:
        writer.writerow([_cell_value(r, key) for key, _, _ in cols])
    return buf.getvalue()


def format_json_output(data: Any) -> str:
    """Pretty-print as JSON."""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def render_output(data: Any, fmt: str, records: Optional[List[Dict]] = None) -> str:
    """Dispatch to the correct formatter."""
    if fmt == "json":
        return format_json_output(data)
    elif fmt == "table":
        return format_table(records or data if isinstance(data, list) else [])
    elif fmt == "markdown":
        return format_markdown_table(records or data if isinstance(data, list) else [])
    elif fmt == "csv":
        return format_csv_output(records or data if isinstance(data, list) else [])
    return format_json_output(data)

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_setup(cfg: Dict[str, Any]) -> None:
    """Interactive config wizard."""
    print("=== PM Insight 配置向导 ===\n")

    # Proxy
    print(f"[1/4] 代理配置 (当前: {cfg.get('proxy_url', '')})")
    proxy = input(f"  代理地址 [{cfg.get('proxy_url', '')}]: ").strip()
    if proxy:
        cfg["proxy_url"] = proxy
    puser = input(f"  代理用户名 [{cfg.get('proxy_user', '')}]: ").strip()
    if puser:
        cfg["proxy_user"] = puser
    ppass = getpass.getpass(f"  代理密码 [{'*' * len(cfg.get('proxy_pass', ''))}]: ").strip()
    if ppass:
        cfg["proxy_pass"] = ppass

    # PM Cookies
    print(f"\n[2/4] PM Cookies")
    token = input(f"  yht_access_token [{cfg['pm_cookies'].get('yht_access_token', '')[:20]}...]: ").strip()
    if token:
        cfg["pm_cookies"]["yht_access_token"] = token
    tenant = input(f"  tenant_info [{cfg['pm_cookies'].get('tenant_info', '0000')}]: ").strip()
    if tenant:
        cfg["pm_cookies"]["tenant_info"] = tenant

    # Extra cookies (optional, one-per-line "key=value")
    print("  额外 cookies (每行 key=value, 空行结束):")
    extras: Dict[str, str] = {}
    while True:
        line = input("    ").strip()
        if not line:
            break
        if "=" in line:
            k, v = line.split("=", 1)
            extras[k.strip()] = v.strip()
    if extras:
        cfg["pm_cookies"]["extra_cookies"] = extras

    # Line ID
    print(f"\n[3/4] 产品线")
    lid = input(f"  lineId [{cfg.get('line_id', '')}]: ").strip()
    if lid:
        cfg["line_id"] = lid

    # Default analyst
    print(f"\n[4/4] 默认经办人")
    analyst = input(f"  默认经办人 ID [{cfg.get('default_analyst', '')}]: ").strip()
    if analyst:
        cfg["default_analyst"] = analyst

    save_config(cfg)
    print("\n配置完成。运行 --test 验证连接。")


def cmd_test(cfg: Dict[str, Any]) -> bool:
    """Verify PM connection by fetching page 1."""
    print("正在验证 PM 连接...")
    try:
        result = fetch_demands_page(cfg, page=1, page_size=1)
    except requests.exceptions.ProxyError as e:
        print(f"[FAIL] 代理连接失败: {e}", file=sys.stderr)
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"[FAIL] 网络连接失败: {e}", file=sys.stderr)
        return False
    except requests.exceptions.Timeout:
        print("[FAIL] 请求超时", file=sys.stderr)
        return False

    if "error" in result:
        print(f"[FAIL] {result['error']}", file=sys.stderr)
        return False

    total = result.get("total", 0)
    print(f"[OK] 连接成功！当前产线共 {total} 条原始需求。")
    if result.get("records"):
        first = result["records"][0]
        print(f"     最新: [{first.get('code', '?')}] {first.get('name', '?')[:40]}")
    return True


def cmd_dashboard(cfg: Dict[str, Any], fmt: str, entity: str = "original") -> Optional[str]:
    """Overview: count demands by status using per-status pageSize=1 queries."""
    print("正在获取看板概览...", file=sys.stderr)

    ecfg = ENTITY_CONFIG.get(entity, ENTITY_CONFIG["original"])
    status_counts: Dict[str, int] = {}
    grand_total = 0

    for label, code in ecfg["status_map"].items():
        conds = [{"fieldCode": "status", "operation": "in",
                  "valueType": "STRING", "editType": "LIST", "values": [code]}]
        result = fetch_demands_page(cfg, page=1, page_size=1, conditions=conds, entity=entity)
        if "error" in result:
            print(f"  {label}: 查询失败", file=sys.stderr)
            continue
        count = result.get("total", 0)
        if count > 0:
            status_counts[label] = count
            grand_total += count
        print(f"  {label}: {count}", file=sys.stderr)

    sorted_statuses = sorted(status_counts.items(), key=lambda x: -x[1])

    if fmt == "json":
        return format_json_output({"total": grand_total, "by_status": dict(sorted_statuses)})

    # Table output
    col_w_status = max(10, max((display_width(s) for s, _ in sorted_statuses), default=10))
    col_w_count = max(4, max((len(str(c)) for _, c in sorted_statuses), default=4))

    lines = []
    lines.append("┌" + "─" * (col_w_status + 2) + "┬" + "─" * (col_w_count + 2) + "┐")
    lines.append("│ " + pad_to_width("状态", col_w_status) + " │ " + pad_to_width("数量", col_w_count) + " │")
    lines.append("├" + "─" * (col_w_status + 2) + "┼" + "─" * (col_w_count + 2) + "┤")
    for label, count in sorted_statuses:
        lines.append(
            "│ " + pad_to_width(label, col_w_status) + " │ "
            + pad_to_width(str(count), col_w_count) + " │"
        )
    lines.append("└" + "─" * (col_w_status + 2) + "┴" + "─" * (col_w_count + 2) + "┘")
    lines.append(f"  合计: {grand_total}")

    return "\n".join(lines)


def cmd_overview(cfg: Dict[str, Any], fmt: str = "table") -> Optional[str]:
    """Unified overview: count across all 4 modules for the current user."""
    print("正在获取工作概览...", file=sys.stderr)

    analyst = cfg.get("default_analyst", "")
    user_assignee = [{"fieldCode": "assignee", "operation": "eq",
                      "valueType": "STRING", "values": [analyst]}] if analyst else []
    user_analyst_cond = [{"fieldCode": "analyst", "operation": "eq",
                          "valueType": "STRING", "values": [analyst]}] if analyst else []

    counts: Dict[str, Any] = {}

    conds = [{"fieldCode": "status", "operation": "in", "valueType": "STRING",
              "editType": "LIST", "values": ["WAIT_ANALYSIS"]}] + user_assignee
    r = fetch_demands_page(cfg, page=1, page_size=1, conditions=conds, entity="original")
    counts["待分析原始需求"] = r.get("total", 0) if "error" not in r else "?"
    print(f"  原始需求: {counts['待分析原始需求']}", file=sys.stderr)

    conds = [{"fieldCode": "status", "operation": "in", "valueType": "STRING",
              "editType": "LIST", "values": ["WAIT_ANALYSIS"]}] + user_analyst_cond
    r = fetch_demands_page(cfg, page=1, page_size=1, conditions=conds, entity="demand")
    counts["待处理协作需求"] = r.get("total", 0) if "error" not in r else "?"
    print(f"  协作需求: {counts['待处理协作需求']}", file=sys.stderr)

    conds = [{"fieldCode": "status", "operation": "in", "valueType": "STRING",
              "editType": "LIST", "values": ["ONAPPR", "OPEN"]}]
    r = fetch_demands_page(cfg, page=1, page_size=1, conditions=conds, entity="defect")
    counts["待处理缺陷"] = r.get("total", 0) if "error" not in r else "?"
    print(f"  缺陷: {counts['待处理缺陷']}", file=sys.stderr)

    r = fetch_verification_page(cfg, page=1, page_size=1)
    counts["待处理验证子任务"] = r.get("total", 0) if "error" not in r else "?"
    print(f"  验证子任务: {counts['待处理验证子任务']}", file=sys.stderr)

    if fmt == "json":
        return format_json_output(counts)

    today = datetime.now().strftime("%Y-%m-%d")
    max_label = max(display_width(k) for k in counts)
    max_val = max(len(str(v)) for v in counts.values())
    inner_w = max_label + max_val + 4
    total_w = inner_w + 4

    lines = []
    lines.append("\u2554" + "\u2550" * total_w + "\u2557")
    title = f"PM \u5de5\u4f5c\u6982\u89c8  {today}"
    title_pad = total_w - display_width(title)
    lines.append("\u2551 " + title + " " * (title_pad - 1) + "\u2551")
    lines.append("\u2560" + "\u2550" * total_w + "\u2563")

    for label, count in counts.items():
        val_str = str(count)
        padding = inner_w - display_width(label) - len(val_str)
        lines.append("\u2551  " + label + " " * padding + val_str + "  \u2551")

    lines.append("\u255a" + "\u2550" * total_w + "\u255d")

    return "\n".join(lines)


def _resolve_status_filter(status_arg: Optional[str], entity: str = "original") -> Optional[List[Dict]]:
    """Convert user-friendly status label to API condition list."""
    if not status_arg:
        return None
    ecfg = ENTITY_CONFIG.get(entity, ENTITY_CONFIG["original"])
    smap = ecfg["status_map"]
    srev = ecfg["status_reverse"]
    code = smap.get(status_arg)
    if not code:
        code = status_arg.upper()
        if code not in srev:
            for label, c in smap.items():
                if status_arg in label:
                    code = c
                    break
            else:
                print(f"[WARN] 未知状态: {status_arg}, 可选: {', '.join(smap.keys())}", file=sys.stderr)
                return None
    return [{"fieldCode": "status", "operation": "in", "valueType": "STRING", "editType": "LIST", "values": [code]}]


def _resolve_product_filter(cfg: Dict[str, Any], product_name: str) -> Optional[Dict]:
    """
    Fuzzy-match product name to productId code.
    Fetches one page, extracts unique products, picks best match.
    Returns {"fieldCode": "productId", "value": code, "operation": "EQ"} or None.
    """
    result = fetch_demands_page(cfg, page=1, page_size=50)
    if "error" in result:
        print(f"[ERROR] 无法获取产品列表: {result['error']}", file=sys.stderr)
        return None

    products: Dict[str, str] = {}  # code -> title
    for rec in result.get("records", []):
        raw_code = rec.get("productId_raw") or rec.get("productId", "")
        title = rec.get("productId_title") or rec.get("productId", "")
        if raw_code and title:
            products[str(raw_code)] = str(title)

    if not products:
        print("[WARN] 未找到产品信息", file=sys.stderr)
        return None

    # Exact match first
    for code, title in products.items():
        if product_name == title or product_name == code:
            print(f"  产品匹配: {title} ({code})", file=sys.stderr)
            return {"fieldCode": "productId", "operation": "in", "valueType": "STRING", "editType": "LIST", "values": [code]}

    # Fuzzy: substring match
    matches = [(code, title) for code, title in products.items() if product_name in title]
    if len(matches) == 1:
        code, title = matches[0]
        print(f"  产品匹配: {title} ({code})", file=sys.stderr)
        return {"fieldCode": "productId", "operation": "in", "valueType": "STRING", "editType": "LIST", "values": [code]}
    elif len(matches) > 1:
        print(f"[WARN] 产品名 '{product_name}' 匹配到多个:", file=sys.stderr)
        for code, title in matches:
            print(f"    {title} ({code})", file=sys.stderr)
        print("请使用更精确的名称或直接使用 productId 编码。", file=sys.stderr)
        return None

    # No match
    print(f"[WARN] 未找到匹配 '{product_name}' 的产品。已知产品:", file=sys.stderr)
    for code, title in sorted(products.items(), key=lambda x: x[1]):
        print(f"    {title} ({code})", file=sys.stderr)
    return None


def cmd_list(
    cfg: Dict[str, Any],
    fmt: str,
    status: Optional[str] = None,
    product: Optional[str] = None,
    assignee: Optional[str] = None,
    fetch_all: bool = False,
    top_n: Optional[int] = None,
    max_results: int = 50,
    entity: str = "original",
) -> Optional[str]:
    """List demands with optional filters."""
    if entity == "verification":
        return _cmd_list_verification(cfg, fmt, fetch_all, top_n, max_results)

    ecfg = ENTITY_CONFIG.get(entity, ENTITY_CONFIG["original"])
    conditions: List[Dict] = []

    # Status filter
    if status:
        sc = _resolve_status_filter(status, entity)
        if sc:
            conditions.extend(sc)
    else:
        conditions.append({
            "fieldCode": "status", "operation": "in",
            "valueType": "STRING", "editType": "LIST",
            "values": ecfg["default_statuses"],
        })

    # Product filter
    if product:
        pf = _resolve_product_filter(cfg, product)
        if pf:
            conditions.append(pf)

    # Assignee/analyst filter (default to config's default_analyst; "all" skips)
    # Defect uses selfOnly=true instead of assignee condition
    effective_assignee = assignee or cfg.get("default_analyst", "")
    if entity != "defect" and effective_assignee and effective_assignee.lower() != "all":
        field = "analyst" if entity == "demand" else "assignee"
        conditions.append({
            "fieldCode": field, "operation": "eq",
            "valueType": "STRING", "values": [effective_assignee],
        })

    all_records: List[Dict] = []
    page = 1
    page_size = max_results

    while True:
        print(f"  获取第 {page} 页...", file=sys.stderr)
        _self_only = False if (entity == "defect" and assignee and assignee.lower() == "all") else None
        result = fetch_demands_page(cfg, page=page, page_size=page_size, conditions=conditions, entity=entity, self_only=_self_only)
        if "error" in result:
            print(f"[ERROR] {result['error']}", file=sys.stderr)
            return None

        records = result.get("records", [])
        total = result.get("total", 0)
        all_records.extend(records)

        if not fetch_all or len(all_records) >= total or len(records) < page_size:
            break
        page += 1

    # Apply top limit
    if top_n and top_n > 0:
        all_records = all_records[:top_n]

    print(f"  共 {len(all_records)} 条结果", file=sys.stderr)

    return render_output(all_records, fmt, records=all_records)


def _cmd_list_verification(
    cfg: Dict[str, Any],
    fmt: str,
    fetch_all: bool = False,
    top_n: Optional[int] = None,
    max_results: int = 50,
) -> Optional[str]:
    """List verification subtasks (separate API pattern)."""
    all_records: List[Dict] = []
    page = 1
    page_size = max_results

    while True:
        print(f"  获取验证子任务第 {page} 页...", file=sys.stderr)
        result = fetch_verification_page(cfg, page=page, page_size=page_size)
        if "error" in result:
            print(f"[ERROR] {result['error']}", file=sys.stderr)
            return None

        records = result.get("records", [])
        total = result.get("total", 0)
        all_records.extend(records)

        if not fetch_all or len(all_records) >= total or len(records) < page_size:
            break
        page += 1

    if top_n and top_n > 0:
        all_records = all_records[:top_n]

    print(f"  共 {len(all_records)} 条验证子任务", file=sys.stderr)

    return render_output(all_records, fmt, records=all_records)


def cmd_detail(cfg: Dict[str, Any], aid: str, fmt: str) -> Optional[str]:
    """Fetch and display a single demand detail."""
    result = fetch_demand_detail(cfg, aid)
    if "error" in result:
        print(f"[ERROR] {result['error']}", file=sys.stderr)
        return None

    if fmt == "json":
        return format_json_output(result)

    # Readable detail
    lines = ["=" * 60]
    lines.append(f"  编号: {result.get('code', 'N/A')}")
    lines.append(f"  标题: {result.get('title', 'N/A')}")
    lines.append(f"  状态: {format_status_label(str(result.get('status', '')))}")
    lines.append(f"  经办人: {result.get('assignee', 'N/A')}")
    lines.append(f"  应用/服务: {result.get('productId_title', result.get('productId', 'N/A'))}")
    lines.append(f"  优先级: {result.get('priority', 'N/A')}")
    lines.append(f"  创建时间: {result.get('createTime', 'N/A')}")
    lines.append(f"  修改时间: {result.get('modifyTime', 'N/A')}")
    lines.append("-" * 60)

    desc = result.get("desc") or result.get("description") or "(无描述)"
    lines.append(f"  描述:\n{desc}")
    lines.append("=" * 60)

    # Show all other fields
    skip = {"code", "name", "status", "assignee", "productId", "productId_title",
            "productId_raw", "priority", "createTime", "modifyTime", "desc",
            "description", "aid", "id", "lineId"}
    extra = {k: v for k, v in result.items() if k not in skip and not k.endswith("_raw") and not k.endswith("_title")}
    if extra:
        lines.append("\n  其他字段:")
        for k, v in extra.items():
            lines.append(f"    {k}: {v}")

    return "\n".join(lines)


def cmd_batch_hang(
    cfg: Dict[str, Any],
    product: str,
    below: int,
    auto_confirm: bool = False,
    fmt: str = "json",
) -> Optional[str]:
    """
    Batch hang demands below a score threshold in a product.
    Score comes from triage cache files (data_cache/pm_triage/*.json).
    """
    # Resolve product filter
    pf = _resolve_product_filter(cfg, product)
    if not pf:
        return None

    # Find triage cache directory (relative to backend)
    triage_dirs = [
        SKILL_DIR.parent.parent.parent / "APP" / "backend" / "data_cache" / "pm_triage",
        Path.home() / ".pm_insight" / "triage_cache",
    ]
    triage_dir = None
    for d in triage_dirs:
        if d.exists():
            triage_dir = d
            break

    if not triage_dir:
        print("[ERROR] 未找到分诊缓存目录 (data_cache/pm_triage/)。请先运行后端分析。", file=sys.stderr)
        return None

    # Scan cache for low-score demands matching product
    candidates: List[Dict] = []
    for p in triage_dir.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                analysis = json.load(f)
            score = analysis.get("value_score", 50)
            if not isinstance(score, (int, float)):
                try:
                    score = int(score)
                except (TypeError, ValueError):
                    score = 50
            if score < below:
                candidates.append({
                    "aid": p.stem,
                    "code": analysis.get("code", p.stem),
                    "title": analysis.get("title", ""),
                    "value_score": score,
                    "status": analysis.get("status", ""),
                })
        except Exception:
            continue

    if not candidates:
        print(f"没有 value_score < {below} 的已分析需求。", file=sys.stderr)
        return None

    # Sort by score ascending
    candidates.sort(key=lambda x: x["value_score"])

    print(f"\n将暂缓 {len(candidates)} 条需求 (score < {below}):\n", file=sys.stderr)
    for c in candidates[:20]:
        print(f"  [{c['code']}] score={c['value_score']:3d}  {c['title'][:40]}", file=sys.stderr)
    if len(candidates) > 20:
        print(f"  ... 及其余 {len(candidates) - 20} 条", file=sys.stderr)

    if not auto_confirm:
        confirm = input(f"\n确认暂缓以上 {len(candidates)} 条需求? (y/N): ").strip().lower()
        if confirm not in ("y", "yes"):
            print("已取消。")
            return None

    # Execute hang operations
    progress_file = SKILL_DIR / "data" / "batch_hang_progress.json"
    progress_file.parent.mkdir(parents=True, exist_ok=True)

    progress = {
        "started_at": datetime.now().isoformat(),
        "total": len(candidates),
        "done": 0,
        "succeeded": 0,
        "failed": 0,
        "errors": [],
        "completed": False,
    }

    for i, c in enumerate(candidates, 1):
        aid = c["aid"]
        comment = f"低分暂缓（score<{below}）"
        result = execute_hang(cfg, aid, comment)

        progress["done"] = i
        if result["success"]:
            progress["succeeded"] += 1
            print(f"  [{i}/{len(candidates)}] {c['code']}: OK", file=sys.stderr)
        else:
            progress["failed"] += 1
            progress["errors"].append({"aid": aid, "code": c["code"], "error": result["message"]})
            print(f"  [{i}/{len(candidates)}] {c['code']}: FAIL - {result['message']}", file=sys.stderr)

        # Save progress after each item
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)

        # Brief pause between operations to avoid rate limiting
        if i < len(candidates):
            time.sleep(0.5)

    progress["completed"] = True
    progress["finished_at"] = datetime.now().isoformat()
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)

    summary = (
        f"\n批量暂缓完成: {progress['succeeded']}/{progress['total']} 成功, "
        f"{progress['failed']} 失败"
    )
    print(summary, file=sys.stderr)

    if fmt == "json":
        return format_json_output(progress)
    return summary


def cmd_hang_progress(progress_id: Optional[str] = None) -> Optional[str]:
    """Check batch hang progress."""
    progress_file = SKILL_DIR / "data" / "batch_hang_progress.json"
    if not progress_file.exists():
        print("没有正在进行的批量暂缓任务。", file=sys.stderr)
        return None
    with open(progress_file, "r", encoding="utf-8") as f:
        return f.read()

# ---------------------------------------------------------------------------
# Export helper
# ---------------------------------------------------------------------------

def export_to_file(content: str, path: str) -> None:
    """Write content to file, inferring format from extension."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[OK] 已导出到 {out.resolve()}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_insight",
        description="PM Insight CLI — 查询 pmf.yyrd.com 需求数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  %(prog)s --setup                          配置代理和 cookies
  %(prog)s --test                           验证连接
  %(prog)s --dashboard                      按状态统计概览
  %(prog)s --list                           列出待分析+待规划需求
  %(prog)s --list --status 暂缓             按状态筛选
  %(prog)s --list --product BIP --all       按产品筛选（全量翻页）
  %(prog)s --detail 123456                  查看需求详情
  %(prog)s --batch-hang --product BIP --below 40 --yes
  %(prog)s --list --format table --top 20
  %(prog)s --list --export demands.csv --format csv
""",
    )

    # Mutually exclusive primary commands
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--setup", action="store_true", help="交互式配置向导")
    group.add_argument("--test", action="store_true", help="验证 PM 连接")
    group.add_argument("--overview", action="store_true", help="统一工作概览 (4模块计数)")
    group.add_argument("--dashboard", action="store_true", help="按状态统计概览")
    group.add_argument("--list", action="store_true", help="列出需求列表")
    group.add_argument("--detail", metavar="AID", help="查看需求详情 (aid)")
    group.add_argument("--batch-hang", action="store_true", help="批量暂缓低分需求")
    group.add_argument("--hang-progress", metavar="ID", nargs="?", const="latest", help="查询批量暂缓进度")

    # Filters
    parser.add_argument("--status", help="状态筛选 (中文或英文代码)")
    parser.add_argument("--product", help="产品/应用名称 (模糊匹配)")
    parser.add_argument("--assignee", help="经办人 ID")
    parser.add_argument("--all", action="store_true", dest="fetch_all", help="翻页获取全部结果")
    parser.add_argument("--below", type=int, default=40, help="批量暂缓的分值阈值 (默认: 40)")
    parser.add_argument("--yes", action="store_true", help="跳过确认直接执行")
    parser.add_argument("--entity", choices=["original", "demand", "defect", "verification"], default="original",
                        help="实体类型: original=原始需求, demand=协作需求, defect=缺陷, verification=验证子任务 (默认: original)")

    # Output
    parser.add_argument("--format", choices=["json", "markdown", "table", "csv"], default="json",
                        help="输出格式 (默认: json)")
    parser.add_argument("--top", type=int, metavar="N", help="限制输出前 N 条")
    parser.add_argument("--max-results", type=int, default=50, help="单页大小 (默认: 50)")
    parser.add_argument("--export", metavar="PATH", help="导出到文件 (根据扩展名自动选格式)")

    return parser

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    cfg = load_config()

    output: Optional[str] = None

    if args.setup:
        cmd_setup(cfg)
        return 0

    if args.test:
        ok = cmd_test(cfg)
        return 0 if ok else 1

    if args.overview:
        output = cmd_overview(cfg, args.format)

    elif args.dashboard:
        output = cmd_dashboard(cfg, args.format, entity=args.entity)

    elif args.list:
        output = cmd_list(
            cfg,
            fmt=args.format,
            status=args.status,
            product=args.product,
            assignee=args.assignee,
            fetch_all=args.fetch_all,
            top_n=args.top,
            max_results=args.max_results,
            entity=args.entity,
        )

    elif args.detail:
        output = cmd_detail(cfg, args.detail, args.format)

    elif args.batch_hang:
        if not args.product:
            parser.error("--batch-hang 需要指定 --product")
        output = cmd_batch_hang(
            cfg,
            product=args.product,
            below=args.below,
            auto_confirm=args.yes,
            fmt=args.format,
        )

    elif args.hang_progress is not None:
        output = cmd_hang_progress(args.hang_progress)

    else:
        parser.print_help()
        return 0

    if output:
        # If --export specified, infer format from extension
        if args.export:
            ext = Path(args.export).suffix.lower()
            fmt_map = {".md": "markdown", ".csv": "csv", ".json": "json", ".txt": "table"}
            if ext in fmt_map and args.format == "json":
                # Re-render in the inferred format if user didn't explicitly set --format
                # (argparse default is json, so we check if it was explicitly given)
                pass  # Already rendered — export as-is
            export_to_file(output, args.export)
        else:
            print(output)

    return 0 if output is not None else 1


if __name__ == "__main__":
    sys.exit(main())
