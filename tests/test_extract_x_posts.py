"""Tests for loader.extract_x_posts.

All fixtures are synthetic X-STRATEGY.md-shaped markdown files created
under tmp_path -- no real note-articles/X-STRATEGY.md content is read by
tests (SPEC-agent-ops-warehouse.md Section 2: fixtures are synthetic
data only).
"""
from __future__ import annotations

from loader.extract_x_posts import extract_x_posts

HEADER = "| 投稿日 | 形態 | 対象/テーマ | URL |\n|-------|------|-----------|-----|\n"


def _write_log(path, table_body: str) -> None:
    content = (
        "# X-STRATEGY\n\n"
        "## 投稿ログ\n\n"
        f"{HEADER}{table_body}\n"
        "## アカウントの実態\n\n"
        "| 項目 | 実測 |\n|------|------|\n| アカウント | @example |\n"
    )
    (path / "X-STRATEGY.md").write_text(content, encoding="utf-8")


def test_extract_x_posts_flow_row_normal_case(tmp_path):
    _write_log(
        tmp_path,
        "| 2026-07-29 | **フロー(引用リポスト)** — 休眠明け1本目 "
        "| グラフエンジニアリング記事への実践報告 | https://x.com/example/status/1 |\n",
    )

    result = extract_x_posts(tmp_path / "X-STRATEGY.md")

    assert len(result.rows) == 1
    row = result.rows[0]
    assert set(row) == {"posted_at", "post_type", "theme", "url", "char_count"}
    assert row["posted_at"] == "2026-07-29T00:00:00Z"
    assert row["post_type"] == "flow"
    assert row["theme"] == "グラフエンジニアリング記事への実践報告"
    assert row["url"] == "https://x.com/example/status/1"
    assert row["char_count"] is None
    assert result.skipped_rows == []


def test_extract_x_posts_stock_single_row(tmp_path):
    _write_log(
        tmp_path,
        "| 2026-08-01 | **ストック(単発)** — 分析結果の要約 "
        "| 記事の分析サマリ | https://x.com/example/status/2 |\n",
    )

    result = extract_x_posts(tmp_path / "X-STRATEGY.md")

    assert result.rows[0]["post_type"] == "stock_single"


def test_extract_x_posts_stock_thread_row(tmp_path):
    _write_log(
        tmp_path,
        "| 2026-08-03 | **ストック(スレッド)** — 三部作の1本目 "
        "| 三部作テーマ | https://x.com/example/status/3 |\n",
    )

    result = extract_x_posts(tmp_path / "X-STRATEGY.md")

    assert result.rows[0]["post_type"] == "stock_thread"


def test_extract_x_posts_undeterminable_type_falls_back_to_flow(tmp_path):
    _write_log(
        tmp_path,
        "| 2026-08-05 | **告知** — その他の型 "
        "| 何かのお知らせ | https://x.com/example/status/4 |\n",
    )

    result = extract_x_posts(tmp_path / "X-STRATEGY.md")

    assert result.rows[0]["post_type"] == "flow"


def test_extract_x_posts_skips_unrecorded_row(tmp_path):
    _write_log(
        tmp_path,
        "| 未記録 | 未記録 | 未記録 | 未記録 |\n"
        "| 2026-08-07 | **フロー** — 通常投稿 "
        "| 通常テーマ | https://x.com/example/status/5 |\n",
    )

    result = extract_x_posts(tmp_path / "X-STRATEGY.md")

    assert len(result.rows) == 1
    assert result.rows[0]["posted_at"] == "2026-08-07T00:00:00Z"
    assert len(result.skipped_rows) == 1
    assert "未記録" in result.skipped_rows[0]


def test_extract_x_posts_missing_file_returns_empty(tmp_path):
    result = extract_x_posts(tmp_path / "does-not-exist.md")

    assert result.rows == []
    assert result.skipped_rows == []


def test_extract_x_posts_missing_section_returns_empty(tmp_path):
    (tmp_path / "X-STRATEGY.md").write_text("# X-STRATEGY\n\nno log here\n", encoding="utf-8")

    result = extract_x_posts(tmp_path / "X-STRATEGY.md")

    assert result.rows == []
