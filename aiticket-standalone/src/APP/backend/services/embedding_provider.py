"""Unified embedding provider with three-tier fallback.

Tier selection via env EMBEDDING_PROVIDER (fastembed|api|hash, default fastembed).
Auto-degrades: fastembed -> api -> hash.
"""
from __future__ import annotations

import hashlib
import math
import os
from collections import Counter
from typing import Optional
import re
import logging

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"[一-鿿]{2,}|[A-Za-z0-9_-]{2,}")


class EmbeddingProvider:
    """Base class / protocol for all embedding providers."""
    dim: int = 0

    def embed(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        raise NotImplementedError


class HashEmbeddingProvider(EmbeddingProvider):
    """Deterministic offline embedding (96-dim bag-of-tokens hash).

    Replicates the LocalHashEmbeddingFunction logic from kb_hybrid_index.
    Prefix-agnostic (is_query has no effect).
    """
    dim: int = 96

    def __init__(self, dimensions: int = 96) -> None:
        self.dim = dimensions

    def embed(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        counts = Counter(token.lower() for token in TOKEN_RE.findall(text or ""))
        if not counts:
            return vector
        for token, weight in counts.items():
            vector[hash(token) % self.dim] += float(weight)
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector


class FastEmbedProvider(EmbeddingProvider):
    """fastembed TextEmbedding with multilingual-e5 (dim=384).

    Uses sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 which is
    available in fastembed 0.8.0 and produces 384-dim embeddings.

    The e5 query/passage prefix convention is applied when is_query differs:
      is_query=True  -> prepend "query: "
      is_query=False -> prepend "passage: "
    (prefix is a no-op for the MiniLM model but kept for future e5 compat)
    """
    dim: int = 384
    # fastembed 0.8.0 supports this model; intfloat/multilingual-e5-small is not listed
    MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # type: ignore
        self._model = TextEmbedding(model_name=self.MODEL_NAME)

    def embed(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        prefix = "query: " if is_query else "passage: "
        prefixed = [prefix + t for t in texts]
        # fastembed returns a generator of np.ndarray
        return [vec.tolist() for vec in self._model.embed(prefixed)]


class ApiEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible /embeddings endpoint.

    Reads llm_config.json for current provider's base_url / api_key.
    Falls back to env vars OPENAI_BASE_URL / OPENAI_API_KEY if config absent.
    """
    dim: int = 1536  # openai text-embedding-3-small; overridden by first call

    def __init__(self) -> None:
        import httpx  # type: ignore
        self._httpx = httpx

        cfg = self._load_config()
        self._base_url = (cfg.get("base_url") or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
        self._api_key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        self._model = cfg.get("embedding_model") or "text-embedding-3-small"

    def embed(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        resp = self._httpx.post(
            f"{self._base_url}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            json={"input": texts, "model": self._model},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        vecs = [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]
        if vecs:
            self.dim = len(vecs[0])
        return vecs

    @staticmethod
    def _load_config() -> dict:
        """Try to read current provider config from llm_config.json."""
        try:
            import json
            from pathlib import Path
            candidates = [
                Path(__file__).parent.parent / "data" / "llm_config.json",
                Path(__file__).parent.parent / "llm_config.json",
            ]
            for p in candidates:
                if p.exists():
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    # Support {"providers": [...], "active": "name"} or flat dict
                    if "providers" in raw and "active" in raw:
                        active = raw["active"]
                        for prov in raw["providers"]:
                            if prov.get("name") == active:
                                return prov
                    return raw
        except Exception:
            pass
        return {}


def get_embedding_provider(provider_hint: Optional[str] = None) -> EmbeddingProvider:
    """Factory: env EMBEDDING_PROVIDER or provider_hint; auto-degrades.

    Order: fastembed -> api -> hash
    """
    wanted = (provider_hint or os.environ.get("EMBEDDING_PROVIDER", "fastembed")).lower()

    if wanted == "hash":
        return HashEmbeddingProvider()

    if wanted == "api":
        try:
            return ApiEmbeddingProvider()
        except Exception as e:
            logger.warning("[EmbeddingProvider] api init failed, falling back to hash: %s", e)
            return HashEmbeddingProvider()

    # default: fastembed -> api -> hash
    if wanted == "fastembed":
        try:
            return FastEmbedProvider()
        except Exception as e:
            logger.warning("[EmbeddingProvider] fastembed init failed: %s — trying api", e)
            try:
                return ApiEmbeddingProvider()
            except Exception as e2:
                logger.warning("[EmbeddingProvider] api init also failed: %s — using hash", e2)
                return HashEmbeddingProvider()

    logger.warning("[EmbeddingProvider] unknown provider '%s', using hash", wanted)
    return HashEmbeddingProvider()
