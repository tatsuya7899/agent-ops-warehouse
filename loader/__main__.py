"""CLI entry point:
`python -m loader --repos <path...> --articles <path> --lessons <dir>
--metrics <file> --x-strategy <file> --sessions <dir...> --kpi --out <dir>`.

Orchestrates extraction (git commits, published articles, LESSON
filenames, METRICS.md monthly summary, X-STRATEGY.md post log, Claude
Code session jsonl aggregates, a CAREER-KPI snapshot) and NDJSON
emission. No BigQuery dependency by default -- P0 scope stops at NDJSON
generation (SPEC-agent-ops-warehouse.md Section 5, phase P0).

`--merge` is the one opt-in exception: it builds (and, only with
`--execute`, runs) a MERGE-based dedup load plan for raw_git_commits
(Section 3.3). Executing it for real requires billing enabled on the
target GCP project -- the P0 sandbox rejects DML outright -- so
`--execute` is never exercised in this repo's own CI/test runs.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from loader.bq_merge import (
    GIT_COMMITS_ALL_COLUMNS,
    GIT_COMMITS_KEY_COLUMNS,
    TableConfig,
    bq_cli_runner,
    build_load_plan,
    describe_step,
    run_plan,
)
from loader.emit import build_load_run, stamp_loaded_at, write_ndjson
from loader.extract_articles import extract_articles
from loader.extract_git import extract_git_commits
from loader.extract_kpi_snapshots import extract_kpi_snapshots
from loader.extract_lessons import extract_lessons
from loader.extract_metrics import extract_metrics
from loader.extract_session_stats import extract_session_stats
from loader.extract_x_posts import extract_x_posts


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loader",
        description="Extract personal git history and published articles to NDJSON.",
    )
    parser.add_argument(
        "--repos",
        nargs="*",
        default=[],
        help="Paths to personal git repositories (checked against an allowlist).",
    )
    parser.add_argument(
        "--articles",
        default=None,
        help="Path to a published/ directory of note-articles.",
    )
    parser.add_argument(
        "--lessons",
        default=None,
        help="Path to a _ops/lessons/ directory (active files + archive/ subdir).",
    )
    parser.add_argument(
        "--metrics",
        default=None,
        help="Path to a METRICS.md file (Section 1 monthly summary table).",
    )
    parser.add_argument(
        "--x-strategy",
        default=None,
        help="Path to X-STRATEGY.md (parses the 投稿ログ table).",
    )
    parser.add_argument(
        "--sessions",
        nargs="*",
        default=[],
        help=(
            "Paths to ~/.claude/projects/<dir>/ session directories. "
            "Company directories (basename containing '-strategic-planning') "
            "are skipped explicitly even if passed here."
        ),
    )
    parser.add_argument(
        "--kpi",
        action="store_true",
        help=(
            "Emit one raw_kpi_snapshots row using generate_status.py's "
            "compute_* functions (reused, not reimplemented)."
        ),
    )
    parser.add_argument(
        "--kpi-file",
        default=None,
        help=(
            "Override path to a CAREER-KPI file (default: auto-detected via "
            "generate_status.find_career_kpi_file()). Only used with --kpi."
        ),
    )
    parser.add_argument(
        "--kpi-published",
        default=None,
        help=(
            "Override path to a published/ dir for streak/evidence computation "
            "(default: <Developer>/note-articles/published). Only used with --kpi."
        ),
    )
    parser.add_argument(
        "--kpi-status-md",
        default=None,
        help=(
            "Override path to a STATUS.md file for monthly_ships "
            "(default: <Developer>/STATUS.md). Only used with --kpi."
        ),
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help=(
            "Build a MERGE-based dedup load plan for raw_git_commits "
            "(SPEC Section 3.3; requires --repos, --project, --dataset). "
            "Without --execute, only prints a dry-run plan -- this is "
            "the default and never touches BigQuery."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Actually run the --merge plan via the `bq` CLI. Requires "
            "billing enabled on the target project (the P0 sandbox "
            "rejects DML). Without this flag, --merge only prints the "
            "plan (dry run)."
        ),
    )
    parser.add_argument(
        "--project",
        default=None,
        help="GCP project id (required with --merge).",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="BigQuery dataset id (required with --merge).",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory for NDJSON files and the load ledger.",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> list[dict]:
    args = parse_args(argv)
    out_dir = Path(args.out)
    load_runs: list[dict] = []

    if args.repos:
        result = extract_git_commits(args.repos)
        rows = stamp_loaded_at(result.rows)
        n = write_ndjson(rows, out_dir / "raw_git_commits.ndjson")
        skipped = ", ".join(result.skipped_repos) if result.skipped_repos else "none"
        note = (
            f"scanned {len(args.repos)} repo(s); "
            f"skipped {len(result.skipped_repos)} not in allowlist: {skipped}"
        )
        load_runs.append(build_load_run("raw_git_commits", n, note))

    if args.articles:
        result = extract_articles(args.articles)
        rows = stamp_loaded_at(result.rows)
        n = write_ndjson(rows, out_dir / "raw_articles.ndjson")
        skipped = ", ".join(result.skipped_files) if result.skipped_files else "none"
        note = (
            f"skipped {len(result.skipped_files)} filename-convention "
            f"violation(s): {skipped}"
        )
        load_runs.append(build_load_run("raw_articles", n, note))

    if args.lessons:
        result = extract_lessons(args.lessons)
        # SPEC Section 3.2 (v1.2.1) unifies loaded_at across every raw
        # table, raw_lessons included -- a stale "no loaded_at column"
        # comment here previously left this source unstamped, which BQ's
        # REQUIRED loaded_at column rejected wholesale on load
        # ("Missing required field: loaded_at").
        rows = stamp_loaded_at(result.rows)
        n = write_ndjson(rows, out_dir / "raw_lessons.ndjson")
        skipped = ", ".join(result.skipped_files) if result.skipped_files else "none"
        note = (
            f"skipped {len(result.skipped_files)} filename-convention "
            f"violation(s): {skipped}"
        )
        load_runs.append(build_load_run("raw_lessons", n, note))

    if args.metrics:
        result = extract_metrics(args.metrics)
        rows = stamp_loaded_at(result.rows)
        n = write_ndjson(rows, out_dir / "raw_metrics_monthly.ndjson")
        note = (
            f"parsed {n} monthly row(s) from the Section 1 summary table"
            if n
            else "no Section 1 monthly summary table found"
        )
        load_runs.append(build_load_run("raw_metrics_monthly", n, note))

    if args.x_strategy:
        result = extract_x_posts(args.x_strategy)
        rows = stamp_loaded_at(result.rows)
        n = write_ndjson(rows, out_dir / "raw_x_posts.ndjson")
        skipped = ", ".join(result.skipped_rows) if result.skipped_rows else "none"
        note = f"skipped {len(result.skipped_rows)} unrecorded/unparseable row(s): {skipped}"
        load_runs.append(build_load_run("raw_x_posts", n, note))

    if args.sessions:
        result = extract_session_stats(args.sessions)
        rows = stamp_loaded_at(result.rows)
        n = write_ndjson(rows, out_dir / "raw_session_stats.ndjson")
        skipped_dirs = ", ".join(result.skipped_dirs) if result.skipped_dirs else "none"
        note = (
            f"scanned {len(args.sessions)} session dir(s); "
            f"skipped {len(result.skipped_dirs)} company dir(s): {skipped_dirs}; "
            f"skipped_lines={result.skipped_lines}"
        )
        load_runs.append(build_load_run("raw_session_stats", n, note))

    if args.kpi:
        result = extract_kpi_snapshots(
            kpi_path=args.kpi_file,
            published_dir=args.kpi_published,
            status_md_path=args.kpi_status_md,
        )
        rows = stamp_loaded_at(result.rows)
        n = write_ndjson(rows, out_dir / "raw_kpi_snapshots.ndjson")
        load_runs.append(build_load_run("raw_kpi_snapshots", n, result.note))

    write_ndjson(load_runs, out_dir / "raw_load_runs.ndjson")

    if args.merge:
        _run_merge_flag(args, out_dir)

    return load_runs


def _run_merge_flag(args: argparse.Namespace, out_dir: Path) -> None:
    """Build (and, with --execute, run) the raw_git_commits MERGE plan.

    Split out from `run()` so the "what does --merge require" checks
    stay next to each other rather than buried mid-orchestration.
    """
    if not args.repos:
        raise SystemExit(
            "--merge requires --repos: its source_uri is the "
            "raw_git_commits.ndjson this same invocation emits."
        )
    if not args.project or not args.dataset:
        raise SystemExit("--merge requires --project and --dataset.")

    config = TableConfig(
        project=args.project,
        dataset=args.dataset,
        table="raw_git_commits",
        key_columns=GIT_COMMITS_KEY_COLUMNS,
        all_columns=GIT_COMMITS_ALL_COLUMNS,
        source_uri=str(out_dir / "raw_git_commits.ndjson"),
    )
    steps = build_load_plan([config])

    if not args.execute:
        for step in steps:
            print(describe_step(step))
        return

    for step, result in zip(steps, run_plan(steps, bq_cli_runner)):
        status = "OK" if result.ok else "FAILED"
        print(f"[{status}] {step.kind} {step.table}: {result.detail}")


if __name__ == "__main__":
    run()
