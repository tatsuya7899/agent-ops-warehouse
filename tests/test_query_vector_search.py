"""Tests for api.query -- VECTOR_SEARCH SQL string construction (RAG API
phase 3, checkpoint 1).

SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.3 (VECTOR_SEARCH,
brute-force, distance_type COSINE) / Section 6-5 (test policy: this is SQL
string assembly only -- no real BigQuery connection anywhere in this file).
"""
from __future__ import annotations

import pytest

from api.query import build_vector_search_sql


def test_includes_vector_search_call_and_fully_qualified_table():
    sql = build_vector_search_sql(
        project="agent-ops-warehouse",
        dataset="raw",
        embedding=[0.1, 0.2, 0.3],
        top_k=5,
    )

    assert "VECTOR_SEARCH(" in sql
    assert "`agent-ops-warehouse.raw.article_chunks`" in sql


def test_embedding_values_are_inlined_as_a_float_array_literal():
    sql = build_vector_search_sql(
        project="p",
        dataset="raw",
        embedding=[0.1, 0.25, -0.5],
        top_k=3,
    )

    assert "[0.1, 0.25, -0.5]" in sql


def test_includes_top_k_and_cosine_distance_type_by_default():
    sql = build_vector_search_sql(
        project="p",
        dataset="raw",
        embedding=[0.1],
        top_k=7,
    )

    assert "top_k => 7" in sql
    assert "distance_type => 'COSINE'" in sql


def test_distance_type_is_overridable():
    sql = build_vector_search_sql(
        project="p",
        dataset="raw",
        embedding=[0.1],
        top_k=1,
        distance_type="EUCLIDEAN",
    )

    assert "distance_type => 'EUCLIDEAN'" in sql
    assert "distance_type => 'COSINE'" not in sql


def test_selects_the_columns_the_api_response_needs():
    sql = build_vector_search_sql(
        project="p",
        dataset="raw",
        embedding=[0.1],
        top_k=1,
    )

    for column in (
        "chunk_id",
        "filename",
        "article_title",
        "section_title",
        "chunk_text",
        "distance",
    ):
        assert column in sql


def test_table_and_embedding_column_are_overridable():
    sql = build_vector_search_sql(
        project="p",
        dataset="raw",
        embedding=[0.1],
        top_k=1,
        table="other_chunks",
        embedding_column="vec",
    )

    assert "`p.raw.other_chunks`" in sql
    assert "'vec'" in sql


def test_raises_valueerror_on_empty_embedding():
    with pytest.raises(ValueError, match="embedding"):
        build_vector_search_sql(project="p", dataset="raw", embedding=[], top_k=5)


@pytest.mark.parametrize("bad_top_k", [0, -1, -5])
def test_raises_valueerror_on_non_positive_top_k(bad_top_k):
    with pytest.raises(ValueError, match="top_k"):
        build_vector_search_sql(project="p", dataset="raw", embedding=[0.1], top_k=bad_top_k)


def test_never_executes_anything_pure_string_only():
    # Regression guard for Section 6-5's "実際のBigQuery接続はしない" constraint:
    # build_vector_search_sql must be a pure function returning str, not a
    # BigQuery client call. If this ever starts requiring network access,
    # this test (and every other test in this file) would need a live
    # connection to pass -- it doesn't, which is the point.
    sql = build_vector_search_sql(project="p", dataset="raw", embedding=[0.1], top_k=1)
    assert isinstance(sql, str)
