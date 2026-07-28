"""Tests for `python -m loader --merge`: the MERGE-based dedup load plan
CLI wiring for raw_git_commits (SPEC-agent-ops-warehouse.md Section 3.3).

Zero BigQuery / network calls: --merge without --execute only prints a
dry-run plan (no subprocess call happens at all -- enforced below by
making subprocess.run raise if it is ever reached); --execute is
exercised here with subprocess.run monkeypatched to a stub, never a
real `bq` invocation.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from loader import bq_merge
from loader.__main__ import run

_real_subprocess_run = subprocess.run


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True)
    return path


def _commit(repo: Path, filename: str, content: str, message: str) -> None:
    (repo / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True, capture_output=True)


def test_merge_flag_dry_run_prints_plan_without_subprocess_call(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path / "note-articles")
    _commit(repo, "a.txt", "hello\n", "first commit")
    out_dir = tmp_path / "out"

    def _forbidden_run(cmd, *args, **kwargs):
        # subprocess.run is one process-wide module: patching it also
        # intercepts extract_git_commits's own `git log` calls, which
        # this test must let through untouched. Only a `bq ...`
        # invocation is what dry-run must never make.
        if cmd[0] == "bq":
            raise AssertionError("dry-run --merge must never invoke subprocess (no bq CLI call)")
        return _real_subprocess_run(cmd, *args, **kwargs)

    monkeypatch.setattr(bq_merge.subprocess, "run", _forbidden_run)

    run(
        [
            "--repos", str(repo),
            "--merge", "--project", "proj", "--dataset", "ds",
            "--out", str(out_dir),
        ]
    )

    captured = capsys.readouterr()
    assert "[DRY RUN]" in captured.out
    assert "raw_git_commits" in captured.out


def test_merge_flag_execute_uses_injected_runner_stub(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path / "note-articles")
    _commit(repo, "a.txt", "hello\n", "first commit")
    out_dir = tmp_path / "out"

    calls = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        # See the dry-run test above: subprocess.run patching is
        # process-wide, so only a `bq ...` invocation is faked here --
        # extract_git_commits's own `git log` calls still run for real.
        if cmd[0] != "bq":
            return _real_subprocess_run(cmd, *args, **kwargs)
        calls.append(cmd)

        class _Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Result()

    monkeypatch.setattr(bq_merge.subprocess, "run", fake_subprocess_run)

    run(
        [
            "--repos", str(repo),
            "--merge", "--execute", "--project", "proj", "--dataset", "ds",
            "--out", str(out_dir),
        ]
    )

    assert len(calls) == 3  # load_staging, merge, drop_staging
    captured = capsys.readouterr()
    assert "[OK]" in captured.out


def test_merge_flag_without_repos_raises_clear_error(tmp_path):
    out_dir = tmp_path / "out"

    with pytest.raises(SystemExit):
        run(["--merge", "--project", "proj", "--dataset", "ds", "--out", str(out_dir)])


def test_merge_flag_without_project_or_dataset_raises_clear_error(tmp_path):
    repo = _init_repo(tmp_path / "note-articles")
    out_dir = tmp_path / "out"

    with pytest.raises(SystemExit):
        run(["--repos", str(repo), "--merge", "--out", str(out_dir)])
