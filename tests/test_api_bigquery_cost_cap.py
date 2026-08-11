"""Tests for run_vector_search_query's BigQuery scan-byte cap (RAG API
phase 3.6 -- SPEC-agent-ops-warehouse-rag-api_20260811.md Section 9 phase
3.6 / Section 8 risk table: "BigQuery`maximum_bytes_billed=100MB`(1クエリ
ごとのハード上限・想定外の高額スキャンを実行前に拒否)").

build_bigquery_client is monkeypatched to a fake client that records the
job_config it receives -- google.cloud.bigquery.QueryJobConfig itself is
real here (constructing it is a local object build, not a network call),
so these tests also confirm api.main passes the SDK's actual kwarg name
(maximum_bytes_billed), not just a stand-in test double that could
silently drift from it. client.query()'s network execution is still
mocked via _FakeClient -- no real BigQuery connection anywhere in this
file (task constraint: 実際のBigQuery呼び出しはしない)."""
from __future__ import annotations

import pytest
from google.cloud import bigquery

from api import main
from api.main import RetrievalError, run_vector_search_query


class _FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return _FakeQueryJob([])


def test_run_vector_search_query_sets_maximum_bytes_billed_to_100mb(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(main, "build_bigquery_client", lambda: fake_client)

    run_vector_search_query("SELECT 1")

    assert len(fake_client.calls) == 1
    sql, job_config = fake_client.calls[0]
    assert sql == "SELECT 1"
    assert isinstance(job_config, bigquery.QueryJobConfig)
    assert job_config.maximum_bytes_billed == 100 * 1024 * 1024


def test_run_vector_search_query_still_passes_sql_through_unchanged(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(main, "build_bigquery_client", lambda: fake_client)

    run_vector_search_query("SELECT chunk_id FROM x")

    assert fake_client.calls[0][0] == "SELECT chunk_id FROM x"


def test_bigquery_rejecting_an_over_budget_scan_still_maps_to_retrieval_error(monkeypatch):
    # SPEC Section 8: BigQuery's own "query exceeded byte billing limit"
    # rejection is just another exception run_vector_search_query already
    # wraps -- no special-casing needed. Regression guard that the existing
    # wrap-into-RetrievalError behavior still covers a maximum_bytes_billed
    # rejection specifically.
    class _RejectingClient:
        def query(self, sql, job_config=None):
            raise RuntimeError("Query exceeded limit for bytes billed: 100000000.")

    monkeypatch.setattr(main, "build_bigquery_client", lambda: _RejectingClient())

    with pytest.raises(RetrievalError):
        run_vector_search_query("SELECT 1")
