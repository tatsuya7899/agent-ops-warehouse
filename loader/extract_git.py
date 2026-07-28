"""Extract git commit history from personal repositories.

Company repositories (e.g. strategic-planning) are excluded via an
explicit allowlist mechanism rather than a denylist, so the exclusion
logic is auditable directly from public source code
(SPEC-agent-ops-warehouse.md Section 2 / Section 3.1). Repositories not
present in the allowlist are skipped explicitly and reported back to the
caller -- never silently dropped, never raised as an error.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Personal repositories only, as enumerated in SPEC Section 3.1.
# Company repositories are never listed here.
ALLOWED_REPOS: tuple[str, ...] = (
    "Developer",
    "note-articles",
    "tatsuyasasaki-portfolio",
    "training-cockpit",
    "aipm_v0",
)

_FIELD_SEP = "\x1f"
_FILES_CHANGED_RE = re.compile(r"(\d+) files? changed")
_INSERTIONS_RE = re.compile(r"(\d+) insertions?\(\+\)")
_DELETIONS_RE = re.compile(r"(\d+) deletions?\(-\)")


@dataclass
class GitExtractionResult:
    rows: list[dict] = field(default_factory=list)
    skipped_repos: list[str] = field(default_factory=list)


def extract_git_commits(
    repo_paths: list[str],
    allowed_repos: tuple[str, ...] = ALLOWED_REPOS,
) -> GitExtractionResult:
    """Extract commit rows (raw_git_commits schema) from the given repos.

    Repositories whose directory name is not in ``allowed_repos`` are
    skipped explicitly and their name is recorded in
    ``GitExtractionResult.skipped_repos``.
    """
    rows: list[dict] = []
    skipped: list[str] = []

    for repo_path in repo_paths:
        repo = Path(repo_path)
        repo_name = repo.name

        if repo_name not in allowed_repos:
            skipped.append(repo_name)
            continue

        commits = _log_commits(repo)
        stats = _log_shortstats(repo)

        for commit in commits:
            commit_stats = stats.get(
                commit["commit_hash"],
                {"files_changed": 0, "insertions": 0, "deletions": 0},
            )
            rows.append(
                {
                    "repo": repo_name,
                    "commit_hash": commit["commit_hash"],
                    "committed_at": commit["committed_at"],
                    "subject": commit["subject"],
                    **commit_stats,
                }
            )

    return GitExtractionResult(rows=rows, skipped_repos=skipped)


def _log_commits(repo: Path) -> list[dict]:
    """Return [{commit_hash, committed_at, subject}, ...], newest first."""
    result = subprocess.run(
        ["git", "-C", str(repo), "log", f"--format=%H{_FIELD_SEP}%cI{_FIELD_SEP}%s"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # An empty repository (no commits yet) exits non-zero here; treat
        # as "no commits" rather than an error. check=False is
        # deliberate: the non-zero exit is inspected below, not raised.
        return []

    commits = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        commit_hash, committed_at, subject = line.split(_FIELD_SEP, 2)
        commits.append(
            {
                "commit_hash": commit_hash,
                "committed_at": committed_at,
                "subject": subject,
            }
        )
    return commits


def _log_shortstats(repo: Path) -> dict[str, dict]:
    """Return {commit_hash: {files_changed, insertions, deletions}}."""
    marker = "COMMIT "
    result = subprocess.run(
        ["git", "-C", str(repo), "log", f"--format={marker}%H", "--shortstat"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}

    stats: dict[str, dict] = {}
    current_hash: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            current_hash = line[len(marker):].strip()
            stats[current_hash] = {
                "files_changed": 0,
                "insertions": 0,
                "deletions": 0,
            }
        elif line.strip() and current_hash is not None:
            stats[current_hash] = _parse_shortstat_line(line)
    return stats


def _parse_shortstat_line(line: str) -> dict:
    files_changed_match = _FILES_CHANGED_RE.search(line)
    insertions_match = _INSERTIONS_RE.search(line)
    deletions_match = _DELETIONS_RE.search(line)
    return {
        "files_changed": int(files_changed_match.group(1)) if files_changed_match else 0,
        "insertions": int(insertions_match.group(1)) if insertions_match else 0,
        "deletions": int(deletions_match.group(1)) if deletions_match else 0,
    }
