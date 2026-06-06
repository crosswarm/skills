"""
Vector Store — sqlite-vec backend (replaces Chroma)

Public API unchanged from the Chroma version so all callers
(board_service_chroma, search_chroma, kb_runtime_service, …) work without modification.

Collections (all stored in data/sqlite/tickets.db):
  issues           — raw issue embeddings
  analysis_cache   — AI analysis result cache
  similarity_graph — issue similarity edges (legacy; kept for compat)
  req_clusters     — requirement clusters
  query_cache      — query result cache (24 h TTL)
"""

import json
import hashlib
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any

from services.embedding_provider import get_embedding_provider, HashEmbeddingProvider
from services.vector_backend import SqliteVecBackend


# ---------------------------------------------------------------------------
# Default DB path
# ---------------------------------------------------------------------------
_BACKEND_DIR = Path(__file__).parent
_DEFAULT_DB = _BACKEND_DIR / "data" / "sqlite" / "tickets.db"


# ---------------------------------------------------------------------------
# Legacy embedding helper (unused — kept so imports don't break)
# ---------------------------------------------------------------------------
def get_embedding_function(*args, **kwargs):
    """Legacy shim — no longer used internally."""
    return None


class VectorStore:
    """
    sqlite-vec backed vector store.
    Provides the same public interface as the Chroma-based version.
    """

    def __init__(
        self,
        persist_directory: str = "./chroma_db",
        api_key: str = None,
        allow_download: bool = True,
    ):
        # Derive DB path from persist_directory for backward compat
        # (callers pass their chroma_db path; we put sqlite next to it)
        pdir = Path(persist_directory)
        db_dir = pdir.parent / "sqlite"
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "tickets.db"

        # Embedding provider
        provider_hint = os.environ.get("EMBEDDING_PROVIDER", "fastembed" if allow_download else "hash")
        try:
            self._provider = get_embedding_provider(provider_hint)
        except Exception:
            self._provider = HashEmbeddingProvider()

        dim = self._provider.dim

        # Open all collections
        self.issues_collection = SqliteVecBackend(self._db_path, "issues", dim=dim, embedding_provider=self._provider)
        self.analysis_collection = SqliteVecBackend(self._db_path, "analysis_cache", dim=dim, embedding_provider=self._provider)
        self.similarity_collection = SqliteVecBackend(self._db_path, "similarity_graph", dim=dim, embedding_provider=self._provider)
        self.req_clusters_collection = SqliteVecBackend(self._db_path, "req_clusters", dim=dim, embedding_provider=self._provider)
        self.query_cache = SqliteVecBackend(self._db_path, "query_cache", dim=dim, embedding_provider=self._provider)

        # Legacy compat attributes
        self.persist_directory = persist_directory
        self.embedding_func = self._provider  # some callers check truthiness

        print(f"[VectorStore] sqlite-vec backend initialised ({self._db_path})")
        print(f"  - issues:         {self.issues_collection.count()}")
        print(f"  - analysis_cache: {self.analysis_collection.count()}")
        print(f"  - req_clusters:   {self.req_clusters_collection.count()}")
        print(f"  - query_cache:    {self.query_cache.count()}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _embed_texts(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        try:
            return self._provider.embed(texts, is_query=is_query)
        except Exception as e:
            print(f"[VectorStore] embed failed: {e}")
            return self._provider.embed(texts, is_query=is_query) if not isinstance(self._provider, HashEmbeddingProvider) else HashEmbeddingProvider(self._provider.dim).embed(texts)

    def _safe_count(self, col: SqliteVecBackend) -> int:
        try:
            return col.count()
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Issue vector operations
    # ------------------------------------------------------------------

    def add_issue(
        self,
        issue_key: str,
        summary: str,
        description: str = "",
        metadata: Dict = None,
        embedding_text: str = None,
    ) -> bool:
        if embedding_text is None:
            embedding_text = f"{summary} {description}"[:2000]

        content_hash = hashlib.md5(embedding_text.encode()).hexdigest()[:16]
        doc_id = f"issue_{issue_key}"

        # Check for unchanged content
        try:
            existing = self.issues_collection.get(ids=[doc_id])
            if existing and existing["ids"]:
                old_hash = existing["metadatas"][0].get("content_hash", "")
                if old_hash == content_hash:
                    return False
        except Exception:
            pass

        meta = {
            "issue_key": issue_key,
            "summary": summary[:500],
            "content_hash": content_hash,
            "added_at": datetime.now().isoformat(),
            **{k: str(v)[:500] for k, v in (metadata or {}).items()},
        }

        embeddings = self._embed_texts([embedding_text])
        self.issues_collection.upsert(
            ids=[doc_id],
            embeddings=embeddings,
            documents=[embedding_text],
            metadatas=[meta],
        )
        return True

    def search_similar_issues(
        self, query: str, top_k: int = 5, min_score: float = 0.7
    ) -> List[Dict]:
        if self.issues_collection.count() == 0:
            return []

        try:
            emb = self._embed_texts([query], is_query=True)[0]
            results = self.issues_collection.query(
                embedding=emb,
                n=min(top_k * 2, max(self.issues_collection.count(), 1)),
            )
        except Exception as e:
            print(f"[VectorStore] semantic search failed, falling back to keyword: {e}")
            return self._keyword_search(query, top_k)

        similar_issues = []
        for i, doc_id in enumerate(results["ids"][0]):
            distance = results["distances"][0][i]
            score = 1.0 - distance
            if score < min_score:
                continue
            metadata = results["metadatas"][0][i]
            similar_issues.append({
                "issue_key": metadata.get("issue_key"),
                "summary": metadata.get("summary", ""),
                "score": float(score),
                "document": results["documents"][0][i],
                "metadata": metadata,
            })

        similar_issues.sort(key=lambda x: x["score"], reverse=True)
        return similar_issues[:top_k]

    def _keyword_search(self, query: str, top_k: int = 5) -> List[Dict]:
        """BM25-style keyword fallback (identical logic to original)."""
        query_lower = query.lower()
        query_words: set = set()
        query_words.update(w for w in query_lower.split() if len(w) >= 2)
        for length in [4, 3, 2]:
            for i in range(len(query_lower) - length + 1):
                substr = query_lower[i:i + length]
                if any("一" <= c <= "鿿" for c in substr):
                    query_words.add(substr)
        if not query_words:
            query_words = {query_lower}
        if not query_words:
            return []

        total = self.issues_collection.count()
        batch_size = 1000
        matches = []
        for offset in range(0, total, batch_size):
            # get() doesn't support offset, so fetch all and slice manually
            all_docs = self.issues_collection.get()
            docs = all_docs.get("documents", [])[offset:offset + batch_size]
            metas = all_docs.get("metadatas", [])[offset:offset + batch_size]
            for doc, meta in zip(docs, metas):
                if not doc:
                    continue
                doc_lower = doc.lower()
                summary = meta.get("summary", "").lower()
                score = 0.0
                title_matches = sum(1 for w in query_words if w in summary)
                score += min(title_matches / len(query_words), 1.0) * 0.3
                content_matches = sum(1 for w in query_words if w in doc_lower)
                if content_matches > 0:
                    score += (content_matches / len(query_words)) * 0.4
                if query_lower in doc_lower:
                    score += 0.2
                if query_lower in summary:
                    score += 0.1
                score = min(score, 1.0)
                if score > 0.1:
                    matches.append({
                        "issue_key": meta.get("issue_key"),
                        "summary": meta.get("summary", ""),
                        "score": float(score),
                        "document": doc,
                        "metadata": meta,
                    })
            break  # get() returns all; one iteration is enough

        matches.sort(key=lambda x: x["score"], reverse=True)
        return matches[:top_k]

    def get_issue_by_key(self, issue_key: str) -> Optional[Dict]:
        try:
            result = self.issues_collection.get(ids=[f"issue_{issue_key}"])
            if result and result["ids"]:
                return {
                    "issue_key": issue_key,
                    "document": result["documents"][0],
                    "metadata": result["metadatas"][0],
                }
        except Exception:
            pass
        return None

    def batch_add_issues(self, issues: List[Dict]):
        """Batch-add issues (used for initial index population)."""
        ids, documents, metadatas = [], [], []
        for issue in issues:
            issue_key = issue.get("key")
            summary = issue.get("summary", "")
            description = issue.get("description", "")
            text = f"{summary} {description}"[:2000]
            content_hash = hashlib.md5(text.encode()).hexdigest()[:16]
            ids.append(f"issue_{issue_key}")
            documents.append(text)
            metadatas.append({
                "issue_key": issue_key,
                "summary": summary[:500],
                "content_hash": content_hash,
                "added_at": datetime.now().isoformat(),
                **{k: str(v)[:500] for k, v in issue.items() if k not in ["key", "summary", "description"]},
            })

        batch_size = 100
        for i in range(0, len(ids), batch_size):
            batch_ids = ids[i:i + batch_size]
            batch_docs = documents[i:i + batch_size]
            batch_metas = metadatas[i:i + batch_size]
            embeddings = self._embed_texts(batch_docs)
            self.issues_collection.upsert(
                ids=batch_ids,
                embeddings=embeddings,
                documents=batch_docs,
                metadatas=batch_metas,
            )

    # ------------------------------------------------------------------
    # AI analysis cache
    # ------------------------------------------------------------------

    def cache_analysis(
        self, issue_key: str, analysis: Dict, summary: str = "", ttl_days: int = 30
    ):
        content_hash = hashlib.md5(summary.encode()).hexdigest()[:16] if summary else ""
        doc_id = f"analysis_{issue_key}"

        meta = {
            "issue_key": issue_key,
            "content_hash": content_hash,
            "recommended_team": analysis.get("recommended_team", ""),
            "recommended_role": analysis.get("recommended_role", ""),
            "functionality_impact": analysis.get("functionality_impact", "")[:500],
            "solution_suggestion": analysis.get("solution_suggestion", "")[:1000],
            "confidence": float(analysis.get("confidence", 0)),
            "similar_issues": json.dumps(analysis.get("similar_issues", [])[:5]),
            "model_used": analysis.get("model_used", "unknown"),
            "is_reused": analysis.get("is_reused", False),
            "reused_from": analysis.get("reused_from", ""),
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(days=ttl_days)).isoformat(),
        }

        embedding_text = (
            f"{analysis.get('solution_suggestion', '')} "
            f"{analysis.get('functionality_impact', '')}"
        )

        try:
            embeddings = self._embed_texts([embedding_text])
            self.analysis_collection.upsert(
                ids=[doc_id],
                embeddings=embeddings,
                documents=[embedding_text],
                metadatas=[meta],
            )
        except Exception as e:
            print(f"[VectorStore] cache_analysis failed: {e}")

    def get_cached_analysis(
        self, issue_key: str, max_age_days: int = 7
    ) -> Optional[Dict]:
        try:
            result = self.analysis_collection.get(ids=[f"analysis_{issue_key}"])
            if not result or not result["ids"]:
                return None
            meta = result["metadatas"][0]
            expires_at = datetime.fromisoformat(meta.get("expires_at", "2000-01-01"))
            if datetime.now() > expires_at:
                return None
            created_at = datetime.fromisoformat(meta.get("created_at", "2000-01-01"))
            if datetime.now() - created_at > timedelta(days=max_age_days):
                return {"stale": True, **self._meta_to_analysis(meta)}
            return self._meta_to_analysis(meta)
        except Exception as e:
            print(f"[Cache Error] {e}")
            return None

    def find_reusable_analysis(
        self,
        query: str,
        min_confidence: float = 0.8,
        min_suggestion_similarity: float = 0.85,
    ) -> Optional[Dict]:
        if self.analysis_collection.count() == 0:
            return None

        try:
            emb = self._embed_texts([query], is_query=True)[0]
            results = self.analysis_collection.query(
                embedding=emb,
                n=3,
                where={"confidence": {"$gte": min_confidence}},
            )
        except Exception:
            return None

        if not results or not results["ids"][0]:
            return None

        best_distance = results["distances"][0][0]
        best_similarity = 1.0 - best_distance
        if best_similarity < min_suggestion_similarity:
            return None

        meta = results["metadatas"][0][0]
        analysis = self._meta_to_analysis(meta)
        analysis["is_reused"] = True
        analysis["reused_similarity"] = float(best_similarity)
        analysis["reused_from"] = meta.get("issue_key", "")
        return analysis

    def _meta_to_analysis(self, meta: Dict) -> Dict:
        return {
            "recommended_team": meta.get("recommended_team", ""),
            "recommended_role": meta.get("recommended_role", ""),
            "functionality_impact": meta.get("functionality_impact", ""),
            "solution_suggestion": meta.get("solution_suggestion", ""),
            "confidence": float(meta.get("confidence", 0)),
            "similar_issues": json.loads(meta.get("similar_issues", "[]")),
            "model_used": meta.get("model_used", ""),
            "is_reused": meta.get("is_reused", False) in (True, "True"),
            "reused_from": meta.get("reused_from", ""),
            "created_at": meta.get("created_at", ""),
        }

    def invalidate_cache(self, issue_key: str) -> bool:
        try:
            doc_id = f"analysis_{issue_key}"
            result = self.analysis_collection.get(ids=[doc_id])
            if not result or not result["ids"]:
                return False
            meta = result["metadatas"][0]
            meta["expires_at"] = "2000-01-01T00:00:00"
            meta["invalidated_at"] = datetime.now().isoformat()
            doc = result["documents"][0]
            emb = self._embed_texts([doc])[0]
            self.analysis_collection.upsert(
                ids=[doc_id], embeddings=[emb], documents=[doc], metadatas=[meta]
            )
            print(f"[VectorStore] {issue_key} cache invalidated")
            return True
        except Exception as e:
            print(f"[Invalidate Error] {e}")
            return False

    # ------------------------------------------------------------------
    # Similarity graph (legacy — kept for caller compat)
    # ------------------------------------------------------------------

    def record_similarity(
        self, issue_key: str, similar_key: str, similarity_score: float, can_reuse: bool = False
    ):
        edge_id = f"sim_{issue_key}_{similar_key}"
        doc = f"{issue_key} similar to {similar_key}"
        meta = {
            "source": issue_key,
            "target": similar_key,
            "similarity_score": float(similarity_score),
            "can_reuse": can_reuse,
            "created_at": datetime.now().isoformat(),
        }
        emb = self._embed_texts([doc])[0]
        self.similarity_collection.upsert(
            ids=[edge_id], embeddings=[emb], documents=[doc], metadatas=[meta]
        )

    def get_similar_neighbors(
        self, issue_key: str, min_score: float = 0.8
    ) -> List[Dict]:
        try:
            results = self.similarity_collection.get(
                where={"source": issue_key}
            )
            neighbors = []
            for meta in results.get("metadatas", []):
                if float(meta.get("similarity_score", 0)) >= min_score:
                    neighbors.append({
                        "issue_key": meta.get("target"),
                        "similarity": float(meta.get("similarity_score", 0)),
                        "can_reuse": meta.get("can_reuse", False),
                    })
            return sorted(neighbors, key=lambda x: x["similarity"], reverse=True)
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Requirement clusters
    # ------------------------------------------------------------------

    def upsert_cluster(self, cluster_id: str, title: str, metadata: Dict) -> bool:
        doc_id = f"cluster_{cluster_id}"
        embedding_text = title[:1000]
        meta = {
            k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v)
            for k, v in metadata.items()
        }
        meta["cluster_id"] = cluster_id
        meta["title"] = title[:500]
        meta.setdefault("status", "new")
        meta.setdefault("created_at", datetime.now().isoformat())
        meta["updated_at"] = datetime.now().isoformat()
        try:
            emb = self._embed_texts([embedding_text])[0]
            self.req_clusters_collection.upsert(
                ids=[doc_id], embeddings=[emb], documents=[embedding_text], metadatas=[meta]
            )
            return True
        except Exception as e:
            print(f"[VectorStore] upsert_cluster failed: {e}")
            return False

    def get_cluster(self, cluster_id: str) -> Optional[Dict]:
        try:
            result = self.req_clusters_collection.get(ids=[f"cluster_{cluster_id}"])
            if result and result["ids"]:
                return result["metadatas"][0]
        except Exception as e:
            print(f"[VectorStore] get_cluster failed: {e}")
        return None

    def list_clusters(self, status: str = None) -> List[Dict]:
        try:
            where = {"status": status} if status else None
            result = self.req_clusters_collection.get(where=where)
            clusters = result.get("metadatas", []) if result else []
            clusters.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return clusters
        except Exception as e:
            print(f"[VectorStore] list_clusters failed: {e}")
            return []

    def update_cluster_field(self, cluster_id: str, fields: Dict) -> bool:
        cluster = self.get_cluster(cluster_id)
        if cluster is None:
            return False
        cluster.update({
            k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v)
            for k, v in fields.items()
        })
        cluster["updated_at"] = datetime.now().isoformat()
        title = cluster.get("title", cluster_id)
        return self.upsert_cluster(cluster_id, title, cluster)

    def delete_cluster(self, cluster_id: str) -> bool:
        try:
            self.req_clusters_collection.delete(ids=[f"cluster_{cluster_id}"])
            return True
        except Exception as e:
            print(f"[VectorStore] delete_cluster failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Query cache
    # ------------------------------------------------------------------

    def save_query_cache(
        self,
        cache_key: str,
        query: str,
        content: str,
        context_keys: List[str],
        ttl_hours: int = 24,
    ) -> bool:
        if not self.query_cache:
            return False
        try:
            emb = self._embed_texts([query], is_query=True)[0]
            self.query_cache.upsert(
                ids=[cache_key],
                embeddings=[emb],
                documents=[content],
                metadatas=[{
                    "original_query": query,
                    "context_keys": json.dumps(context_keys),
                    "created_at": datetime.now().isoformat(),
                    "expire_at": (datetime.now() + timedelta(hours=ttl_hours)).isoformat(),
                    "hit_count": 0,
                }],
            )
            return True
        except Exception as e:
            print(f"[VectorStore] save_query_cache failed: {e}")
            return False

    def get_query_cache(self, cache_key: str) -> Optional[Dict]:
        if not self.query_cache:
            return None
        try:
            result = self.query_cache.get(ids=[cache_key])
            if not result or not result["ids"]:
                return None
            meta = result["metadatas"][0]
            expire_at = datetime.fromisoformat(meta.get("expire_at", "2000-01-01"))
            if datetime.now() > expire_at:
                return None
            # Update hit count
            meta["hit_count"] = int(meta.get("hit_count", 0)) + 1
            doc = result["documents"][0]
            emb = self._embed_texts([meta.get("original_query", doc)], is_query=True)[0]
            self.query_cache.upsert(
                ids=[cache_key], embeddings=[emb], documents=[doc], metadatas=[meta]
            )
            return {
                "content": doc,
                "original_query": meta.get("original_query", ""),
                "context_keys": json.loads(meta.get("context_keys", "[]")),
                "hit_count": meta["hit_count"],
                "created_at": meta.get("created_at", ""),
                "expire_at": meta.get("expire_at", ""),
            }
        except Exception as e:
            print(f"[VectorStore] get_query_cache failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Stats / cleanup
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict:
        return {
            "issues_count": self._safe_count(self.issues_collection),
            "analysis_count": self._safe_count(self.analysis_collection),
            "similarity_edges": self._safe_count(self.similarity_collection),
        }

    def cleanup_expired(self, dry_run: bool = True) -> int:
        """Count (and optionally delete) expired analysis cache entries."""
        try:
            result = self.analysis_collection.get(
                where={"expires_at": {"$lt": datetime.now().isoformat()}}
            )
            ids = result.get("ids", [])
            count = len(ids)
            if not dry_run and count > 0:
                self.analysis_collection.delete(ids)
            return count
        except Exception:
            return 0
