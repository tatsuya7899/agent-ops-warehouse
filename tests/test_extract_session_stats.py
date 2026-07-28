"""Tests for loader.extract_session_stats.

All fixtures are synthetic *.jsonl files created under tmp_path -- no
real ~/.claude/projects/ transcript content (prompts, tool inputs,
file paths) is read by tests (SPEC-agent-ops-warehouse.md Section 2:
fixtures are synthetic data only; only aggregate counts ever leave this
module).
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from loader.extract_session_stats import extract_session_stats


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False))
            f.write("\n")


def test_extract_session_stats_normal_case(tmp_path):
    session_dir = tmp_path / "-Users-example-Developer"
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [
            {"type": "user", "timestamp": "2026-07-07T00:31:05.000Z", "message": {"role": "user", "content": "hi"}},
            {
                "type": "assistant",
                "timestamp": "2026-07-07T00:31:10.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "ok"},
                        {"type": "tool_use", "name": "Bash", "input": {}},
                        {"type": "tool_use", "name": "Read", "input": {}},
                    ],
                },
            },
        ],
    )

    result = extract_session_stats([str(session_dir)])

    assert len(result.rows) == 1
    row = result.rows[0]
    assert set(row) == {
        "stat_date",
        "session_count",
        "user_messages",
        "assistant_messages",
        "tool_calls",
        "distinct_tools",
    }
    assert row["stat_date"] == "2026-07-07"
    assert row["session_count"] == 1
    assert row["user_messages"] == 1
    assert row["assistant_messages"] == 1
    assert row["tool_calls"] == 2
    assert row["distinct_tools"] == 2
    assert result.skipped_dirs == []
    assert result.skipped_lines == 0


def test_extract_session_stats_aggregates_multiple_files_same_day(tmp_path):
    session_dir = tmp_path / "-Users-example-Developer"
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [{"type": "user", "timestamp": "2026-07-08T01:00:00.000Z", "message": {"role": "user", "content": "a"}}],
    )
    _write_jsonl(
        session_dir / "session-b.jsonl",
        [{"type": "user", "timestamp": "2026-07-08T02:00:00.000Z", "message": {"role": "user", "content": "b"}}],
    )

    result = extract_session_stats([str(session_dir)])

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["stat_date"] == "2026-07-08"
    assert row["session_count"] == 2
    assert row["user_messages"] == 2


def test_extract_session_stats_excludes_dir_via_argument(tmp_path):
    excluded_dir = tmp_path / "-Users-example-Developer-synthetic-excluded"
    excluded_dir.mkdir()
    _write_jsonl(
        excluded_dir / "secret.jsonl",
        [{"type": "user", "timestamp": "2026-07-09T00:00:00.000Z", "message": {"role": "user", "content": "secret"}}],
    )

    result = extract_session_stats(
        [str(excluded_dir)], excluded_dir_substrings=("-synthetic-excluded",)
    )

    assert result.rows == []
    assert result.skipped_dirs == ["-Users-example-Developer-synthetic-excluded"]


def test_extract_session_stats_excludes_dir_via_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("AOW_EXCLUDED_DIRS", "-synthetic-excluded")
    excluded_dir = tmp_path / "-Users-example-Developer-synthetic-excluded"
    excluded_dir.mkdir()
    _write_jsonl(
        excluded_dir / "secret.jsonl",
        [{"type": "user", "timestamp": "2026-07-09T00:00:00.000Z", "message": {"role": "user", "content": "secret"}}],
    )

    result = extract_session_stats([str(excluded_dir)])

    assert result.rows == []
    assert result.skipped_dirs == ["-Users-example-Developer-synthetic-excluded"]


def test_extract_session_stats_default_excluded_dirs_empty_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("AOW_EXCLUDED_DIRS", raising=False)
    session_dir = tmp_path / "-Users-example-Developer"
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [{"type": "user", "timestamp": "2026-07-09T00:00:00.000Z", "message": {"role": "user", "content": "hi"}}],
    )

    result = extract_session_stats([str(session_dir)])

    assert result.skipped_dirs == []
    assert len(result.rows) == 1


def test_extract_session_stats_falls_back_to_mtime_when_no_timestamp(tmp_path):
    session_dir = tmp_path / "-Users-example-Developer"
    session_dir.mkdir()
    jsonl_path = session_dir / "session-no-ts.jsonl"
    _write_jsonl(
        jsonl_path,
        [{"type": "user", "message": {"role": "user", "content": "no timestamp field"}}],
    )
    known_mtime = datetime(2026, 6, 15, tzinfo=UTC).timestamp()
    os.utime(jsonl_path, (known_mtime, known_mtime))

    result = extract_session_stats([str(session_dir)])

    assert len(result.rows) == 1
    assert result.rows[0]["stat_date"] == "2026-06-15"


def test_extract_session_stats_counts_skipped_lines(tmp_path):
    session_dir = tmp_path / "-Users-example-Developer"
    session_dir.mkdir()
    jsonl_path = session_dir / "session-mixed.jsonl"
    jsonl_path.write_text(
        json.dumps({"type": "user", "timestamp": "2026-07-10T00:00:00.000Z", "message": {"role": "user", "content": "hi"}})
        + "\n"
        + json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-07-10T00:00:01.000Z"})
        + "\n"
        + "{not valid json\n",
        encoding="utf-8",
    )

    result = extract_session_stats([str(session_dir)])

    assert len(result.rows) == 1
    assert result.rows[0]["user_messages"] == 1
    assert result.skipped_lines == 2


def test_extract_session_stats_missing_directory_returns_empty(tmp_path):
    missing_dir = tmp_path / "does-not-exist"

    result = extract_session_stats([str(missing_dir)])

    assert result.rows == []
    assert result.skipped_dirs == []
    assert result.skipped_lines == 0
