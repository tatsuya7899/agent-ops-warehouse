"""End-to-end test for `python -m loader`.

Builds a synthetic git repo and a synthetic published/ directory under
tmp_path, runs the CLI entry point, and inspects the resulting NDJSON
files and load ledger. No real repository or file is touched.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from loader.__main__ import run


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "-q")
    _run_git(path, "config", "user.email", "test@example.com")
    _run_git(path, "config", "user.name", "Test User")
    return path


def _commit(repo: Path, filename: str, content: str, message: str) -> None:
    (repo / filename).write_text(content, encoding="utf-8")
    _run_git(repo, "add", filename)
    _run_git(repo, "commit", "-q", "-m", message)


def _read_ndjson(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_cli_end_to_end_writes_ndjson_and_load_ledger(tmp_path):
    # "note-articles" is in loader.extract_git.ALLOWED_REPOS by name;
    # "strategic-planning" deliberately is not, exercising the real
    # production allowlist end to end (no monkeypatching needed).
    repo = _init_repo(tmp_path / "note-articles")
    _commit(repo, "a.txt", "hello\n", "first commit")

    excluded_repo = _init_repo(tmp_path / "strategic-planning")
    _commit(excluded_repo, "secret.txt", "company\n", "company commit")

    articles_dir = tmp_path / "published"
    articles_dir.mkdir()
    (articles_dir / "20260701_example.md").write_text(
        "# Example\n\ngate: G1-3\n", encoding="utf-8"
    )
    (articles_dir / "bad-name.md").write_text("# Bad\n", encoding="utf-8")

    out_dir = tmp_path / "out"

    run(
        [
            "--repos",
            str(repo),
            str(excluded_repo),
            "--articles",
            str(articles_dir),
            "--out",
            str(out_dir),
        ]
    )

    commits = _read_ndjson(out_dir / "raw_git_commits.ndjson")
    assert len(commits) == 1
    assert set(commits[0]) == {
        "repo",
        "commit_hash",
        "committed_at",
        "subject",
        "files_changed",
        "insertions",
        "deletions",
        "loaded_at",
    }

    articles = _read_ndjson(out_dir / "raw_articles.ndjson")
    assert len(articles) == 1
    assert set(articles[0]) == {
        "filename",
        "published_date",
        "title",
        "gate_id",
        "char_count",
        "loaded_at",
    }
    assert articles[0]["gate_id"] == "G1-3"

    load_runs = _read_ndjson(out_dir / "raw_load_runs.ndjson")
    sources = {row["source"] for row in load_runs}
    assert sources == {"raw_git_commits", "raw_articles"}

    git_run = next(r for r in load_runs if r["source"] == "raw_git_commits")
    assert git_run["rows_loaded"] == 1
    assert "strategic-planning" in git_run["exclusions_note"]

    articles_run = next(r for r in load_runs if r["source"] == "raw_articles")
    assert articles_run["rows_loaded"] == 1
    assert "bad-name.md" in articles_run["exclusions_note"]


def test_cli_end_to_end_all_four_sources(tmp_path):
    repo = _init_repo(tmp_path / "note-articles")
    _commit(repo, "a.txt", "hello\n", "first commit")

    articles_dir = tmp_path / "published"
    articles_dir.mkdir()
    (articles_dir / "20260701_example.md").write_text(
        "# Example\n\ngate: G1-3\n", encoding="utf-8"
    )

    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "LESSON-20260704-001_synthetic_lesson.md").write_text(
        "# Lesson\n\nbody\n", encoding="utf-8"
    )
    (lessons_dir / "LESSON_TEMPLATE.md").write_text("# Template\n", encoding="utf-8")
    archive_dir = lessons_dir / "archive"
    archive_dir.mkdir()
    (archive_dir / "LESSON-20260601-001_old_lesson.md").write_text(
        "# Old Lesson\n\nbody\n", encoding="utf-8"
    )

    metrics_file = tmp_path / "METRICS.md"
    metrics_file.write_text(
        "# Metrics Log\n\n"
        "## 1. 月次サマリ(1行追記)\n\n"
        "| 月 | note公開数 | note総view | スキ計 | コメント計 | X投稿数 "
        "| Xインプレ計 | **プロフィールクリック** | フォロワー増 | 一行所見 |\n"
        "|----|----------|-----------|-------|----------|--------"
        "|------------|----------------------|-----------|---------|\n"
        "| 2026-01 | 3 | **42** | **1** | **0** | **0** | — | 取得不可 "
        "| 0(累計12人) | synthetic note |\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "out"

    run(
        [
            "--repos",
            str(repo),
            "--articles",
            str(articles_dir),
            "--lessons",
            str(lessons_dir),
            "--metrics",
            str(metrics_file),
            "--out",
            str(out_dir),
        ]
    )

    lessons = _read_ndjson(out_dir / "raw_lessons.ndjson")
    assert len(lessons) == 2
    statuses = {row["lesson_id"]: row["status"] for row in lessons}
    assert statuses == {
        "LESSON-20260704-001": "active",
        "LESSON-20260601-001": "archived",
    }
    for row in lessons:
        assert set(row) == {
            "lesson_id",
            "created_date",
            "seq",
            "title",
            "status",
            "graduated_to",
            "loaded_at",
        }

    metrics = _read_ndjson(out_dir / "raw_metrics_monthly.ndjson")
    assert len(metrics) == 1
    assert metrics[0]["month"] == "2026-01-01"
    assert metrics[0]["x_followers_total"] == 0

    load_runs = _read_ndjson(out_dir / "raw_load_runs.ndjson")
    sources = {row["source"] for row in load_runs}
    assert sources == {
        "raw_git_commits",
        "raw_articles",
        "raw_lessons",
        "raw_metrics_monthly",
    }

    lessons_run = next(r for r in load_runs if r["source"] == "raw_lessons")
    assert lessons_run["rows_loaded"] == 2
    assert "LESSON_TEMPLATE.md" in lessons_run["exclusions_note"]

    metrics_run = next(r for r in load_runs if r["source"] == "raw_metrics_monthly")
    assert metrics_run["rows_loaded"] == 1


def test_cli_end_to_end_all_sources_stamp_loaded_at(tmp_path):
    """Regression test for the BQ load failure "Missing required field:
    loaded_at": every raw table with a loaded_at column in its schema
    (SPEC Section 3.2) must have loaded_at on every emitted row, for
    every source -- not just the sources that happened to be exercised
    when the bug was found. raw_load_runs is the one documented
    exception (its schema has no loaded_at column).
    """
    repo = _init_repo(tmp_path / "note-articles")
    _commit(repo, "a.txt", "hello\n", "first commit")

    articles_dir = tmp_path / "published"
    articles_dir.mkdir()
    (articles_dir / "20260701_example.md").write_text(
        "# Example\n\ngate: G1-3\n", encoding="utf-8"
    )

    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "LESSON-20260704-001_synthetic_lesson.md").write_text(
        "# Lesson\n\nbody\n", encoding="utf-8"
    )

    metrics_file = tmp_path / "METRICS.md"
    metrics_file.write_text(
        "# Metrics Log\n\n"
        "## 1. 月次サマリ(1行追記)\n\n"
        "| 月 | note公開数 | note総view | スキ計 | コメント計 | X投稿数 "
        "| Xインプレ計 | **プロフィールクリック** | フォロワー増 | 一行所見 |\n"
        "|----|----------|-----------|-------|----------|--------"
        "|------------|----------------------|-----------|---------|\n"
        "| 2026-01 | 3 | **42** | **1** | **0** | **0** | — | 取得不可 "
        "| 0(累計12人) | synthetic note |\n",
        encoding="utf-8",
    )

    x_strategy_file = tmp_path / "X-STRATEGY.md"
    x_strategy_file.write_text(
        "# X-STRATEGY\n\n## 投稿ログ\n\n"
        "| 投稿日 | 形態 | 対象/テーマ | URL |\n"
        "|-------|------|-----------|-----|\n"
        "| 2026-07-29 | **フロー** — 例 | 例のテーマ "
        "| https://x.com/example/status/1 |\n",
        encoding="utf-8",
    )

    sessions_dir = tmp_path / "-Users-example-Developer"
    sessions_dir.mkdir()
    (sessions_dir / "session-a.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-07-29T00:00:00.000Z",
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    kpi_file = tmp_path / "CAREER-KPI.md"
    kpi_file.write_text("## 2. Evidence\n- [x] G1-3 foo\n", encoding="utf-8")
    status_md_file = tmp_path / "STATUS.md"
    status_md_file.write_text("<!-- SHIP_COUNT: 1 (2026-07) -->\n", encoding="utf-8")

    out_dir = tmp_path / "out"

    run(
        [
            "--repos", str(repo),
            "--articles", str(articles_dir),
            "--lessons", str(lessons_dir),
            "--metrics", str(metrics_file),
            "--x-strategy", str(x_strategy_file),
            "--sessions", str(sessions_dir),
            "--kpi",
            "--kpi-file", str(kpi_file),
            "--kpi-published", str(articles_dir),
            "--kpi-status-md", str(status_md_file),
            "--out", str(out_dir),
        ]
    )

    stamped_files = [
        "raw_git_commits.ndjson",
        "raw_articles.ndjson",
        "raw_lessons.ndjson",
        "raw_metrics_monthly.ndjson",
        "raw_x_posts.ndjson",
        "raw_session_stats.ndjson",
        "raw_kpi_snapshots.ndjson",
    ]
    for filename in stamped_files:
        rows = _read_ndjson(out_dir / filename)
        assert rows, f"{filename} produced no rows"
        for row in rows:
            assert "loaded_at" in row, f"{filename} row missing loaded_at: {row}"

    # raw_load_runs is the documented exception: its schema (SPEC Section
    # 3.2) has run_at/source/rows_loaded/exclusions_note only.
    load_runs = _read_ndjson(out_dir / "raw_load_runs.ndjson")
    assert {r["source"] for r in load_runs} == {
        "raw_git_commits",
        "raw_articles",
        "raw_lessons",
        "raw_metrics_monthly",
        "raw_x_posts",
        "raw_session_stats",
        "raw_kpi_snapshots",
    }
    for row in load_runs:
        assert "loaded_at" not in row
