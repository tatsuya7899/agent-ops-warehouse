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
