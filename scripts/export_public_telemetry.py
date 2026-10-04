"""Export the public telemetry JSON from BigQuery
(SPEC-telemetry-dashboard_design.md).

Three-stage design: BigQuery dash_*/mart_* views + raw.cost -> this
script -> telemetry.json (a generated artifact, committed on the
publishing side). The page renders the file; there is no live query
path, no API, no auth surface.

Secrecy boundary is enforced twice, once per layer:
1. QUERY_TEMPLATES is a fixed allowlist -- column names are written out
   in full and anything not listed is never selected (default-deny;
   the SPEC's exclusion table records why evidence_*/streak/status
   columns stay out).
2. EMITTED_KEYS projects each returned row down to the published key
   set, so even a future query change cannot leak extra columns into
   the output.

The BigQuery dependency is injectable: build_payload()/main() take a
`query_fn(sql) -> rows` callable. The default implementation lazy-
imports google.cloud.bigquery inside the factory, so this module -- and
the whole test suite -- works with no cloud SDK installed.

Modes (mutually exclusive):
  --out PATH           query BQ and write the JSON (temp+rename atomic:
                       a failed run never corrupts the committed file --
                       SPEC scenario 3)
  --out PATH --check   regenerate and diff the metrics subtree against
                       the committed file (generated_at/data_as_of are
                       excluded -- they change every run, SPEC FR-2)
  --out PATH --verify  audit mode: re-query BQ and report per-series
                       value match, including data_as_of (stricter than
                       --check; for the "BQ value matches JSON" success
                       criterion)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

DEFAULT_PROJECT = "agent-ops-warehouse"

# --- Allowlist (SPEC データモデル・インターフェース section) -------------
# Every metric maps to exactly one fixed SQL. dash_* views live in the
# marts dataset (dbt_project.yml: dashboard -> +schema marts); raw.cost
# is the Terraform-managed raw layer table.
QUERY_TEMPLATES: dict[str, str] = {
    "ship_velocity_monthly": (
        "SELECT month, articles, commits, x_posts, total\n"
        "FROM `{project}.marts.dash_ship_velocity`\n"
        "ORDER BY month"
    ),
    # month is selected only to derive data_as_of; it is NOT emitted
    # (SPEC exclusion table: dimension already published via ship_velocity)
    "publish_cadence": (
        "SELECT month, avg_gap_days\n"
        "FROM `{project}.marts.dash_publish_cadence`\n"
        "ORDER BY month DESC LIMIT 1"
    ),
    "agent_sessions_weekly": (
        "SELECT week_start, session_count AS sessions, user_messages,\n"
        "       assistant_messages, tool_calls\n"
        "FROM `{project}.marts.mart_agent_activity`\n"
        "ORDER BY week_start"
    ),
    "kpi_history": (
        "SELECT snapshot_date AS date, ships_this_month,\n"
        "       publications_last_two_weeks\n"
        "FROM `{project}.marts.dash_kpi_history`\n"
        "ORDER BY snapshot_date"
    ),
    # raw.cost is per (stat_date, model); the published series is per day.
    # unpriced_rows counts model-day buckets whose cost_usd is NULL (models
    # absent from the price table): SUM() silently drops them while the token
    # sums still include them, so the count must be emitted for the page to
    # disclose that the total can understate real spend.
    "cost_daily": (
        "SELECT stat_date AS date, ROUND(SUM(cost_usd), 2) AS cost_usd,\n"
        "       SUM(input_tokens) AS input_tokens,\n"
        "       SUM(output_tokens) AS output_tokens,\n"
        "       COUNTIF(cost_usd IS NULL) AS unpriced_rows\n"
        "FROM `{project}.raw.cost`\n"
        "GROUP BY stat_date\n"
        "ORDER BY stat_date"
    ),
}

# The published key set per metric -- output rows are projected through
# this, so emitted keys can never exceed the allowlist.
EMITTED_KEYS: dict[str, tuple[str, ...]] = {
    "ship_velocity_monthly": ("month", "articles", "commits", "x_posts", "total"),
    "publish_cadence": ("avg_gap_days",),
    "agent_sessions_weekly": (
        "week_start",
        "sessions",
        "user_messages",
        "assistant_messages",
        "tool_calls",
    ),
    "kpi_history": ("date", "ships_this_month", "publications_last_two_weeks"),
    "cost_daily": (
        "date",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "unpriced_rows",
    ),
}

# Which field carries each series' own "last data date" for data_as_of
# (the max across all series). publish_cadence uses the fetched-but-
# unemitted month.
_LAST_DATE_FIELD: dict[str, str] = {
    "ship_velocity_monthly": "month",
    "publish_cadence": "month",
    "agent_sessions_weekly": "week_start",
    "kpi_history": "date",
    "cost_daily": "date",
}

QueryFn = Callable[[str], Iterable]


def build_queries(project: str) -> dict[str, str]:
    """Render the allowlist SQL for a GCP project. Returns
    metric-name -> SQL in template order."""
    return {
        name: template.format(project=project)
        for name, template in QUERY_TEMPLATES.items()
    }


def default_query_fn(project: str) -> QueryFn:
    """Real BigQuery-backed query function. google.cloud.bigquery is
    imported lazily here -- never at module import -- so tests and CI
    need no cloud SDK."""
    from google.cloud import bigquery

    client = bigquery.Client(project=project)

    def query(sql: str) -> list[dict]:
        return client.query(sql).result()

    return query


def _jsonable(value):
    """Normalize BQ value types (date/datetime/Decimal) into JSON-safe
    values; everything else passes through unchanged."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row_to_dict(row) -> dict:
    return {key: _jsonable(value) for key, value in dict(row).items()}


def _project_row(row: dict, keys: tuple[str, ...]) -> dict:
    return {key: row.get(key) for key in keys}


def _month_to_date(value) -> str | None:
    """A monthly bucket ('2026-09') contributes its first day as the
    comparable date for data_as_of; full dates pass through."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) > 7 else f"{text}-01"


def _series_last_date(metric: str, rows: list[dict]) -> str | None:
    field = _LAST_DATE_FIELD[metric]
    dates = [_month_to_date(row.get(field)) for row in rows]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def build_payload(
    query_fn: QueryFn,
    project: str = DEFAULT_PROJECT,
    generated_at: str | None = None,
) -> dict:
    """Run every allowlist query through `query_fn` and build the
    telemetry.json payload. Pure given the injected query function --
    no I/O, no clock unless generated_at is left None."""
    queries = build_queries(project)
    raw_rows: dict[str, list[dict]] = {
        name: [_row_to_dict(row) for row in query_fn(sql)]
        for name, sql in queries.items()
    }

    metrics: dict[str, object] = {}
    for name, rows in raw_rows.items():
        keys = EMITTED_KEYS[name]
        if name == "publish_cadence":
            metrics[name] = _project_row(rows[0], keys) if rows else {"avg_gap_days": None}
        else:
            metrics[name] = [_project_row(row, keys) for row in rows]

    last_dates = [
        _series_last_date(name, rows) for name, rows in raw_rows.items()
    ]
    data_as_of = max((d for d in last_dates if d), default=None)

    return {
        "generated_at": generated_at
        or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "data_as_of": data_as_of,
        "metrics": metrics,
    }


def _write_json_atomic(payload: dict, out_path: Path) -> None:
    """temp+rename write: a crash mid-write leaves the committed file
    untouched (SPEC scenario 3)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp_path, out_path)


def _diff_report(fresh: dict, existing: dict) -> list[str]:
    """Per-series mismatch names between two payloads' metrics subtree
    (plus data_as_of) -- empty list means the file is faithful."""
    diffs = []
    fresh_metrics = fresh["metrics"]
    existing_metrics = existing.get("metrics", {})
    for name in sorted(set(fresh_metrics) | set(existing_metrics)):
        if fresh_metrics.get(name) != existing_metrics.get(name):
            diffs.append(name)
    return diffs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="export_public_telemetry",
        description="Export public telemetry JSON from BigQuery (allowlist SQL).",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Path to telemetry.json -- write target, or the file to compare against with --check/--verify.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Regenerate and diff the metrics subtree against --out (generated_at/data_as_of excluded). Exit 1 on drift.",
    )
    mode.add_argument(
        "--verify",
        action="store_true",
        help="Re-query BigQuery and report per-series value match including data_as_of. Exit 1 on mismatch.",
    )
    parser.add_argument(
        "--project",
        default=DEFAULT_PROJECT,
        help="GCP project id for fully-qualified table names.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, query_fn: QueryFn | None = None) -> int:
    args = parse_args(argv)
    out_path = Path(args.out)

    if query_fn is None:
        query_fn = default_query_fn(args.project)

    try:
        fresh = build_payload(query_fn, project=args.project)
    except Exception as exc:  # noqa: BLE001 -- a failed export must leave the committed JSON untouched
        print(f"export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.check or args.verify:
        if not out_path.exists():
            print(f"no file at {out_path} to compare against", file=sys.stderr)
            return 1
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"{out_path} is not valid JSON: {exc}", file=sys.stderr)
            return 1
        diffs = _diff_report(fresh, existing)

        if args.verify:
            for name in sorted(set(fresh["metrics"]) | set(existing.get("metrics", {}))):
                status = "MISMATCH" if name in diffs else "OK"
                print(f"{status} {name}")
            if fresh["data_as_of"] != existing.get("data_as_of"):
                diffs.append("data_as_of")
                print(
                    f"MISMATCH data_as_of: file={existing.get('data_as_of')} bq={fresh['data_as_of']}"
                )
            else:
                print(f"OK data_as_of ({fresh['data_as_of']})")
            return 0 if not diffs else 1

        # --check: metrics subtree only (generated_at/data_as_of excluded)
        if diffs:
            print(f"CHECK FAIL: metrics drift in {', '.join(diffs)}", file=sys.stderr)
            return 1
        print("CHECK PASS: metrics match committed file")
        return 0

    _write_json_atomic(fresh, out_path)
    print(
        f"wrote {out_path} (data_as_of={fresh['data_as_of']}, "
        f"{sum(len(v) for v in fresh['metrics'].values() if isinstance(v, list))} rows)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
