"""CLI entry point:
`python -m loader --repos <path...> --articles <path> --lessons <dir>
--metrics <file> --out <dir>`.

Orchestrates extraction (git commits, published articles, LESSON
filenames, METRICS.md monthly summary) and NDJSON emission. No BigQuery
dependency -- P0 scope stops at NDJSON generation
(SPEC-agent-ops-warehouse.md Section 5, phase P0).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from loader.emit import build_load_run, stamp_loaded_at, write_ndjson
from loader.extract_articles import extract_articles
from loader.extract_git import extract_git_commits
from loader.extract_lessons import extract_lessons
from loader.extract_metrics import extract_metrics


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
        # raw_lessons has no loaded_at column (SPEC Section 3.2), so rows
        # are written as-is, unlike raw_git_commits / raw_articles above.
        n = write_ndjson(result.rows, out_dir / "raw_lessons.ndjson")
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

    write_ndjson(load_runs, out_dir / "raw_load_runs.ndjson")
    return load_runs


if __name__ == "__main__":
    run()
