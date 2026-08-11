"""Tests for scripts.build_embeddings ndjson output / bq load command
construction / main() wiring (RAG API phase 2, checkpoint 3).

SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.2/4.3 (ndjson schema,
`bq load --replace`) / Section 6-3,6-4 (test policy: ndjson row-count and
schema correctness; `bq load` argv construction; command is built, never
executed). main() is exercised end-to-end with a fake embed_fn injected via
monkeypatch -- no real Gemini API call, no real `bq` subprocess call
anywhere in this file.
"""
from __future__ import annotations

import json

import pytest

from scripts import build_embeddings
from scripts.build_embeddings import (
    SCHEMA_FIELDS,
    build_bq_load_args,
    build_ndjson_rows,
)

# ---------------------------------------------------------------------------
# 3. ndjson output (Section 6-3)
# ---------------------------------------------------------------------------


def test_build_ndjson_rows_selects_schema_fields_and_stamps_loaded_at():
    embedded_rows = [
        {
            "chunk_id": "20260701_a__001",
            "filename": "20260701_a.md",
            "article_title": "記事A",
            "section_title": "セクション",
            "chunk_text": "記事A > セクション\n\n本文。",
            "published_date": "2026-07-01",
            "embedding": [0.1, 0.2],
        }
    ]

    rows = build_ndjson_rows(embedded_rows, loaded_at="2026-08-12T00:00:00+00:00")

    assert len(rows) == 1
    assert set(rows[0]) == set(SCHEMA_FIELDS)
    assert rows[0]["loaded_at"] == "2026-08-12T00:00:00+00:00"
    assert rows[0]["embedding"] == [0.1, 0.2]


def test_build_ndjson_rows_row_count_matches_input_chunk_count():
    embedded_rows = [
        {
            "chunk_id": f"c{i}",
            "filename": "f.md",
            "article_title": "t",
            "section_title": "s",
            "chunk_text": "x",
            "published_date": "2026-07-01",
            "embedding": [0.0],
        }
        for i in range(5)
    ]

    rows = build_ndjson_rows(embedded_rows)

    assert len(rows) == 5


def test_build_ndjson_rows_raises_on_missing_required_field():
    embedded_rows = [{"chunk_id": "c1", "chunk_text": "x"}]  # missing filename, embedding, etc.

    with pytest.raises(KeyError):
        build_ndjson_rows(embedded_rows)


def test_ndjson_written_to_disk_matches_schema(tmp_path):
    from loader.emit import write_ndjson

    embedded_rows = [
        {
            "chunk_id": "20260701_a__001",
            "filename": "20260701_a.md",
            "article_title": "記事A",
            "section_title": "セクション",
            "chunk_text": "記事A > セクション\n\n本文。",
            "published_date": "2026-07-01",
            "embedding": [0.1, 0.2],
        }
    ]
    rows = build_ndjson_rows(embedded_rows, loaded_at="2026-08-12T00:00:00+00:00")

    n = write_ndjson(rows, tmp_path / "raw_article_chunks.ndjson")

    assert n == 1
    lines = (tmp_path / "raw_article_chunks.ndjson").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    written = json.loads(lines[0])
    assert set(written) == set(SCHEMA_FIELDS)


# ---------------------------------------------------------------------------
# 4. `bq load` command construction (Section 6-4) -- argv only, never executed
# ---------------------------------------------------------------------------


def test_build_bq_load_args_includes_table_schema_and_replace_flag():
    args = build_bq_load_args(
        project="agent-ops-warehouse",
        dataset="raw",
        source_uri="/tmp/out/raw_article_chunks.ndjson",
        table="article_chunks",
        schema_path="/tmp/schemas/raw_article_chunks.json",
    )

    assert args[0] == "bq"
    assert args[1] == "load"
    assert "--replace" in args
    assert "--schema=/tmp/schemas/raw_article_chunks.json" in args
    assert "agent-ops-warehouse:raw.article_chunks" in args
    assert args[-1] == "/tmp/out/raw_article_chunks.ndjson"


def test_build_bq_load_args_defaults_table_and_schema_path():
    args = build_bq_load_args(
        project="agent-ops-warehouse",
        dataset="raw",
        source_uri="/tmp/out/raw_article_chunks.ndjson",
    )

    assert "agent-ops-warehouse:raw.article_chunks" in args
    assert any(a.startswith("--schema=") and a.endswith("raw_article_chunks.json") for a in args)


# ---------------------------------------------------------------------------
# 5. main() -- wires 1-4 together (Section 9 phase 2). Fake embed_fn only;
#    no real Gemini API call, no real `bq` subprocess call.
# ---------------------------------------------------------------------------


def _write_one_article(published_dir):
    published_dir.mkdir(parents=True, exist_ok=True)
    (published_dir / "20260701_a.md").write_text(
        "# 記事A\n\n## セクション\n\n本文。\n", encoding="utf-8"
    )


def test_main_end_to_end_writes_ndjson_and_prints_bq_load_command(tmp_path, monkeypatch, capsys):
    published_dir = tmp_path / "published"
    _write_one_article(published_dir)
    out_dir = tmp_path / "out"

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test-only")
    monkeypatch.setattr(build_embeddings, "build_gemini_client", lambda api_key: object())
    monkeypatch.setattr(
        build_embeddings,
        "call_embedding_api",
        lambda client, text, model=build_embeddings.EMBEDDING_MODEL: [0.1, 0.2],
    )

    rows = build_embeddings.main(
        [
            "--published-dir",
            str(published_dir),
            "--out",
            str(out_dir),
            "--project",
            "test-project",
            "--dataset",
            "raw",
        ]
    )

    assert len(rows) == 1
    out_path = out_dir / "raw_article_chunks.ndjson"
    assert out_path.exists()
    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    written = json.loads(lines[0])
    assert set(written) == set(SCHEMA_FIELDS)
    assert written["embedding"] == [0.1, 0.2]

    captured = capsys.readouterr()
    assert "bq load" in captured.out
    assert "--replace" in captured.out
    assert "test-project:raw.article_chunks" in captured.out


def test_main_dry_run_chunks_skips_embedding_entirely(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    _write_one_article(published_dir)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)  # must not be needed in dry-run

    rows = build_embeddings.main(
        ["--published-dir", str(published_dir), "--project", "p", "--dry-run-chunks"]
    )

    assert len(rows) == 1
    assert "embedding" not in rows[0]


def test_main_raises_systemexit_when_api_key_missing(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    _write_one_article(published_dir)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(SystemExit):
        build_embeddings.main(["--published-dir", str(published_dir), "--project", "p"])


def test_main_skips_chunk_that_fails_embedding_and_still_writes_the_rest(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    published_dir.mkdir()
    (published_dir / "20260701_a.md").write_text(
        "# 記事A\n\n## 一番目\n\n本文1。\n\n## 二番目\n\n本文2。\n", encoding="utf-8"
    )
    out_dir = tmp_path / "out"

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test-only")
    monkeypatch.setattr(build_embeddings, "build_gemini_client", lambda api_key: object())

    def fake_call(client, text, model=build_embeddings.EMBEDDING_MODEL):
        if "一番目" in text:
            raise build_embeddings.RateLimitError("429")
        return [0.5]

    monkeypatch.setattr(build_embeddings, "call_embedding_api", fake_call)
    monkeypatch.setattr(build_embeddings.time, "sleep", lambda s: None)  # no real waiting in tests

    rows = build_embeddings.main(
        ["--published-dir", str(published_dir), "--out", str(out_dir), "--project", "p"]
    )

    assert len(rows) == 1  # the "一番目" chunk was skipped after exhausting retries
    assert rows[0]["section_title"] == "二番目"
