"""Tests for loader.extract_git.

All fixtures are synthetic git repositories created under tmp_path.
Real repositories (this Mac's Developer / note-articles / etc.) are never
touched by tests (SPEC-agent-ops-warehouse.md Section 2: fixtures are
synthetic data only).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from loader.extract_git import extract_git_commits


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(path, "init", "-q")
    _run(path, "config", "user.email", "test@example.com")
    _run(path, "config", "user.name", "Test User")
    return path


def _commit(repo: Path, filename: str, content: str, message: str) -> None:
    (repo / filename).write_text(content, encoding="utf-8")
    _run(repo, "add", filename)
    _run(repo, "commit", "-q", "-m", message)


def test_extract_git_commits_normal_case(tmp_path):
    repo = _init_repo(tmp_path / "note-articles")
    _commit(repo, "a.txt", "hello\n", "first commit")
    _commit(repo, "b.txt", "world\nmore\n", "second commit")

    result = extract_git_commits([str(repo)], allowed_repos=("note-articles",))

    assert len(result.rows) == 2
    assert result.skipped_repos == []
    for row in result.rows:
        assert row["repo"] == "note-articles"
        assert set(row) == {
            "repo",
            "commit_hash",
            "committed_at",
            "subject",
            "files_changed",
            "insertions",
            "deletions",
        }
    subjects = {row["subject"] for row in result.rows}
    assert subjects == {"first commit", "second commit"}

    second = next(r for r in result.rows if r["subject"] == "second commit")
    assert second["files_changed"] == 1
    assert second["insertions"] == 2
    assert second["deletions"] == 0


def test_extract_git_commits_empty_repo(tmp_path):
    repo = _init_repo(tmp_path / "note-articles")

    result = extract_git_commits([str(repo)], allowed_repos=("note-articles",))

    assert result.rows == []
    assert result.skipped_repos == []


def test_extract_git_commits_skips_repo_outside_allowlist(tmp_path):
    repo = _init_repo(tmp_path / "excluded-repo")
    _commit(repo, "secret.txt", "company stuff\n", "company commit")

    result = extract_git_commits([str(repo)], allowed_repos=("note-articles",))

    assert result.rows == []
    assert result.skipped_repos == ["excluded-repo"]


def test_extract_git_commits_skips_allowlisted_name_that_is_not_a_repo(tmp_path):
    """An allow-listed directory name that is not a git repo must be
    reported, not silently counted as zero commits."""
    plain = tmp_path / "note-articles"
    plain.mkdir()
    (plain / "a.txt").write_text("not a repo\n")

    result = extract_git_commits([str(plain)], allowed_repos=("note-articles",))

    assert result.rows == []
    assert result.skipped_repos == ["note-articles (not a git repo)"]


def test_extract_git_commits_skips_allowlisted_subdir_of_another_repo(tmp_path):
    """A subdirectory inside a different repository must not inherit the
    parent's history under the subdirectory's name (the _ops/Developer
    mislabeling case)."""
    outer = _init_repo(tmp_path / "Developer")
    _commit(outer, "a.txt", "root file\n", "outer commit")
    subdir = outer / "_ops"
    subdir.mkdir()

    result = extract_git_commits([str(subdir)], allowed_repos=("_ops",))

    assert result.rows == []
    assert result.skipped_repos == ["_ops (not a repo root; inside Developer)"]


def test_extract_git_commits_reports_nonexistent_allowed_path(tmp_path):
    result = extract_git_commits(
        [str(tmp_path / "note-articles")], allowed_repos=("note-articles",)
    )

    assert result.rows == []
    assert result.skipped_repos == ["note-articles (not a git repo)"]
