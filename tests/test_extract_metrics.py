"""Tests for loader.extract_metrics.

All fixtures are synthetic METRICS.md files created under tmp_path -- no
real note-articles/METRICS.md content is read by tests
(SPEC-agent-ops-warehouse.md Section 2: fixtures are synthetic data
only).
"""
from __future__ import annotations

from loader.extract_metrics import extract_metrics

HEADER = (
    "| 月 | note公開数 | note総view | スキ計 | コメント計 | X投稿数 "
    "| Xインプレ計 | **プロフィールクリック** | フォロワー増 | 一行所見 |\n"
    "|----|----------|-----------|-------|----------|--------"
    "|------------|----------------------|-----------|---------|\n"
)


def _write_metrics(path, table_body: str) -> None:
    content = (
        "# Metrics Log\n\n"
        "## 1. 月次サマリ(1行追記)\n\n"
        f"{HEADER}{table_body}\n"
    )
    (path / "METRICS.md").write_text(content, encoding="utf-8")


def test_extract_metrics_normal_case(tmp_path):
    _write_metrics(
        tmp_path,
        "| 2026-07 | 15 | **119** | **4** | **0** | **0** | — | 取得不可 "
        "| 0(累計76人) | 平均7.9view/記事 |\n",
    )

    result = extract_metrics(tmp_path / "METRICS.md")

    assert len(result.rows) == 1
    row = result.rows[0]
    assert set(row) == {
        "month",
        "note_articles",
        "note_views",
        "note_likes",
        "note_comments",
        "x_posts",
        "x_impressions",
        "x_followers_total",
        "note_text",
    }
    assert row["month"] == "2026-07-01"
    assert row["note_articles"] == 15
    assert row["note_views"] == 119
    assert row["note_likes"] == 4
    assert row["note_comments"] == 0
    assert row["x_posts"] == 0
    assert row["x_impressions"] is None
    assert row["x_followers_total"] == 0
    assert row["note_text"] == "平均7.9view/記事"


def test_extract_metrics_null_markers_become_none(tmp_path):
    _write_metrics(
        tmp_path,
        "| 2026-08 | 0 | 未計測 | — | — | — | — | 取得不可 | — | 検証中 |\n",
    )

    result = extract_metrics(tmp_path / "METRICS.md")

    row = result.rows[0]
    assert row["note_articles"] == 0
    assert row["note_views"] is None
    assert row["note_likes"] is None
    assert row["note_comments"] is None
    assert row["x_posts"] is None
    assert row["x_impressions"] is None
    assert row["x_followers_total"] is None
    assert row["note_text"] == "検証中"


def test_extract_metrics_composite_value_takes_leading_number(tmp_path):
    _write_metrics(
        tmp_path,
        "| 2026-09 | 16 | **200** | **5** | **1** | **3** | **500** "
        "| 取得不可 | 3(累計80人) | 好調 |\n",
    )

    result = extract_metrics(tmp_path / "METRICS.md")

    row = result.rows[0]
    assert row["x_followers_total"] == 3


def test_extract_metrics_missing_table_returns_empty(tmp_path):
    content = "# Metrics Log\n\n## 1. 月次サマリ(1行追記)\n\n(未記入)\n"
    (tmp_path / "METRICS.md").write_text(content, encoding="utf-8")

    result = extract_metrics(tmp_path / "METRICS.md")

    assert result.rows == []


def test_extract_metrics_missing_file_returns_empty(tmp_path):
    result = extract_metrics(tmp_path / "does-not-exist.md")

    assert result.rows == []
