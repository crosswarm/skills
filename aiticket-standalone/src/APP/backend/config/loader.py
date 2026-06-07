"""
Instance configuration loader. Call get_instance_config() anywhere to access
deployment.yaml values. The file is loaded once at startup.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


_DEFAULT_CONFIG: dict[str, Any] = {
    "instance": {
        "name": "AITicket",
        "slug": "aiticket",
        "primary_project_key": "",
        "allowed_project_keys": [],
    },
    "jira": {
        "base_url": "https://gfjira.yyrd.com",
        "cookie_domains": [],
        "ssl_verify": True,
        "ca_bundle": "",
    },
    "pm_system": {
        "enabled": False,
        "base_url": "",
        "tenant_info": "0000",
        "default_analyst": "",
    },
    "feishu": {
        "enabled": False,
        "webhook_secret_env": "FEISHU_WEBHOOK_SECRET",
        "notify_chat_ids": [],
    },
    "llm": {
        "default_provider_chain": ["zhipu", "minimax"],
        "features": {},
    },
    "module_taxonomy": [
        {"name": "通用", "team": "", "keywords": []},
    ],
    "kb": {
        "root_dir": "/data/kb",
        "source_kinds": ["doc", "ticket_case", "fact"],
        "ontology_validator": "default",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _find_config_file() -> Path | None:
    candidates = [
        Path(os.environ.get("CONFIG_FILE", "")),
        Path("/app/config/deployment.yaml"),
        Path(__file__).parent / "deployment.yaml",
        Path("config/deployment.yaml"),
    ]
    for p in candidates:
        if p and p.is_file():
            return p
    return None


@lru_cache(maxsize=1)
def get_instance_config() -> dict[str, Any]:
    config = dict(_DEFAULT_CONFIG)
    path = _find_config_file()
    if path is None:
        print("[config] WARNING: deployment.yaml not found, using defaults")
        return config
    if yaml is None:
        raise RuntimeError("pyyaml is required: pip install pyyaml")
    with open(path) as f:
        user_cfg = yaml.safe_load(f) or {}
    return _deep_merge(config, user_cfg)


def cfg(section: str, key: str | None = None, default: Any = None) -> Any:
    """Convenience accessor: cfg('jira', 'base_url') or cfg('instance')."""
    c = get_instance_config()
    section_data = c.get(section, {})
    if key is None:
        return section_data
    return section_data.get(key, default)
