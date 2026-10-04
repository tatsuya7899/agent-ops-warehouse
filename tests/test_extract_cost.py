"""Tests for loader.extract_cost.

All fixtures are synthetic *.jsonl files created under tmp_path -- no
real ~/.claude/projects/ transcript content (prompts, tool inputs,
file paths) is read by tests (SPEC-agent-ops-warehouse.md Section 2:
fixtures are synthetic data only; only aggregate counts ever leave this
module).

Acceptance basis: SPEC-telemetry-dashboard_design.md -- raw_cost rows are
daily per-model aggregates of the assistant-message `usage` fields
(input_tokens / output_tokens / cache_creation_input_tokens /
cache_read_input_tokens); USD is estimated from a static per-model
price table and stays None for models the table does not price; the
extractor reuses extract_session_stats' exclusion mechanism
(AOW_EXCLUDED_DIRS) and converts internal exceptions into a note rather
than raising (loader.__main__ has no per-extractor try/except).
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loader import extract_cost
from loader.extract_cost import extract_cost as extract_cost_fn

ROW_KEYS = {
    "stat_date",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cost_usd",
}


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False))
            f.write("\n")


def _usage(input_tokens=0, output_tokens=0, cache_creation=0, cache_read=0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }


def _assistant(model: str | None, usage: dict | None, timestamp="2026-10-01T00:00:00.000Z") -> dict:
    record = {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    }
    if model is not None:
        record["message"]["model"] = model
    if usage is not None:
        record["message"]["usage"] = usage
    return record


def _session_dir(tmp_path: Path, name="-Users-example-Developer") -> Path:
    session_dir = tmp_path / name
    session_dir.mkdir()
    return session_dir


def test_extract_cost_aggregates_per_day_and_model(tmp_path):
    session_dir = _session_dir(tmp_path)
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [
            _assistant("claude-sonnet-5", _usage(100, 10), "2026-10-01T01:00:00.000Z"),
            _assistant("claude-sonnet-5", _usage(200, 20), "2026-10-01T02:00:00.000Z"),
            _assistant("claude-opus-5", _usage(50, 5), "2026-10-01T03:00:00.000Z"),
            _assistant("claude-sonnet-5", _usage(400, 40), "2026-10-02T00:30:00.000Z"),
        ],
    )

    result = extract_cost_fn([str(session_dir)])

    assert len(result.rows) == 3
    by_key = {(r["stat_date"], r["model"]): r for r in result.rows}
    assert by_key[("2026-10-01", "claude-sonnet-5")]["input_tokens"] == 300
    assert by_key[("2026-10-01", "claude-sonnet-5")]["output_tokens"] == 30
    assert by_key[("2026-10-01", "claude-opus-5")]["input_tokens"] == 50
    assert by_key[("2026-10-02", "claude-sonnet-5")]["input_tokens"] == 400
    # rows sorted deterministically by (stat_date, model)
    assert [(r["stat_date"], r["model"]) for r in result.rows] == sorted(
        (r["stat_date"], r["model"]) for r in result.rows
    )
    assert result.skipped_dirs == []
    assert result.note == ""


def test_extract_cost_row_keys_match_raw_cost_contract(tmp_path):
    session_dir = _session_dir(tmp_path)
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [_assistant("claude-sonnet-5", _usage(1, 2, 3, 4))],
    )

    result = extract_cost_fn([str(session_dir)])

    assert len(result.rows) == 1
    # aggregates only: no transcript text, prompts, or file paths ever
    # leave this module (same boundary as extract_session_stats)
    assert set(result.rows[0]) == ROW_KEYS


def test_extract_cost_sums_cache_token_fields(tmp_path):
    session_dir = _session_dir(tmp_path)
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [
            _assistant("claude-sonnet-5", _usage(10, 1, 1000, 2000)),
            _assistant("claude-sonnet-5", _usage(5, 2, 3000, 4000)),
        ],
    )

    row = extract_cost_fn([str(session_dir)]).rows[0]
    assert row["cache_creation_input_tokens"] == 4000
    assert row["cache_read_input_tokens"] == 6000


def test_extract_cost_estimates_usd_from_static_price_table(tmp_path, monkeypatch):
    """Known-model rows get a USD estimate computed from the static
    per-model price table (per-million-token rates)."""
    monkeypatch.setattr(
        extract_cost,
        "MODEL_PRICES_USD_PER_MTOK",
        {"test-model": (1.0, 2.0, 3.0, 4.0)},
    )
    session_dir = _session_dir(tmp_path)
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [_assistant("test-model", _usage(1_000_000, 1_000_000, 1_000_000, 1_000_000))],
    )

    row = extract_cost_fn([str(session_dir)]).rows[0]
    # (1e6*1 + 1e6*2 + 1e6*3 + 1e6*4) / 1e6 = 10.0 USD
    assert row["cost_usd"] == pytest.approx(10.0)


def test_extract_cost_real_price_table_prices_known_model(tmp_path):
    """Contract check on the shipped table itself: a listed model gets a
    numeric estimate..."""
    session_dir = _session_dir(tmp_path)
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [_assistant("claude-sonnet-5", _usage(1000, 1000, 0, 0))],
    )

    row = extract_cost_fn([str(session_dir)]).rows[0]
    assert isinstance(row["cost_usd"], float)
    assert row["cost_usd"] > 0


def test_extract_cost_unknown_model_gets_null_cost_not_fabricated(tmp_path):
    """...while a model absent from the price table keeps cost_usd=None
    (tokens still aggregate; no fabricated numbers)."""
    session_dir = _session_dir(tmp_path)
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [_assistant("claude-fable-5", _usage(123, 45))],
    )

    row = extract_cost_fn([str(session_dir)]).rows[0]
    assert row["cost_usd"] is None
    assert row["input_tokens"] == 123
    assert row["output_tokens"] == 45


def test_extract_cost_missing_model_and_missing_usage(tmp_path):
    session_dir = _session_dir(tmp_path)
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [
            # assistant record carrying no usage field -> skipped, counted
            _assistant("claude-sonnet-5", None),
            # usage present but no model -> aggregated under null model
            _assistant(None, _usage(7, 8)),
        ],
    )

    result = extract_cost_fn([str(session_dir)])

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["model"] is None
    assert row["cost_usd"] is None
    assert row["input_tokens"] == 7
    assert result.skipped_lines == 1


def test_extract_cost_user_records_are_not_counted_as_skipped(tmp_path):
    """Only assistant lines can carry usage; user/queue-operation lines
    are simply out of scope for this extractor (they are session_stats'
    domain), so they must not inflate skipped_lines."""
    session_dir = _session_dir(tmp_path)
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [
            {"type": "user", "timestamp": "2026-10-01T00:00:00.000Z", "message": {"role": "user", "content": "hi"}},
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-01T00:00:01.000Z"},
            _assistant("claude-sonnet-5", _usage(3, 1)),
        ],
    )

    result = extract_cost_fn([str(session_dir)])

    assert result.skipped_lines == 0
    assert result.rows[0]["input_tokens"] == 3


def test_extract_cost_non_dict_usage_and_broken_json_count_as_skipped(tmp_path):
    session_dir = _session_dir(tmp_path)
    path = session_dir / "session-mixed.jsonl"
    path.write_text(
        json.dumps(_assistant("claude-sonnet-5", "not-a-dict"))
        + "\n"
        + "{not valid json\n"
        + json.dumps(_assistant("claude-sonnet-5", _usage(9, 9)))
        + "\n",
        encoding="utf-8",
    )

    result = extract_cost_fn([str(session_dir)])

    assert result.skipped_lines == 2
    assert len(result.rows) == 1
    assert result.rows[0]["input_tokens"] == 9


def test_extract_cost_empty_file_and_missing_dir(tmp_path):
    session_dir = _session_dir(tmp_path)
    (session_dir / "empty.jsonl").write_text("", encoding="utf-8")

    result = extract_cost_fn([str(session_dir), str(tmp_path / "does-not-exist")])

    assert result.rows == []
    assert result.skipped_dirs == []
    assert result.note == ""


def test_extract_cost_excludes_dir_via_argument(tmp_path):
    excluded_dir = _session_dir(tmp_path, "-Users-example-Developer-synthetic-excluded")
    _write_jsonl(
        excluded_dir / "secret.jsonl",
        [_assistant("claude-sonnet-5", _usage(999, 999))],
    )

    result = extract_cost_fn(
        [str(excluded_dir)], excluded_dir_substrings=("-synthetic-excluded",)
    )

    assert result.rows == []
    assert result.skipped_dirs == ["-Users-example-Developer-synthetic-excluded"]


def test_extract_cost_excludes_dir_via_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("AOW_EXCLUDED_DIRS", "-synthetic-excluded")
    excluded_dir = _session_dir(tmp_path, "-Users-example-Developer-synthetic-excluded")
    _write_jsonl(
        excluded_dir / "secret.jsonl",
        [_assistant("claude-sonnet-5", _usage(999, 999))],
    )

    result = extract_cost_fn([str(excluded_dir)])

    assert result.rows == []
    assert result.skipped_dirs == ["-Users-example-Developer-synthetic-excluded"]


def test_extract_cost_date_comes_from_each_records_own_timestamp(tmp_path):
    """A session crossing midnight contributes to two stat_dates --
    usage is attributed to the record's own timestamp, not the file's
    first-timestamp day (extract_session_stats' file-level rule would
    misattribute the second day's spend)."""
    session_dir = _session_dir(tmp_path)
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [
            _assistant("claude-sonnet-5", _usage(10, 1), "2026-10-01T23:59:00.000Z"),
            _assistant("claude-sonnet-5", _usage(20, 2), "2026-10-02T00:01:00.000Z"),
        ],
    )

    result = extract_cost_fn([str(session_dir)])

    assert {r["stat_date"] for r in result.rows} == {"2026-10-01", "2026-10-02"}


def test_extract_cost_falls_back_to_mtime_when_no_timestamp(tmp_path):
    session_dir = _session_dir(tmp_path)
    jsonl_path = session_dir / "session-no-ts.jsonl"
    record = _assistant("claude-sonnet-5", _usage(11, 1))
    del record["timestamp"]
    _write_jsonl(jsonl_path, [record])
    known_mtime = datetime(2026, 9, 20, tzinfo=UTC).timestamp()
    os.utime(jsonl_path, (known_mtime, known_mtime))

    result = extract_cost_fn([str(session_dir)])

    assert len(result.rows) == 1
    assert result.rows[0]["stat_date"] == "2026-09-20"


def test_extract_cost_unreadable_file_becomes_note_not_exception(tmp_path):
    """A *.jsonl entry that cannot be read (here: a directory matching
    the glob) must not raise -- __main__ has no per-extractor try/except,
    so failures convert into the result note and other files still
    scan."""
    session_dir = _session_dir(tmp_path)
    (session_dir / "broken.jsonl").mkdir()  # directory named *.jsonl -> read_text raises
    _write_jsonl(
        session_dir / "good.jsonl",
        [_assistant("claude-sonnet-5", _usage(5, 5))],
    )

    result = extract_cost_fn([str(session_dir)])

    assert len(result.rows) == 1  # the readable file still produced rows
    assert "broken.jsonl" in result.note


def test_extract_cost_usage_values_non_numeric_are_treated_as_zero(tmp_path):
    session_dir = _session_dir(tmp_path)
    _write_jsonl(
        session_dir / "session-a.jsonl",
        [
            _assistant("claude-sonnet-5", {"input_tokens": "NaN-ish", "output_tokens": 4}),
            _assistant("claude-sonnet-5", {"input_tokens": None}),
        ],
    )

    result = extract_cost_fn([str(session_dir)])

    assert len(result.rows) == 1
    assert result.rows[0]["input_tokens"] == 0
    assert result.rows[0]["output_tokens"] == 4


def test_main_wires_extract_cost_to_raw_cost_ndjson_and_load_run(tmp_path):
    """`python -m loader --sessions <dir> --out <dir>` emits
    raw_cost.ndjson (loaded_at stamped, same as every raw table) and a
    raw_load_runs ledger entry -- sharing the --sessions argument with
    extract_session_stats (SPEC-telemetry-dashboard_design.md)."""
    from loader.__main__ import run

    session_dir = _session_dir(tmp_path)
    (session_dir / "session-a.jsonl").write_text(
        json.dumps(_assistant("claude-sonnet-5", _usage(10, 2, 100, 50)))
        + "\n{malformed json line\n",  # one unparseable line -> ledger note
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    run(["--sessions", str(session_dir), "--out", str(out_dir)])

    cost_path = out_dir / "raw_cost.ndjson"
    assert cost_path.exists()
    cost_rows = [json.loads(line) for line in cost_path.read_text().splitlines() if line.strip()]
    assert len(cost_rows) == 1
    assert set(cost_rows[0]) == ROW_KEYS | {"loaded_at"}
    assert cost_rows[0]["stat_date"] == "2026-10-01"
    assert cost_rows[0]["input_tokens"] == 10
    assert cost_rows[0]["cache_creation_input_tokens"] == 100

    load_runs = [
        json.loads(line)
        for line in (out_dir / "raw_load_runs.ndjson").read_text().splitlines()
        if line.strip()
    ]
    cost_run = next(r for r in load_runs if r["source"] == "raw_cost")
    assert cost_run["rows_loaded"] == 1
    assert "skipped_lines=1" in cost_run["exclusions_note"]
