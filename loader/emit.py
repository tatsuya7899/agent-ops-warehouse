"""Write extraction results to newline-delimited JSON (NDJSON) files.

NDJSON is the intermediate artifact consumed by `bq load` in later
phases (CHECKLIST P0-7). This module has no BigQuery / cloud dependency
by design (SPEC-agent-ops-warehouse.md Section 5, P0 scope).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def stamp_loaded_at(rows: list[dict], loaded_at: str | None = None) -> list[dict]:
    """Return copies of rows with a loaded_at field stamped on each.

    loaded_at is a load-time attribute, not an extraction-time one, so
    extractors stay deterministic/pure and this stamping happens only at
    emission time.
    """
    stamp = loaded_at or datetime.now(UTC).isoformat()
    return [{**row, "loaded_at": stamp} for row in rows]


def write_ndjson(rows: list[dict], out_path: Path) -> int:
    """Write rows as NDJSON to out_path. Returns the number of rows written."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")
    return len(rows)


def build_load_run(
    source: str,
    rows_loaded: int,
    exclusions_note: str,
    run_at: str | None = None,
) -> dict:
    """Build one raw_load_runs row (SPEC Section 3.2)."""
    return {
        "run_at": run_at or datetime.now(UTC).isoformat(),
        "source": source,
        "rows_loaded": rows_loaded,
        "exclusions_note": exclusions_note,
    }
