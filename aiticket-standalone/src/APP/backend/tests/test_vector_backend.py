"""Tests for services/vector_backend.py — TDD red→green."""
import sys
import json
import math

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "services"))

import pytest
from services.vector_backend import SqliteVecBackend, _where_to_sql


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_backend(tmp_path, collection="test_col", dim=4):
    return SqliteVecBackend(tmp_path / "test.db", collection=collection, dim=dim)


def unit_vec(idx, dim=4):
    """Return a unit vector with 1.0 at position idx."""
    v = [0.0] * dim
    v[idx] = 1.0
    return v


# ---------------------------------------------------------------------------
# _where_to_sql unit tests
# ---------------------------------------------------------------------------

def test_where_to_sql_equality():
    sql, params = _where_to_sql({"source_kind": "kb_local"})
    assert "json_extract" in sql
    assert "= ?" in sql
    assert params == ["kb_local"]


def test_where_to_sql_gte():
    sql, params = _where_to_sql({"confidence": {"$gte": 0.8}})
    assert ">= ?" in sql
    assert params == [0.8]


def test_where_to_sql_in():
    sql, params = _where_to_sql({"kind": {"$in": ["a", "b"]}})
    assert "IN (?,?)" in sql
    assert params == ["a", "b"]


def test_where_to_sql_multiple_keys():
    sql, params = _where_to_sql({"a": "x", "b": {"$gt": 5}})
    assert "AND" in sql
    assert len(params) == 2


# ---------------------------------------------------------------------------
# Basic upsert / count / get
# ---------------------------------------------------------------------------

def test_upsert_and_count(tmp_path):
    b = make_backend(tmp_path)
    b.upsert(
        ids=["id1", "id2"],
        embeddings=[unit_vec(0), unit_vec(1)],
        documents=["doc1", "doc2"],
        metadatas=[{"k": "v1"}, {"k": "v2"}],
    )
    assert b.count() == 2


def test_upsert_replace(tmp_path):
    b = make_backend(tmp_path)
    b.upsert(["id1"], [unit_vec(0)], ["old"], [{"x": 1}])
    b.upsert(["id1"], [unit_vec(0)], ["new"], [{"x": 2}])
    assert b.count() == 1
    result = b.get(ids=["id1"])
    assert result["documents"][0] == "new"
    assert result["metadatas"][0]["x"] == 2


def test_get_by_ids(tmp_path):
    b = make_backend(tmp_path)
    b.upsert(["a", "b"], [unit_vec(0), unit_vec(1)], ["doc_a", "doc_b"], [{}, {}])
    result = b.get(ids=["a"])
    assert result["ids"] == ["a"]
    assert result["documents"] == ["doc_a"]


def test_get_with_where_filter(tmp_path):
    b = make_backend(tmp_path)
    b.upsert(
        ["a", "b"],
        [unit_vec(0), unit_vec(1)],
        ["doc_a", "doc_b"],
        [{"kind": "x"}, {"kind": "y"}],
    )
    result = b.get(where={"kind": "x"})
    assert result["ids"] == ["a"]


def test_delete(tmp_path):
    b = make_backend(tmp_path)
    b.upsert(["a", "b"], [unit_vec(0), unit_vec(1)], ["d1", "d2"], [{}, {}])
    b.delete(["a"])
    assert b.count() == 1
    result = b.get(ids=["a"])
    assert result["ids"] == []


def test_count_excludes_meta_sentinel(tmp_path):
    b = make_backend(tmp_path)
    assert b.count() == 0  # __meta__ row should not be counted


# ---------------------------------------------------------------------------
# KNN query
# ---------------------------------------------------------------------------

def test_query_returns_nearest(tmp_path):
    b = make_backend(tmp_path, dim=4)
    b.upsert(
        ["near", "far"],
        [unit_vec(0), unit_vec(3)],
        ["near doc", "far doc"],
        [{}, {}],
    )
    results = b.query(unit_vec(0), n=2)
    assert results["ids"][0][0] == "near"
    assert results["distances"][0][0] < results["distances"][0][1]


def test_query_score_cosine_distance(tmp_path):
    """Identical vector → distance 0; orthogonal → distance > 0."""
    b = make_backend(tmp_path, dim=4)
    b.upsert(["same"], [unit_vec(0)], ["doc"], [{}])
    results = b.query(unit_vec(0), n=1)
    assert abs(results["distances"][0][0]) < 1e-5


def test_query_with_where_filter(tmp_path):
    b = make_backend(tmp_path, dim=4)
    b.upsert(
        ["a", "b"],
        [unit_vec(0), unit_vec(0)],
        ["doc_a", "doc_b"],
        [{"source_kind": "kb_local"}, {"source_kind": "kb_auto"}],
    )
    results = b.query(unit_vec(0), n=5, where={"source_kind": "kb_local"})
    assert results["ids"][0] == ["a"]


def test_query_with_gte_filter(tmp_path):
    b = make_backend(tmp_path, dim=4)
    b.upsert(
        ["hi", "lo"],
        [unit_vec(0), unit_vec(0)],
        ["high conf", "low conf"],
        [{"confidence": 0.9}, {"confidence": 0.3}],
    )
    results = b.query(unit_vec(0), n=5, where={"confidence": {"$gte": 0.8}})
    assert results["ids"][0] == ["hi"]


def test_query_empty_collection(tmp_path):
    b = make_backend(tmp_path, dim=4)
    results = b.query(unit_vec(0), n=5)
    assert results["ids"] == [[]]


# ---------------------------------------------------------------------------
# dim mismatch → reset
# ---------------------------------------------------------------------------

def test_dim_mismatch_triggers_reset(tmp_path):
    db = tmp_path / "shared.db"
    # Create with dim=4, insert data
    b1 = SqliteVecBackend(db, "col", dim=4)
    b1.upsert(["x"], [unit_vec(0, 4)], ["doc"], [{}])
    assert b1.count() == 1

    # Reopen with different dim — should reset (data gone)
    b2 = SqliteVecBackend(db, "col", dim=8)
    assert b2.count() == 0
    assert b2.dim == 8


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------

def test_reset_clears_data(tmp_path):
    b = make_backend(tmp_path)
    b.upsert(["a"], [unit_vec(0)], ["doc"], [{}])
    b.reset()
    assert b.count() == 0


def test_reset_allows_new_inserts(tmp_path):
    b = make_backend(tmp_path)
    b.upsert(["a"], [unit_vec(0)], ["doc"], [{}])
    b.reset()
    b.upsert(["b"], [unit_vec(1)], ["doc2"], [{}])
    assert b.count() == 1


# ---------------------------------------------------------------------------
# Multiple collections in same db file
# ---------------------------------------------------------------------------

def test_two_collections_isolated(tmp_path):
    db = tmp_path / "shared.db"
    col_a = SqliteVecBackend(db, "col_a", dim=4)
    col_b = SqliteVecBackend(db, "col_b", dim=4)
    col_a.upsert(["x"], [unit_vec(0)], ["in_a"], [{}])
    assert col_a.count() == 1
    assert col_b.count() == 0


# ---------------------------------------------------------------------------
# score = 1 - distance convention (checked by callers)
# ---------------------------------------------------------------------------

def test_score_from_distance(tmp_path):
    b = make_backend(tmp_path, dim=4)
    b.upsert(["v"], [unit_vec(0)], ["doc"], [{}])
    results = b.query(unit_vec(0), n=1)
    distance = results["distances"][0][0]
    score = 1.0 - distance
    assert score >= 0.99  # identical → ~1.0
