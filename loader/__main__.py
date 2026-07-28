"""CLI entry point:
`python -m loader --repos <path...> --articles <path> --lessons <dir>
--metrics <file> --x-strategy <file> --sessions <dir...> --kpi --out <dir>`.

Orchestrates extraction (git commits, published articles, LESSON
filenames, METRICS.md monthly summary, X-STRATEGY.md post log, Claude
Code session jsonl aggregates, a CAREER-KPI snapshot) and NDJSON
emission. No BigQuery dependency -- P0 scope stops at NDJSON generation
(SPEC-agent-ops-warehouse.md Section 5, phase P0).
"""
from __future__ import annotations

import argparse
from pathlib import Path

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
    return load_runs


if __name__ == "__main__":
    run()
