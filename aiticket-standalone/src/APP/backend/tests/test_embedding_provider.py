"""Tests for services/embedding_provider.py — TDD red→green."""
import os
import sys

# Ensure backend root is on path
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "services"))

import pytest
from services.embedding_provider import (
    HashEmbeddingProvider,
    FastEmbedProvider,
    get_embedding_provider,
)


# ---------------------------------------------------------------------------
# HashEmbeddingProvider
# ---------------------------------------------------------------------------

def test_hash_provider_returns_correct_dim():
    p = HashEmbeddingProvider()
    vecs = p.embed(["hello world"])
    assert len(vecs) == 1
    assert len(vecs[0]) == 96


def test_hash_provider_is_deterministic():
    p = HashEmbeddingProvider()
    v1 = p.embed(["流程监控测试"])
    v2 = p.embed(["流程监控测试"])
    assert v1 == v2


def test_hash_provider_different_texts_differ():
    p = HashEmbeddingProvider()
    v1 = p.embed(["apple"])
    v2 = p.embed(["orange"])
    assert v1 != v2


def test_hash_provider_empty_text():
    p = HashEmbeddingProvider()
    vecs = p.embed([""])
    assert len(vecs[0]) == 96
    # empty text → zero vector
    assert all(x == 0.0 for x in vecs[0])


def test_hash_provider_is_query_has_no_effect():
    """Hash provider ignores is_query (no prefix logic)."""
    p = HashEmbeddingProvider()
    v_passage = p.embed(["test text"], is_query=False)
    v_query = p.embed(["test text"], is_query=True)
    assert v_passage == v_query


def test_hash_provider_unit_norm():
    """Non-empty vectors should be unit-normalized."""
    import math
    p = HashEmbeddingProvider()
    v = p.embed(["some content"])[0]
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-5


def test_hash_provider_batch():
    p = HashEmbeddingProvider()
    vecs = p.embed(["text a", "text b", "text c"])
    assert len(vecs) == 3
    assert all(len(v) == 96 for v in vecs)


# ---------------------------------------------------------------------------
# FastEmbedProvider — marked slow/network (downloads model on first run)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_fastembed_provider_dim():
    p = FastEmbedProvider()
    vecs = p.embed(["hello world"])
    assert len(vecs) == 1
    assert len(vecs[0]) == 384


@pytest.mark.slow
def test_fastembed_e5_query_prefix():
    """query: prefix should shift the embedding relative to passage: prefix."""
    p = FastEmbedProvider()
    v_passage = p.embed(["flow monitoring"], is_query=False)[0]
    v_query = p.embed(["flow monitoring"], is_query=True)[0]
    # They should be different (prefix changes embedding)
    assert v_passage != v_query


@pytest.mark.slow
def test_fastembed_returns_floats():
    p = FastEmbedProvider()
    vecs = p.embed(["测试嵌入"])
    assert isinstance(vecs[0][0], float)


@pytest.mark.slow
def test_fastembed_batch_same_dim():
    p = FastEmbedProvider()
    vecs = p.embed(["text a", "text b"])
    assert len(vecs) == 2
    assert len(vecs[0]) == len(vecs[1]) == 384


# ---------------------------------------------------------------------------
# Factory / degradation
# ---------------------------------------------------------------------------

def test_factory_hash_explicit():
    p = get_embedding_provider("hash")
    assert isinstance(p, HashEmbeddingProvider)
    assert p.dim == 96


def test_factory_env_hash(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")
    p = get_embedding_provider()
    assert isinstance(p, HashEmbeddingProvider)


def test_factory_unknown_falls_back_to_hash():
    p = get_embedding_provider("nonexistent_provider_xyz")
    assert isinstance(p, HashEmbeddingProvider)


def test_factory_fastembed_degrades_to_hash_if_import_fails(monkeypatch):
    """Simulate fastembed import failure — should degrade to hash."""
    import services.embedding_provider as ep_mod

    original = ep_mod.FastEmbedProvider

    class BrokenFastEmbed(ep_mod.FastEmbedProvider):
        def __init__(self):
            raise ImportError("simulated fastembed missing")

    monkeypatch.setattr(ep_mod, "FastEmbedProvider", BrokenFastEmbed)
    monkeypatch.setattr(ep_mod, "ApiEmbeddingProvider", type(
        "BrokenApi", (), {"__init__": lambda s: (_ for _ in ()).throw(ImportError("no api"))}
    ))
    p = ep_mod.get_embedding_provider("fastembed")
    assert isinstance(p, ep_mod.HashEmbeddingProvider)
    monkeypatch.setattr(ep_mod, "FastEmbedProvider", original)
