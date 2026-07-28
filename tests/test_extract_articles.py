"""Tests for loader.extract_articles.

All fixtures are synthetic markdown files created under tmp_path -- no
real note-articles/published/ content is read by tests (SPEC-agent-ops-
warehouse.md Section 2: fixtures are synthetic data only).
"""
from __future__ import annotations

from loader.extract_articles import extract_articles


def _write(path, name: str, content: str) -> None:
    (path / name).write_text(content, encoding="utf-8")


def test_extract_articles_normal_case(tmp_path):
    _write(
        tmp_path,
        "20260701_example_article.md",
        "# An Example Article\n\nSome body text about shipping.\n",
    )

    result = extract_articles(tmp_path)

    assert len(result.rows) == 1
    row = result.rows[0]
    assert set(row) == {"filename", "published_date", "title", "gate_id", "char_count"}
    assert row["filename"] == "20260701_example_article.md"
    assert row["published_date"] == "2026-07-01"
    assert row["title"] == "An Example Article"
    assert row["gate_id"] is None
    assert row["char_count"] == len(
        "# An Example Article\n\nSome body text about shipping.\n"
    )
    assert result.skipped_files == []


def test_extract_articles_skips_filename_convention_violation(tmp_path):
    _write(tmp_path, "no-date-prefix.md", "# Untitled\n\nbody\n")
    _write(tmp_path, "20260701_example_article.md", "# Example\n\nbody\n")

    result = extract_articles(tmp_path)

    assert len(result.rows) == 1
    assert result.rows[0]["filename"] == "20260701_example_article.md"
    assert result.skipped_files == ["no-date-prefix.md"]


def test_extract_articles_extracts_gate_id_when_present(tmp_path):
    _write(
        tmp_path,
        "20260702_gated_article.md",
        "# Gated Article\n\nThis one references gate: G1-3 in the body.\n",
    )

    result = extract_articles(tmp_path)

    assert result.rows[0]["gate_id"] == "G1-3"
