"""MERGE-based dedup load flow for append-only raw sources (P1 scope).

SPEC-agent-ops-warehouse.md Section 3.3: raw is a faithful, append-only
copy of the local record of fact, so a re-run of the loader against the
same git history must not create duplicate rows in BigQuery. P0 (the BQ
sandbox) does not support DML at all, so P0 only ever ran a plain load
job; MERGE-based dedup is explicitly P1+ scope, and P1 requires a paid
account with billing enabled (SPEC Section 5). **This module never
executes anything against BigQuery itself** -- it only builds a plan
(a list of Step objects) and, separately, runs that plan through an
injected runner callable, so the whole flow is unit-testable with a
fake runner and stays free of network calls until an operator with
billing enabled chooses to run it for real via bq_cli_runner.

Flow per table (mirrors CHECKLIST P1's "load to staging -> MERGE ->
drop staging" three-step shape):
    1. load_staging  -- WRITE_TRUNCATE load of the source NDJSON into a
       `{table}__staging` table (staging is a disposable scratch copy,
       never the record of fact).
    2. merge         -- `MERGE ... WHEN NOT MATCHED THEN INSERT` only.
       raw rows are never UPDATEd once loaded (append-only, SPEC
       Section 3.3): a commit_hash that is already present in the
       target table is simply skipped on re-run.
    3. drop_staging  -- remove the scratch staging table.

Dedup key for raw_git_commits is the composite (repo, commit_hash), not
commit_hash alone: SPEC Section 3.1 tracks five repositories, and a
commit_hash collision across two unrelated (independent) repositories,
while astronomically unlikely, is not ruled out by git's hash space the
way it would be within a single repo's own history.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

# raw_git_commits schema, in column order (SPEC Section 3.2). Kept here
# (not re-derived from extract_git.py) because the BQ column order is a
# load-time / schema concern, independent of the extractor's dict order.
GIT_COMMITS_KEY_COLUMNS: tuple[str, ...] = ("repo", "commit_hash")
GIT_COMMITS_ALL_COLUMNS: tuple[str, ...] = (
    "repo",
    "commit_hash",
    "committed_at",
    "subject",
    "files_changed",
    "insertions",
    "deletions",
    "loaded_at",
)


@dataclass(frozen=True)
class TableConfig:
    """One table's MERGE load configuration."""

    project: str
    dataset: str
    table: str
    key_columns: tuple[str, ...]
    all_columns: tuple[str, ...]
    source_uri: str  # local NDJSON path (or GCS URI) to load into staging
    schema_path: str | None = None  # BQ schema JSON file; required because the
    # staging table does not exist yet and bq load cannot infer a schema for a
    # brand-new table without one (verified against the real API, 2026-07-29)


@dataclass(frozen=True)
class Step:
    """One unit of the load plan (a *plan*, not an executed action).

    kind is one of "load_staging" / "merge" / "drop_staging". `args`
    carries everything a runner needs -- both the structured fields a
    fake/test runner uses (e.g. key_columns) and the literal `sql` a
    real bq-client runner would execute -- so plan generation and plan
    execution stay fully decoupled (SPEC Section 3.3 / this task: "実行
    器と計画を分離しテスト可能に").
    """

    kind: str
    table: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StepResult:
    """The outcome of running one Step through a runner."""

    step: Step
    ok: bool
    detail: str = ""


def build_merge_sql(
    project: str,
    dataset: str,
    table: str,
    key_columns: tuple[str, ...],
    all_columns: tuple[str, ...],
) -> str:
    """Build a `MERGE ... WHEN NOT MATCHED THEN INSERT`-only statement.

    Deliberately has no `WHEN MATCHED THEN UPDATE` clause: raw is
    append-only (SPEC Section 3.3), so an existing row is never
    overwritten -- it is simply left alone on re-run, and only rows
    whose key is new to the target table are inserted.

    Requires billing enabled to execute (the P0 sandbox rejects DML
    including MERGE); this function only ever returns SQL text.
    """
    staging_table = f"{table}__staging"
    target_ref = f"`{project}.{dataset}.{table}`"
    staging_ref = f"`{project}.{dataset}.{staging_table}`"
    on_clause = " AND ".join(f"T.{column} = S.{column}" for column in key_columns)
    insert_columns = ", ".join(all_columns)
    insert_values = ", ".join(f"S.{column}" for column in all_columns)

    return (
        f"MERGE {target_ref} AS T\n"
        f"USING {staging_ref} AS S\n"
        f"ON {on_clause}\n"
        f"WHEN NOT MATCHED THEN\n"
        f"  INSERT ({insert_columns}) VALUES ({insert_values})"
    )


def build_load_plan(table_configs: list[TableConfig]) -> list[Step]:
    """Turn each TableConfig into its [load_staging, merge, drop_staging] steps.

    A *plan*, i.e. plain data -- no I/O happens here. `run_plan` is what
    actually executes it, against whichever runner is injected.
    """
    steps: list[Step] = []

    for config in table_configs:
        staging_table = f"{config.table}__staging"

        steps.append(
            Step(
                kind="load_staging",
                table=staging_table,
                args={
                    "project": config.project,
                    "dataset": config.dataset,
                    "source_uri": config.source_uri,
                    "write_disposition": "WRITE_TRUNCATE",
                    "all_columns": config.all_columns, "schema_path": config.schema_path,},
            )
        )

        merge_sql = build_merge_sql(
            project=config.project,
            dataset=config.dataset,
            table=config.table,
            key_columns=config.key_columns,
            all_columns=config.all_columns,
        )
        steps.append(
            Step(
                kind="merge",
                table=config.table,
                args={
                    "project": config.project,
                    "dataset": config.dataset,
                    "staging_table": staging_table,
                    "key_columns": config.key_columns,
                    "all_columns": config.all_columns,
                    "sql": merge_sql,
                },
            )
        )

        steps.append(
            Step(
                kind="drop_staging",
                table=staging_table,
                args={"project": config.project, "dataset": config.dataset},
            )
        )

    return steps


def run_plan(steps: list[Step], runner: Callable[[Step], StepResult]) -> list[StepResult]:
    """Run each Step through `runner` in order, stopping at the first failure.

    Stopping early is deliberate: a table's merge/drop steps are only
    meaningful if that table's own preceding load succeeded, so running
    them anyway after a failure would operate on stale or absent staging
    data.
    """
    results: list[StepResult] = []
    for step in steps:
        result = runner(step)
        results.append(result)
        if not result.ok:
            break
    return results


def describe_step(step: Step) -> str:
    """One human-readable dry-run line for a Step (no execution)."""
    if step.kind == "load_staging":
        return (
            f"[DRY RUN] load {step.args['source_uri']} -> "
            f"{step.args['dataset']}.{step.table} "
            f"({step.args['write_disposition']})"
        )
    if step.kind == "merge":
        keys = ", ".join(step.args["key_columns"])
        return (
            f"[DRY RUN] MERGE {step.args['dataset']}.{step.args['staging_table']} -> "
            f"{step.args['dataset']}.{step.table} ON ({keys}), INSERT-only (no UPDATE)"
        )
    if step.kind == "drop_staging":
        return f"[DRY RUN] DROP TABLE {step.args['dataset']}.{step.table}"
    return f"[DRY RUN] unknown step kind={step.kind}"


def bq_cli_runner(step: Step) -> StepResult:
    """Execute one Step for real, via the `bq` CLI.

    This is the production runner. It requires: the `bq` binary on
    PATH, ADC auth already configured, and -- critically -- **billing
    enabled on the target project**, since the P0 sandbox rejects all
    DML (including the MERGE this issues). It is never invoked by the
    test suite: tests inject a fake runner (or monkeypatch
    `subprocess.run`) instead, so pytest never makes a network call.
    """
    project = step.args.get("project")
    dataset = step.args.get("dataset")

    if step.kind == "load_staging":
        cmd = [
            "bq",
            "load",
            "--source_format=NEWLINE_DELIMITED_JSON",
            "--replace",  # staging is WRITE_TRUNCATE: always a full replace
        ]
        if step.args.get("schema_path"):
            cmd.append(f"--schema={step.args['schema_path']}")
        cmd += [
            f"{project}:{dataset}.{step.table}",
            step.args["source_uri"],
        ]
    elif step.kind == "merge":
        cmd = ["bq", "query", "--use_legacy_sql=false", step.args["sql"]]
    elif step.kind == "drop_staging":
        cmd = ["bq", "rm", "-f", "-t", f"{project}:{dataset}.{step.table}"]
    else:
        return StepResult(step=step, ok=False, detail=f"unknown step kind={step.kind}")

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    ok = result.returncode == 0
    detail = result.stdout if ok else result.stderr
    return StepResult(step=step, ok=ok, detail=detail)
