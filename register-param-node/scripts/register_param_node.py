#!/usr/bin/env python3
"""Generate and optionally execute parameter node registration SQL."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import pymysql
except ImportError:
    pymysql = None


HEADERS = [
    "领域云",
    "子领域",
    "子领域对接人",
    "参数节点",
    "负责人",
    "服务编码",
    "节点所属应用",
    "节点所属应用多语词条ID",
    "应用编码",
    "参数类型",
    "关联微服务",
    "option_id\n研发提供",
    "二方包参数\n研发提供",
    "组织级参照类型",
    "组织参照编码",
    "是否框架",
    "配置迁移",
    "框架改造",
    "地址改造",
    "确认产品形态\n包括所属领域云、子领域、服务编码、微服务编码、参数类型等",
    "录入验证故事/bug\n（填写单号）",
    "标准化改造\n去自定义树",
    "标准化改造\n使用标准组织参照",
    "UE规范改造",
    "接入平台时间\n（不接入填写不确认并填写最后一列备注）",
    "所有适配任务完成时间",
    "原参数节点下线时间计划\n（不确定留空，不下线写保留不下线）",
    "确认人",
    "备注\n（特殊问题、不接入理由等）",
]

ALIASES = {
    "option_id 研发提供": "option_id\n研发提供",
    "option_id": "option_id\n研发提供",
    "多语资源ID": "节点所属应用多语词条ID",
    "多语词条ID": "节点所属应用多语词条ID",
    "name_resid": "节点所属应用多语词条ID",
    "application_code": "应用编码",
    "service_code": "服务编码",
    "micro_service_code": "关联微服务",
    "参数编码": "注册编码",
    "节点编码": "注册编码",
    "注册编码": "注册编码",
    "code": "注册编码",
    "服务地址": "服务地址",
    "url": "服务地址",
}


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-file", help="File containing a pasted tab-separated row or key-value text.")
    source.add_argument("--fields-json", help="JSON object with Chinese field names.")
    parser.add_argument("--execute", action="store_true", help="Execute inserts in the parameter database.")
    parser.add_argument("--creator", default=env("PARAM_NODE_CREATOR", "testqx"))
    parser.add_argument("--tenant-id", default="0")
    parser.add_argument("--ordernum", type=int, default=120)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")

    parser.add_argument("--service-db-host", default=env("PARAM_NODE_SERVICE_DB_HOST", "dbproxy.diwork.com"))
    parser.add_argument("--service-db-port", type=int, default=int(env("PARAM_NODE_SERVICE_DB_PORT", "12999")))
    parser.add_argument("--service-db-user", default=env("PARAM_NODE_SERVICE_DB_USER", "iuap_benchservice"))
    parser.add_argument("--service-db-password", default=env("PARAM_NODE_SERVICE_DB_PASSWORD"))
    parser.add_argument("--service-db-name", default=env("PARAM_NODE_SERVICE_DB_NAME", "iuap_apcom_benchservice"))

    parser.add_argument("--auth-db-host", default=env("PARAM_NODE_AUTH_DB_HOST", "dbproxy.diwork.com"))
    parser.add_argument("--auth-db-port", type=int, default=int(env("PARAM_NODE_AUTH_DB_PORT", "12368")))
    parser.add_argument("--auth-db-user", default=env("PARAM_NODE_AUTH_DB_USER", "iuap_apauth"))
    parser.add_argument("--auth-db-password", default=env("PARAM_NODE_AUTH_DB_PASSWORD"))
    parser.add_argument("--auth-db-name", default=env("PARAM_NODE_AUTH_DB_NAME", "iuap_apcom_auth"))
    return parser.parse_args()


def normalize_key(key: str) -> str:
    compact = re.sub(r"\s+", " ", key.strip())
    return ALIASES.get(compact, ALIASES.get(key.strip(), key.strip()))


def clean_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def normalize_fields(raw: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key, value in raw.items():
        cleaned = clean_value(value)
        if cleaned is not None:
            fields[normalize_key(key)] = cleaned
    return fields


def strip_code_fences(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.strip().startswith("```") and not line.strip().startswith("````")
    )


def parse_key_value_text(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in strip_code_fences(text).splitlines():
        if not line.strip():
            continue
        if "：" in line:
            key, value = line.split("：", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        elif "=" in line:
            key, value = line.split("=", 1)
        else:
            continue
        value = clean_value(value)
        if value:
            fields[normalize_key(key)] = value
    return fields


def parse_input_file(path: str) -> dict[str, str]:
    text = open(path, "r", encoding="utf-8").read()
    body = strip_code_fences(text)
    for line in body.splitlines():
        if "\t" not in line or not line.strip():
            continue
        row = next(csv.reader(io.StringIO(line), dialect="excel-tab"))
        return normalize_fields(dict(zip(HEADERS, row)))
    fields = parse_key_value_text(text)
    if fields:
        return normalize_fields(fields)
    raise SystemExit("No tab-separated row or key-value fields were found in the input file.")


def load_fields(args: argparse.Namespace) -> dict[str, str]:
    if args.fields_json:
        try:
            parsed = json.loads(args.fields_json)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--fields-json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SystemExit("--fields-json must be a JSON object.")
        return normalize_fields(parsed)
    return parse_input_file(args.input_file)


def is_url_like(value: str) -> bool:
    return bool(re.match(r"^(https?://|/|\\$\\{domain\\.)", value.strip()))


def db_config(prefix: str, args: argparse.Namespace, require_password: bool) -> tuple[dict[str, Any], list[str]]:
    if prefix == "service":
        cfg = {
            "host": args.service_db_host,
            "port": args.service_db_port,
            "user": args.service_db_user,
            "password": args.service_db_password,
            "database": args.service_db_name,
        }
        names = {
            "password": "PARAM_NODE_SERVICE_DB_PASSWORD 或 --service-db-password",
        }
    else:
        cfg = {
            "host": args.auth_db_host,
            "port": args.auth_db_port,
            "user": args.auth_db_user,
            "password": args.auth_db_password,
            "database": args.auth_db_name,
        }
        names = {
            "password": "PARAM_NODE_AUTH_DB_PASSWORD 或 --auth-db-password",
        }
    missing = []
    for key in ["host", "port", "user", "database"]:
        if cfg.get(key) in (None, ""):
            missing.append(f"{prefix}.{key}")
    if require_password and not cfg.get("password"):
        missing.append(names["password"])
    return cfg, missing


def connect(cfg: dict[str, Any]):
    if pymysql is None:
        raise RuntimeError("Missing Python package pymysql. Install with: python3 -m pip install pymysql")
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=20,
        write_timeout=20,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def fetch_service(fields: dict[str, str], args: argparse.Namespace) -> tuple[dict[str, Any] | None, str, str]:
    service_code = fields.get("服务编码", "")
    param_name = fields.get("参数节点", "")
    query_by = "service_name" if is_url_like(service_code) else "service_code"
    query_value = param_name if query_by == "service_name" else service_code
    cfg, missing = db_config("service", args, require_password=True)
    if missing:
        raise MissingConfig(missing)
    sql = f"select * from sys_service where {query_by}=%s limit 1"
    with connect(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (query_value,))
            row = cur.fetchone()
    return row, query_by, query_value


class MissingConfig(Exception):
    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(", ".join(missing))


def first_present(*values: Any) -> str | None:
    for value in values:
        cleaned = clean_value(value)
        if cleaned is not None:
            return cleaned
    return None


def service_url(fields: dict[str, str], service: dict[str, Any] | None) -> str | None:
    raw = first_present(
        service.get("url") if service else None,
        fields.get("服务地址"),
        fields.get("服务编码") if fields.get("服务编码") and is_url_like(fields["服务编码"]) else None,
    )
    if not raw:
        return None
    if raw.startswith("${domain."):
        return raw
    if raw.startswith("/"):
        return "${domain.iuap-mdf-node}" + raw
    return raw


def url_parts(raw: str | None) -> dict[str, str | None]:
    if not raw:
        return {"domain_key": None, "busi_obj": None, "path_code": None, "domain_placeholder": None}
    parsed = urlparse(raw)
    query = parse_qs(parsed.query)
    path = parsed.path or raw.split("?", 1)[0]
    path_tail = path.rstrip("/").rsplit("/", 1)[-1] if path else None
    placeholder = re.search(r"\$\{domain\.([^}]+)\}", raw)
    domain_key = first_present(
        query.get("domainKey", [None])[0],
        query.get("domain_key", [None])[0],
    )
    return {
        "domain_key": domain_key,
        "busi_obj": first_present(query.get("busiObj", [None])[0], query.get("busi_obj", [None])[0]),
        "path_code": sanitize_code(path_tail),
        "domain_placeholder": placeholder.group(1) if placeholder else None,
    }


def sanitize_code(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip()).strip("_")
    return cleaned[:100] if cleaned else None


def snowflakeish_id() -> int:
    return int(time.time() * 1000) * 1_000_000 + random.randint(1000, 999999)


def param_type_value(param_type: str | None) -> int | None:
    if not param_type:
        return 1
    if "组织" in param_type:
        return 1
    return None


def build_payload(fields: dict[str, str], service: dict[str, Any] | None, args: argparse.Namespace) -> tuple[dict[str, Any], list[str], list[str]]:
    missing: list[str] = []
    warnings: list[str] = []
    for key in ["参数节点", "服务编码"]:
        if not fields.get(key):
            missing.append(key)

    ptype = param_type_value(fields.get("参数类型"))
    if ptype is None:
        missing.append("参数类型（当前脚本仅能确认“组织级参数”如何映射）")

    service_code_input = fields.get("服务编码")
    url_fallback = bool(service_code_input and is_url_like(service_code_input) and not service)
    parsed_url = url_parts(service_code_input if url_fallback else fields.get("服务地址"))

    code = first_present(
        service.get("service_code") if service else None,
        fields.get("注册编码"),
        None if url_fallback else fields.get("服务编码"),
        parsed_url["busi_obj"] if url_fallback else None,
        parsed_url["path_code"] if url_fallback else None,
    )
    name = first_present(fields.get("参数节点"), service.get("service_name") if service else None)
    app_code = first_present(fields.get("应用编码"), service.get("application_code") if service else None)
    name_resid = first_present(fields.get("节点所属应用多语词条ID"), service.get("resid") if service else None)
    micro_service_code = first_present(
        fields.get("关联微服务"),
        service.get("micro_service_code") if service else None,
        service.get("runtime_micro_service_code") if service else None,
        service.get("domain_key") if service else None,
        parsed_url["domain_key"] if url_fallback else None,
        parsed_url["domain_placeholder"] if url_fallback else None,
    )
    domain = first_present(service.get("domain_key") if service else None, parsed_url["domain_key"], micro_service_code)
    request_content = service_url(fields, service)

    required_after_lookup = {
        "服务编码/最终 code": code,
        "参数节点/name": name,
        "关联微服务/micro_service_code": micro_service_code,
        "服务地址/request_content": request_content,
        "domain": domain,
    }
    if not url_fallback:
        required_after_lookup["应用编码/application_code"] = app_code
        required_after_lookup["多语词条ID/name_resid"] = name_resid
    else:
        if not app_code:
            warnings.append("服务编码为 URL 且服务库未查到记录，application_code 未补全，将按 null 写入。")
        if not name_resid:
            warnings.append("服务编码为 URL 且服务库未查到记录，name_resid 未补全，将按 null 写入。")
        warnings.append("服务编码为 URL 且服务库未查到记录，已按 URL 本身进行补全注册。")
    for label, value in required_after_lookup.items():
        if not value:
            missing.append(label)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "param_group": {
            "id": str(snowflakeish_id()),
            "code": code,
            "name": name,
            "name_resid": name_resid,
            "application_code": app_code,
            "micro_service_code": micro_service_code,
            "request_content": request_content,
            "request_type": 1,
            "external_data": None,
            "param_type": ptype,
            "org_reference": fields.get("组织参照编码"),
            "group_code": code,
            "is_domain_param": 1,
            "domain": domain,
            "option_id": fields.get("option_id\n研发提供"),
            "creator": args.creator,
            "creator_time": now,
            "modifier": None,
            "modify_time": None,
            "pubts": now,
            "ytenant_id": args.tenant_id,
            "dr": 0,
        },
        "option_group": {
            "id": snowflakeish_id(),
            "code": code,
            "name": name,
            "ordernum": args.ordernum,
            "parentcode": "common_option_01",
            "pubts": now,
            "ideleted": 0,
            "datasourcename": None,
            "image": None,
            "controltype": None,
            "align": None,
            "ismain": 1,
            "optionid": "common_option",
            "iCols": 0,
            "industrytype": None,
            "cStyle": None,
            "systemcode": "U8C3",
            "name_resid": name_resid,
            "ytenant_id": args.tenant_id,
            "micro_service_code": micro_service_code,
        },
    }
    payload["url_fallback"] = url_fallback
    return payload, missing, warnings


def sql_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


def insert_statement(table: str, row: dict[str, Any]) -> str:
    columns = list(row.keys())
    values = ", ".join(sql_literal(row[col]) for col in columns)
    return f"INSERT INTO iuap_apcom_auth.{table} ({', '.join(columns)}) VALUES ({values});"


def check_duplicates(cur: Any, code: str, tenant_id: str) -> list[dict[str, Any]]:
    duplicates: list[dict[str, Any]] = []
    cur.execute(
        "select id, code, name, ytenant_id from pub_param_group where code=%s and ytenant_id=%s and dr=0",
        (code, tenant_id),
    )
    for row in cur.fetchall():
        row["table"] = "pub_param_group"
        duplicates.append(row)
    cur.execute(
        "select id, code, name, ytenant_id from pub_option_group where code=%s and ytenant_id=%s and ideleted=0",
        (code, tenant_id),
    )
    for row in cur.fetchall():
        row["table"] = "pub_option_group"
        duplicates.append(row)
    return duplicates


def generate_sql(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "pub_param_group": insert_statement("pub_param_group", payload["param_group"]),
        "pub_option_group": insert_statement("pub_option_group", payload["option_group"]),
    }


def execute(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    cfg, missing = db_config("auth", args, require_password=True)
    if missing:
        return {"status": "missing_db_config", "missing": missing}
    with connect(cfg) as conn:
        try:
            with conn.cursor() as cur:
                duplicates = check_duplicates(
                    cur,
                    payload["param_group"]["code"],
                    payload["param_group"]["ytenant_id"],
                )
                if duplicates:
                    conn.rollback()
                    return {"status": "duplicate_rows", "duplicates": duplicates}
                statements = []
                for table, key in [("pub_param_group", "param_group"), ("pub_option_group", "option_group")]:
                    row = payload[key]
                    columns = list(row.keys())
                    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
                    cur.execute(sql, tuple(row[col] for col in columns))
                    statements.append({"table": table, "affected_rows": cur.rowcount})
            conn.commit()
            return {"status": "executed", "statements": statements}
        except Exception as exc:
            conn.rollback()
            return {"status": "error", "error": str(exc)}


def main() -> int:
    args = parse_args()
    fields = load_fields(args)
    result: dict[str, Any] = {"input_fields": fields}

    missing_initial = [key for key in ["参数节点", "服务编码"] if not fields.get(key)]
    if missing_initial:
        result.update({"status": "missing_fields", "missing": missing_initial})
        print_result(result, args.json)
        return 2

    service = None
    if args.service_db_password:
        try:
            service, query_by, query_value = fetch_service(fields, args)
            result["service_lookup"] = {
                "query_by": query_by,
                "query_value": query_value,
                "found": bool(service),
                "service_code": service.get("service_code") if service else None,
                "service_name": service.get("service_name") if service else None,
                "application_code": service.get("application_code") if service else None,
                "resid": service.get("resid") if service else None,
                "url": service.get("url") if service else None,
                "domain_key": service.get("domain_key") if service else None,
                "micro_service_code": service.get("micro_service_code") if service else None,
            }
        except MissingConfig as exc:
            result.update({"status": "missing_db_config", "missing": exc.missing})
            print_result(result, args.json)
            return 3
        except Exception as exc:
            result.update({"status": "service_lookup_error", "error": str(exc)})
            print_result(result, args.json)
            return 3
    else:
        result["service_lookup"] = {
            "skipped": True,
            "reason": "missing PARAM_NODE_SERVICE_DB_PASSWORD or --service-db-password",
        }

    payload, missing, warnings = build_payload(fields, service, args)
    if missing:
        result.update({"status": "missing_fields", "missing": sorted(set(missing))})
        if warnings:
            result["warnings"] = warnings
        print_result(result, args.json)
        return 4

    result["resolved_values"] = {
        "code": payload["param_group"]["code"],
        "name": payload["param_group"]["name"],
        "application_code": payload["param_group"]["application_code"],
        "name_resid": payload["param_group"]["name_resid"],
        "micro_service_code": payload["param_group"]["micro_service_code"],
        "request_content": payload["param_group"]["request_content"],
        "domain": payload["param_group"]["domain"],
        "url_fallback": payload["url_fallback"],
    }
    if warnings:
        result["warnings"] = warnings
    result["sql"] = generate_sql(payload)

    if args.execute:
        result["execution"] = execute(payload, args)
        result["status"] = result["execution"]["status"]
        exit_code = 0 if result["status"] == "executed" else 6
    else:
        result["status"] = "dry_run"
        exit_code = 0

    print_result(result, args.json)
    return exit_code


def print_result(result: dict[str, Any], json_only: bool) -> None:
    if json_only:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print("STATUS:", result.get("status"))
    if "service_lookup" in result:
        print("SERVICE_LOOKUP:", json.dumps(result["service_lookup"], ensure_ascii=False, default=str))
    if "resolved_values" in result:
        print("RESOLVED:", json.dumps(result["resolved_values"], ensure_ascii=False, default=str))
    if "missing" in result:
        print("MISSING:", "、".join(result["missing"]))
    if "warnings" in result:
        print("WARNINGS:", "、".join(result["warnings"]))
    if "error" in result:
        print("ERROR:", result["error"])
    if "sql" in result:
        print("\n-- pub_param_group")
        print(result["sql"]["pub_param_group"])
        print("\n-- pub_option_group")
        print(result["sql"]["pub_option_group"])
    if "execution" in result:
        print("\nEXECUTION:", json.dumps(result["execution"], ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
