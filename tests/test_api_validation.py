"""Tests for POST /query request-body validation (RAG API phase 3,
checkpoint 2).

SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.4 ("黙って200を
返さない" -- empty question / bad top_k are explicit 400s, not silently
coerced) / Section 6-7. Auth always uses a correct token here so these
tests isolate validation from Section 6-6's auth cases; api.main.execute_query
is monkeypatched so no request reaches BigQuery.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import main
from api.main import QueryResponse, app

client = TestClient(app)
AUTH_HEADERS = {"Authorization": "Bearer correct-token"}


def _stub_execute_query(question: str, top_k: int, summarize: bool) -> QueryResponse:
    return QueryResponse(results=[])


@pytest.fixture(autouse=True)
def _api_token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "correct-token")
    monkeypatch.setattr(main, "execute_query", _stub_execute_query)


def test_empty_question_returns_400():
    response = client.post("/query", json={"question": ""}, headers=AUTH_HEADERS)

    assert response.status_code == 400


def test_whitespace_only_question_returns_400():
    response = client.post("/query", json={"question": "   "}, headers=AUTH_HEADERS)

    assert response.status_code == 400


@pytest.mark.parametrize("bad_top_k", [0, -1, -100])
def test_non_positive_top_k_returns_400(bad_top_k):
    response = client.post(
        "/query",
        json={"question": "何か質問", "top_k": bad_top_k},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 400


def test_missing_question_field_returns_422_not_500():
    # Pydantic's own required-field check, not our 400 -- distinguishing
    # "malformed request" (422) from "well-formed but empty" (400) is a
    # deliberate line, not an oversight (SPEC Section 4.4's error modes are
    # 400/401/503; FastAPI's automatic 422 sits alongside them, never a 500).
    response = client.post("/query", json={}, headers=AUTH_HEADERS)

    assert response.status_code == 422


def test_valid_question_and_default_top_k_returns_200():
    response = client.post("/query", json={"question": "何か質問"}, headers=AUTH_HEADERS)

    assert response.status_code == 200


def test_valid_question_and_positive_top_k_returns_200():
    response = client.post(
        "/query",
        json={"question": "何か質問", "top_k": 3},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
