"""Tests for POST /query's daily request-count limit (RAG API phase 3.6 --
SPEC-agent-ops-warehouse-rag-api_20260811.md Section 9 phase 3.6 / Section
8 risk table: "日次リクエスト数の上限100件(インメモリカウンタ・
max_instance_count=1により単一インスタンスなので正しく機能する)").

require_daily_request_limit is an in-memory, module-level counter keyed by
UTC calendar date (api.main._daily_request_counts /
api.main._today_utc_key) -- these tests never sleep or wait for a real day
to roll over; they either call require_daily_request_limit directly with
_today_utc_key monkeypatched to move the "day", or pre-seed
_daily_request_counts directly to a boundary count. Every test resets
_daily_request_counts to a fresh dict first so it never leaks into (or
inherits from) counts left behind by other test modules that also hit
POST /query (test_api_auth.py, test_api_validation.py, test_api_503.py).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import main
from api.main import QueryResponse, app

client = TestClient(app)
AUTH_HEADERS = {"Authorization": "Bearer correct-token"}


def _stub_execute_query(question: str, top_k: int, summarize: bool) -> QueryResponse:
    return QueryResponse(results=[])


@pytest.fixture(autouse=True)
def _isolated_daily_counter(monkeypatch):
    # Fresh dict per test -- monkeypatch reverts it afterward, so this
    # test file's counts never bleed into (or get pushed over the limit
    # by) the ~9 /query requests other test modules send in the same
    # pytest process.
    monkeypatch.setattr(main, "_daily_request_counts", {})


# ---------------------------------------------------------------------------
# require_daily_request_limit as a plain function (direct, no HTTP layer)
# ---------------------------------------------------------------------------


def test_default_daily_request_limit_constant_is_100():
    assert main.DEFAULT_DAILY_REQUEST_LIMIT == 100


def test_calls_under_the_limit_do_not_raise(monkeypatch):
    monkeypatch.setenv("DAILY_REQUEST_LIMIT", "3")

    main.require_daily_request_limit()
    main.require_daily_request_limit()
    main.require_daily_request_limit()  # 3rd call: count was 2, still < 3


def test_call_at_the_limit_raises_429(monkeypatch):
    monkeypatch.setenv("DAILY_REQUEST_LIMIT", "3")
    main.require_daily_request_limit()
    main.require_daily_request_limit()
    main.require_daily_request_limit()

    with pytest.raises(HTTPException) as exc_info:
        main.require_daily_request_limit()

    assert exc_info.value.status_code == 429


def test_default_limit_applies_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("DAILY_REQUEST_LIMIT", raising=False)
    # Pre-seed the counter to exactly the default limit's boundary rather
    # than looping 100 real calls -- same code path, no slow test.
    monkeypatch.setattr(main, "_daily_request_counts", {main._today_utc_key(): 100})

    with pytest.raises(HTTPException) as exc_info:
        main.require_daily_request_limit()

    assert exc_info.value.status_code == 429


def test_limit_is_configurable_via_env_var(monkeypatch):
    monkeypatch.setenv("DAILY_REQUEST_LIMIT", "1")

    main.require_daily_request_limit()  # 1st call allowed

    with pytest.raises(HTTPException) as exc_info:
        main.require_daily_request_limit()  # 2nd call same day: over the configured limit

    assert exc_info.value.status_code == 429


def test_daily_limit_resets_when_utc_date_changes(monkeypatch):
    monkeypatch.setenv("DAILY_REQUEST_LIMIT", "2")
    day1, day2 = "2026-08-12", "2026-08-13"
    monkeypatch.setattr(main, "_today_utc_key", lambda: day1)

    main.require_daily_request_limit()
    main.require_daily_request_limit()
    with pytest.raises(HTTPException) as exc_info:
        main.require_daily_request_limit()
    assert exc_info.value.status_code == 429

    monkeypatch.setattr(main, "_today_utc_key", lambda: day2)

    main.require_daily_request_limit()  # new UTC day -> fresh counter, not blocked


# ---------------------------------------------------------------------------
# HTTP layer via POST /query
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _api_token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "correct-token")


def test_query_returns_429_once_daily_limit_reached(monkeypatch):
    monkeypatch.setenv("DAILY_REQUEST_LIMIT", "2")
    monkeypatch.setattr(main, "execute_query", _stub_execute_query)

    r1 = client.post("/query", json={"question": "質問"}, headers=AUTH_HEADERS)
    r2 = client.post("/query", json={"question": "質問"}, headers=AUTH_HEADERS)
    r3 = client.post("/query", json={"question": "質問"}, headers=AUTH_HEADERS)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429


def test_429_response_carries_an_explicit_detail(monkeypatch):
    monkeypatch.setenv("DAILY_REQUEST_LIMIT", "1")
    monkeypatch.setattr(main, "execute_query", _stub_execute_query)

    client.post("/query", json={"question": "質問"}, headers=AUTH_HEADERS)
    response = client.post("/query", json={"question": "質問"}, headers=AUTH_HEADERS)

    assert response.status_code == 429
    assert "detail" in response.json()


def test_invalid_requests_do_not_count_toward_the_daily_limit(monkeypatch):
    # Design decision under test (SPEC task instruction: "どちらを先にする
    # かを設計判断としてコメントに残す" -- api.main documents choosing
    # validation-before-counting): a flood of structurally invalid (400)
    # requests must not exhaust the day's budget for legitimate requests,
    # since an invalid request never reaches the costly embed/BigQuery
    # pipeline this limit exists to bound.
    monkeypatch.setenv("DAILY_REQUEST_LIMIT", "1")
    monkeypatch.setattr(main, "execute_query", _stub_execute_query)

    for _ in range(5):
        response = client.post("/query", json={"question": ""}, headers=AUTH_HEADERS)
        assert response.status_code == 400

    response = client.post("/query", json={"question": "質問"}, headers=AUTH_HEADERS)

    assert response.status_code == 200


def test_401_requests_do_not_count_toward_the_daily_limit(monkeypatch):
    monkeypatch.setenv("DAILY_REQUEST_LIMIT", "1")
    monkeypatch.setattr(main, "execute_query", _stub_execute_query)

    for _ in range(5):
        response = client.post(
            "/query",
            json={"question": "質問"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    response = client.post("/query", json={"question": "質問"}, headers=AUTH_HEADERS)

    assert response.status_code == 200
