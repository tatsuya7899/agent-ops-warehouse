"""Tests for loader.bq_merge: MERGE-based dedup load plan generation.

No BigQuery call is made anywhere in this test file -- billing is not
yet enabled and the P0 sandbox does not support DML at all
(SPEC-agent-ops-warehouse.md Section 3.3 / Section 5). The fake
warehouse below models exactly the semantics build_merge_sql generates
(staging is replaced wholesale, MERGE only performs WHEN NOT MATCHED
THEN INSERT) purely to prove the *generated plan* is idempotent -- it
never touches a network or subprocess. The one test that does touch
loader.bq_merge.subprocess monkeypatches it to a stub, so no real `bq`
invocation ever happens.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from loader.bq_merge import (
    GIT_COMMITS_ALL_COLUMNS,
    GIT_COMMITS_KEY_COLUMNS,
    Step,
    StepResult,
    TableConfig,
    bq_cli_runner,
    build_load_plan,
    build_merge_sql,
    describe_step,
    run_plan,
)


def test_build_merge_sql_insert_only_no_update():
    sql = build_merge_sql(
        project="proj",
        dataset="ds",
        table="raw_git_commits",
        key_columns=GIT_COMMITS_KEY_COLUMNS,
        all_columns=GIT_COMMITS_ALL_COLUMNS,
    )

    assert "MERGE `proj.ds.raw_git_commits` AS T" in sql
    assert "USING `proj.ds.raw_git_commits__staging` AS S" in sql
    assert "ON T.repo = S.repo AND T.commit_hash = S.commit_hash" in sql
    assert "WHEN NOT MATCHED THEN" in sql
    assert "INSERT (" in sql
    # raw is append-only (SPEC Section 3.3): must never UPDATE existing rows
    assert "WHEN MATCHED" not in sql
    assert "UPDATE" not in sql


def test_build_load_plan_produces_load_merge_drop_steps():
    config = TableConfig(
        project="proj",
        dataset="ds",
        table="raw_git_commits",
        key_columns=GIT_COMMITS_KEY_COLUMNS,
        all_columns=GIT_COMMITS_ALL_COLUMNS,
        source_uri="/tmp/whatever/raw_git_commits.ndjson",
    )

    steps = build_load_plan([config])

    assert [s.kind for s in steps] == ["load_staging", "merge", "drop_staging"]
    assert steps[0].table == "raw_git_commits__staging"
    assert steps[0].args["write_disposition"] == "WRITE_TRUNCATE"
    assert steps[1].table == "raw_git_commits"
    assert steps[1].args["key_columns"] == GIT_COMMITS_KEY_COLUMNS
    assert steps[1].args["staging_table"] == "raw_git_commits__staging"
    assert "MERGE" in steps[1].args["sql"]
    assert steps[2].table == "raw_git_commits__staging"


@dataclass
class _FakeBqWarehouse:
    """In-memory stand-in for BigQuery, keyed by (dataset, table_name).

    Models exactly the semantics build_merge_sql generates: staging is
    WRITE_TRUNCATE (replaced wholesale on every load), and MERGE only
    performs WHEN NOT MATCHED THEN INSERT (never UPDATE) -- so re-running
    the same load plan against the same source rows must not duplicate
    target rows.
    """

    tables: dict = field(default_factory=dict)  # (dataset, table_name) -> list[dict]

    def runner(self, step: Step) -> StepResult:
        dataset = step.args["dataset"]
        if step.kind == "load_staging":
            rows = [
                json.loads(line)
                for line in Path(step.args["source_uri"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.tables[(dataset, step.table)] = rows
            return StepResult(step=step, ok=True, detail=f"loaded {len(rows)} row(s)")

        if step.kind == "merge":
            staging_key = (dataset, step.args["staging_table"])
            target_key = (dataset, step.table)
            staging_rows = self.tables.get(staging_key, [])
            target_rows = self.tables.setdefault(target_key, [])
            existing_keys = {
                tuple(row[c] for c in step.args["key_columns"]) for row in target_rows
            }
            inserted = 0
            for row in staging_rows:
                row_key = tuple(row[c] for c in step.args["key_columns"])
                if row_key in existing_keys:
                    continue
                target_rows.append(row)
                existing_keys.add(row_key)
                inserted += 1
            return StepResult(step=step, ok=True, detail=f"inserted {inserted} row(s)")

        if step.kind == "drop_staging":
            self.tables.pop((dataset, step.table), None)
            return StepResult(step=step, ok=True, detail="dropped")

        return StepResult(step=step, ok=False, detail=f"unknown kind {step.kind}")


def test_run_plan_is_idempotent_on_repeated_loads(tmp_path):
    """Same source rows flowed through run_plan twice must not duplicate
    target rows (SPEC Section 3.3: raw is append-only, MERGE dedups on
    (repo, commit_hash))."""
    source = tmp_path / "raw_git_commits.ndjson"
    rows = [
        {
            "repo": "note-articles",
            "commit_hash": "abc123",
            "committed_at": "2026-07-01T00:00:00+09:00",
            "subject": "first",
            "files_changed": 1,
            "insertions": 2,
            "deletions": 0,
            "loaded_at": "2026-07-01T00:00:00Z",
        },
        {
            "repo": "note-articles",
            "commit_hash": "def456",
            "committed_at": "2026-07-02T00:00:00+09:00",
            "subject": "second",
            "files_changed": 1,
            "insertions": 1,
            "deletions": 1,
            "loaded_at": "2026-07-01T00:00:00Z",
        },
    ]
    source.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    config = TableConfig(
        project="proj",
        dataset="ds",
        table="raw_git_commits",
        key_columns=GIT_COMMITS_KEY_COLUMNS,
        all_columns=GIT_COMMITS_ALL_COLUMNS,
        source_uri=str(source),
    )
    warehouse = _FakeBqWarehouse()

    results_1 = run_plan(build_load_plan([config]), warehouse.runner)
    assert all(r.ok for r in results_1)
    assert len(warehouse.tables[("ds", "raw_git_commits")]) == 2

    # Re-run the exact same plan against the exact same source rows.
    results_2 = run_plan(build_load_plan([config]), warehouse.runner)
    assert all(r.ok for r in results_2)
    assert len(warehouse.tables[("ds", "raw_git_commits")]) == 2  # not 4

    # staging is dropped by the plan's own last step, every time.
    assert ("ds", "raw_git_commits__staging") not in warehouse.tables


def test_run_plan_stops_after_first_failing_step():
    def failing_runner(step: Step) -> StepResult:
        return StepResult(step=step, ok=False, detail="boom")

    config = TableConfig(
        project="proj",
        dataset="ds",
        table="raw_git_commits",
        key_columns=GIT_COMMITS_KEY_COLUMNS,
        all_columns=GIT_COMMITS_ALL_COLUMNS,
        source_uri="/does/not/matter.ndjson",
    )

    results = run_plan(build_load_plan([config]), failing_runner)

    assert len(results) == 1
    assert results[0].ok is False


def test_describe_step_dry_run_text_has_no_network_call_shape():
    config = TableConfig(
        project="proj",
        dataset="ds",
        table="raw_git_commits",
        key_columns=GIT_COMMITS_KEY_COLUMNS,
        all_columns=GIT_COMMITS_ALL_COLUMNS,
        source_uri="/tmp/whatever.ndjson",
    )

    descriptions = [describe_step(s) for s in build_load_plan([config])]

    assert all(d.startswith("[DRY RUN]") for d in descriptions)
    assert any("WRITE_TRUNCATE" in d for d in descriptions)
    assert any("repo, commit_hash" in d for d in descriptions)


def test_bq_cli_runner_builds_commands_without_real_subprocess_call(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)

        class _Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Result()

    import loader.bq_merge as bq_merge_module

    monkeypatch.setattr(bq_merge_module.subprocess, "run", fake_run)

    config = TableConfig(
        project="proj",
        dataset="ds",
        table="raw_git_commits",
        key_columns=GIT_COMMITS_KEY_COLUMNS,
        all_columns=GIT_COMMITS_ALL_COLUMNS,
        source_uri=str(tmp_path / "raw_git_commits.ndjson"),
    )

    results = run_plan(build_load_plan([config]), bq_cli_runner)

    assert all(r.ok for r in results)
    assert len(calls) == 3
    assert calls[0][:2] == ["bq", "load"]
    assert calls[1][:2] == ["bq", "query"]
    assert calls[2][:2] == ["bq", "rm"]
