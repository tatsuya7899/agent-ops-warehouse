"""Index diff: compare note-articles/published/*.md to raw.article_chunks
(SPEC: _ops/specs/SPEC-index-on-publish_design.md Section "データモデル・
インターフェース" / "方式").

Six categories (design Section "触るファイル(名指し)" row for this file):
  missing        -- 追加漏れ(filenameが索引に完全不在、かつchunk生成>0)
  missing_empty  -- 追加漏れだが修復不可(H2見出しゼロでchunk生成0)
  partial        -- 部分索引(indexed_ids は expected_ids の真部分集合、かつ
                     共通chunk_idのchunk_textが全て一致=中断/429残滓)
  changed        -- 内容変更(共通chunk_idのtext不一致、または索引側に
                     expected外のidがある=真の編集)
  deleted        -- 索引にあるがpublished/に無い
  invalid_filenames -- FILENAME_PATTERN不適合(全処理対象外、報告のみ)
  duplicates     -- 同一chunk_idが索引に複数行存在する

修復対象は missing + partial の不足chunk_idのみ(FR-6)。changed / deleted /
invalid_filenames / duplicates は報告のみで自動修復しない。

This module never calls the Gemini embedding API and does not need
RAG_API_TOKEN (FR-4 / FR-8): the only network call is `bq query`, gated
behind the injectable fetch_fn boundary so tests never touch bq for real.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Bootstrap: make `scripts.*` and `loader.*` importable whether this file is
# run as `python3 scripts/index_diff.py`, `python -m scripts.index_diff`, or
# imported normally by pytest (pythonpath=["."] already covers the latter;
# this insert is a no-op in that case).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loader.extract_articles import FILENAME_PATTERN  # noqa: E402
from scripts.build_embeddings import (  # noqa: E402
    DEFAULT_PUBLISHED_DIR,
    chunk_published_articles,
)

BQ_PROJECT = "agent-ops-warehouse"
# Standard SQL resolves the fully-qualified table name without
# backtick-quoting (see eval/run_eval.py's indexed_filenames for the same
# convention and the Studio-guard rationale).
BQ_INDEX_SQL = (
    "SELECT chunk_id, chunk_text, filename "
    "FROM agent-ops-warehouse.raw.article_chunks"
)
BQ_TIMEOUT_SECONDS = 120
BQ_MAX_ROWS = 1_000_000


class IndexDiffError(Exception):
    """The bq query could not run (transport/auth/malformed output)."""


def fetch_indexed_rows() -> list[dict]:
    """One `bq query` call returning every (chunk_id, chunk_text, filename)
    row currently in raw.article_chunks. Injectable boundary -- tests pass
    their own fetch_fn / indexed_rows instead of calling this (FR-4/FR-8:
    no network call needed to exercise classify())."""
    proc = subprocess.run(
        [
            "bq",
            "query",
            f"--project_id={BQ_PROJECT}",
            "--use_legacy_sql=false",
            "--format=json",
            # `bq query` defaults to returning only 100 rows -- silently
            # truncating a 750-row index makes every later chunk_id look
            # "missing" (false-positive drift + double-embedding repair
            # loops). Cap far above any plausible corpus size, and verify
            # below that we did not hit the cap (incident 2026-10-08).
            f"--max_rows={BQ_MAX_ROWS}",
            BQ_INDEX_SQL,
        ],
        capture_output=True,
        text=True,
        timeout=BQ_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise IndexDiffError(f"bq query failed: {proc.stderr.strip()[:200]}")
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise IndexDiffError("bq query returned non-JSON") from exc
    if len(rows) >= BQ_MAX_ROWS:
        raise IndexDiffError(
            f"bq query returned {len(rows)} rows -- at the --max_rows cap; "
            "the index view is truncated and every diff would be wrong"
        )
    return rows


@dataclass
class IndexDiff:
    missing: list[str] = field(default_factory=list)
    missing_empty: list[str] = field(default_factory=list)
    partial: list[dict] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    invalid_filenames: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    # Every filename with >=1 indexed row -- including files that fall in
    # no category (invalid names that are indexed, healthy files). Lets
    # consumers (run_eval --check-index) rebuild the exact "is this
    # filename indexed" set the legacy DISTINCT-filename query returned.
    indexed_filenames: set[str] = field(default_factory=set)

    def is_clean(self) -> bool:
        return not (
            self.missing
            or self.missing_empty
            or self.partial
            or self.changed
            or self.deleted
            or self.invalid_filenames
            or self.duplicates
        )

    def repair_chunk_ids(self, expected_by_file: dict[str, dict[str, dict]]) -> list[str]:
        """FR-6:修復対象=追加漏れ(missing)の全chunk_id + 部分索引(partial)の
        不足chunk_idのみ。changed/deleted/invalid_filenames/duplicatesは含めない。"""
        ids: list[str] = []
        for filename in self.missing:
            ids.extend(expected_by_file[filename].keys())
        for entry in self.partial:
            ids.extend(entry["missing_chunk_ids"])
        return ids


def build_expected_by_file(published_dir) -> tuple[dict[str, dict[str, dict]], list[str]]:
    """filename -> {chunk_id: chunk_row} for every valid-filename article in
    published_dir, including an empty dict for H2-less articles (0 chunks)
    so they are still classified as missing_empty rather than silently
    absent from the comparison. Returns (expected_by_file, invalid_filenames)."""
    result = chunk_published_articles(published_dir)
    by_file: dict[str, dict[str, dict]] = {}
    for row in result.rows:
        by_file.setdefault(row["filename"], {})[row["chunk_id"]] = row

    published_dir = Path(published_dir)
    if published_dir.exists():
        for path in sorted(published_dir.glob("*.md")):
            if FILENAME_PATTERN.match(path.name):
                by_file.setdefault(path.name, {})

    return by_file, sorted(result.skipped_files)


def classify(
    expected_by_file: dict[str, dict[str, dict]],
    invalid_filenames: list[str],
    indexed_rows: list[dict],
) -> IndexDiff:
    diff = IndexDiff(invalid_filenames=sorted(invalid_filenames))

    indexed_by_file: dict[str, dict[str, str]] = {}
    chunk_id_counts: dict[str, int] = {}
    for row in indexed_rows:
        chunk_id_counts[row["chunk_id"]] = chunk_id_counts.get(row["chunk_id"], 0) + 1
        indexed_by_file.setdefault(row["filename"], {})[row["chunk_id"]] = row["chunk_text"]
    diff.duplicates = sorted(cid for cid, count in chunk_id_counts.items() if count > 1)
    diff.indexed_filenames = set(indexed_by_file)

    for filename, expected_chunks in expected_by_file.items():
        expected_ids = set(expected_chunks.keys())
        if filename not in indexed_by_file:
            if expected_ids:
                diff.missing.append(filename)
            else:
                diff.missing_empty.append(filename)
            continue

        indexed_chunks = indexed_by_file[filename]
        indexed_ids = set(indexed_chunks.keys())
        common = expected_ids & indexed_ids
        text_matches = all(
            indexed_chunks[cid] == expected_chunks[cid]["chunk_text"] for cid in common
        )
        extra_in_index = indexed_ids - expected_ids
        missing_ids = expected_ids - indexed_ids

        if indexed_ids == expected_ids and text_matches:
            continue  # healthy, no category
        if missing_ids and text_matches and not extra_in_index:
            diff.partial.append(
                {"filename": filename, "missing_chunk_ids": sorted(missing_ids)}
            )
        else:
            diff.changed.append(filename)

    diff.deleted = sorted(
        set(indexed_by_file) - set(expected_by_file) - set(invalid_filenames)
    )
    diff.missing.sort()
    diff.missing_empty.sort()
    diff.changed.sort()
    diff.partial.sort(key=lambda e: e["filename"])
    return diff


def compute_index_diff(published_dir=DEFAULT_PUBLISHED_DIR, *, fetch_fn=None):
    """Full pipeline: chunk published_dir -> fetch indexed rows (via the
    injectable fetch_fn) -> classify(). Returns (IndexDiff, expected_by_file)
    -- callers needing repair chunk rows (build_embeddings) use
    expected_by_file alongside diff.repair_chunk_ids().

    fetch_fn defaults to None (resolved to the module-level
    fetch_indexed_rows at call time, not at def time) so that
    `monkeypatch.setattr(module, "fetch_indexed_rows", fake)` -- the
    standard test injection pattern used across this repo (e.g.
    eval/run_eval.py's post_query) -- actually takes effect; a mutable
    default bound at def time would not see that patch."""
    if fetch_fn is None:
        fetch_fn = fetch_indexed_rows
    expected_by_file, invalid_filenames = build_expected_by_file(published_dir)
    indexed_rows = fetch_fn()
    diff = classify(expected_by_file, invalid_filenames, indexed_rows)
    return diff, expected_by_file


HOOK_MARKER = "# >>> index-on-publish (agent-ops-warehouse)"


def check_hooks_installed(note_articles_dir) -> dict[str, bool]:
    """Presence alone is not enough -- a post-commit hook that exists for
    an unrelated purpose (or a stale hook whose marker block was edited
    out) must report False, because the index-on-publish call is the
    actual mechanism being checked (review P2)."""
    hooks_dir = Path(note_articles_dir) / ".git" / "hooks"
    result = {}
    for name in ("post-commit", "post-merge"):
        hook = hooks_dir / name
        try:
            result[name] = HOOK_MARKER in hook.read_text(encoding="utf-8")
        except OSError:
            result[name] = False
    return result


def render_report(diff: IndexDiff, hooks: dict[str, bool] | None = None) -> str:
    sections = [
        ("missing(追加漏れ)", diff.missing),
        ("missing_empty(追加漏れ・修復不可: chunk生成0)", diff.missing_empty),
        (
            "partial(部分索引)",
            [f"{e['filename']} missing={e['missing_chunk_ids']}" for e in diff.partial],
        ),
        ("changed(内容変更・報告のみ)", diff.changed),
        ("deleted(削除・報告のみ)", diff.deleted),
        ("invalid_filenames(命名違反・処理対象外)", diff.invalid_filenames),
        ("duplicates(重複)", diff.duplicates),
    ]
    lines = []
    for title, items in sections:
        lines.append(f"{title}: {len(items)}")
        for item in items:
            lines.append(f"  - {item}")
    if hooks is not None:
        missing_hooks = [name for name, present in hooks.items() if not present]
        if missing_hooks:
            lines.append(f"warning: hooks not installed: {', '.join(missing_hooks)}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="index_diff",
        description="Diff published/*.md against raw.article_chunks (no API keys needed).",
    )
    parser.add_argument("--published-dir", default=str(DEFAULT_PUBLISHED_DIR))
    parser.add_argument(
        "--note-articles-dir",
        default=str(Path(DEFAULT_PUBLISHED_DIR).parent),
        help="Used only for the hook-installed check.",
    )
    parser.add_argument("--check", action="store_true", help="Run the diff and print a report.")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        diff, _ = compute_index_diff(args.published_dir)
    except IndexDiffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    hooks = check_hooks_installed(args.note_articles_dir)
    if args.json:
        payload = {
            "missing": diff.missing,
            "missing_empty": diff.missing_empty,
            "partial": diff.partial,
            "changed": diff.changed,
            "deleted": diff.deleted,
            "invalid_filenames": diff.invalid_filenames,
            "duplicates": diff.duplicates,
            "hooks_installed": hooks,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_report(diff, hooks))
    return 0 if diff.is_clean() else 1


if __name__ == "__main__":
    raise SystemExit(main())
