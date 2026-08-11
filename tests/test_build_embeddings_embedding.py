"""Tests for scripts.build_embeddings embedding generation (RAG API phase 2,
checkpoint 2).

SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.2 (embedding
generation, retry/backoff) / Section 6-2 (test policy: mock the API call;
verify 429 exponential backoff and skip-after-3-failures).

No real Gemini API call is made anywhere in this file -- embed_fn is always
a fake/injected callable, never google.genai (repo convention: see
test_bq_merge.py's "no network call" discipline for loader.bq_merge).
"""
from __future__ import annotations

import pytest

from scripts.build_embeddings import (
    RateLimitError,
    call_embedding_api,
    embed_chunk_with_retry,
    embed_chunks,
)


def test_embed_chunk_with_retry_returns_vector_on_first_success():
    calls = []

    def fake_embed_fn(text: str) -> list[float]:
        calls.append(text)
        return [0.1, 0.2, 0.3]

    vector = embed_chunk_with_retry(fake_embed_fn, "hello", sleep_fn=lambda s: None)

    assert vector == [0.1, 0.2, 0.3]
    assert calls == ["hello"]


def test_embed_chunk_with_retry_backs_off_exponentially_then_succeeds():
    attempts = {"n": 0}
    sleeps: list[float] = []

    def flaky_embed_fn(text: str) -> list[float]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RateLimitError("429")
        return [1.0]

    vector = embed_chunk_with_retry(
        flaky_embed_fn,
        "hello",
        max_retries=3,
        initial_backoff_seconds=1.0,
        sleep_fn=sleeps.append,
    )

    assert vector == [1.0]
    assert attempts["n"] == 3
    assert sleeps == [1.0, 2.0]  # exponential: 1s, then 2s -- 2 retries before the 3rd (final) try


def test_embed_chunk_with_retry_skips_after_max_retries_exhausted(caplog):
    def always_429(text: str) -> list[float]:
        raise RateLimitError("429")

    with caplog.at_level("WARNING"):
        vector = embed_chunk_with_retry(always_429, "hello", max_retries=3, sleep_fn=lambda s: None)

    assert vector is None
    assert "skipping chunk" in caplog.text


def test_embed_chunk_with_retry_does_not_retry_non_rate_limit_errors():
    def boom(text: str) -> list[float]:
        raise ValueError("not a rate limit error")

    with pytest.raises(ValueError):
        embed_chunk_with_retry(boom, "hello", sleep_fn=lambda s: None)


def test_embed_chunks_skips_failed_chunk_and_continues(caplog):
    rows = [
        {"chunk_id": "c1", "chunk_text": "ok"},
        {"chunk_id": "c2", "chunk_text": "always fails"},
        {"chunk_id": "c3", "chunk_text": "ok too"},
    ]

    def embed_fn(text: str) -> list[float]:
        if text == "always fails":
            raise RateLimitError("429")
        return [0.5]

    with caplog.at_level("WARNING"):
        embedded_rows, skipped_ids = embed_chunks(rows, embed_fn, max_retries=2, sleep_fn=lambda s: None)

    assert [r["chunk_id"] for r in embedded_rows] == ["c1", "c3"]
    assert all(r["embedding"] == [0.5] for r in embedded_rows)
    assert skipped_ids == ["c2"]
    assert "c2" in caplog.text


def test_embed_chunks_preserves_original_row_fields():
    rows = [{"chunk_id": "c1", "chunk_text": "ok", "section_title": "セクション"}]

    def embed_fn(text: str) -> list[float]:
        return [0.9]

    embedded_rows, skipped_ids = embed_chunks(rows, embed_fn)

    assert skipped_ids == []
    assert embedded_rows[0]["section_title"] == "セクション"
    assert embedded_rows[0]["embedding"] == [0.9]


# ---------------------------------------------------------------------------
# call_embedding_api task_type (Phase 3.5 / SPEC Section 9 phase 3.5, Plan
# agent review): the indexing side must pass task_type="RETRIEVAL_DOCUMENT"
# so index-time and query-time embeddings are optimized asymmetrically per
# the Gemini embedding API's documented contract. A fake client (not a real
# google.genai.Client -- no network call) captures what config object
# embed_content actually received.
# ---------------------------------------------------------------------------


class _FakeEmbedding:
    def __init__(self, values: list[float]) -> None:
        self.values = values


class _FakeEmbedContentResponse:
    def __init__(self, values: list[float]) -> None:
        self.embeddings = [_FakeEmbedding(values)]


class _FakeModels:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def embed_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return _FakeEmbedContentResponse([0.1, 0.2, 0.3])


class _FakeGenaiClient:
    def __init__(self) -> None:
        self.models = _FakeModels()


def test_call_embedding_api_sets_retrieval_document_task_type():
    from google.genai import types

    client = _FakeGenaiClient()

    vector = call_embedding_api(client, "本文", task_type="RETRIEVAL_DOCUMENT")

    assert vector == [0.1, 0.2, 0.3]
    assert len(client.models.calls) == 1
    config = client.models.calls[0]["config"]
    assert isinstance(config, types.EmbedContentConfig)
    assert config.task_type == "RETRIEVAL_DOCUMENT"


def test_call_embedding_api_sets_retrieval_query_task_type():
    from google.genai import types

    client = _FakeGenaiClient()

    call_embedding_api(client, "質問文", task_type="RETRIEVAL_QUERY")

    config = client.models.calls[0]["config"]
    assert isinstance(config, types.EmbedContentConfig)
    assert config.task_type == "RETRIEVAL_QUERY"


def test_call_embedding_api_without_task_type_passes_no_config():
    client = _FakeGenaiClient()

    call_embedding_api(client, "本文")

    assert client.models.calls[0]["config"] is None
