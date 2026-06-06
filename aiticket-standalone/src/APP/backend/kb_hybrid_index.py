from __future__ import annotations

import logging
import math
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from services.vector_backend import SqliteVecBackend

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"[一-鿿]{2,}|[A-Za-z0-9_-]{2,}")

_SOURCE_PRIOR = {
    'human_verified':   1.0,
    'kb_compiled':      0.98,
    'apcom_docs':       0.95,
    'kb_local':         0.90,
    'user_contributed': 0.88,
    'kb_auto_enriched': 0.75,
    'reply_example':    0.70,
    'ticket_case':      0.60,
    'legacy_files':     0.40,
}

# sync/rebuild 时必须保留的 source_kind
_PRESERVED_SOURCE_KINDS: tuple[str, ...] = (
    'kb_compiled',
    'user_contributed',
    'kb_auto_enriched',
    'human_verified',
    'reply_example',
)

# Vec dimension for KB chunks — 96-dim hash (offline, deterministic)
_KB_VEC_DIM = 96


class LocalHashEmbeddingFunction:
    """Deterministic local embedding, kept for backward-compat and direct use."""

    def __init__(self, dimensions: int = 96) -> None:
        self.dimensions = dimensions

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in input]

    def name(self) -> str:
        return "local-hash-embedding"

    def embed_documents(self, input: list[str]) -> list[list[float]]:
        return self.__call__(input)

    def embed_query(self, input: list[str] | str) -> list[list[float]] | list[float]:
        if isinstance(input, str):
            return self._embed(input)
        return self.__call__(input)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        counts = Counter(token.lower() for token in TOKEN_RE.findall(text or ""))
        if not counts:
            return vector
        for token, weight in counts.items():
            vector[hash(token) % self.dimensions] += float(weight)
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector


class KnowledgeHybridIndex:
    def __init__(self, sqlite_path: Path, chroma_path: Path, collection_name: str = "prd_kb") -> None:
        self.sqlite_path = Path(sqlite_path)
        # chroma_path kept for API compat but not used for storage
        self.chroma_path = Path(chroma_path)
        self.collection_name = collection_name
        self.embedding_function = LocalHashEmbeddingFunction()
        self._lock = threading.RLock()

        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(self.sqlite_path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_sqlite()

        # sqlite-vec vector collection (stored in the same .db file as FTS)
        self.collection = SqliteVecBackend(
            db_path=self.conn,  # share the connection
            collection=collection_name,
            dim=_KB_VEC_DIM,
        )

    def rebuild(self, items: list[dict[str, Any]], text_loader: Callable[[dict[str, Any]], str]) -> dict[str, int]:
        """全量重建索引。portalocker 防止多进程同时 rebuild 损坏文件（跨平台）。"""
        import portalocker
        _lock_path = str(self.sqlite_path) + ".rebuild.lock"
        with open(_lock_path, "w") as _flock_file:
            try:
                portalocker.lock(_flock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.AlreadyLocked:
                logger.warning("[KnowledgeHybridIndex] rebuild: 另一进程正在重建，跳过")
                return {"chunk_count": -1, "skipped_unchanged": 0}
            try:
                return self._rebuild_locked(items, text_loader)
            finally:
                portalocker.unlock(_flock_file)

    def _rebuild_locked(self, items: list[dict[str, Any]], text_loader: Callable[[dict[str, Any]], str]) -> dict[str, int]:
        with self._lock:
            preserved = self._dump_preserved_kinds()
            if preserved:
                print(f"[KnowledgeHybridIndex] rebuild: 保护 {len(preserved)} 条受保护数据"
                      f"（{set(p[0]['source_kind'] for p in preserved)}）")

            _existing_mtime: dict[str, str | None] = {}
            try:
                rows = self.conn.execute("SELECT content_id, source_mtime FROM documents").fetchall()
                for r in rows:
                    _existing_mtime[r["content_id"]] = r["source_mtime"]
            except Exception:
                pass

            _cached_text: dict[str, str] = {}
            for item in items:
                _mtime = item.get("source_mtime")
                if _mtime is not None and _existing_mtime.get(item["content_id"]) == _mtime:
                    try:
                        chunks = self.conn.execute(
                            "SELECT chunk_text FROM chunks WHERE content_id=? ORDER BY chunk_index",
                            (item["content_id"],)
                        ).fetchall()
                        _text = "\n".join(c["chunk_text"] for c in chunks)
                        if _text.strip():
                            _cached_text[item["content_id"]] = _text
                    except Exception:
                        pass

            self._reset()
            chunk_count = 0
            skipped_unchanged = 0
            _batch_counter = 0

            try:
                for item in items:
                    _item_mtime: str | None = item.get("source_mtime")
                    if _item_mtime is not None:
                        _prev = _existing_mtime.get(item["content_id"])
                        if _prev is not None and _prev == _item_mtime:
                            _ct = _cached_text.get(item["content_id"])
                            if _ct:
                                skipped_unchanged += 1
                                text = _ct
                            else:
                                text = text_loader(item).strip()
                        else:
                            text = text_loader(item).strip()
                    else:
                        text = text_loader(item).strip()
                    if not text:
                        continue

                    _pk = item.get("project_key", "_global") or "_global"
                    self.conn.execute(
                        """
                        INSERT INTO documents (
                            content_id, source_kind, name, summary, source_rel_path,
                            citation_label, l1_module, l2_module, doc_type, project_key,
                            source_mtime
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["content_id"], item["source_kind"],
                            item.get("name", ""), item.get("summary", ""),
                            item.get("source_rel_path", ""), item.get("citation_label", ""),
                            item.get("l1_module", ""), item.get("l2_module", ""),
                            item.get("doc_type", ""), _pk, _item_mtime,
                        ),
                    )

                    vec_ids: list[str] = []
                    vec_embeddings: list[list[float]] = []
                    vec_docs: list[str] = []
                    vec_metas: list[dict[str, Any]] = []

                    for index, chunk in enumerate(self._chunk_text(text), start=1):
                        chunk_id = f"{item['content_id']}::chunk-{index:03d}"
                        self.conn.execute(
                            """
                            INSERT INTO chunks (
                                chunk_id, content_id, chunk_index, chunk_text, chunk_preview,
                                source_kind, name, summary, source_rel_path, citation_label,
                                l1_module, l2_module, doc_type, project_key
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                chunk_id, item["content_id"], index, chunk, chunk[:240],
                                item["source_kind"], item.get("name", ""),
                                item.get("summary", ""), item.get("source_rel_path", ""),
                                item.get("citation_label", ""), item.get("l1_module", ""),
                                item.get("l2_module", ""), item.get("doc_type", ""), _pk,
                            ),
                        )
                        self.conn.execute(
                            """
                            INSERT INTO chunks_fts (
                                chunk_id, name, summary, keywords, source_rel_path, chunk_text
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                chunk_id, item.get("name", ""), item.get("summary", ""),
                                " ".join(item.get("keywords", [])),
                                item.get("source_rel_path", ""), chunk,
                            ),
                        )
                        vec_ids.append(chunk_id)
                        vec_embeddings.append(self.embedding_function._embed(chunk))
                        vec_docs.append(chunk)
                        vec_metas.append({
                            "content_id": item["content_id"],
                            "source_kind": item["source_kind"],
                            "name": item.get("name", ""),
                            "summary": item.get("summary", "")[:500],
                            "source_rel_path": item.get("source_rel_path", ""),
                            "citation_label": item.get("citation_label", ""),
                            "l1_module": item.get("l1_module", ""),
                            "l2_module": item.get("l2_module", ""),
                            "doc_type": item.get("doc_type", ""),
                            "project_key": _pk,
                        })
                        chunk_count += 1

                    if vec_ids:
                        try:
                            self.collection.upsert(
                                ids=vec_ids,
                                embeddings=vec_embeddings,
                                documents=vec_docs,
                                metadatas=vec_metas,
                            )
                        except Exception as e:
                            logger.warning("[rebuild] vector upsert failed: %s", e)

                    _batch_counter += 1
                    if _batch_counter % 50 == 0:
                        self.conn.commit()

                self.conn.commit()
            finally:
                for p_item, p_text in preserved:
                    try:
                        added = self.add_item(p_item, p_text)
                        if added > 0:
                            chunk_count += added
                    except Exception as e:
                        print(f"[KnowledgeHybridIndex] 恢复受保护条目失败 {p_item.get('content_id')}: {e}")

            if skipped_unchanged:
                print(f"[KnowledgeHybridIndex] rebuild: 跳过未变化 {skipped_unchanged} 条（mtime 相同）")
            return {"chunk_count": chunk_count, "skipped_unchanged": skipped_unchanged}

    def _dump_preserved_kinds(self) -> list[tuple[dict[str, Any], str]]:
        if not _PRESERVED_SOURCE_KINDS:
            return []
        placeholders = ",".join("?" * len(_PRESERVED_SOURCE_KINDS))
        rows = self.conn.execute(
            f"""SELECT content_id, source_kind, name, summary, source_rel_path,
                       citation_label, l1_module, l2_module, doc_type
                FROM documents WHERE source_kind IN ({placeholders})""",
            _PRESERVED_SOURCE_KINDS,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            chunks = self.conn.execute(
                "SELECT chunk_text FROM chunks WHERE content_id = ? ORDER BY chunk_index",
                (row["content_id"],),
            ).fetchall()
            full_text = "\n".join(c["chunk_text"] for c in chunks)
            if full_text.strip():
                result.append((item, full_text))
        return result

    def list_by_source_kinds(self, source_kinds: tuple[str, ...], top_k: int = 500) -> list[dict[str, Any]]:
        if not source_kinds:
            return []
        placeholders = ",".join("?" * len(source_kinds))
        rows = self.conn.execute(
            f"""SELECT content_id, source_kind, name, summary, source_rel_path,
                       citation_label, l1_module, l2_module, doc_type
                FROM documents WHERE source_kind IN ({placeholders}) LIMIT ?""",
            (*source_kinds, top_k),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            chunks = self.conn.execute(
                "SELECT chunk_text FROM chunks WHERE content_id = ? ORDER BY chunk_index",
                (row["content_id"],),
            ).fetchall()
            d["content"] = "\n".join(c["chunk_text"] for c in chunks)
            result.append(d)
        return result

    def count_by_source_kind(self, source_kind: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE source_kind = ?", (source_kind,)
        ).fetchone()
        return row[0] if row else 0

    def delete_item(self, content_id: str) -> bool:
        with self._lock:
            existing = self.conn.execute(
                "SELECT 1 FROM documents WHERE content_id = ?", (content_id,)
            ).fetchone()
            if not existing:
                return False
            old_ids = [
                row[0] for row in self.conn.execute(
                    "SELECT chunk_id FROM chunks WHERE content_id = ?", (content_id,)
                ).fetchall()
            ]
            if old_ids:
                try:
                    self.collection.delete(ids=old_ids)
                except Exception:
                    pass
                placeholders = ",".join("?" * len(old_ids))
                self.conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", old_ids)
                self.conn.execute(f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", old_ids)
            self.conn.execute("DELETE FROM documents WHERE content_id = ?", (content_id,))
            self.conn.commit()
            return True

    def add_item(self, item: dict[str, Any], text: str, source_mtime: str | None = None) -> int:
        """增量插入单条文档（支持 upsert）。"""
        text = text.strip()
        if not text:
            return 0
        content_id = item["content_id"]
        with self._lock:
            if source_mtime is not None:
                existing_row = self.conn.execute(
                    "SELECT source_mtime FROM documents WHERE content_id = ?", (content_id,)
                ).fetchone()
                if existing_row is not None and existing_row["source_mtime"] == source_mtime:
                    return -1

            existing = self.conn.execute(
                "SELECT 1 FROM documents WHERE content_id = ?", (content_id,)
            ).fetchone()
            if existing:
                old_ids = [
                    row[0] for row in self.conn.execute(
                        "SELECT chunk_id FROM chunks WHERE content_id = ?", (content_id,)
                    ).fetchall()
                ]
                if old_ids:
                    try:
                        self.collection.delete(ids=old_ids)
                    except Exception:
                        pass
                    placeholders = ",".join("?" * len(old_ids))
                    self.conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", old_ids)
                    self.conn.execute(f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", old_ids)
                self.conn.execute("DELETE FROM documents WHERE content_id = ?", (content_id,))

            _pk = item.get("project_key", "_global") or "_global"
            self.conn.execute(
                """INSERT INTO documents (content_id, source_kind, name, summary,
                   source_rel_path, citation_label, l1_module, l2_module, doc_type, project_key,
                   source_mtime)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (content_id, item["source_kind"], item.get("name", ""),
                 item.get("summary", ""), item.get("source_rel_path", ""),
                 item.get("citation_label", ""), item.get("l1_module", ""),
                 item.get("l2_module", ""), item.get("doc_type", ""), _pk,
                 source_mtime),
            )

            vec_ids, vec_embeddings, vec_docs, vec_metas = [], [], [], []
            chunk_count = 0
            for index, chunk in enumerate(self._chunk_text(text), start=1):
                chunk_id = f"{content_id}::chunk-{index:03d}"
                self.conn.execute(
                    """INSERT INTO chunks (chunk_id, content_id, chunk_index, chunk_text,
                       chunk_preview, source_kind, name, summary, source_rel_path,
                       citation_label, l1_module, l2_module, doc_type, project_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (chunk_id, content_id, index, chunk, chunk[:240],
                     item["source_kind"], item.get("name", ""),
                     item.get("summary", ""), item.get("source_rel_path", ""),
                     item.get("citation_label", ""), item.get("l1_module", ""),
                     item.get("l2_module", ""), item.get("doc_type", ""), _pk),
                )
                self.conn.execute(
                    """INSERT INTO chunks_fts (chunk_id, name, summary, keywords,
                       source_rel_path, chunk_text) VALUES (?, ?, ?, ?, ?, ?)""",
                    (chunk_id, item.get("name", ""), item.get("summary", ""),
                     " ".join(item.get("keywords", [])),
                     item.get("source_rel_path", ""), chunk),
                )
                vec_ids.append(chunk_id)
                vec_embeddings.append(self.embedding_function._embed(chunk))
                vec_docs.append(chunk)
                vec_metas.append({
                    "content_id": content_id,
                    "source_kind": item["source_kind"],
                    "name": item.get("name", ""),
                    "summary": item.get("summary", "")[:500],
                    "source_rel_path": item.get("source_rel_path", ""),
                    "citation_label": item.get("citation_label", ""),
                    "l1_module": item.get("l1_module", ""),
                    "l2_module": item.get("l2_module", ""),
                    "doc_type": item.get("doc_type", ""),
                    "project_key": _pk,
                })
                chunk_count += 1

            self.conn.commit()

            if vec_ids:
                try:
                    self.collection.upsert(
                        ids=vec_ids,
                        embeddings=vec_embeddings,
                        documents=vec_docs,
                        metadatas=vec_metas,
                    )
                except Exception as e:
                    logger.warning("[add_item] vector upsert failed (SQLite committed OK): %s", e)

            return chunk_count

    def search(self, query: str, top_k: int = 20, source_kind: str | None = None,
               category: str | None = None, project_key: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            fts_hits = self._search_fts(query, top_k=top_k * 3, source_kind=source_kind,
                                         category=category, project_key=project_key)
            vector_hits = self._search_vector(query, top_k=top_k * 3, source_kind=source_kind,
                                               category=category, project_key=project_key)

            combined: dict[str, dict[str, Any]] = {}
            for hit in fts_hits:
                row = combined.setdefault(hit["chunk_id"], {**hit, "fts_score": 0.0, "vector_score": 0.0})
                row["fts_score"] = max(row["fts_score"], hit["fts_score"])

            for hit in vector_hits:
                row = combined.setdefault(hit["chunk_id"], {**hit, "fts_score": 0.0, "vector_score": 0.0})
                row["vector_score"] = max(row["vector_score"], hit["vector_score"])

            for item in combined.values():
                source_prior = _SOURCE_PRIOR.get(item["source_kind"], 0.85)
                item["score"] = round(
                    item["vector_score"] * 0.25 + item["fts_score"] * 0.55 + source_prior * 0.20, 4
                )
                item["match_type"] = "chunk"

            return sorted(combined.values(), key=lambda item: (-item["score"], item["name"]))[:top_k]

    def list_by_source_kind(self, source_kind: str, top_k: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT content_id, source_kind, name, summary, source_rel_path,
                          citation_label, l1_module, l2_module, doc_type
                   FROM documents WHERE source_kind = ? LIMIT ?""",
                (source_kind, top_k),
            ).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                chunks = self.conn.execute(
                    "SELECT chunk_text FROM chunks WHERE content_id = ? ORDER BY chunk_index",
                    (row["content_id"],),
                ).fetchall()
                d["content"] = "\n".join(c["chunk_text"] for c in chunks)
                results.append(d)
            return results

    def get_chunks_for_content(self, content_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self.conn.execute(
                """SELECT chunk_id, chunk_index, chunk_preview, chunk_text
                   FROM chunks WHERE content_id = ?
                   ORDER BY chunk_index LIMIT ?""",
                (content_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def count_chunks(self) -> int:
        with self._lock:
            cursor = self.conn.execute("SELECT COUNT(*) FROM chunks")
            return int(cursor.fetchone()[0])

    def _init_sqlite(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    content_id TEXT PRIMARY KEY,
                    source_kind TEXT,
                    name TEXT,
                    summary TEXT,
                    source_rel_path TEXT,
                    citation_label TEXT,
                    l1_module TEXT,
                    l2_module TEXT,
                    doc_type TEXT,
                    project_key TEXT DEFAULT '_global',
                    source_mtime TEXT,
                    credibility REAL DEFAULT 0.8,
                    validation_sources TEXT,
                    last_validated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    content_id TEXT,
                    chunk_index INTEGER,
                    chunk_text TEXT,
                    chunk_preview TEXT,
                    source_kind TEXT,
                    name TEXT,
                    summary TEXT,
                    source_rel_path TEXT,
                    citation_label TEXT,
                    l1_module TEXT,
                    l2_module TEXT,
                    doc_type TEXT,
                    project_key TEXT DEFAULT '_global'
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    name,
                    summary,
                    keywords,
                    source_rel_path,
                    chunk_text
                );

                CREATE TABLE IF NOT EXISTS pending_kb_questions (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    source_compiled_id TEXT,
                    options_json TEXT NOT NULL,
                    default_choice TEXT NOT NULL DEFAULT 'C',
                    llm_confidence REAL,
                    llm_reasoning TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    jobmaster_decision_id TEXT,
                    created_at TEXT,
                    asked_at TEXT,
                    resolved_at TEXT,
                    resolved_choice TEXT,
                    resolved_by TEXT
                );
                """
            )
            self.conn.commit()
            # Migrations
            for tbl in ("documents", "chunks"):
                try:
                    self.conn.execute(f"ALTER TABLE {tbl} ADD COLUMN project_key TEXT DEFAULT '_global'")
                    self.conn.commit()
                except Exception:
                    pass
            try:
                self.conn.execute("ALTER TABLE documents ADD COLUMN source_mtime TEXT")
                self.conn.commit()
            except Exception:
                pass
            for col, defn in [
                ("credibility", "REAL DEFAULT 0.8"),
                ("validation_sources", "TEXT"),
                ("last_validated_at", "TEXT"),
            ]:
                try:
                    self.conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {defn}")
                    self.conn.commit()
                except Exception:
                    pass
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_content_id ON chunks(content_id)"
            )
            self.conn.commit()

    def _reset(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM documents")
            self.conn.execute("DELETE FROM chunks")
            self.conn.execute("DELETE FROM chunks_fts")
            self.conn.commit()
            try:
                self.collection.reset()
            except Exception:
                pass

    def _chunk_text(self, text: str, max_chars: int = 900, overlap: int = 120) -> list[str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        sections: list[str] = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 <= max_chars:
                current = f"{current}\n{line}".strip()
                continue
            if current:
                sections.append(current)
            current = f"{current[-overlap:]}\n{line}".strip() if current else line
        if current:
            sections.append(current)
        return sections or [text[:max_chars]]

    def _search_fts(self, query: str, top_k: int, source_kind: str | None, category: str | None,
                    project_key: str | None = None) -> list[dict[str, Any]]:
        tokens = [token for token in TOKEN_RE.findall(query or "") if token]
        if not tokens:
            return []
        match_query = " OR ".join(f'"{t}"' for t in tokens)
        where = []
        params: list[Any] = [match_query]
        if source_kind:
            where.append("c.source_kind = ?")
            params.append(source_kind)
        if category:
            where.append("(c.l1_module = ? OR c.l2_module = ?)")
            params.extend([category, category])
        if project_key:
            where.append("(c.project_key = ? OR c.project_key = '_global')")
            params.append(project_key)
        where_sql = f" AND {' AND '.join(where)}" if where else ""
        sql = f"""
            SELECT
                c.chunk_id, c.content_id, c.chunk_index, c.chunk_preview, c.chunk_text,
                c.source_kind, c.name, c.summary, c.source_rel_path, c.citation_label,
                c.l1_module, c.l2_module, c.doc_type, c.project_key,
                bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ? {where_sql}
            ORDER BY rank
            LIMIT ?
        """
        params.append(top_k)
        rows = []
        for row in self.conn.execute(sql, params).fetchall():
            rank = abs(float(row["rank"]))
            rows.append({**dict(row), "fts_score": 1.0 / (1.0 + rank)})
        return rows

    def _search_vector(self, query: str, top_k: int, source_kind: str | None, category: str | None,
                       project_key: str | None = None) -> list[dict[str, Any]]:
        try:
            count = self.collection.count()
            if count == 0:
                return []
        except Exception as e:
            logger.warning("[_search_vector] vector collection unavailable, using FTS only: %s", e)
            return []

        where = {"source_kind": source_kind} if source_kind else None
        try:
            query_emb = self.embedding_function._embed(query)
            results = self.collection.query(
                embedding=query_emb,
                n=min(max(top_k, 1), count),
                where=where,
            )
        except Exception as e:
            logger.warning("[_search_vector] vector query failed, using FTS only: %s", e)
            return []

        hits: list[dict[str, Any]] = []
        ids = results.get("ids", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        documents = results.get("documents", [[]])[0]
        for chunk_id, metadata, distance, document in zip(ids, metadatas, distances, documents):
            if category and category not in {metadata.get("l1_module"), metadata.get("l2_module")}:
                continue
            if project_key and metadata.get("project_key", "_global") not in (project_key, "_global"):
                continue
            hits.append({
                "chunk_id": chunk_id,
                "content_id": metadata.get("content_id", ""),
                "chunk_index": self._chunk_index_from_id(chunk_id),
                "chunk_preview": (document or "")[:240],
                "chunk_text": document or "",
                "source_kind": metadata.get("source_kind", ""),
                "name": metadata.get("name", ""),
                "summary": metadata.get("summary", ""),
                "source_rel_path": metadata.get("source_rel_path", ""),
                "citation_label": metadata.get("citation_label", ""),
                "l1_module": metadata.get("l1_module", ""),
                "l2_module": metadata.get("l2_module", ""),
                "doc_type": metadata.get("doc_type", ""),
                "project_key": metadata.get("project_key", "_global"),
                "vector_score": max(0.0, 1.0 - float(distance)),
            })
        return hits

    def _chunk_index_from_id(self, chunk_id: str) -> int:
        match = re.search(r"chunk-(\d+)$", chunk_id)
        return int(match.group(1)) if match else 0
