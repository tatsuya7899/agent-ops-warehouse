"""Tests for scripts.export_public_telemetry.

Acceptance basis: SPEC-telemetry-dashboard_design.md ("データモデル・
インターフェース" and "非機能" sections) -- the exporter queries BigQuery
through a fixed allowlist SQL set (column names explicit, default-deny),
emits {generated_at, data_as_of, metrics{...}} JSON via temp+rename
atomic writes, and supports --check (metrics-subtree re-generation diff,
generated_at/data_as_of excluded) and --verify (re-query BQ, report
value match) modes.

No real BigQuery access: the query function is injected, so unit tests
run entirely on canned row sets (SPEC-agent-ops-warehouse.md Section 2:
fixtures are synthetic only).
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from scripts import export_public_telemetry as ept
from scripts.export_public_telemetry import build_payload, main

ALLOWED_METRICS = {
    "ship_velocity_monthly",
    "publish_cadence",
    "agent_sessions_weekly",
    "kpi_history",
    "cost_daily",
}


def _canned_rows() -> dict[str, list[dict]]:
    """Table-name -> rows the fake BQ returns. Keys are marker substrings
    of the allowlist SQL's FROM clause."""
    return {
        "dash_ship_velocity": [
            {"month": "2026-08", "articles": 2, "commits": 80, "x_posts": 1, "total": 3},
            {"month": "2026-09", "articles": 1, "commits": 121, "x_posts": 1, "total": 5},
        ],
        "dash_publish_cadence": [{"month": "2026-09", "avg_gap_days": 8.2}],
        "mart_agent_activity": [
            {
                "week_start": "2026-09-21",
                "sessions": 4,
                "user_messages": 50,
                "assistant_messages": 100,
                "tool_calls": 140,
            },
            {
                "week_start": "2026-09-28",
                "sessions": 3,
                "user_messages": 40,
                "assistant_messages": 90,
                "tool_calls": 120,
            },
        ],
        "dash_kpi_history": [
            {"date": "2026-10-04", "ships_this_month": 5, "publications_last_two_weeks": 5},
        ],
        "raw.cost": [
            {"date": "2026-10-03", "cost_usd": 1.23, "input_tokens": 10, "output_tokens": 20, "unpriced_rows": 0},
            {"date": "2026-10-04", "cost_usd": 0.5, "input_tokens": 5, "output_tokens": 5, "unpriced_rows": 1},
        ],
    }


def _fake_query(responses: dict[str, list[dict]]):
    """Injectable stand-in for the BigQuery query function: routes on
    the table-name marker inside the SQL text and records every call."""
    calls: list[str] = []

    def query(sql: str) -> list[dict]:
        calls.append(sql)
        for marker, rows in responses.items():
            if marker in sql:
                return [dict(r) for r in rows]
        return []

    query.calls = calls
    return query


def test_build_payload_emits_documented_schema_shape():
    payload = build_payload(_fake_query(_canned_rows()), project="test-project")

    assert set(payload) == {"generated_at", "data_as_of", "metrics"}
    assert set(payload["metrics"]) == ALLOWED_METRICS


def test_build_payload_rows_pass_through_allowlist_columns_only():
    payload = build_payload(_fake_query(_canned_rows()), project="test-project")
    metrics = payload["metrics"]

    assert metrics["ship_velocity_monthly"] == [
        {"month": "2026-08", "articles": 2, "commits": 80, "x_posts": 1, "total": 3},
        {"month": "2026-09", "articles": 1, "commits": 121, "x_posts": 1, "total": 5},
    ]
    assert metrics["agent_sessions_weekly"][0] == {
        "week_start": "2026-09-21",
        "sessions": 4,
        "user_messages": 50,
        "assistant_messages": 100,
        "tool_calls": 140,
    }
    assert metrics["kpi_history"] == [
        {"date": "2026-10-04", "ships_this_month": 5, "publications_last_two_weeks": 5}
    ]
    assert metrics["cost_daily"] == [
        {"date": "2026-10-03", "cost_usd": 1.23, "input_tokens": 10, "output_tokens": 20, "unpriced_rows": 0},
        {"date": "2026-10-04", "cost_usd": 0.5, "input_tokens": 5, "output_tokens": 5, "unpriced_rows": 1},
    ]
    # publish_cadence emits avg_gap_days only -- the month used for
    # data_as_of is not part of the published metric (SPEC exclusion table)
    assert metrics["publish_cadence"] == {"avg_gap_days": 8.2}


def test_build_payload_strips_non_allowlist_columns_from_output():
    """Default-deny at the emit layer too: even if a query returns extra
    columns, only the allowlisted keys reach the JSON."""
    responses = _canned_rows()
    responses["dash_kpi_history"] = [
        {
            "date": "2026-10-04",
            "ships_this_month": 5,
            "publications_last_two_weeks": 5,
            "evidence_ratio": 0.66,  # excluded series must not leak
            "streak_weeks": 9,
        }
    ]

    payload = build_payload(_fake_query(responses), project="test-project")

    assert payload["metrics"]["kpi_history"] == [
        {"date": "2026-10-04", "ships_this_month": 5, "publications_last_two_weeks": 5}
    ]


def test_data_as_of_is_max_of_each_series_last_date():
    payload = build_payload(_fake_query(_canned_rows()), project="test-project")

    # kpi_history's 2026-10-04 is the freshest among: months -> 2026-09-01,
    # week_start 2026-09-28, kpi 2026-10-04, cost 2026-10-03
    assert payload["data_as_of"] == "2026-10-04"


def test_data_as_of_normalizes_monthly_series_to_first_of_month():
    """When a monthly-bucketed series is the freshest source, its
    contribution is the first of the month (an ISO date), never a bare
    'YYYY-MM' string."""
    responses = {
        "dash_ship_velocity": [
            {"month": "2026-09", "articles": 1, "commits": 5, "x_posts": 0, "total": 1}
        ]
    }

    payload = build_payload(_fake_query(responses), project="test-project")

    assert payload["data_as_of"] == "2026-09-01"


def test_data_as_of_is_null_when_all_series_empty():
    payload = build_payload(_fake_query({}), project="test-project")

    assert payload["data_as_of"] is None
    assert payload["metrics"]["ship_velocity_monthly"] == []
    assert payload["metrics"]["publish_cadence"] == {"avg_gap_days": None}


def test_bq_types_are_normalized_to_json_values():
    """Real BQ rows carry datetime.date / Decimal -- they must land in
    JSON as ISO strings and plain numbers."""
    responses = {
        "mart_agent_activity": [
            {
                "week_start": date(2026, 9, 28),
                "sessions": Decimal("3"),
                "user_messages": 40,
                "assistant_messages": 90,
                "tool_calls": 120,
            }
        ],
        "dash_publish_cadence": [{"month": "2026-09", "avg_gap_days": Decimal("8.2")}],
    }

    payload = build_payload(_fake_query(responses), project="test-project")

    assert payload["metrics"]["agent_sessions_weekly"][0]["week_start"] == "2026-09-28"
    assert payload["metrics"]["publish_cadence"]["avg_gap_days"] == 8.2
    # the whole payload must be JSON-serializable as-is
    json.dumps(payload)


def test_each_allowlisted_source_queried_exactly_once():
    fake = _fake_query(_canned_rows())

    build_payload(fake, project="test-project")

    assert len(fake.calls) == len(ALLOWED_METRICS)
    joined = "\n".join(fake.calls)
    for table in (
        "dash_ship_velocity",
        "dash_publish_cadence",
        "mart_agent_activity",
        "dash_kpi_history",
        "raw.cost",
    ):
        assert table in joined
    assert "test-project" in joined


def test_allowlist_sql_selects_named_columns_only():
    """Contract test (SPEC テスト方針 #4): every allowlist query names its
    columns explicitly -- no SELECT *, and no excluded series (internal
    evidence/streak/status columns) anywhere in the SQL."""
    queries = ept.build_queries("test-project")
    joined = "\n".join(queries.values())

    assert "*" not in joined
    for forbidden in (
        "evidence_done",
        "evidence_target",
        "evidence_ratio",
        "streak_weeks",
        "cadence_status",
        "ship_status",
        "articles_published",
        "dash_kpi_current",
        "max_distinct_tools",
    ):
        assert forbidden not in joined
    # required selections are present verbatim
    assert "session_count AS sessions" in joined
    assert "snapshot_date AS date" in joined
    assert "ORDER BY month DESC LIMIT 1" in joined
    assert set(queries) == ALLOWED_METRICS


def test_out_writes_json_atomically(tmp_path):
    out_path = tmp_path / "data" / "telemetry.json"

    rc = main(["--out", str(out_path)], query_fn=_fake_query(_canned_rows()))

    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(payload) == {"generated_at", "data_as_of", "metrics"}
    assert set(payload["metrics"]) == ALLOWED_METRICS
    # temp+rename: no partial/backup files left behind
    siblings = [p.name for p in out_path.parent.iterdir()]
    assert siblings == ["telemetry.json"]


def test_check_passes_when_committed_file_is_fresh(tmp_path, capsys):
    out_path = tmp_path / "telemetry.json"
    fake = _fake_query(_canned_rows())
    main(["--out", str(out_path)], query_fn=fake)

    rc = main(["--out", str(out_path), "--check"], query_fn=_fake_query(_canned_rows()))

    assert rc == 0


def test_check_fails_when_file_metrics_are_stale(tmp_path):
    out_path = tmp_path / "telemetry.json"
    main(["--out", str(out_path)], query_fn=_fake_query(_canned_rows()))
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    payload["metrics"]["cost_daily"][0]["cost_usd"] = 999.0
    out_path.write_text(json.dumps(payload), encoding="utf-8")

    rc = main(["--out", str(out_path), "--check"], query_fn=_fake_query(_canned_rows()))

    assert rc == 1


def test_check_ignores_generated_at_and_data_as_of(tmp_path):
    """--check compares the metrics subtree only (SPEC: generated_at and
    data_as_of change every run and are excluded from the diff)."""
    out_path = tmp_path / "telemetry.json"
    main(["--out", str(out_path)], query_fn=_fake_query(_canned_rows()))
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    payload["generated_at"] = "1999-01-01T00:00:00Z"
    payload["data_as_of"] = "1999-01-01"
    out_path.write_text(json.dumps(payload), encoding="utf-8")

    rc = main(["--out", str(out_path), "--check"], query_fn=_fake_query(_canned_rows()))

    assert rc == 0


def test_verify_reports_per_series_match(tmp_path, capsys):
    out_path = tmp_path / "telemetry.json"
    main(["--out", str(out_path)], query_fn=_fake_query(_canned_rows()))

    rc = main(["--out", str(out_path), "--verify"], query_fn=_fake_query(_canned_rows()))

    assert rc == 0
    out = capsys.readouterr().out
    for metric in ALLOWED_METRICS:
        assert metric in out


def test_verify_reports_mismatch_including_data_as_of(tmp_path, capsys):
    """--verify is the audit-grade comparison: unlike --check it also
    flags a data_as_of that no longer matches what BQ returns."""
    out_path = tmp_path / "telemetry.json"
    main(["--out", str(out_path)], query_fn=_fake_query(_canned_rows()))
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    payload["data_as_of"] = "1999-01-01"
    out_path.write_text(json.dumps(payload), encoding="utf-8")

    rc = main(["--out", str(out_path), "--verify"], query_fn=_fake_query(_canned_rows()))

    assert rc == 1


def test_verify_fails_on_metric_value_mismatch(tmp_path):
    out_path = tmp_path / "telemetry.json"
    main(["--out", str(out_path)], query_fn=_fake_query(_canned_rows()))
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    payload["metrics"]["kpi_history"][0]["ships_this_month"] = 42
    out_path.write_text(json.dumps(payload), encoding="utf-8")

    rc = main(["--out", str(out_path), "--verify"], query_fn=_fake_query(_canned_rows()))

    assert rc == 1


def test_query_failure_leaves_existing_file_unchanged(tmp_path):
    """Failure scenario (SPEC シナリオ3): a failed export must not corrupt
    the committed JSON -- the page keeps showing the last good data."""
    out_path = tmp_path / "telemetry.json"
    main(["--out", str(out_path)], query_fn=_fake_query(_canned_rows()))
    before = out_path.read_text(encoding="utf-8")

    def failing_query(sql: str) -> list[dict]:
        raise RuntimeError("synthetic BQ failure")

    rc = main(["--out", str(out_path)], query_fn=failing_query)

    assert rc == 1
    assert out_path.read_text(encoding="utf-8") == before


def test_check_and_verify_fail_when_file_is_missing(tmp_path):
    out_path = tmp_path / "telemetry.json"

    assert main(["--out", str(out_path), "--check"], query_fn=_fake_query({})) == 1
    assert main(["--out", str(out_path), "--verify"], query_fn=_fake_query({})) == 1


def test_main_without_query_fn_does_not_import_bigquery_at_module_load():
    """The BQ client dependency must be lazy: importing the module and
    building queries has to work in environments without
    google-cloud-bigquery installed (CI test suite)."""
    assert "google.cloud.bigquery" not in dir(ept)
    queries = ept.build_queries("p")
    assert len(queries) == len(ALLOWED_METRICS)
