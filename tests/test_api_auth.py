"""Tests for POST /query Bearer-token authentication (RAG API phase 3,
checkpoint 2).

SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.4 (single static
Bearer token against the API_TOKEN env var) / Section 6-6 (token missing
401, wrong token 401, correct token 200). BigQuery is never reached here:
api.main.execute_query is monkeypatched to a stub so these tests only
exercise the auth dependency, not retrieval (Section 6-6/checkpoint 2
scope).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api import main
from api.main import QueryResponse, app

client = TestClient(app)


def _stub_execute_query(question: str, top_k: int, summarize: bool) -> QueryResponse:
    return QueryResponse(results=[])


def test_missing_authorization_header_returns_401(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "correct-token")

    response = client.post("/query", json={"question": "何か質問"})

    assert response.status_code == 401


def test_wrong_token_returns_401(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "correct-token")

    response = client.post(
        "/query",
        json={"question": "何か質問"},
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


def test_non_bearer_authorization_header_returns_401(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "correct-token")

    response = client.post(
        "/query",
        json={"question": "何か質問"},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )

    assert response.status_code == 401


def test_correct_token_returns_401_is_false_and_reaches_the_handler(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "correct-token")
    monkeypatch.setattr(main, "execute_query", _stub_execute_query)

    response = client.post(
        "/query",
        json={"question": "何か質問"},
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200


def test_api_token_unset_rejects_every_bearer_value(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)

    response = client.post(
        "/query",
        json={"question": "何か質問"},
        headers={"Authorization": "Bearer anything"},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# API_TOKEN whitespace stripping (Phase 3.5 / SPEC Section 9 phase 3.5:
# "Secret Manager登録時の空白混入事故対策"). The Bearer header side already
# strips (`token = authorization.removeprefix("Bearer ").strip()`) -- these
# tests cover the expected/env-var side, which previously did not.
# ---------------------------------------------------------------------------


def test_api_token_with_surrounding_whitespace_still_authenticates(monkeypatch):
    monkeypatch.setattr(main, "execute_query", _stub_execute_query)
    # Simulates a Secret Manager value accidentally saved with a trailing
    # newline/leading space -- the client still sends the clean token.
    monkeypatch.setenv("API_TOKEN", "  correct-token\n")

    response = client.post(
        "/query",
        json={"question": "何か質問"},
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200


def test_api_token_all_whitespace_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "   \n")

    response = client.post(
        "/query",
        json={"question": "何か質問"},
        headers={"Authorization": "Bearer anything"},
    )

    assert response.status_code == 401
