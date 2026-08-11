"""Tests for api.main's BigQuery/Gemini wiring behind POST /query (RAG API
phase 3, checkpoint 3).

SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.3/4.4 (VECTOR_SEARCH
wiring, response shape: article title/URL/excerpt/similarity score,
summarize=true) / Section 6-5 continuation. Every Gemini/BigQuery call is
monkeypatched at the function-factory boundary (embed_question,
run_vector_search_query, summarize_answer's client factory) -- no real
network call anywhere in this file (task constraint: "実際のBigQuery・
Gemini APIへの接続は一切しない")."""
from __future__ import annotations

import pytest

from api import main
from api.main import ChunkResult, RetrievalError, execute_query


@pytest.fixture(autouse=True)
def _bq_config(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "agent-ops-warehouse")
    monkeypatch.setenv("BQ_DATASET", "raw")


# ---------------------------------------------------------------------------
# execute_query wiring: embed_question -> build_vector_search_sql ->
# run_vector_search_query -> response rows
# ---------------------------------------------------------------------------


def test_execute_query_passes_embedding_and_top_k_into_the_sql(monkeypatch):
    captured_sql: list[str] = []

    monkeypatch.setattr(main, "embed_question", lambda question: [0.1, 0.2, 0.3])

    def fake_run(sql: str) -> list[dict]:
        captured_sql.append(sql)
        return []

    monkeypatch.setattr(main, "run_vector_search_query", fake_run)

    execute_query("AIエージェント統治とは", top_k=3, summarize=False)

    assert len(captured_sql) == 1
    assert "top_k => 3" in captured_sql[0]
    assert "[0.1, 0.2, 0.3]" in captured_sql[0]
    assert "`agent-ops-warehouse.raw.article_chunks`" in captured_sql[0]


def test_execute_query_maps_bigquery_rows_to_chunk_results(monkeypatch):
    monkeypatch.setattr(main, "embed_question", lambda question: [0.1])
    monkeypatch.setattr(
        main,
        "run_vector_search_query",
        lambda sql: [
            {
                "chunk_id": "20260703_a__001",
                "filename": "20260703_a.md",
                "article_title": "AI組織を『憲法』で統治する設計と実装",
                "section_title": "見出し",
                "chunk_text": "見出し\n\n本文抜粋。",
                "distance": 0.2,
            }
        ],
    )

    response = execute_query("質問", top_k=5, summarize=False)

    assert len(response.results) == 1
    result = response.results[0]
    assert isinstance(result, ChunkResult)
    assert result.article_title == "AI組織を『憲法』で統治する設計と実装"
    assert result.url == "20260703_a.md"  # no filename->note.com URL mapping exists (SPEC 4.4)
    assert result.excerpt == "見出し\n\n本文抜粋。"
    assert result.similarity_score == pytest.approx(0.8)  # 1 - distance (COSINE)


def test_execute_query_keeps_top_k_result_order_from_bigquery(monkeypatch):
    monkeypatch.setattr(main, "embed_question", lambda question: [0.1])
    monkeypatch.setattr(
        main,
        "run_vector_search_query",
        lambda sql: [
            {
                "chunk_id": f"c{i}",
                "filename": f"f{i}.md",
                "article_title": f"title{i}",
                "section_title": "s",
                "chunk_text": "t",
                "distance": i * 0.1,
            }
            for i in range(3)
        ],
    )

    response = execute_query("質問", top_k=3, summarize=False)

    assert [r.article_title for r in response.results] == ["title0", "title1", "title2"]


def test_execute_query_returns_empty_results_without_error_when_bigquery_finds_nothing(
    monkeypatch,
):
    monkeypatch.setattr(main, "embed_question", lambda question: [0.1])
    monkeypatch.setattr(main, "run_vector_search_query", lambda sql: [])

    response = execute_query("無関係な質問", top_k=5, summarize=False)

    assert response.results == []
    assert response.answer is None


# ---------------------------------------------------------------------------
# Failure paths -> RetrievalError (route handler maps this to 503, tested
# at the HTTP layer in tests/test_api_503.py)
# ---------------------------------------------------------------------------


def test_execute_query_wraps_embedding_failure_in_retrieval_error(monkeypatch):
    def failing_embed(question: str):
        raise RuntimeError("network down")

    monkeypatch.setattr(main, "embed_question", failing_embed)

    with pytest.raises(RetrievalError):
        execute_query("質問", top_k=5, summarize=False)


def test_execute_query_wraps_bigquery_failure_in_retrieval_error(monkeypatch):
    monkeypatch.setattr(main, "embed_question", lambda question: [0.1])

    def failing_search(sql: str):
        raise RuntimeError("bigquery connection refused")

    monkeypatch.setattr(main, "run_vector_search_query", failing_search)

    with pytest.raises(RetrievalError):
        execute_query("質問", top_k=5, summarize=False)


def test_execute_query_raises_retrieval_error_when_project_env_missing(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)

    with pytest.raises(RetrievalError):
        execute_query("質問", top_k=5, summarize=False)


# ---------------------------------------------------------------------------
# summarize=true (SPEC Section 4.4: optional 1-2 sentence Gemini summary)
# ---------------------------------------------------------------------------


def test_summarize_true_calls_summarize_answer_and_fills_answer_field(monkeypatch):
    monkeypatch.setattr(main, "embed_question", lambda question: [0.1])
    monkeypatch.setattr(
        main,
        "run_vector_search_query",
        lambda sql: [
            {
                "chunk_id": "c1",
                "filename": "f.md",
                "article_title": "t",
                "section_title": "s",
                "chunk_text": "本文。",
                "distance": 0.1,
            }
        ],
    )
    calls: list[tuple] = []

    def fake_summarize(question: str, results: list[ChunkResult]) -> str:
        calls.append((question, results))
        return "これは要約です。"

    monkeypatch.setattr(main, "summarize_answer", fake_summarize)

    response = execute_query("質問文", top_k=5, summarize=True)

    assert response.answer == "これは要約です。"
    assert len(calls) == 1
    assert calls[0][0] == "質問文"


def test_summarize_false_never_calls_summarize_answer(monkeypatch):
    monkeypatch.setattr(main, "embed_question", lambda question: [0.1])
    monkeypatch.setattr(
        main,
        "run_vector_search_query",
        lambda sql: [
            {
                "chunk_id": "c1",
                "filename": "f.md",
                "article_title": "t",
                "section_title": "s",
                "chunk_text": "本文。",
                "distance": 0.1,
            }
        ],
    )

    def unexpected_summarize(question: str, results: list[ChunkResult]) -> str:
        raise AssertionError("summarize_answer must not be called when summarize=False")

    monkeypatch.setattr(main, "summarize_answer", unexpected_summarize)

    response = execute_query("質問", top_k=5, summarize=False)

    assert response.answer is None


def test_summarize_true_with_no_results_skips_summarize_answer_call(monkeypatch):
    monkeypatch.setattr(main, "embed_question", lambda question: [0.1])
    monkeypatch.setattr(main, "run_vector_search_query", lambda sql: [])

    def unexpected_summarize(question: str, results: list[ChunkResult]) -> str:
        raise AssertionError("nothing to summarize when there are zero results")

    monkeypatch.setattr(main, "summarize_answer", unexpected_summarize)

    response = execute_query("無関係な質問", top_k=5, summarize=True)

    assert response.results == []
    assert response.answer is None


def test_summarize_failure_wraps_in_retrieval_error(monkeypatch):
    monkeypatch.setattr(main, "embed_question", lambda question: [0.1])
    monkeypatch.setattr(
        main,
        "run_vector_search_query",
        lambda sql: [
            {
                "chunk_id": "c1",
                "filename": "f.md",
                "article_title": "t",
                "section_title": "s",
                "chunk_text": "本文。",
                "distance": 0.1,
            }
        ],
    )

    def failing_summarize(question: str, results: list[ChunkResult]) -> str:
        raise RuntimeError("gemini quota exceeded")

    monkeypatch.setattr(main, "summarize_answer", failing_summarize)

    with pytest.raises(RetrievalError):
        execute_query("質問", top_k=5, summarize=True)
