"""Regression guard for LESSON-20260728-002.

On 2026-07-28, builder implemented the loader and the maintainer wrote
the Terraform BigQuery schemas independently from the same
SPEC-agent-ops-warehouse.md Section 3.2 description, and the `loaded_at`
column ended up present on one side and missing on the other (the SPEC
prose didn't spell it out explicitly enough for both implementations to
agree). This test makes that class of drift a machine-checked failure
instead of something that is only caught by chance during a later review.

For every raw table that is backed by an extract_*.py module, we build
one synthetic row with the extractor (reusing the exact fixture-building
approach of the corresponding tests/test_extract_*.py file -- no real
repo/file/API data), stamp it with loader.emit.stamp_loaded_at (the same
step the real `python -m loader` pipeline applies before writing NDJSON),
and assert the resulting key set is *exactly* the schema's `name` field
set -- in both directions.

Two raw tables are intentionally excluded:
- article_chunks: built by build_embeddings.py from chunked/embedded
  article text, not by an extract_*.py + stamp_loaded_at() pipeline.
- load_runs: built by loader.emit.build_load_run, a record of the load
  run itself (source/rows_loaded/exclusions_note), not sourced from an
  extractor either.
Neither goes through the extract_*() -> stamp_loaded_at() path this test
exercises, so they have no counterpart to compare here.
"""
from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from loader.emit import stamp_loaded_at
from loader.extract_articles import extract_articles
from loader.extract_git import extract_git_commits
from loader.extract_kpi_snapshots import extract_kpi_snapshots
from loader.extract_lessons import extract_lessons
from loader.extract_metrics import extract_metrics
from loader.extract_session_stats import extract_session_stats
from loader.extract_x_posts import extract_x_posts

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "terraform" / "schemas"

FAKE_GENERATE_STATUS_MODULE = '''
def compute_kpi_c_progress(kpi_path):
    return (2, 3)


def compute_kpi_r_streak(published_dir, today=None):
    return 5


def compute_recent_two_week_count(published_dir, today=None):
    return 2


def compute_evidence_ship_count(published_dir, today=None):
    return 1


def compute_total_ship_count(text, this_month):
    return 9


def find_career_kpi_file():
    return None
'''


def _schema_field_names(table: str) -> set[str]:
    schema_path = SCHEMAS_DIR / f"raw_{table}.json"
    fields = json.loads(schema_path.read_text(encoding="utf-8"))
    return {field["name"] for field in fields}


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _build_git_commits_row(tmp_path: Path) -> dict:
    repo = tmp_path / "note-articles"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test User")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _run_git(repo, "add", "a.txt")
    _run_git(repo, "commit", "-q", "-m", "first commit")

    result = extract_git_commits([str(repo)], allowed_repos=("note-articles",))
    return result.rows[0]


def _build_articles_row(tmp_path: Path) -> dict:
    (tmp_path / "20260701_example_article.md").write_text(
        "# An Example Article\n\nSome body text about shipping.\n", encoding="utf-8"
    )
    result = extract_articles(tmp_path)
    return result.rows[0]


def _build_lessons_row(tmp_path: Path) -> dict:
    (tmp_path / "LESSON-20260101-001_sample_lesson.md").write_text(
        "# Lesson\n\nbody\n", encoding="utf-8"
    )
    result = extract_lessons(tmp_path)
    return result.rows[0]


def _build_metrics_row(tmp_path: Path) -> dict:
    header = (
        "| 月 | note公開数 | note総view | スキ計 | コメント計 | X投稿数 "
        "| Xインプレ計 | **プロフィールクリック** | フォロワー増 | 一行所見 |\n"
        "|----|----------|-----------|-------|----------|--------"
        "|------------|----------------------|-----------|---------|\n"
    )
    body = (
        "| 2026-01 | 3 | **42** | **1** | **0** | **0** | — | 取得不可 "
        "| 0(累計12人) | synthetic note |\n"
    )
    metrics_path = tmp_path / "METRICS.md"
    metrics_path.write_text(
        "# Metrics Log\n\n## 1. 月次サマリ(1行追記)\n\n" + header + body,
        encoding="utf-8",
    )
    result = extract_metrics(metrics_path)
    return result.rows[0]


def _build_x_posts_row(tmp_path: Path) -> dict:
    header = "| 投稿日 | 形態 | 対象/テーマ | URL |\n|-------|------|-----------|-----|\n"
    body = (
        "| 2026-07-29 | **フロー(引用リポスト)** — 休眠明け1本目 "
        "| グラフエンジニアリング記事への実践報告 | https://x.com/example/status/1 |\n"
    )
    strategy_path = tmp_path / "X-STRATEGY.md"
    strategy_path.write_text(
        "# X-STRATEGY\n\n## 投稿ログ\n\n"
        + header
        + body
        + "\n## アカウントの実態\n\n| 項目 | 実測 |\n|------|------|\n| アカウント | @example |\n",
        encoding="utf-8",
    )
    result = extract_x_posts(strategy_path)
    return result.rows[0]


def _build_session_stats_row(tmp_path: Path) -> dict:
    session_dir = tmp_path / "-Users-example-Developer"
    session_dir.mkdir()
    lines = [
        {
            "type": "user",
            "timestamp": "2026-07-07T00:31:05.000Z",
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-07-07T00:31:10.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                ],
            },
        },
    ]
    with (session_dir / "session-a.jsonl").open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False))
            f.write("\n")
    result = extract_session_stats([str(session_dir)])
    return result.rows[0]


def _build_kpi_snapshots_row(tmp_path: Path) -> dict:
    ops_dir = tmp_path / "fake_ops"
    ops_dir.mkdir()
    (ops_dir / "generate_status.py").write_text(
        FAKE_GENERATE_STATUS_MODULE, encoding="utf-8"
    )
    kpi_path = tmp_path / "CAREER-KPI.md"
    kpi_path.write_text("## 2. x\n- [x] G1-3 a\n", encoding="utf-8")
    published_dir = tmp_path / "published"
    published_dir.mkdir()
    status_md = tmp_path / "STATUS.md"
    status_md.write_text("<!-- SHIP_COUNT: 3 (2026-07) -->\n", encoding="utf-8")

    result = extract_kpi_snapshots(
        kpi_path=kpi_path,
        published_dir=published_dir,
        status_md_path=status_md,
        ops_dir=ops_dir,
        today=date(2026, 7, 29),
    )
    return result.rows[0]


# table name (matches terraform/schemas/raw_<table>.json and
# terraform/main.tf's raw_tables map) -> fixture builder
SOURCES = {
    "git_commits": _build_git_commits_row,
    "articles": _build_articles_row,
    "lessons": _build_lessons_row,
    "metrics_monthly": _build_metrics_row,
    "x_posts": _build_x_posts_row,
    "session_stats": _build_session_stats_row,
    "kpi_snapshots": _build_kpi_snapshots_row,
}


@pytest.mark.parametrize("table", sorted(SOURCES))
def test_loader_output_keys_match_schema_field_names(table, tmp_path):
    raw_row = SOURCES[table](tmp_path)
    stamped_row = stamp_loaded_at([raw_row])[0]

    implementation_keys = set(stamped_row)
    schema_keys = _schema_field_names(table)

    only_in_implementation = sorted(implementation_keys - schema_keys)
    only_in_schema = sorted(schema_keys - implementation_keys)

    assert implementation_keys == schema_keys, (
        f"raw_{table}: loader output keys and terraform/schemas/raw_{table}.json "
        f"field names diverged (LESSON-20260728-002 regression). "
        f"In loader implementation but not in schema: {only_in_implementation or 'none'}. "
        f"In schema but not in loader implementation: {only_in_schema or 'none'}."
    )
