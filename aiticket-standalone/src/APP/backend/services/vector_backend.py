"""sqlite-vec vector backend — drop-in replacement for ChromaDB collections.

Each SqliteVecBackend instance manages two tables inside a shared SQLite file:
  {col}_meta(id TEXT PRIMARY KEY, document TEXT, metadata TEXT/*JSON*/)
  {col}_vec  USING vec0(id TEXT PRIMARY KEY, embedding float[{dim}])

A special sentinel row '__meta__' in {col}_meta stores schema metadata
(dim, provider) for consistency checking on reopen.
"""
from __future__ import annotations

import json
import logging
import struct
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# sqlite3 shim: prefer pysqlite3 (if installed) so that macOS system sqlite
# doesn't interfere with extension loading; fall back to stdlib sqlite3.
# ---------------------------------------------------------------------------
try:
    import pysqlite3 as sqlite3  # type: ignore
except ImportError:
    import sqlite3  # type: ignore


def _pack_f32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _open_connection(db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a SQLite connection and load sqlite_vec extension."""
    con = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.enable_load_extension(True)
    import sqlite_vec  # type: ignore
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    return con


# ---------------------------------------------------------------------------
# Where-clause translator (Chroma-style → SQL)
# Supported operators: equality, $gte, $gt, $lte, $lt, $ne, $in
# ---------------------------------------------------------------------------
_OP_MAP = {"$gte": ">=", "$gt": ">", "$lte": "<=", "$lt": "<", "$ne": "!="}


def _where_to_sql(
    where: dict[str, Any], table_alias: str = "m"
) -> tuple[str, list[Any]]:
    """Translate a Chroma-style where dict to (sql_fragment, params).

    Supports:
      {"key": value}                     → json_extract = ?
      {"key": {"$gte": x}}               → json_extract >= ?
      {"key": {"$in": [a, b]}}           → json_extract IN (?,?)
    Multiple keys are AND-joined.
    """
    clauses: list[str] = []
    params: list[Any] = []
    for key, val in where.items():
        col = f"json_extract({table_alias}.metadata, '$.{key}')"
        if isinstance(val, dict):
            for op, operand in val.items():
                if op in _OP_MAP:
                    clauses.append(f"{col} {_OP_MAP[op]} ?")
                    params.append(operand)
                elif op == "$in":
                    placeholders = ",".join("?" * len(operand))
                    clauses.append(f"{col} IN ({placeholders})")
                    params.extend(operand)
                else:
                    logger.warning("[vector_backend] unsupported where op: %s", op)
        else:
            clauses.append(f"{col} = ?")
            params.append(val)
    sql = " AND ".join(clauses)
    return sql, params


class SqliteVecBackend:
    """Vector collection backed by sqlite-vec (vec0 virtual table).

    Args:
        db_path: Path to the .db file, or an existing sqlite3.Connection.
        collection: Collection name (becomes table name prefix).
        dim: Embedding dimension. If the stored dim differs, reset() is called.
        embedding_provider: Optional EmbeddingProvider instance (used only by
            callers that want the backend to embed for them; most callers embed
            externally and pass raw vectors to upsert).
    """

    def __init__(
        self,
        db_path: Union[str, Path, "sqlite3.Connection"],
        collection: str,
        dim: int,
        embedding_provider=None,
    ) -> None:
        self.collection = collection
        self.dim = dim
        self.provider = embedding_provider

        if isinstance(db_path, (str, Path)):
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._con = _open_connection(db_path)
            self._owns_connection = True
        else:
            # Caller-provided connection — load sqlite_vec if not already loaded
            self._con = db_path
            self._owns_connection = False
            try:
                self._con.enable_load_extension(True)
                import sqlite_vec  # type: ignore
                sqlite_vec.load(self._con)
                self._con.enable_load_extension(False)
            except Exception:
                pass  # Already loaded or not available; _init_tables will surface the error

        self._meta_table = f"{collection}_meta"
        self._vec_table = f"{collection}_vec"
        self._init_tables()
        self._check_dim()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_tables(self) -> None:
        self._con.execute(
            f"""CREATE TABLE IF NOT EXISTS {self._meta_table}
                (id TEXT PRIMARY KEY, document TEXT, metadata TEXT)"""
        )
        self._con.execute(
            f"""CREATE VIRTUAL TABLE IF NOT EXISTS {self._vec_table}
                USING vec0(id TEXT PRIMARY KEY, embedding float[{self.dim}])"""
        )
        self._con.commit()

    def _check_dim(self) -> None:
        """Verify stored dim matches; reset if mismatch."""
        row = self._con.execute(
            f"SELECT metadata FROM {self._meta_table} WHERE id='__meta__'"
        ).fetchone()
        if row is None:
            # First open — write sentinel
            self._con.execute(
                f"INSERT OR REPLACE INTO {self._meta_table}(id, document, metadata) VALUES(?,?,?)",
                ("__meta__", "", json.dumps({"_embed_dim": self.dim, "_provider": str(self.provider)})),
            )
            self._con.commit()
            return
        try:
            stored = json.loads(row["metadata"] or "{}")
            stored_dim = int(stored.get("_embed_dim", self.dim))
        except Exception:
            stored_dim = self.dim

        if stored_dim != self.dim:
            logger.warning(
                "[SqliteVecBackend] dim mismatch: stored=%d requested=%d — resetting collection '%s'",
                stored_dim, self.dim, self.collection,
            )
            self.reset()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        """Insert-or-replace records. Embeddings must already be computed.

        vec0 does not support INSERT OR REPLACE, so we delete first then insert.
        """
        for doc_id, emb, doc, meta in zip(ids, embeddings, documents, metadatas):
            packed = _pack_f32(emb)
            # Delete existing rows first (vec0 doesn't support INSERT OR REPLACE)
            self._con.execute(f"DELETE FROM {self._vec_table} WHERE id = ?", (doc_id,))
            self._con.execute(
                f"INSERT OR REPLACE INTO {self._meta_table}(id, document, metadata) VALUES(?,?,?)",
                (doc_id, doc, json.dumps(meta, ensure_ascii=False, default=str)),
            )
            self._con.execute(
                f"INSERT INTO {self._vec_table}(id, embedding) VALUES(?,?)",
                (doc_id, packed),
            )
        self._con.commit()

    def query(
        self,
        embedding: list[float],
        n: int,
        where: Optional[dict] = None,
    ) -> dict:
        """KNN search. Returns chroma-compatible dict with ids/distances/documents/metadatas."""
        packed = _pack_f32(embedding)

        # Build WHERE clause for metadata filter
        where_sql = ""
        where_params: list[Any] = []
        if where:
            fragment, where_params = _where_to_sql(where, table_alias="m")
            if fragment:
                where_sql = f"AND {fragment}"

        sql = f"""
            SELECT v.id, v.distance, m.document, m.metadata
            FROM {self._vec_table} v
            JOIN {self._meta_table} m ON v.id = m.id
            WHERE v.embedding MATCH ? AND k = ?
              AND m.id != '__meta__'
              {where_sql}
            ORDER BY v.distance
        """
        # sqlite-vec requires the query vector and k as the first two params
        # before any additional WHERE params
        try:
            rows = self._con.execute(sql, [packed, n, *where_params]).fetchall()
        except Exception as e:
            logger.warning("[SqliteVecBackend] query failed: %s", e)
            return {"ids": [[]], "distances": [[]], "documents": [[]], "metadatas": [[]]}

        ids, distances, documents, metadatas = [], [], [], []
        for row in rows:
            ids.append(row["id"])
            distances.append(float(row["distance"]))
            documents.append(row["document"] or "")
            try:
                metadatas.append(json.loads(row["metadata"] or "{}"))
            except Exception:
                metadatas.append({})

        return {
            "ids": [ids],
            "distances": [distances],
            "documents": [documents],
            "metadatas": [metadatas],
        }

    def get(
        self,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
        include: Optional[list[str]] = None,
    ) -> dict:
        """Fetch records by id and/or metadata filter."""
        where_clauses: list[str] = ["id != '__meta__'"]
        params: list[Any] = []

        if ids:
            placeholders = ",".join("?" * len(ids))
            where_clauses.append(f"id IN ({placeholders})")
            params.extend(ids)

        if where:
            fragment, wp = _where_to_sql(where, table_alias="t")
            if fragment:
                # We alias the table in the query below
                where_clauses.append(fragment)
                params.extend(wp)

        where_sql = " AND ".join(where_clauses)
        sql = f"SELECT id, document, metadata FROM {self._meta_table} AS t WHERE {where_sql}"

        rows = self._con.execute(sql, params).fetchall()
        result_ids, documents, metadatas = [], [], []
        for row in rows:
            result_ids.append(row["id"])
            documents.append(row["document"] or "")
            try:
                metadatas.append(json.loads(row["metadata"] or "{}"))
            except Exception:
                metadatas.append({})

        return {"ids": result_ids, "documents": documents, "metadatas": metadatas}

    def delete(self, ids: list[str]) -> None:
        """Delete records by id."""
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._con.execute(f"DELETE FROM {self._meta_table} WHERE id IN ({placeholders})", ids)
        self._con.execute(f"DELETE FROM {self._vec_table} WHERE id IN ({placeholders})", ids)
        self._con.commit()

    def count(self) -> int:
        """Return number of real records (excluding __meta__ sentinel)."""
        row = self._con.execute(
            f"SELECT COUNT(*) FROM {self._meta_table} WHERE id != '__meta__'"
        ).fetchone()
        return int(row[0]) if row else 0

    def reset(self) -> None:
        """Drop and recreate both tables, then re-write sentinel."""
        self._con.execute(f"DROP TABLE IF EXISTS {self._meta_table}")
        self._con.execute(f"DROP TABLE IF EXISTS {self._vec_table}")
        self._con.commit()
        self._init_tables()
        # Re-write sentinel with current dim
        self._con.execute(
            f"INSERT OR REPLACE INTO {self._meta_table}(id, document, metadata) VALUES(?,?,?)",
            ("__meta__", "", json.dumps({"_embed_dim": self.dim, "_provider": str(self.provider)})),
        )
        self._con.commit()
