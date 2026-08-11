"""Tests for POST /query's 503 error path (RAG API phase 3, checkpoint 3).

SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.4: "BigQuery接続失
敗をそれぞれ明確なHTTPステータス...で返す。黙って200を返さない". These are
HTTP-layer tests (via TestClient) confirming the route handler maps
api.main.RetrievalError to 503 -- the underlying embedding/BigQuery calls
are monkeypatched at the same seams as tests/test_api_retrieval.py, so no
real network call happens here either.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api import main
from api.main import RetrievalError, app

client = TestClient(app)
AUTH_HEADERS = {"Authorization": "Bearer correct-token"}


def _autouse_env(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "correct-token")
    monkeypatch.setenv("GCP_PROJECT_ID", "agent-ops-warehouse")
    monkeypatch.setenv("BQ_DATASET", "raw")


def test_bigquery_connection_failure_returns_503(monkeypatch):
    _autouse_env(monkeypatch)

    def failing_execute(question, top_k, summarize):
        raise RetrievalError("BigQuery VECTOR_SEARCH failed: connection refused")

    monkeypatch.setattr(main, "execute_query", failing_execute)

    response = client.post("/query", json={"question": "質問"}, headers=AUTH_HEADERS)

    assert response.status_code == 503


def test_embedding_api_failure_returns_503(monkeypatch):
    _autouse_env(monkeypatch)

    def failing_execute(question, top_k, summarize):
        raise RetrievalError("embedding call failed: 429")

    monkeypatch.setattr(main, "execute_query", failing_execute)

    response = client.post("/query", json={"question": "質問"}, headers=AUTH_HEADERS)

    assert response.status_code == 503


def test_503_response_never_looks_like_a_silent_200(monkeypatch):
    # Regression guard for SPEC 4.4's "黙って200を返さない": the body must
    # carry an explicit error detail, not an empty/placeholder success shape.
    _autouse_env(monkeypatch)

    def failing_execute(question, top_k, summarize):
        raise RetrievalError("BigQuery VECTOR_SEARCH failed: timeout")

    monkeypatch.setattr(main, "execute_query", failing_execute)

    response = client.post("/query", json={"question": "質問"}, headers=AUTH_HEADERS)

    body = response.json()
    assert "results" not in body
    assert "detail" in body


def test_validation_still_runs_before_retrieval_on_401_free_path(monkeypatch):
    # 400 (empty question) must still win over a would-be 503 -- validation
    # happens before execute_query is ever called (checkpoint 2 contract
    # unchanged by checkpoint 3's wiring).
    _autouse_env(monkeypatch)

    def unexpected_execute(question, top_k, summarize):
        raise AssertionError("execute_query must not run for an invalid request")

    monkeypatch.setattr(main, "execute_query", unexpected_execute)

    response = client.post("/query", json={"question": ""}, headers=AUTH_HEADERS)

    assert response.status_code == 400
