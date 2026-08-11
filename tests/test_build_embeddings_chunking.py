"""Tests for scripts.build_embeddings chunking (RAG API phase 2, checkpoint 1).

SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.1 (chunking rules) /
Section 6-1 (test policy: H2-unit split / H3 re-split when a section has 2+
H3 headings / long-text (>2000 chars) paragraph re-split).

All fixtures are synthetic markdown text -- no real note-articles/published/
content is read here (repo convention, see test_extract_articles.py). A
one-off manual check against the real corpus is reported separately in the
checkpoint-1 completion report, not baked into this pytest file, so the
suite stays reproducible and offline.
"""
from __future__ import annotations

from scripts.build_embeddings import (
    MAX_CHUNK_CHARS,
    chunk_article_text,
    chunk_published_articles,
    split_h2_sections,
    split_h3_subsections,
    split_long_text,
)


def _write(path, name: str, content: str) -> None:
    (path / name).write_text(content, encoding="utf-8")


def test_split_h2_sections_splits_on_h2_boundaries():
    body = (
        "intro text before any heading, dropped\n\n"
        "## First Section\n\nfirst body.\n\n"
        "## Second Section\n\nsecond body.\n"
    )

    sections = split_h2_sections(body)

    assert sections == [
        ("First Section", "first body."),
        ("Second Section", "second body."),
    ]


def test_chunk_article_text_h2_unit_when_zero_or_one_h3():
    text = (
        "# 記事タイトル\n\n"
        "## セクションA\n\n本文A。\n\n"
        "## セクションB\n\n### 唯一の副見出し\n\n本文B。\n"
    )

    rows = chunk_article_text(text, "20260701_example.md", "2026-07-01")

    assert len(rows) == 2
    assert rows[0]["section_title"] == "セクションA"
    assert rows[0]["chunk_text"] == "記事タイトル > セクションA\n\n本文A。"
    # exactly one H3 -- SPEC: 0〜1個のH3はそのままH2単位を維持する
    assert rows[1]["section_title"] == "セクションB"
    assert "### 唯一の副見出し" in rows[1]["chunk_text"]
    assert rows[1]["chunk_text"].startswith("記事タイトル > セクションB\n\n")
    for row in rows:
        assert row["filename"] == "20260701_example.md"
        assert row["article_title"] == "記事タイトル"
        assert row["published_date"] == "2026-07-01"
        assert "chunk_id" in row


def test_chunk_article_text_splits_h3_when_two_or_more():
    text = (
        "# 記事タイトル\n\n"
        "## 設計原則 — 3つの転換\n\n"
        "### 転換1: 役割\n\n本文1。\n\n"
        "### 転換2: 観点\n\n本文2。\n\n"
        "### 転換3: 上限\n\n本文3。\n\n"
        "## 次のセクション\n\n次の本文。\n"
    )

    rows = chunk_article_text(text, "20260701_example.md", "2026-07-01")

    h3_rows = [r for r in rows if r["section_title"].startswith("設計原則")]
    assert len(h3_rows) == 3
    assert h3_rows[0]["section_title"] == "設計原則 — 3つの転換 > 転換1: 役割"
    assert h3_rows[0]["chunk_text"] == (
        "記事タイトル > 設計原則 — 3つの転換 > 転換1: 役割\n\n本文1。"
    )
    assert h3_rows[1]["section_title"] == "設計原則 — 3つの転換 > 転換2: 観点"
    assert h3_rows[2]["section_title"] == "設計原則 — 3つの転換 > 転換3: 上限"

    next_rows = [r for r in rows if r["section_title"] == "次のセクション"]
    assert len(next_rows) == 1
    assert next_rows[0]["chunk_text"] == "記事タイトル > 次のセクション\n\n次の本文。"


def test_split_h3_subsections_keeps_leading_text_before_first_h3():
    section_text = "リード文。\n\n### 副見出し1\n\n本文1\n\n### 副見出し2\n\n本文2"

    parts = split_h3_subsections(section_text)

    assert parts[0] == (None, "リード文。")
    assert parts[1] == ("副見出し1", "本文1")
    assert parts[2] == ("副見出し2", "本文2")


def test_split_long_text_resplits_over_2000_chars_by_paragraph():
    para_a = "あ" * 1200
    para_b = "い" * 1200
    text = f"{para_a}\n\n{para_b}"
    assert len(text) > MAX_CHUNK_CHARS

    groups = split_long_text(text)

    assert len(groups) == 2
    assert groups[0] == para_a
    assert groups[1] == para_b
    for group in groups:
        assert len(group) <= MAX_CHUNK_CHARS


def test_split_long_text_leaves_short_text_untouched():
    text = "短い本文。"

    groups = split_long_text(text)

    assert groups == [text]


def test_chunk_article_text_applies_long_text_split_to_a_section():
    para_a = "う" * 1500
    para_b = "え" * 1500
    text = (
        "# 記事タイトル\n\n"
        f"## 長いセクション\n\n{para_a}\n\n{para_b}\n"
    )

    rows = chunk_article_text(text, "20260701_example.md", "2026-07-01")

    assert len(rows) == 2
    assert all(r["section_title"] == "長いセクション" for r in rows)
    assert para_a in rows[0]["chunk_text"]
    assert para_b in rows[1]["chunk_text"]
    assert para_b not in rows[0]["chunk_text"]
    # chunk_id must stay unique across the paragraph-level split
    assert rows[0]["chunk_id"] != rows[1]["chunk_id"]


def test_chunk_article_text_cascades_long_text_split_within_h3_subsection():
    """SPEC 4.1's two split rules can both apply to the same H2 (H3 count
    >= 2 *and* one resulting H3 sub-chunk is itself still > 2000 chars).
    The cascade is: H2 -> H3 split (structural, applied first) -> each
    resulting piece re-checked against the 2000-char cap (this test pins
    that 3rd stage down explicitly, coordinator follow-up on checkpoint 1).
    """
    para_a = "か" * 1500
    para_b = "き" * 1500
    text = (
        "# 記事タイトル\n\n"
        "## 大きなH2\n\n"
        "### 副見出し1\n\n短い本文。\n\n"
        f"### 副見出し2\n\n{para_a}\n\n{para_b}\n\n"
        "### 副見出し3\n\nまた短い本文。\n"
    )

    rows = chunk_article_text(text, "20260701_example.md", "2026-07-01")

    h2_rows = [r for r in rows if r["section_title"].startswith("大きなH2")]
    # 副見出し1: 1 chunk / 副見出し2: cascaded into 2 chunks / 副見出し3: 1 chunk
    assert len(h2_rows) == 4

    sub2_rows = [r for r in h2_rows if r["section_title"] == "大きなH2 > 副見出し2"]
    assert len(sub2_rows) == 2
    assert para_a in sub2_rows[0]["chunk_text"]
    assert para_b in sub2_rows[1]["chunk_text"]
    assert para_b not in sub2_rows[0]["chunk_text"]
    assert para_a not in sub2_rows[1]["chunk_text"]
    # both still carry the full H2>H3 context prefix, not just the H2 one
    for row in sub2_rows:
        assert row["chunk_text"].startswith("記事タイトル > 大きなH2 > 副見出し2\n\n")

    ids = [row["chunk_id"] for row in rows]
    assert len(ids) == len(set(ids))  # cascaded split must not collide chunk_ids


def test_chunk_published_articles_dynamic_count_and_filename_skip(tmp_path):
    _write(
        tmp_path,
        "20260701_a.md",
        "# 記事A\n\n## セクション\n\n本文。\n",
    )
    _write(
        tmp_path,
        "20260702_b.md",
        "# 記事B\n\n## セクション\n\n本文。\n",
    )
    _write(tmp_path, "no-date-prefix.md", "# Untitled\n\n## X\n\nbody\n")

    result = chunk_published_articles(tmp_path)

    assert len(result.rows) == 2  # one H2 chunk per well-named article
    assert result.skipped_files == ["no-date-prefix.md"]
    filenames = {row["filename"] for row in result.rows}
    assert filenames == {"20260701_a.md", "20260702_b.md"}
