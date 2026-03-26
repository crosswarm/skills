#!/usr/bin/env python3
"""
Jira 工单查询辅助脚本
BIP应用与开发平台产品规划部 qiangxiao, 2026
"""
import argparse
import base64
import csv
import getpass
import hashlib
import json
import os
import socket
import ssl
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.json"
FIELDS = (
    "summary,status,assignee,reporter,created,updated,priority,issuetype,"
    "project,duedate,labels,description,"
    "customfield_10725,"   # 项目名称（=客户）
    "customfield_10729,"   # 研发确认问题类型
    "customfield_10402,"   # 客户问题类型
    "customfield_10906,"   # 解决方式
    "customfield_10404,"   # 联系人
    "customfield_10405,"   # 联系方式
    "customfield_11942,"   # 项目领域模块
    "customfield_13529,"   # SOP产品版本
    "customfield_13211,"   # 客户属性
    "customfield_10410,"   # 回复方式
    "customfield_14301,"   # 重点客户类型
    "customfield_10411,"   # 解决方案
    "customfield_11910,"   # 所属伙伴
    "customfield_11908,"   # 所属大区
    "customfield_11909,"   # 机构
    "customfield_10401,"   # 需求负责人
    "customfield_15200,"   # 冲刺标签
    "customfield_10123,"   # 领域模块
    "resolutiondate"       # 解决日期
)

# CSV 导出列定义: (表头, 提取路径)
CSV_COLUMNS = [
    ("工单号", "key"),
    ("概要", "fields.summary"),
    ("状态", "fields.status.name"),
    ("优先级", "fields.priority.name"),
    ("问题类型", "fields.issuetype.name"),
    ("经办人", "fields.assignee.displayName"),
    ("报告人/创建者", "fields.reporter.displayName"),
    ("客户/项目名称", "fields.customfield_10725"),
    ("客户问题类型", "fields.customfield_10402.value"),
    ("研发确认问题类型", "fields.customfield_10729.value"),
    ("解决方式", "fields.customfield_10906"),
    ("解决方案", "fields.customfield_10411"),
    ("回复方式", "fields.customfield_10410.value"),
    ("客户属性", "fields.customfield_13211.value"),
    ("重点客户类型", "fields.customfield_14301.value"),
    ("所属伙伴", "fields.customfield_11910"),
    ("所属大区", "fields.customfield_11908"),
    ("机构", "fields.customfield_11909"),
    ("需求负责人", "fields.customfield_10401"),
    ("项目领域模块", "fields.customfield_11942"),
    ("SOP产品版本", "fields.customfield_13529.value"),
    ("领域模块", "fields.customfield_10123.value"),
    ("冲刺标签", "fields.customfield_15200"),
    ("标签", "fields.labels"),
    ("联系人", "fields.customfield_10404"),
    ("联系方式", "fields.customfield_10405"),
    ("创建日期", "fields.created"),
    ("更新日期", "fields.updated"),
    ("到期日", "fields.duedate"),
]

# 周报兼容 CSV 列定义（与手动 Jira 导出格式完全一致的 29 列）
REPORT_CSV_COLUMNS = [
    ("问题关键字", lambda i: i.get("key", "")),
    ("问题ID", lambda i: i.get("id", "")),
    ("项目关键字", lambda i: extract(i, "fields.project.key")),
    ("项目名称", lambda i: extract(i, "fields.project.name")),
    ("项目类型", lambda i: extract(i, "fields.project.projectTypeKey")),
    ("项目主管", lambda i: ""),
    ("项目描述", lambda i: ""),
    ("项目URL", lambda i: ""),
    ("自定义字段(领域模块)", lambda i: _extract_cascading(i, "customfield_10123")),
    ("经办人", lambda i: extract(i, "fields.assignee.name")),  # username 不是 displayName
    ("概要", lambda i: extract(i, "fields.summary")),
    ("创建日期", lambda i: _fmt_datetime(extract(i, "fields.created", ""))),
    ("状态", lambda i: extract(i, "fields.status.name")),
    ("自定义字段(到期日)", lambda i: _fmt_datetime(extract(i, "fields.duedate", ""))),
    ("创建者", lambda i: extract(i, "fields.reporter.name")),  # username
    ("自定义字段(项目名称)", lambda i: _extract_list(i, "customfield_10725")),
    ("自定义字段(SOP产品版本)", lambda i: extract(i, "fields.customfield_13529.value")),
    ("自定义字段(客户问题类型)", lambda i: extract(i, "fields.customfield_10402.value")),
    ("自定义字段(研发确认问题类型)", lambda i: extract(i, "fields.customfield_10729.value")),
    ("自定义字段(客户属性)", lambda i: extract(i, "fields.customfield_13211.value")),
    ("自定义字段(回复方式)", lambda i: extract(i, "fields.customfield_10410.value")),
    ("自定义字段(重点客户类型)", lambda i: extract(i, "fields.customfield_14301.value")),
    ("自定义字段(解决方案)", lambda i: extract(i, "fields.customfield_10411")),
    ("自定义字段(所属伙伴)", lambda i: extract(i, "fields.customfield_11910")),
    ("自定义字段(所属大区)", lambda i: extract(i, "fields.customfield_11908")),
    ("自定义字段(机构)", lambda i: extract(i, "fields.customfield_11909")),
    ("自定义字段(解决方式)", lambda i: extract(i, "fields.customfield_10906")),
    ("自定义字段(需求负责人)", lambda i: _extract_user(i, "customfield_10401")),
    ("标签", lambda i: ", ".join(i.get("fields", {}).get("labels", []))),
]


def _extract_cascading(issue, field_id):
    """提取级联选择字段（如领域模块: 父级 -> 子级）"""
    cf = issue.get("fields", {}).get(field_id)
    if not cf:
        return ""
    if isinstance(cf, dict):
        parent = cf.get("value", "")
        child = cf.get("child", {}).get("value", "") if isinstance(cf.get("child"), dict) else ""
        return f"{parent} -> {child}" if child else parent
    return str(cf)


def _extract_list(issue, field_id):
    """提取列表字段（如项目名称）"""
    cf = issue.get("fields", {}).get(field_id)
    if isinstance(cf, list) and cf:
        return cf[0].rstrip(",").strip() if isinstance(cf[0], str) else str(cf[0])
    return ""


def _extract_user(issue, field_id):
    """提取用户字段的 displayName"""
    cf = issue.get("fields", {}).get(field_id)
    if isinstance(cf, dict):
        return cf.get("displayName", cf.get("name", ""))
    return str(cf) if cf else ""


def _fmt_datetime(dt_str):
    """格式化日期时间: 2026-03-25T10:30:00.000+0800 → 2026-03-25 10:30"""
    if not dt_str or dt_str == "-":
        return ""
    try:
        return dt_str[:16].replace("T", " ")
    except Exception:
        return dt_str


def to_report_csv(issues, filepath):
    """导出周报兼容格式的 CSV（与手动 Jira 导出完全一致的列名和格式）"""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([col[0] for col in REPORT_CSV_COLUMNS])
        for i in issues:
            w.writerow([col[1](i) for col in REPORT_CSV_COLUMNS])
    progress_done(f"已导出周报格式 CSV: {filepath} ({len(issues)} 条)")


# 进度动画符号
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def progress(msg, current=0, total=0):
    """输出带进度的状态信息到 stderr"""
    spinner = SPINNER[current % len(SPINNER)]
    if total > 0:
        pct = min(current / total * 100, 100)
        bar_len = 20
        filled = int(bar_len * current / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  {spinner} {msg} {bar} {pct:.0f}% ({current}/{total})", end="", file=sys.stderr)
    else:
        print(f"\r  {spinner} {msg}", end="", file=sys.stderr)


def progress_done(msg):
    """进度完成"""
    print(f"\r  ✓ {msg}                                        ", file=sys.stderr)


def _derive_machine_key() -> bytes:
    """从机器特征派生 Fernet 密钥（hostname + 用户目录，无需额外密钥文件）"""
    machine_id = f"{socket.gethostname()}:{os.path.expanduser('~')}"
    key_bytes = hashlib.pbkdf2_hmac('sha256', machine_id.encode(),
                                     b'ticket-query-skill-v1', 100_000)
    return base64.urlsafe_b64encode(key_bytes)


def _encrypt_password(pwd: str) -> str:
    """用机器绑定密钥加密密码（Fernet），返回密文字符串"""
    if not _HAS_CRYPTO:
        raise RuntimeError("需要 cryptography 包：pip install cryptography")
    return Fernet(_derive_machine_key()).encrypt(pwd.encode()).decode()


def _decrypt_password(enc: str) -> str:
    """解密 Fernet 密文密码"""
    if not _HAS_CRYPTO:
        print("\n  ✗ 需要 cryptography 包来解密密码：pip install cryptography", file=sys.stderr)
        sys.exit(1)
    try:
        return Fernet(_derive_machine_key()).decrypt(enc.encode()).decode()
    except Exception:
        print("\n  ✗ 密码解密失败（加密配置不能跨机器使用）", file=sys.stderr)
        print("  ✗ 请重新运行: python jira_query.py --setup", file=sys.stderr)
        sys.exit(1)


def _get_password(config: dict) -> str:
    """获取密码（优先级：环境变量 > 加密字段 > 明文字段）"""
    env_pwd = os.environ.get('JIRA_PASSWORD')
    if env_pwd:
        return env_pwd
    if 'password_enc' in config:
        return _decrypt_password(config['password_enc'])
    if 'password' in config:
        print("\n  ⚠️  config.json 使用明文密码，建议运行 --setup 升级为加密存储", file=sys.stderr)
        return config['password']
    print("\n  ✗ 配置文件中未找到密码（password 或 password_enc 字段）", file=sys.stderr)
    sys.exit(1)


def setup_config():
    """交互式配置向导，生成加密 config.json"""
    print("\n🔧 Jira 工单查询 SKILL 配置向导")
    print("─" * 40)

    existing = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                existing = json.load(f)
        except Exception:
            pass
        print(f"  已有配置文件: {CONFIG_PATH}")
        answer = input("  是否覆盖现有配置？[y/N] ").strip().lower()
        if answer != 'y':
            print("  已取消")
            return

    default_url = existing.get('jira_base_url', 'https://gfjira.yyrd.com')
    jira_url = input(f"  Jira 地址 [{default_url}]: ").strip() or default_url

    default_user = existing.get('username', '')
    prompt_user = f"  用户名 [{default_user}]: " if default_user else "  用户名: "
    username = input(prompt_user).strip() or default_user
    if not username:
        print("  ✗ 用户名不能为空")
        sys.exit(1)

    password = getpass.getpass("  密码（输入不可见）: ")
    if not password:
        print("  ✗ 密码不能为空")
        sys.exit(1)

    cfg = {
        "jira_base_url": jira_url,
        "username": username,
        "ssl_verify": existing.get('ssl_verify', True),
        "default_project": existing.get('default_project', 'LCZX'),
    }

    if _HAS_CRYPTO:
        cfg['password_enc'] = _encrypt_password(password)
        enc_note = "✓ 密码已加密（机器专属 Fernet 密钥，无法跨机器使用）"
    else:
        cfg['password'] = password
        enc_note = "⚠️  cryptography 未安装，密码以明文存储（pip install cryptography 可启用加密）"

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print(f"\n  {enc_note}")
    print(f"  ✓ 配置已保存: {CONFIG_PATH}")
    print(f"  ✓ 运行 --test-connection 验证配置\n")


def load_config():
    if not CONFIG_PATH.exists():
        print("⚠️  未找到配置文件", file=sys.stderr)
        print(f"   请运行: python {Path(__file__).name} --setup", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def jira_request(config, path, params=None):
    """发送 Jira REST API 请求"""
    url = f"{config['jira_base_url']}{path}"
    if params:
        url += "?" + urlencode(params, quote_via=quote)
    username = os.environ.get('JIRA_USERNAME') or config['username']
    password = _get_password(config)
    cred = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = Request(url, headers={
        "Authorization": f"Basic {cred}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    ctx = ssl.create_default_context()
    if not config.get("ssl_verify", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()
        try:
            err = json.loads(body)
            msgs = err.get("errorMessages", [body[:200]])
        except Exception:
            msgs = [body[:200]]
        print(f"\n  ✗ Jira API 错误 ({e.code}): {'; '.join(msgs)}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"\n  ✗ 网络错误: {e.reason}", file=sys.stderr)
        sys.exit(1)


def search(config, jql, max_results=50, start_at=0, fields=FIELDS):
    return jira_request(config, "/rest/api/2/search", {
        "jql": jql, "startAt": start_at, "maxResults": max_results, "fields": fields
    })


def fetch_all(config, jql, fields=FIELDS):
    """分页获取全部工单（带进度条）"""
    all_issues = []
    start_at = 0
    # 先获取总数
    first = search(config, jql, max_results=1, start_at=0, fields=fields)
    total = first.get("total", 0)
    if total == 0:
        progress_done(f"查询完成，共 0 条")
        return [], 0

    progress(f"正在获取 {total} 条工单", 0, total)
    while True:
        data = search(config, jql, max_results=500, start_at=start_at, fields=fields)
        issues = data.get("issues", [])
        all_issues.extend(issues)
        progress(f"正在获取 {total} 条工单", len(all_issues), total)
        if start_at + len(issues) >= total or not issues:
            break
        start_at += len(issues)
    progress_done(f"获取完成，共 {len(all_issues)} 条工单")
    return all_issues, total


def extract(issue, path, default="-"):
    """安全提取嵌套字段"""
    obj = issue
    for key in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and obj:
            obj = obj[0]
        else:
            return default
    if obj is None:
        return default
    return str(obj) if not isinstance(obj, str) else obj


def to_csv(issues, filepath):
    """导出 CSV（UTF-8 BOM）"""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([col[0] for col in CSV_COLUMNS])
        for i in issues:
            row = []
            for _, path in CSV_COLUMNS:
                if path == "key":
                    row.append(i.get("key", ""))
                elif path == "fields.customfield_10725":
                    cf = i.get("fields", {}).get("customfield_10725")
                    row.append(cf[0].rstrip(",").strip() if isinstance(cf, list) and cf else "-")
                elif path == "fields.labels":
                    labels = i.get("fields", {}).get("labels", [])
                    row.append(", ".join(labels) if labels else "-")
                elif path.startswith("fields.created") or path.startswith("fields.updated"):
                    row.append(extract(i, path, "")[:10])
                elif path == "fields.duedate":
                    row.append(extract(i, path, ""))
                else:
                    row.append(extract(i, path))
            w.writerow(row)
    progress_done(f"已导出 {filepath} ({len(issues)} 条)")


def test_connection(config):
    """验证 Jira 连接"""
    progress("正在验证 Jira 连接", 0, 0)
    try:
        info = jira_request(config, "/rest/api/2/serverInfo")
        progress_done(f"连接成功 — {info.get('serverTitle', 'Jira')} v{info.get('version', '?')}")
        # 验证用户身份
        progress("正在验证用户身份", 0, 0)
        user_data = jira_request(config, f"/rest/api/2/user?username={config['username']}")
        display_name = user_data.get("displayName", config["username"])
        progress_done(f"用户验证通过 — {display_name}")
        print(json.dumps({"status": "ok", "server": info.get("serverTitle"), "user": display_name}, ensure_ascii=False))
    except SystemExit:
        print(json.dumps({"status": "error", "message": "连接失败，请检查用户名密码"}))
        sys.exit(1)


def discover_fields(config):
    """发现 Jira 字段元数据"""
    progress("正在获取优先级列表", 0, 0)
    priorities = jira_request(config, "/rest/api/2/priority")
    progress_done(f"优先级: {', '.join(p['name'] for p in priorities)}")

    progress("正在获取状态列表", 0, 0)
    statuses = jira_request(config, "/rest/api/2/status")
    progress_done(f"状态: {len(statuses)} 个")

    progress("正在获取项目列表", 0, 0)
    projects = jira_request(config, "/rest/api/2/project")
    progress_done(f"项目: {len(projects)} 个")

    print(json.dumps({
        "priorities": [p["name"] for p in priorities],
        "statuses": [s["name"] for s in statuses],
        "projects": [{"key": p["key"], "name": p["name"]} for p in projects],
    }, ensure_ascii=False, indent=2))


def group_by(issues, month=False, field=None):
    """聚合统计"""
    progress("正在聚合分析", 0, 0)
    if month:
        monthly = defaultdict(list)
        for i in issues:
            m = extract(i, "fields.created")[:7]
            monthly[m].append(i)
        result = {}
        for m in sorted(monthly):
            entry = {"count": len(monthly[m])}
            if field:
                counter = Counter()
                for i in monthly[m]:
                    val = extract(i, f"fields.{field}.value", extract(i, f"fields.{field}"))
                    counter[val] += 1
                entry["by_field"] = dict(counter.most_common())
            result[m] = entry
        progress_done(f"聚合完成 — {len(result)} 个月份")
        return result
    elif field:
        counter = Counter()
        for i in issues:
            val = extract(i, f"fields.{field}.value", extract(i, f"fields.{field}"))
            counter[val] += 1
        progress_done(f"聚合完成 — {len(counter)} 个分类")
        return dict(counter.most_common())


def top_n_analysis(issues, field_path, n=10, label="项目"):
    """内置 TOP N 分析，避免 Agent 创建外部脚本"""
    counter = Counter()
    for i in issues:
        if field_path == "customer":
            cf = i.get("fields", {}).get("customfield_10725", [])
            val = cf[0].rstrip(",").strip() if isinstance(cf, list) and cf and isinstance(cf[0], str) else None
        elif field_path == "assignee":
            a = i.get("fields", {}).get("assignee")
            val = a.get("displayName", a.get("name")) if isinstance(a, dict) else None
        else:
            val = extract(i, field_path)
            val = val if val != "-" else None
        if val:
            counter[val] += 1
    total_counted = sum(counter.values())
    results = counter.most_common(n)
    lines = [f"## {label} TOP {n}\n", f"| 排名 | {label} | 数量 | 占比 |", f"|------|--------|------|------|"]
    for rank, (name, count) in enumerate(results, 1):
        pct = count / total_counted * 100 if total_counted else 0
        lines.append(f"| {rank} | {name} | {count} | {pct:.1f}% |")
    lines.append(f"\n> 有效数据: {total_counted} 条 / 总计: {len(issues)} 条")
    return "\n".join(lines)


def summary_analysis(issues, total):
    """内置概要统计"""
    status_ct = Counter()
    priority_ct = Counter()
    for i in issues:
        status_ct[extract(i, "fields.status.name")] += 1
        priority_ct[extract(i, "fields.priority.name")] += 1
    lines = [
        f"## 查询概要\n",
        f"- **总数**: {total} 条",
        f"- **已获取**: {len(issues)} 条\n",
        f"### 按状态分布\n| 状态 | 数量 | 占比 |", "|------|------|------|",
    ]
    for s, c in status_ct.most_common():
        lines.append(f"| {s} | {c} | {c/len(issues)*100:.1f}% |")
    lines.extend([f"\n### 按优先级分布\n| 优先级 | 数量 | 占比 |", "|--------|------|------|"])
    for p, c in priority_ct.most_common():
        lines.append(f"| {p} | {c} | {c/len(issues)*100:.1f}% |")
    return "\n".join(lines)


def format_markdown_table(issues, max_rows=50):
    """输出 Markdown 表格（直接可展示）"""
    lines = [
        f"## 查询结果 (共 {len(issues)} 条)\n",
        "| 工单号 | 标题 | 状态 | 优先级 | 经办人 | 客户 | 创建日期 |",
        "|--------|------|------|--------|--------|------|----------|",
    ]
    for i in issues[:max_rows]:
        key = i.get("key", "")
        f = i.get("fields", {})
        summary = (extract(i, "fields.summary") or "-")[:40]
        status = extract(i, "fields.status.name")
        priority = extract(i, "fields.priority.name")
        assignee = extract(i, "fields.assignee.displayName")
        cf = f.get("customfield_10725", [])
        customer = cf[0].rstrip(",").strip()[:15] if isinstance(cf, list) and cf and isinstance(cf[0], str) else "-"
        created = extract(i, "fields.created")[:10]
        lines.append(f"| {key} | {summary} | {status} | {priority} | {assignee} | {customer} | {created} |")
    if len(issues) > max_rows:
        lines.append(f"| ... | 还有 {len(issues) - max_rows} 条 | | | | | |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Jira 工单查询 — BIP产品规划部 qiangxiao")
    parser.add_argument("--jql", required=False, help="JQL 查询语句")
    parser.add_argument("--max-results", type=int, default=50, help="最大返回数（默认50）")
    parser.add_argument("--all", action="store_true", help="分页获取全部结果")
    parser.add_argument("--csv", metavar="FILE", help="导出 CSV 到指定路径")
    parser.add_argument("--report-csv", metavar="FILE", help="导出周报兼容格式 CSV")
    parser.add_argument("--group-by-month", action="store_true", help="按月聚合")
    parser.add_argument("--group-by-field", metavar="FIELD", help="按字段聚合")
    parser.add_argument("--top-customers", type=int, metavar="N", help="按客户 TOP N 统计")
    parser.add_argument("--top-assignees", type=int, metavar="N", help="按经办人 TOP N 统计")
    parser.add_argument("--summary", action="store_true", help="输出概要统计（状态/优先级分布）")
    parser.add_argument("--format", choices=["json", "markdown"], default="json", help="输出格式（默认json）")
    parser.add_argument("--discover-fields", action="store_true", help="发现 Jira 字段元数据")
    parser.add_argument("--test-connection", action="store_true", help="验证 Jira 连接")
    parser.add_argument("--setup", action="store_true", help="交互式配置向导（加密存储密码）")
    args = parser.parse_args()

    if args.setup:
        setup_config()
        return

    config = load_config()

    if args.test_connection:
        test_connection(config)
        return

    if args.discover_fields:
        discover_fields(config)
        return

    if not args.jql:
        parser.error("需要 --jql 参数、--discover-fields 或 --test-connection")

    if args.all:
        issues, total = fetch_all(config, args.jql)
    else:
        progress("正在查询 Jira", 0, 0)
        data = search(config, args.jql, max_results=args.max_results)
        issues = data.get("issues", [])
        total = data.get("total", 0)
        progress_done(f"查询完成 — 共 {total} 条，返回 {len(issues)} 条")

    if args.csv:
        to_csv(issues, args.csv)

    if args.report_csv:
        to_report_csv(issues, args.report_csv)

    # 内置聚合分析（直接输出 Markdown，不需要外部脚本）
    if args.top_customers:
        print(top_n_analysis(issues, "customer", args.top_customers, "客户"))
        return
    if args.top_assignees:
        print(top_n_analysis(issues, "assignee", args.top_assignees, "经办人"))
        return
    if args.summary:
        print(summary_analysis(issues, total))
        return

    # 聚合输出
    if args.group_by_month or args.group_by_field:
        result = group_by(issues, month=args.group_by_month, field=args.group_by_field)
        print(json.dumps({"total": total, "fetched": len(issues), "groups": result}, ensure_ascii=False, indent=2))
    elif getattr(args, 'format', 'json') == "markdown":
        print(format_markdown_table(issues))
    else:
        print(json.dumps({"total": total, "count": len(issues), "issues": issues}, ensure_ascii=False))


if __name__ == "__main__":
    main()
