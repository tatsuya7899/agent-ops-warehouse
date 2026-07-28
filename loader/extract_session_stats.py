"""Extract daily aggregated session statistics from Claude Code project
jsonl transcripts, for the raw_session_stats table.

Only aggregate counts (per calendar day) ever leave this module -- no
jsonl body text (prompts, tool inputs/outputs, file paths) is emitted
(SPEC-agent-ops-warehouse.md Section 2: "セッションログはローカルで集計
してから集計値のみをBQへ").

Company session directories (whose basename contains "-strategic-
planning", e.g. the real
`~/.claude/projects/-Users-tatsuyasasaki-Developer-strategic-planning/`)
are excluded via an explicit substring check -- the same
never-silently-dropped, always-reported-back pattern as the repo
allowlist in extract_git.py. Excluded directories are recorded in
SessionStatsResult.skipped_dirs even when passed in explicitly by the
caller.

Each *.jsonl file directly under a surviving directory is treated as one
session and contributes to exactly one stat_date: the date of the first
"timestamp" field found in the file (UTC), or the file's mtime date
(also interpreted in UTC, so the result does not depend on the host
timezone) when no line in the file carries a timestamp.

Lines that are not valid JSON, or whose "type" is not "user"/"assistant"
(e.g. "queue-operation", "attachment", "summary"), are not silently
ignored -- they are counted in SessionStatsResult.skipped_lines, which
the CLI records in the raw_load_runs ledger note.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

EXCLUDED_DIR_SUBSTRING = "-strategic-planning"
COUNTED_TYPES = {"user", "assistant"}


@dataclass
class SessionStatsResult:
    rows: list[dict] = field(default_factory=list)
    skipped_dirs: list[str] = field(default_factory=list)
    skipped_lines: int = 0


def extract_session_stats(session_dirs: list[str]) -> SessionStatsResult:
    """Extract daily aggregate rows (raw_session_stats schema) from the
    given `~/.claude/projects/<dir>/` session directories.
    """
    by_date: dict[str, dict] = {}
    skipped_dirs: list[str] = []
    skipped_lines = 0

    for dir_path in session_dirs:
        directory = Path(dir_path)

        if EXCLUDED_DIR_SUBSTRING in directory.name:
            skipped_dirs.append(directory.name)
            continue

        if not directory.exists():
            continue

        for jsonl_path in sorted(directory.glob("*.jsonl")):
            skipped_lines += _scan_file(jsonl_path, by_date)

    rows = [_build_row(stat_date, by_date[stat_date]) for stat_date in sorted(by_date)]
    return SessionStatsResult(rows=rows, skipped_dirs=skipped_dirs, skipped_lines=skipped_lines)


def _build_row(stat_date: str, bucket: dict) -> dict:
    return {
        "stat_date": stat_date,
        "session_count": bucket["session_count"],
        "user_messages": bucket["user_messages"],
        "assistant_messages": bucket["assistant_messages"],
        "tool_calls": bucket["tool_calls"],
        "distinct_tools": len(bucket["tools"]),
    }


def _scan_file(jsonl_path: Path, by_date: dict[str, dict]) -> int:
    """Scan one session file, folding its counts into by_date. Returns
    the number of skipped (unparseable / unrecognized-type) lines.
    """
    file_date: str | None = None
    user_messages = assistant_messages = tool_calls = 0
    tools: set[str] = set()
    skipped_lines = 0

    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            skipped_lines += 1
            continue

        if file_date is None:
            file_date = _extract_date(record)

        record_type = record.get("type")
        if record_type not in COUNTED_TYPES:
            skipped_lines += 1
            continue

        if record_type == "user":
            user_messages += 1
        else:  # "assistant"
            assistant_messages += 1
            calls, called_tools = _count_tool_uses(record)
            tool_calls += calls
            tools |= called_tools

    if file_date is None:
        file_date = _mtime_date(jsonl_path)

    bucket = by_date.setdefault(
        file_date,
        {
            "session_count": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "tool_calls": 0,
            "tools": set(),
        },
    )
    bucket["session_count"] += 1
    bucket["user_messages"] += user_messages
    bucket["assistant_messages"] += assistant_messages
    bucket["tool_calls"] += tool_calls
    bucket["tools"] |= tools

    return skipped_lines


def _extract_date(record: dict) -> str | None:
    ts = record.get("timestamp")
    if isinstance(ts, str) and len(ts) >= 10:
        return ts[:10]
    return None


def _count_tool_uses(record: dict) -> tuple[int, set[str]]:
    message = record.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return 0, set()

    calls = 0
    tools: set[str] = set()
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            calls += 1
            name = block.get("name")
            if name:
                tools.add(name)
    return calls, tools


def _mtime_date(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime("%Y-%m-%d")
