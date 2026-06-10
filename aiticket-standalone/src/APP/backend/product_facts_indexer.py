#!/usr/bin/env python3
"""
产品事实向量索引器

- 将 product_facts.md 中的每条事实按粒度索引到 Chroma collection "product_facts"
- 基于文件 mtime 的增量重索引：mtime 未变则跳过
- 对外暴露 query(text, top_k) → list[str]，供 board_service_chroma 调用
- 可直接运行触发重索引：python product_facts_indexer.py
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings

BACKEND_DIR = Path(__file__).resolve().parent
FACTS_PATH = BACKEND_DIR / "data" / "product_facts.md"
CHROMA_DIR = BACKEND_DIR / "chroma_db"
MTIME_CACHE = BACKEND_DIR / "data" / ".product_facts_mtime"
COLLECTION_NAME = "product_facts"


def _get_embedding_function():
    try:
        from chromadb.utils import embedding_functions
        return embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="paraphrase-multilingual-MiniLM-L12-v2"
        )
    except Exception as e:
        print(f"[FactsIndexer] 嵌入模型加载失败，使用 Chroma 默认: {e}")
        return None


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _parse_facts(md_text: str) -> list[dict]:
    """解析 product_facts.md，返回 [{id, text, topic}] 列表"""
    facts = []
    current_topic = "general"
    for line in md_text.splitlines():
        if line.startswith("## "):
            current_topic = line[3:].strip()
        elif line.startswith("- ") and len(line) > 4:
            text = line[2:].strip()
            facts.append({
                "id": _sha1(text),
                "text": text,
                "topic": current_topic,
            })
    return facts


def _get_collection(client: chromadb.PersistentClient, ef):
    try:
        return client.get_collection(COLLECTION_NAME, embedding_function=ef)
    except Exception:
        return client.create_collection(
            COLLECTION_NAME,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )


def _current_mtime() -> float:
    if not FACTS_PATH.exists():
        return 0.0
    return FACTS_PATH.stat().st_mtime


def _cached_mtime() -> float:
    if not MTIME_CACHE.exists():
        return 0.0
    try:
        return float(MTIME_CACHE.read_text().strip())
    except Exception:
        return 0.0


def _save_mtime(mtime: float) -> None:
    MTIME_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MTIME_CACHE.write_text(str(mtime))


def reindex(force: bool = False) -> int:
    """重新索引 product_facts.md。返回索引条数，0 表示跳过（mtime 未变）。"""
    if not FACTS_PATH.exists():
        print("[FactsIndexer] product_facts.md 不存在，跳过")
        return 0

    mtime = _current_mtime()
    if not force and mtime == _cached_mtime():
        return 0

    print(f"[FactsIndexer] 检测到 product_facts.md 变更，重新索引...")
    md_text = FACTS_PATH.read_text(encoding="utf-8")
    facts = _parse_facts(md_text)
    if not facts:
        print("[FactsIndexer] 未解析到任何事实条目")
        return 0

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    from services.chroma_factory import get_chroma_client
    client = get_chroma_client(persist_path=str(CHROMA_DIR))
    ef = _get_embedding_function()
    collection = _get_collection(client, ef)

    # 清空后重建（facts 数量级小，全量重索引更简单）
    existing_ids = collection.get(include=[])["ids"]
    if existing_ids:
        collection.delete(ids=existing_ids)

    collection.upsert(
        ids=[f["id"] for f in facts],
        documents=[f["text"] for f in facts],
        metadatas=[{"topic": f["topic"]} for f in facts],
    )

    _save_mtime(mtime)
    print(f"[FactsIndexer] 索引完成 — {len(facts)} 条事实")
    return len(facts)


# ---------------------------------------------------------------------------
# 查询接口（供 board_service_chroma 调用）
# ---------------------------------------------------------------------------

_client: Optional[chromadb.PersistentClient] = None
_collection = None


def _ensure_collection():
    global _client, _collection
    if _collection is not None:
        return _collection
    if not CHROMA_DIR.exists():
        return None
    try:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        from services.chroma_factory import get_chroma_client
        _client = get_chroma_client(persist_path=str(CHROMA_DIR))
        ef = _get_embedding_function()
        _collection = _get_collection(_client, ef)
        return _collection
    except Exception as e:
        print(f"[FactsIndexer] Chroma 初始化失败: {e}")
        return None


def query_facts(text: str, top_k: int = 10) -> list[str]:
    """
    检索与 text 最相关的产品事实。
    返回 fact 文本列表（已去重，按相关度排序）。
    若 Chroma 不可用则返回空列表，调用方应 fallback 到全文截断。

    注意：本函数【不做 per-request reindex】。此前每次回复请求都无条件调 reindex()
    （靠 mtime 门跳过），但一旦 product_facts.md 改动，下一个回复请求就会在 reply 的
    线程池槽内同步全量重建 + 懒加载第二个 embedding 模型(+0.4-0.5GB)，造成回复尖峰。
    索引新鲜度改由定时任务 daily-kb-reindex-watchdog(product_facts_indexer.py --force)
    维护；本函数仅在集合为空(冷启)时兜底重建一次。
    """
    col = _ensure_collection()
    if col is None:
        return []

    try:
        count = col.count()
        if count == 0:
            # 冷启兜底：集合为空时重建一次（非每请求）
            try:
                reindex()
            except Exception as e:
                print(f"[FactsIndexer] 冷启重索引失败: {e}")
            col = _ensure_collection()
            if col is None:
                return []
            count = col.count()
            if count == 0:
                return []
        n = min(top_k, count)
        results = col.query(query_texts=[text], n_results=n, include=["documents"])
        docs = results.get("documents", [[]])[0]
        return [d for d in docs if d]
    except Exception as e:
        print(f"[FactsIndexer] 查询失败: {e}")
        return []


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="产品事实索引器")
    parser.add_argument("--force", action="store_true", help="强制重索引（忽略 mtime 缓存）")
    args = parser.parse_args()
    n = reindex(force=args.force)
    if n == 0:
        print("[FactsIndexer] 无变更，未重索引（使用 --force 强制）")
