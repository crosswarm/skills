#!/usr/bin/env python3
"""
KB 自动成长系统 — 知识萃取 + KB比对 + 自动入库 + 交叉验证
每日 03:00 自动运行，从工单回复中萃取知识并入库 KB。

用法：
  python kb_auto_enricher.py                    # 默认萃取最近1天
  python kb_auto_enricher.py --days 7           # 萃取最近7天
  python kb_auto_enricher.py --max-entries 10   # 最多入库10条
  python kb_auto_enricher.py --dry-run          # 只萃取不入库
"""
import sys, os, json, re, time, argparse, hashlib
from pathlib import Path
from datetime import datetime, timedelta

# no_proxy 防止 requests 走代理超时
for _h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
    _existing = os.environ.get("no_proxy", "")
    if _h not in _existing:
        os.environ["no_proxy"] = f"{_existing},{_h}".strip(",")
os.environ["NO_PROXY"] = os.environ.get("no_proxy", "")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent.parent
sys.path.insert(0, str(BACKEND_DIR))

FEEDBACK_LOG = BACKEND_DIR / "data" / "reply_trainer" / "feedback_log.jsonl"
TRAINING_DIR = PROJECT_ROOT / "conclusion" / "training"
ENRICHMENT_LOG = BACKEND_DIR / "data" / "kb_enrichment_log.jsonl"
PENDING_REVIEW = BACKEND_DIR / "data" / "kb_pending_review.json"
DB_PATH = PROJECT_ROOT / "data" / "sqlite" / "kb_chunks.db"
API_BASE = "http://127.0.0.1:3000"

# ── 可信度权重 ──
SOURCE_TRUST = {
    "human_verified_2plus": 1.0,   # 2+人工回复验证 — 最高
    "kb_local":             0.9,
    "feedback_single":      0.8,
    "training_high_score":  0.65,
    "auto_enriched":        0.5,
}


def _load_llm_config():
    cfg_path = BACKEND_DIR / "llm_config.json"
    with open(cfg_path, encoding="utf-8") as f:
        raw = json.load(f)
    p = raw.get("minimax", {})
    return {"api_key": p.get("api_key",""), "model": p.get("model_name","MiniMax-M2.7"), "base_url": p.get("base_url","")}


def _llm_call(system: str, user: str, cfg: dict) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=90)
    resp = client.chat.completions.create(
        model=cfg["model"],
        messages=[{"role":"system","content":system}, {"role":"user","content":user}],
        max_tokens=2000, temperature=0.2,
    )
    text = (resp.choices[0].message.content or "").strip()
    return re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.DOTALL).strip()


# ── Phase 2: 知识萃取 ──

def extract_knowledge_from_replies(days: int = 1) -> list:
    """从 feedback_log.jsonl 萃取结构化知识"""
    if not FEEDBACK_LOG.exists():
        print("[萃取] feedback_log.jsonl 不存在")
        return []
    cutoff = datetime.now() - timedelta(days=days)
    entries = []
    with open(FEEDBACK_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e.get("ts", "2000-01-01"))
                if ts >= cutoff and e.get("user_final"):
                    entries.append(e)
            except Exception:
                continue
    if not entries:
        print(f"[萃取] 最近 {days} 天无新的用户回复")
        return []
    print(f"[萃取] 找到 {len(entries)} 条用户回复，开始 LLM 萃取...")
    cfg = _load_llm_config()
    all_items = []
    for e in entries[:30]:  # 每次最多处理 30 条
        try:
            items = _extract_one(e, cfg, source_type="feedback_single")
            all_items.extend(items)
        except Exception as err:
            print(f"[萃取] {e.get('issue_key','?')} 失败: {err}")
    print(f"[萃取] 从 {len(entries)} 条回复中萃取出 {len(all_items)} 条知识")
    return all_items


def extract_from_training_evaluations() -> list:
    """从最近训练会话的高分回复中萃取知识"""
    sessions_dir = TRAINING_DIR / "sessions"
    if not sessions_dir.exists():
        return []
    # 找最新的 session
    sessions = sorted([d for d in sessions_dir.iterdir() if d.is_dir()], reverse=True)
    if not sessions:
        return []
    latest = sessions[0]
    evals_file = latest / "evaluations.json"
    replies_file = latest / "replies.json"
    questions_file = latest / "questions.json"
    if not all(f.exists() for f in [evals_file, replies_file, questions_file]):
        return []
    evals = json.loads(evals_file.read_text(encoding="utf-8"))
    replies = json.loads(replies_file.read_text(encoding="utf-8"))
    questions = json.loads(questions_file.read_text(encoding="utf-8"))
    cfg = _load_llm_config()
    all_items = []
    for i, ev in enumerate(evals):
        if ev.get("total_score", 0) >= 8 and i < len(replies) and i < len(questions):
            reply_text = re.sub(r'<think>[\s\S]*?</think>', '', replies[i].get("reply",""), flags=re.DOTALL).strip()
            entry = {
                "issue_key": f"TRAIN-{latest.name}-Q{i}",
                "ticket_summary": questions[i].get("summary",""),
                "user_final": reply_text[:1500],
            }
            try:
                items = _extract_one(entry, cfg, source_type="training_high_score")
                all_items.extend(items)
            except Exception:
                continue
    print(f"[萃取] 从训练高分回复中萃取出 {len(all_items)} 条知识")
    return all_items


def _extract_one(entry: dict, cfg: dict, source_type: str) -> list:
    """对单条回复调 LLM 萃取知识条目"""
    system = "你是知识库萃取专家。从工单回复中提取可入库的结构化知识。只提取事实性知识（设计规则、操作步骤、限制说明、配置方法），不提取问候语或模糊建议。"
    user = f"""分析以下工单回复，提取可入库的知识条目。

工单: {entry.get('issue_key','')}
标题: {entry.get('ticket_summary','')[:200]}
回复内容:
{entry.get('user_final','')[:1200]}

输出严格 JSON（无 markdown 包装）：
[
  {{
    "topic": "话题关键词（如：撤回、字段权限、加签）",
    "fact_type": "design_rule|operation_step|limitation|config_method",
    "fact": "具体知识内容（一句话，可直接入库）",
    "confidence": 0.5到1.0之间的数值
  }}
]
如果没有可提取的事实性知识，返回空数组 []。"""
    raw = _llm_call(system, user, cfg)
    try:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start >= 0 and end > start:
            items = json.loads(raw[start:end])
        else:
            items = []
    except Exception:
        items = []
    # 标注来源
    for item in items:
        item["source_ticket"] = entry.get("issue_key", "")
        item["source_type"] = source_type
        item["extracted_at"] = datetime.now().isoformat()
    return items


# ── Phase 3: KB 比对 + 入库 ──

def compare_with_kb(knowledge_items: list) -> dict:
    """对每条知识搜索 KB，分类为 skip/enrich/create"""
    from kb_runtime_service import KnowledgeRuntimeService
    kb = KnowledgeRuntimeService()
    decisions = {"skip": [], "enrich": [], "create": []}
    cfg = _load_llm_config()
    for item in knowledge_items:
        query = f"{item.get('topic','')} {item.get('fact','')[:100]}"
        try:
            results = kb.search_bundle(query, top_k=3)
            hits = results.get("items", [])[:3]
        except Exception:
            hits = []
        if not hits:
            decisions["create"].append(item)
            continue
        # 用 LLM 判断关系
        hit_summaries = "\n".join([f"- {h.get('name','')}: {h.get('summary','')[:100]}" for h in hits])
        system = "判断新知识与已有 KB 条目的关系。"
        user = f"""新知识: [{item['topic']}] {item['fact']}
已有 KB 条目:
{hit_summaries}

判断（只返回一个词）：
- skip（已有完全覆盖这个知识点）
- enrich（已有相关话题但缺少这个具体细节）
- create（完全新的话题，已有条目无关）"""
        try:
            decision = _llm_call(system, user, cfg).lower().strip()
            if "skip" in decision:
                decisions["skip"].append(item)
            elif "enrich" in decision:
                item["target_doc"] = hits[0].get("name", "")
                item["target_content_id"] = hits[0].get("content_id", "")
                decisions["enrich"].append(item)
            else:
                decisions["create"].append(item)
        except Exception:
            decisions["create"].append(item)
    return decisions


def enrich_kb(item: dict):
    """更新已有 KB 条目：追加新知识"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    content_id = item.get("target_content_id", "")
    if not content_id:
        conn.close()
        return
    # 在 chunk_text 末尾追加
    row = conn.execute("SELECT chunk_text FROM chunks WHERE content_id=? LIMIT 1", (content_id,)).fetchone()
    if row:
        new_text = row[0] + f"\n\n[自动萃取 {item['source_ticket']} {item['extracted_at'][:10]}] {item['fact']}"
        conn.execute("UPDATE chunks SET chunk_text=? WHERE content_id=?", (new_text, content_id))
        # 更新 validation_sources
        doc = conn.execute("SELECT validation_sources FROM documents WHERE content_id=?", (content_id,)).fetchone()
        sources = json.loads(doc[0]) if doc and doc[0] else []
        sources.append({"ticket": item["source_ticket"], "at": item["extracted_at"][:10], "type": item["source_type"]})
        conn.execute("UPDATE documents SET validation_sources=?, last_validated_at=? WHERE content_id=?",
                      (json.dumps(sources, ensure_ascii=False), datetime.now().isoformat(), content_id))
    conn.commit()
    conn.close()
    print(f"  [enrich] {item['target_doc'][:30]} ← {item['fact'][:50]}")


def create_kb_entry(item: dict):
    """创建新 KB 条目"""
    import sqlite3
    topic = item.get("topic", "未分类")
    fact = item.get("fact", "")
    content_id = f"kb_auto_enriched:{hashlib.md5(f'{topic}:{fact}'.encode()).hexdigest()[:12]}"
    # 生成标准结构内容
    content = f"""## 定义与背景
{fact}

## 来源
- 工单: {item.get('source_ticket','')}
- 类型: {item.get('fact_type','')}
- 萃取时间: {item.get('extracted_at','')[:10]}
"""
    conn = sqlite3.connect(DB_PATH)
    # 检查是否已存在
    existing = conn.execute("SELECT 1 FROM documents WHERE content_id=?", (content_id,)).fetchone()
    if existing:
        conn.close()
        return
    # 插入 document
    conn.execute("""INSERT INTO documents (content_id, source_kind, name, summary, source_rel_path, citation_label,
                    l1_module, l2_module, doc_type, credibility, validation_sources, last_validated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (content_id, "kb_auto_enriched", f"自动萃取：{topic}",
                  f"来自工单 {item.get('source_ticket','')} 的 {item.get('fact_type','')} 知识",
                  f"auto_enriched/{topic}", "auto",
                  topic, item.get("fact_type",""), "kb_auto_enriched",
                  item.get("confidence", 0.5),
                  json.dumps([{"ticket": item["source_ticket"], "type": item["source_type"]}], ensure_ascii=False),
                  datetime.now().isoformat()))
    # 插入 chunk
    conn.execute("""INSERT INTO chunks (chunk_id, content_id, chunk_text, chunk_preview,
                    source_kind, name, summary, doc_type, citation_label)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (f"{content_id}::chunk-001", content_id, content, fact[:200],
                  "kb_auto_enriched", f"自动萃取：{topic}",
                  f"{item.get('fact_type','')}: {fact[:100]}", "kb_auto_enriched", "auto"))
    conn.commit()
    conn.close()
    print(f"  [create] 新条目: {topic} — {fact[:50]}")


# ── Phase 4: 交叉验证 ──

def cross_validate(new_items: list):
    """对新入库条目进行交叉验证"""
    import sqlite3
    from kb_runtime_service import KnowledgeRuntimeService
    kb = KnowledgeRuntimeService()
    conn = sqlite3.connect(DB_PATH)
    for item in new_items:
        query = f"{item.get('topic','')} {item.get('fact','')[:80]}"
        try:
            results = kb.search_bundle(query, top_k=5)
            hits = [h for h in results.get("items",[])[:5]
                    if h.get("content_id") != item.get("target_content_id","")]
        except Exception:
            continue
        if not hits:
            continue
        # 检查验证来源数量 → 更新 credibility
        topic_str = item.get("topic", "")
        fact_str = item.get("fact", "")
        content_id = item.get("target_content_id") or f"kb_auto_enriched:{hashlib.md5(f'{topic_str}:{fact_str}'.encode()).hexdigest()[:12]}"
        doc = conn.execute("SELECT validation_sources, credibility FROM documents WHERE content_id=?", (content_id,)).fetchone()
        if doc:
            sources = json.loads(doc[0]) if doc[0] else []
            base_trust = SOURCE_TRUST.get(item.get("source_type","auto_enriched"), 0.5)
            # 多源验证加权
            if len(sources) >= 2:
                base_trust = SOURCE_TRUST["human_verified_2plus"]
            # 时间衰减
            time_decay = 1.0
            extracted = item.get("extracted_at","")
            if extracted:
                try:
                    age_days = (datetime.now() - datetime.fromisoformat(extracted)).days
                    if age_days > 365: time_decay = 0.5
                    elif age_days > 180: time_decay = 0.7
                    elif age_days > 90: time_decay = 0.85
                except Exception:
                    pass
            new_cred = min(1.0, base_trust * time_decay * min(1.0 + len(sources)*0.05, 1.5))
            conn.execute("UPDATE documents SET credibility=? WHERE content_id=?", (round(new_cred,3), content_id))
    conn.commit()
    conn.close()
    print(f"[交叉验证] 完成 {len(new_items)} 条")


# ── 主入口 ──

def main():
    parser = argparse.ArgumentParser(description="KB 自动成长系统")
    parser.add_argument("--days", type=int, default=1, help="萃取最近N天")
    parser.add_argument("--max-entries", type=int, default=20, help="每天最多入库条数")
    parser.add_argument("--dry-run", action="store_true", help="只萃取不入库")
    parser.add_argument("--skip-training", action="store_true", help="跳过训练数据萃取")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  KB 自动成长系统 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  萃取最近 {args.days} 天，最多入库 {args.max_entries} 条")
    print(f"{'='*60}\n")

    # Phase 2: 萃取
    items = extract_knowledge_from_replies(days=args.days)
    if not args.skip_training:
        items.extend(extract_from_training_evaluations())
    print(f"\n[总计] 萃取 {len(items)} 条知识\n")
    if not items:
        print("无新知识，退出")
        return {"markdown": "NO_CHANGE"}

    if args.dry_run:
        print("[DRY-RUN] 萃取结果：")
        for i, item in enumerate(items[:10], 1):
            print(f"  {i}. [{item.get('topic','')}] ({item.get('fact_type','')}) {item.get('fact','')[:80]}")
        if len(items) > 10:
            print(f"  ...还有 {len(items)-10} 条")
        return {"markdown": "DRY_RUN", "count": len(items)}

    # Phase 3: 比对 + 入库
    print("[Phase 3] KB 比对...")
    decisions = compare_with_kb(items)
    print(f"  skip={len(decisions['skip'])}, enrich={len(decisions['enrich'])}, create={len(decisions['create'])}")

    # 限制入库数量
    to_process = (decisions["enrich"] + decisions["create"])[:args.max_entries]
    affected_topics = set()

    for item in to_process:
        if item.get("confidence", 0) < 0.7:
            # 低置信度 → pending_review
            _add_pending_review(item)
            continue
        if item in decisions["enrich"]:
            enrich_kb(item)
        else:
            create_kb_entry(item)
        affected_topics.add(item.get("topic", ""))
        _log_enrichment(item)

    # Phase 4: 交叉验证
    print(f"\n[Phase 4] 交叉验证 {len(to_process)} 条...")
    cross_validate(to_process)

    # 重编译受影响话题
    if affected_topics:
        print(f"\n[重编译] 受影响话题: {affected_topics}")
        import requests
        for topic in affected_topics:
            try:
                requests.post(f"{API_BASE}/api/kb/compile", json={"topic": topic}, timeout=120)
            except Exception:
                pass

    # 生成摘要
    enriched = len([i for i in to_process if i.get("confidence",0) >= 0.7 and i in decisions["enrich"]])
    created = len([i for i in to_process if i.get("confidence",0) >= 0.7 and i in decisions["create"]])
    pending = len([i for i in to_process if i.get("confidence",0) < 0.7])

    md = (
        f"📚 KB 自动成长日报 — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"萃取: {len(items)} 条知识\n"
        f"入库: {enriched + created} 条 ({enriched} enrich + {created} create)\n"
        f"跳过: {len(decisions['skip'])} 条（已有覆盖）\n"
        f"待审核: {pending} 条（低置信度）\n"
        f"受影响话题: {', '.join(affected_topics) or '无'}\n"
        f"📖 查看: http://100.80.80.100:3000/kb.html"
    )
    print(f"\n{md}")
    return {"markdown": md, "enriched": enriched, "created": created}


def _log_enrichment(item: dict):
    with open(ENRICHMENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _add_pending_review(item: dict):
    pending = []
    if PENDING_REVIEW.exists():
        try:
            pending = json.loads(PENDING_REVIEW.read_text(encoding="utf-8"))
        except Exception:
            pending = []
    pending.append(item)
    PENDING_REVIEW.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [pending] {item.get('topic','')}: {item.get('fact','')[:50]} (conf={item.get('confidence',0)})")


if __name__ == "__main__":
    main()
