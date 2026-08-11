"""Build the VECTOR_SEARCH SQL string for the RAG API's /query endpoint
(RAG API phase 3 -- SPEC-agent-ops-warehouse-rag-api_20260811.md Section
4.3 / 4.4 / 6-5).

This module never opens a BigQuery connection: build_vector_search_sql is
a pure function (embedding vector in, SQL text out), mirroring
loader.bq_merge.build_merge_sql's plan/execute separation -- api.main
(checkpoint 2/3) is where the returned SQL is actually handed to a
BigQuery client, and only that wiring is mocked in tests, never this
string-building function itself.
"""
from __future__ import annotations

BQ_TABLE = "article_chunks"
EMBEDDING_COLUMN = "embedding"
DISTANCE_TYPE = "COSINE"

# raw_article_chunks columns surfaced to the API response (SPEC Section
# 4.4: article title, URL/filename, body excerpt, similarity score). Kept
# as an explicit tuple, not re-derived from
# scripts.build_embeddings.SCHEMA_FIELDS, because the API intentionally
# omits embedding/loaded_at/published_date -- SELECTing every schema
# column here would silently start returning the raw embedding vector
# over the wire the day that upstream tuple grows.
RESULT_COLUMNS: tuple[str, ...] = (
    "chunk_id",
    "filename",
    "article_title",
    "section_title",
    "chunk_text",
)


def build_vector_search_sql(
    project: str,
    dataset: str,
    embedding: list[float],
    top_k: int,
    table: str = BQ_TABLE,
    embedding_column: str = EMBEDDING_COLUMN,
    distance_type: str = DISTANCE_TYPE,
) -> str:
    """Build a brute-force VECTOR_SEARCH query (SPEC Section 4.3: no
    vector index -- the corpus is far below BigQuery's 5,000-row minimum
    for one). Returns SQL text only; never executes anything (Section
    6-5).

    The query embedding is inlined as a literal FLOAT64 array in a
    `(SELECT [...] AS embedding_column)` subquery -- the documented
    VECTOR_SEARCH shape for a single ad hoc query vector, as opposed to
    a query *table* for batch search (not needed here: one question in,
    one SQL statement out, SPEC Section 2's "1リクエスト1質問").
    top_k/distance_type are injected as literals, not query parameters,
    because both are internal config validated by the caller (api.main
    checkpoint 2 rejects a non-positive top_k with 400 before this
    function is ever called) -- not user-supplied SQL text.
    """
    if not embedding:
        raise ValueError("embedding must be a non-empty list of floats")
    if top_k <= 0:
        raise ValueError(f"top_k must be a positive integer, got {top_k!r}")

    table_ref = f"`{project}.{dataset}.{table}`"
    embedding_literal = f"[{', '.join(repr(v) for v in embedding)}]"
    select_columns = ",\n  ".join(f"base.{column}" for column in RESULT_COLUMNS)

    return (
        "SELECT\n"
        f"  {select_columns},\n"
        "  distance\n"
        "FROM VECTOR_SEARCH(\n"
        f"  TABLE {table_ref},\n"
        f"  '{embedding_column}',\n"
        f"  (SELECT {embedding_literal} AS {embedding_column}),\n"
        f"  top_k => {top_k},\n"
        f"  distance_type => '{distance_type}')"
    )
