"""Extract daily per-model token usage and a USD estimate from Claude
Code project jsonl transcripts, for the raw_cost table
(SPEC-telemetry-dashboard_design.md).

Only aggregate counts (per calendar day, per model) ever leave this
module -- no jsonl body text (prompts, tool inputs/outputs, file paths)
is emitted, the same boundary as extract_session_stats
(SPEC-agent-ops-warehouse.md Section 2).

The exclusion mechanism is inherited unchanged: directory basenames
matching any AOW_EXCLUDED_DIRS substring are skipped and reported in
CostResult.skipped_dirs (the same never-silently-dropped pattern; tests
can pass `excluded_dir_substrings` explicitly to override the env var).

Unlike extract_session_stats -- which attributes a whole session file
to its first timestamp's day -- usage here is attributed to each
record's own timestamp date, so a session crossing midnight bills each
day correctly. Records with no timestamp fall back to the file's mtime
date (UTC, host-timezone independent), mirroring the session_stats
fallback.

Failures never raise: __main__ has no per-extractor try/except, so an
unreadable file would otherwise abort every extractor that follows.
Scan errors are appended to CostResult.note for the raw_load_runs
ledger instead (the same soft-fail contract as
extract_kpi_snapshots' note field).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from loader.extract_session_stats import _default_excluded_dirs

# USD per 1M tokens, as of 2026-10: static Anthropic API list-price
# estimate by model family tier -- (input, output, cache_creation,
# cache_read). Models with no published price (e.g. "claude-fable-5*")
# and ambiguous aliases ("opus", "sonnet", "fable", "<synthetic>") are
# deliberately absent: their rows keep the token counts but report
# cost_usd=None rather than a fabricated number.
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    # Opus tier: $15 input / $75 output / $18.75 cache-write / $1.50 cache-read
    "claude-opus-5": (15.0, 75.0, 18.75, 1.50),
    "claude-opus-5-5": (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-8": (15.0, 75.0, 18.75, 1.50),
    # Sonnet tier: $3 / $15 / $3.75 / $0.30
    "claude-sonnet-5": (3.0, 15.0, 3.75, 0.30),
    # Haiku 4.5 tier: $1 / $5 / $1.25 / $0.10
    "claude-haiku-4-5-20251001": (1.0, 5.0, 1.25, 0.10),
}


@dataclass
class CostResult:
    rows: list[dict] = field(default_factory=list)
    skipped_dirs: list[str] = field(default_factory=list)
    skipped_lines: int = 0
    note: str = ""


def extract_cost(
    session_dirs: list[str],
    excluded_dir_substrings: tuple[str, ...] | None = None,
) -> CostResult:
    """Extract daily per-model rows (raw_cost schema) from the given
    `~/.claude/projects/<dir>/` session directories.

    skipped_lines counts unparseable lines plus assistant records that
    carry no usable usage field -- never silently dropped. Non-assistant
    records (user, queue-operation, ...) carry no usage data and are
    ignored without counting (they are extract_session_stats' domain).
    """
    if excluded_dir_substrings is None:
        excluded_dir_substrings = _default_excluded_dirs()

    buckets: dict[tuple[str, str | None], dict] = {}
    skipped_dirs: list[str] = []
    notes: list[str] = []
    skipped_lines = 0

    for dir_path in session_dirs:
        directory = Path(dir_path)

        if any(substring in directory.name for substring in excluded_dir_substrings):
            skipped_dirs.append(directory.name)
            continue

        if not directory.exists():
            continue

        for jsonl_path in sorted(directory.glob("*.jsonl")):
            try:
                skipped_lines += _scan_file(jsonl_path, buckets)
            except Exception as exc:  # noqa: BLE001 -- soft-fail by design (module docstring)
                notes.append(f"{jsonl_path.name}: {type(exc).__name__}: {exc}")

    rows = [
        _build_row(stat_date, model, buckets[(stat_date, model)])
        for stat_date, model in sorted(buckets, key=lambda k: (k[0], k[1] or ""))
    ]
    return CostResult(
        rows=rows,
        skipped_dirs=skipped_dirs,
        skipped_lines=skipped_lines,
        note="; ".join(notes),
    )


def _build_row(stat_date: str, model: str | None, bucket: dict) -> dict:
    return {
        "stat_date": stat_date,
        "model": model,
        "input_tokens": bucket["input_tokens"],
        "output_tokens": bucket["output_tokens"],
        "cache_creation_input_tokens": bucket["cache_creation_input_tokens"],
        "cache_read_input_tokens": bucket["cache_read_input_tokens"],
        "cost_usd": _estimate_usd(model, bucket),
    }


def _estimate_usd(model: str | None, bucket: dict) -> float | None:
    prices = MODEL_PRICES_USD_PER_MTOK.get(model) if model else None
    if prices is None:
        return None
    input_price, output_price, cache_write_price, cache_read_price = prices
    usd = (
        bucket["input_tokens"] * input_price
        + bucket["output_tokens"] * output_price
        + bucket["cache_creation_input_tokens"] * cache_write_price
        + bucket["cache_read_input_tokens"] * cache_read_price
    ) / 1_000_000
    return round(usd, 6)


def _scan_file(jsonl_path: Path, buckets: dict[tuple[str, str | None], dict]) -> int:
    """Scan one session file, folding per-record usage into buckets
    keyed by (stat_date, model). Returns the skipped-line count."""
    fallback_date: str | None = None
    skipped_lines = 0

    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            skipped_lines += 1
            continue

        if record.get("type") != "assistant":
            continue  # no usage data -- not counted as skipped (see docstring)

        message = record.get("message") or {}
        usage = message.get("usage")
        if not isinstance(usage, dict):
            skipped_lines += 1
            continue

        stat_date = _record_date(record)
        if stat_date is None:
            if fallback_date is None:
                fallback_date = _mtime_date(jsonl_path)
            stat_date = fallback_date

        model = message.get("model")
        bucket = buckets.setdefault(
            (stat_date, model if isinstance(model, str) else None),
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )
        bucket["input_tokens"] += _token_count(usage.get("input_tokens"))
        bucket["output_tokens"] += _token_count(usage.get("output_tokens"))
        bucket["cache_creation_input_tokens"] += _token_count(
            usage.get("cache_creation_input_tokens")
        )
        bucket["cache_read_input_tokens"] += _token_count(
            usage.get("cache_read_input_tokens")
        )

    return skipped_lines


def _record_date(record: dict) -> str | None:
    ts = record.get("timestamp")
    if isinstance(ts, str) and len(ts) >= 10:
        return ts[:10]
    return None


def _token_count(value) -> int:
    """Coerce a usage counter to int; malformed values (strings, None,
    bools) contribute 0 rather than aborting the file."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _mtime_date(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime("%Y-%m-%d")
