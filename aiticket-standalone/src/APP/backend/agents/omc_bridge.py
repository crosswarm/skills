"""
omc_bridge — OMC subagent 注册桥接器

启动期扫描 ~/.claude/plugins/cache/omc/oh-my-claudecode/*/agents/*.md
和 .claude/agents/*.md，把每个 OMC subagent 注册为固定角色进入 AgentRegistry。

父 agent 映射规则（可在 identity yaml 内手动覆盖）：
  designer, design-consultation → ux_master
  其他 → claude（ClaudeAgent）
"""
from __future__ import annotations

import glob
import logging
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

import yaml

if TYPE_CHECKING:
    from agents.registry import AgentRegistry

logger = logging.getLogger(__name__)

# 父 agent 映射：关键词 → parent_agent name
_PARENT_MAP: Dict[str, str] = {
    "designer": "ux_master",
    "design": "ux_master",
}
_DEFAULT_PARENT = "claude"

# Identity yaml 落盘目录
_IDENTITY_DIR = Path(__file__).resolve().parent / "identity"


def _omc_agent_dirs() -> List[Path]:
    """返回所有可能存放 OMC agents .md 的目录（不锁版本）。"""
    dirs: List[Path] = []
    pattern = str(Path.home() / ".claude" / "plugins" / "cache" / "omc" / "oh-my-claudecode" / "*" / "agents")
    dirs += [Path(p) for p in glob.glob(pattern) if Path(p).is_dir()]
    # 项目本地 .claude/agents/
    local = Path(__file__).resolve().parents[3] / ".claude" / "agents"
    if local.is_dir():
        dirs.append(local)
    return dirs


def _parse_md_frontmatter(md_path: Path) -> Optional[dict]:
    """解析 .md 文件的 YAML frontmatter（--- ... ---）。"""
    try:
        text = md_path.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not m:
            return None
        return yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        logger.warning(f"[omc_bridge] parse frontmatter {md_path}: {e}")
        return None


def _map_parent(omc_name: str, description: str) -> str:
    """按名称和描述关键词映射父 agent。"""
    combined = (omc_name + " " + (description or "")).lower()
    for keyword, parent in _PARENT_MAP.items():
        if keyword in combined:
            return parent
    return _DEFAULT_PARENT


def _ensure_identity_yaml(omc_name: str, frontmatter: dict, parent: str, md_path: Path) -> Path:
    """若 identity yaml 不存在则落盘；已存在则跳过（保留用户手改）。"""
    yaml_path = _IDENTITY_DIR / f"omc_{omc_name.replace('-', '_')}.yaml"
    if yaml_path.exists():
        return yaml_path

    safe_name = re.sub(r"[^a-z0-9_]", "_", omc_name.lower())
    if not re.match(r"^[a-z]", safe_name):
        safe_name = "omc_" + safe_name

    data = {
        "name": f"omc_{safe_name}",
        "display_name": f"[OMC] {frontmatter.get('name', omc_name)}",
        "personality": (frontmatter.get("description") or f"OMC subagent: {omc_name}")[:600].strip() or f"OMC subagent: {omc_name}",
        "behavioral_guidelines": [
            f"遵循 OMC {omc_name} 的角色定义执行任务",
            "任务完成后向父 agent 报告结果",
        ],
        "tool_chain": ["omc_dispatch"],
        "memory_write_trigger": "never",
        "memory_write_scope": "none",
        "memory_write_template": None,
        "id": str(uuid.uuid4()),
        "version": "1.0",
        "hidden": True,
        "parent_agent": parent,
        "kind": "omc_subagent",
        "triggerable_via": "parent_only",
        "source_md": str(md_path),
        "omc_subagent_type": f"oh-my-claudecode:{omc_name}",
        "tags": ["omc", omc_name],
    }

    try:
        yaml_path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        logger.info(f"[omc_bridge] created identity: {yaml_path.name}")
    except Exception as e:
        logger.warning(f"[omc_bridge] write identity yaml failed: {e}")

    return yaml_path


class OmcSubagentShell:
    """
    OMC subagent 的轻量 BaseAgent 壳子。
    不实现实际 run_task——触发必须走父 agent authorize_subagent()。
    """

    def __init__(self, omc_name: str, parent_name: str, display_name: str, description: str):
        self.name = f"omc_{re.sub(r'[^a-z0-9_]', '_', omc_name.lower())}"
        self.display_name = display_name
        self.description = description
        self.hidden = True
        self.parent_agent = parent_name
        self.tags = ["omc", omc_name]
        self._omc_name = omc_name
        self._job_token = None
        self._agent_registry_id = str(uuid.uuid4())[:8]

    # BaseAgent 接口最小实现
    def describe(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "kind": "omc_subagent",
            "parent_agent": self.parent_agent,
            "omc_subagent_type": f"oh-my-claudecode:{self._omc_name}",
            "triggerable_via": "parent_only",
        }

    def health_check(self) -> dict:
        return {"healthy": True, "detail": "omc_shell"}

    def on_register(self, registry) -> None:
        pass

    def run_task(self, task) -> dict:
        raise RuntimeError(
            f"OmcSubagentShell '{self.name}' 不能直接执行——请通过父 agent "
            f"'{self.parent_agent}' 的 authorize_subagent() 触发"
        )


def register_all(registry: "AgentRegistry") -> int:
    """
    扫描所有 OMC .md 文件，注册壳子 agents。
    返回成功注册的数量。
    """
    registered = 0
    seen: set = set()

    for agent_dir in _omc_agent_dirs():
        for md_path in sorted(agent_dir.glob("*.md")):
            omc_name = md_path.stem
            if omc_name in seen:
                continue
            seen.add(omc_name)

            fm = _parse_md_frontmatter(md_path) or {}
            description = fm.get("description", f"OMC subagent: {omc_name}")
            parent = _map_parent(omc_name, description)

            # 检查父 agent 是否已注册；若未注册则降到 claude
            if registry.get(parent) is None:
                logger.warning(f"[omc_bridge] parent '{parent}' not found for '{omc_name}', fallback to 'claude'")
                parent = _DEFAULT_PARENT

            _ensure_identity_yaml(omc_name, fm, parent, md_path)

            safe = f"omc_{re.sub(r'[^a-z0-9_]', '_', omc_name.lower())}"
            if registry.get(safe) is not None:
                logger.debug(f"[omc_bridge] {safe} already registered, skip")
                continue

            shell = OmcSubagentShell(
                omc_name=omc_name,
                parent_name=parent,
                display_name=f"[OMC] {fm.get('name', omc_name)}",
                description=description,
            )
            try:
                registry.register(shell)
                registered += 1
                logger.info(f"[omc_bridge] registered {safe} → parent={parent}")
            except Exception as e:
                logger.warning(f"[omc_bridge] register {safe} failed: {e}")

    logger.info(f"[omc_bridge] total registered: {registered}")
    return registered
