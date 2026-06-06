#!/usr/bin/env python3
"""
Agent A 批量数据加工器 — 闲时增量处理所有历史回复数据
每次运行处理一批（默认60条），追踪进度，直到所有数据分析完毕。
产物合并到 pattern_library.json，供 Agent B 学习使用。

用法：
  python agent_a_indexer.py              # 处理下一批
  python agent_a_indexer.py --batch 100  # 指定批量大小
  python agent_a_indexer.py --batches 5  # 本次运行处理5批
  python agent_a_indexer.py --reset      # 重置进度，从头开始
  python agent_a_indexer.py --status     # 查看当前进度
"""
import sys
import os
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent.parent

sys.path.insert(0, str(BACKEND_DIR))

TRAINING_DIR = PROJECT_ROOT / "conclusion" / "_local" / "training"
PATTERN_LIBRARY_FILE = TRAINING_DIR / "pattern_library.json"
INDEX_STATE_FILE = TRAINING_DIR / "agent_a_index_state.json"
REPLY_DB = BACKEND_DIR / "data" / "reply_trainer" / "reply_examples.db"

TRAINING_DIR.mkdir(parents=True, exist_ok=True)

QCL_SSH_HOST = os.environ.get("QCL_SSH_HOST", "qcl")
QCL_REMOTE_DIR = os.environ.get("QCL_REMOTE_DIR", "/opt/ai-ticket")


# ── 进度状态 ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if INDEX_STATE_FILE.exists():
        return json.loads(INDEX_STATE_FILE.read_text(encoding="utf-8"))
    return {
        "last_rowid": 0,
        "processed": 0,
        "total": 0,
        "completed": False,
        "started_at": None,
        "last_run_at": None,
        "batches_run": 0,
    }


def save_state(state: dict):
    INDEX_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── LLM 调用（复用训练器配置）─────────────────────────────────────────────────

def _load_llm_config() -> dict:
    cfg_path = BACKEND_DIR / "llm_config.json"
    with open(cfg_path, encoding="utf-8") as f:
        raw = json.load(f)
    provider = raw.get("last_provider", "minimax")
    pc = raw.get(provider, {})
    return {
        "provider": provider,
        "api_key": pc.get("api_key", ""),
        "model_name": pc.get("model_name", ""),
        "base_url": pc.get("base_url", ""),
    }


def llm_call(system: str, user: str, cfg: dict) -> str:
    from openai import OpenAI
    client = OpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"] or "https://api.openai.com/v1",
        timeout=90,
    )
    resp = client.chat.completions.create(
        model=cfg["model_name"],
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2000,
        temperature=0.3,
    )
    return resp.choices[0].message.content or ""


# ── 批量读取回复样本 ───────────────────────────────────────────────────────────

def fetch_batch(last_rowid: int, batch_size: int) -> list[dict]:
    """从 SQLite 按 ROWID 顺序读取下一批回复样本。"""
    conn = sqlite3.connect(REPLY_DB)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT c.rowid, c.chunk_text, c.chunk_preview, d.summary, d.l1_module, d.l2_module
        FROM chunks c
        JOIN documents d ON c.content_id = d.content_id
        WHERE c.source_kind = 'reply_example' AND c.rowid > ?
        ORDER BY c.rowid
        LIMIT ?
        """,
        (last_rowid, batch_size),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_total_count() -> int:
    conn = sqlite3.connect(REPLY_DB)
    total = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE source_kind='reply_example'"
    ).fetchone()[0]
    conn.close()
    return total


# ── 模式提取 ──────────────────────────────────────────────────────────────────

def extract_patterns_from_batch(rows: list[dict], cfg: dict) -> dict:
    """
    发送一批回复样本给 LLM，提取思维模式和处理套路。
    返回可合并的 JSON 结构。
    """
    # 按解决方式分组，生成摘要
    groups: dict[str, list] = {}
    for r in rows:
        method = r.get("l1_module") or "其他"
        if method not in groups:
            groups[method] = []
        # 用 chunk_preview（简洁）或前300字
        snippet = (r.get("chunk_preview") or r.get("chunk_text", ""))[:300]
        summary = r.get("summary", "")[:100]
        groups[method].append(f"问题：{summary}\n回复：{snippet}")

    examples_text = ""
    for method, items in groups.items():
        examples_text += f"\n\n=== [{method}] ===\n"
        examples_text += "\n---\n".join(items[:5])  # 每类最多5条展示

    system = "你是工单支持知识库分析专家，负责从大量真实回复中提炼可复用的思维模式和处理套路。"
    user = f"""分析以下 {len(rows)} 条真实工单回复样本（已按解决方式分类），提炼：
1. 各解决方式的核心处理套路（具体步骤/方法论）
2. 新发现的思维模式（如果与已知模式不同）
3. 高频关键短语

回复样本：
{examples_text[:3000]}

输出严格 JSON（无 markdown）：
{{
  "reply_method_patterns": {{
    "指导解决": ["套路描述1", "套路描述2"],
    "方案解决": ["套路描述1"]
  }},
  "new_thinking_modes": [
    {{
      "name": "模式名",
      "description": "一句话描述",
      "trigger_scenarios": ["场景1"],
      "common_routines": ["套路1"],
      "key_phrases": ["短语1"]
    }}
  ],
  "topic_insights": {{
    "话题关键词": "核心处理方法"
  }}
}}"""

    raw = llm_call(system, user, cfg)
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
    except Exception:
        pass
    return {}


# ── 模式合并 ──────────────────────────────────────────────────────────────────

def merge_into_library(extracted: dict, library: dict) -> dict:
    """将本批提取的模式增量合并到现有 pattern_library。"""
    # 1. reply_method_patterns（新增字段）
    existing_rmp = library.setdefault("reply_method_patterns", {})
    for method, routines in extracted.get("reply_method_patterns", {}).items():
        existing = set(existing_rmp.get(method, []))
        for r in routines:
            if r and r not in existing:
                existing.add(r)
        existing_rmp[method] = list(existing)

    # 2. 新思维模式（按 name 去重）
    existing_modes = library.setdefault("thinking_modes", [])
    existing_names = {m.get("name") for m in existing_modes}
    for mode in extracted.get("new_thinking_modes", []):
        if mode.get("name") and mode["name"] not in existing_names:
            existing_modes.append(mode)
            existing_names.add(mode["name"])

    # 3. topic_insights → topic_handling
    existing_th = library.setdefault("topic_handling", {})
    for topic, insight in extracted.get("topic_insights", {}).items():
        if topic not in existing_th:
            existing_th[topic] = []
        if isinstance(existing_th[topic], list):
            if insight not in existing_th[topic]:
                existing_th[topic].append(insight)

    return library


# ── 推送到 QCL ────────────────────────────────────────────────────────────────

def push_to_qcl():
    import subprocess
    src = str(PATTERN_LIBRARY_FILE)
    dst = f"{QCL_SSH_HOST}:{QCL_REMOTE_DIR}/conclusion/_local/training/pattern_library.json"
    try:
        r = subprocess.run(["rsync", "-az", src, dst], timeout=30, capture_output=True)
        if r.returncode == 0:
            print("[Sync→QCL] ✓ pattern_library.json")
        else:
            print(f"[Sync→QCL] ✗ rsync 失败: {r.stderr[:100]}")
    except Exception as e:
        print(f"[Sync→QCL] ✗ {e}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def show_status(state: dict):
    total = state.get("total") or get_total_count()
    processed = state.get("processed", 0)
    pct = processed / total * 100 if total else 0
    print(f"进度: {processed}/{total} ({pct:.1f}%)")
    print(f"上次运行: {state.get('last_run_at', '—')}")
    print(f"批次数: {state.get('batches_run', 0)}")
    print(f"状态: {'✓ 已完成' if state.get('completed') else '进行中'}")


def main():
    parser = argparse.ArgumentParser(description="Agent A 批量数据加工器")
    parser.add_argument("--batch", type=int, default=60, help="每批处理条数（默认60）")
    parser.add_argument("--batches", type=int, default=1, help="本次运行处理批数（默认1）")
    parser.add_argument("--reset", action="store_true", help="重置进度")
    parser.add_argument("--status", action="store_true", help="查看进度")
    parser.add_argument("--no-push", action="store_true", help="不推送到QCL")
    args = parser.parse_args()

    state = load_state()

    if args.reset:
        state = {
            "last_rowid": 0, "processed": 0, "total": 0,
            "completed": False, "started_at": None,
            "last_run_at": None, "batches_run": 0,
        }
        save_state(state)
        print("[A-Indexer] 进度已重置")
        return

    if args.status:
        show_status(state)
        return

    if state.get("completed"):
        print(f"[A-Indexer] 所有数据已处理完毕（共 {state.get('processed')} 条）")
        return

    total = get_total_count()
    state["total"] = total
    if not state.get("started_at"):
        state["started_at"] = datetime.now().isoformat()

    cfg = _load_llm_config()
    print(f"[A-Indexer] LLM: {cfg['provider']} / {cfg['model_name']}")

    # 加载现有 pattern_library
    library = {}
    if PATTERN_LIBRARY_FILE.exists():
        library = json.loads(PATTERN_LIBRARY_FILE.read_text(encoding="utf-8"))

    batches_done = 0
    for _ in range(args.batches):
        rows = fetch_batch(state["last_rowid"], args.batch)
        if not rows:
            state["completed"] = True
            print("[A-Indexer] 所有回复数据处理完毕！")
            break

        print(f"[A-Indexer] 处理批次 #{state['batches_run'] + 1}: rowid {rows[0]['rowid']}~{rows[-1]['rowid']} ({len(rows)}条)")

        extracted = extract_patterns_from_batch(rows, cfg)
        if extracted:
            library = merge_into_library(extracted, library)
            mode_count = len(library.get("thinking_modes", []))
            method_keys = list(library.get("reply_method_patterns", {}).keys())
            print(f"  → 思维模式: {mode_count} 种, 解决方式: {method_keys}")
        else:
            print("  → LLM 返回格式异常，跳过本批")

        state["last_rowid"] = rows[-1]["rowid"]
        state["processed"] = state.get("processed", 0) + len(rows)
        state["batches_run"] = state.get("batches_run", 0) + 1
        state["last_run_at"] = datetime.now().isoformat()
        batches_done += 1

        pct = state["processed"] / total * 100 if total else 0
        print(f"  → 累计进度: {state['processed']}/{total} ({pct:.1f}%)")

    # 更新 _index_meta 嵌入 pattern_library
    library["_index_meta"] = {
        "reply_processed": state["processed"],
        "reply_total": total,
        "completed": state.get("completed", False),
        "last_updated": datetime.now().isoformat(),
    }

    # 保存
    PATTERN_LIBRARY_FILE.write_text(
        json.dumps(library, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    save_state(state)
    print(f"[A-Indexer] 本次运行完成，处理 {batches_done} 批")

    if not args.no_push:
        push_to_qcl()


if __name__ == "__main__":
    main()
