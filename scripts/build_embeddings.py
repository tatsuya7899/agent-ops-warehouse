"""Build embeddings for note-articles/published/*.md into raw.article_chunks
(RAG API phase 2 -- SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.1
/ 4.2 / 6).

Independently-testable stages, wired together by main() once all of them
exist (SPEC Section 9 phase 2 implements them in this order):
    1. chunk_published_articles() -- markdown -> chunk rows. Pure/offline:
       reads the .md files, no network call.
    2. embed_chunks()              -- chunk rows -> chunk rows + embedding
       vectors (checkpoint 2, not yet implemented in this file).
    3. ndjson output -- reuses loader.emit (checkpoint 3).
    4. build_bq_load_args()        -- `bq load` argv construction (checkpoint 3).

Phase 3 (FastAPI /query, /health) is out of scope for this script.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loader.emit import build_load_run, stamp_loaded_at, write_ndjson
from loader.extract_articles import FILENAME_PATTERN, TITLE_PATTERN

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-001"
MAX_CHUNK_CHARS = 2000  # SPEC 4.1: sections longer than this are re-split by paragraph
MAX_EMBED_RETRIES = 3  # SPEC 4.2: exponential backoff, then skip
INITIAL_BACKOFF_SECONDS = 1.0
BQ_TABLE = "article_chunks"
DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "terraform" / "schemas" / "raw_article_chunks.json"
)
DEFAULT_LOAD_RUNS_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "terraform" / "schemas" / "raw_load_runs.json"
)
DEFAULT_PUBLISHED_DIR = Path.home() / "Developer" / "note-articles" / "published"

# raw_article_chunks schema field order (terraform/schemas/raw_article_chunks.json).
# Kept here explicitly -- not re-derived from the schema JSON at import time --
# so build_ndjson_rows fails fast (KeyError) on a malformed chunk row instead
# of silently emitting a row `bq load`'s REQUIRED-mode columns would reject
# later, out of sight of this script.
SCHEMA_FIELDS: tuple[str, ...] = (
    "chunk_id",
    "filename",
    "article_title",
    "section_title",
    "chunk_text",
    "embedding",
    "published_date",
    "loaded_at",
)

H2_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
H3_HEADING_RE = re.compile(r"^###[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# NOTE: this regex-based split does not track fenced code blocks (```), so a
# line starting with literal "## "/"### " inside a code fence would be
# mis-parsed as a heading. Not observed anywhere in the current 19-article
# corpus (checked manually for checkpoint 1); flagged here rather than
# solved speculatively, per SPEC's "no untested complexity" posture.


# ---------------------------------------------------------------------------
# 1. Chunking (SPEC Section 4.1 / Section 6-1)
# ---------------------------------------------------------------------------


@dataclass
class ChunkResult:
    rows: list[dict] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)


def split_h2_sections(body: str) -> list[tuple[str, str]]:
    """Split a markdown body into (h2_title, section_text) pairs, in
    document order. Text before the first H2 (title/lede) is dropped --
    the H2 section is the chunking unit (SPEC Section 4.1)."""
    matches = list(H2_HEADING_RE.finditer(body))
    sections: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((match.group(1).strip(), body[start:end].strip()))
    return sections


def split_h3_subsections(section_text: str) -> list[tuple[str | None, str]]:
    """Split one H2 section into (h3_title_or_None, text) parts.

    SPEC Section 4.1: an H2 section with 2+ H3 subheadings is re-split at
    H3 granularity (21% of the real corpus mixes unrelated points -- e.g.
    "転換1/2/3" -- under one H2). An H2 with 0 or 1 H3 stays intact (single
    (None, section_text) entry). Text before the first H3, if any, becomes
    its own (None, text) entry instead of being silently dropped -- not
    observed in the real corpus, but this is the safety net for it.
    """
    matches = list(H3_HEADING_RE.finditer(section_text))
    if len(matches) < 2:
        return [(None, section_text)]

    parts: list[tuple[str | None, str]] = []
    lead = section_text[: matches[0].start()].strip()
    if lead:
        parts.append((None, lead))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(section_text)
        parts.append((match.group(1).strip(), section_text[start:end].strip()))
    return parts


def split_long_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Re-split text over max_chars at paragraph boundaries (SPEC Section
    4.1). Paragraphs are packed greedily so no group exceeds max_chars
    unless a single paragraph alone already does -- a paragraph is never
    cut mid-sentence.

    Explicit accepted behavior (Phase 3.5 / Plan agent review, SPEC Section
    9 phase 3.5): a single paragraph with no blank-line break that is
    itself over max_chars has no safe cut point within this function
    (splitting mid-sentence would defeat the point of paragraph-boundary
    re-splitting), so it is returned as one oversized group rather than
    silently truncated or dropped. A warning is logged so this is visible
    to an operator instead of only showing up as a slightly-larger-than-
    expected chunk in raw.article_chunks. Not observed in the real
    19-article corpus today (SPEC Section 4.1) -- this is the safety net
    for future articles."""
    if len(text) <= max_chars:
        return [text]

    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    groups: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        para_len = len(para)
        if para_len > max_chars:
            logger.warning(
                "split_long_text: a single paragraph (%d chars) exceeds "
                "max_chars=%d with no blank-line break to split on; kept as "
                "one oversized chunk (accepted safety-valve behavior, SPEC "
                "Section 4.1)",
                para_len,
                max_chars,
            )
        if current and current_len + 2 + para_len > max_chars:
            groups.append("\n\n".join(current))
            current, current_len = [], 0
        current.append(para)
        current_len += para_len + (2 if len(current) > 1 else 0)
    if current:
        groups.append("\n\n".join(current))
    return groups or [text]


def chunk_article_text(text: str, filename: str, published_date: str) -> list[dict]:
    """Chunk one article's markdown body into raw_article_chunks rows
    (minus embedding/loaded_at, added at later stages).

    Context prefix (SPEC Section 4.1): "{article_title} > {h2}" for
    H2-unit chunks, "{article_title} > {h2} > {h3}" for H3-split chunks,
    prepended to the chunk's body text.

    Three-stage cascade (H2 -> H3 split if applicable -> long-text split if
    applicable), so an H3 sub-chunk that is itself still over 2000 chars is
    not left oversized just because it already went through the H3 split
    (coordinator follow-up, checkpoint 1 review, 2026-08-12): every piece
    produced by split_h3_subsections is passed through split_long_text
    below, regardless of whether that piece came from an H3 split or is a
    whole H2 kept intact.
    """
    title_match = TITLE_PATTERN.search(text)
    article_title = title_match.group(1).strip() if title_match else filename

    # Precedence when both rules could apply to the same H2 (design choice,
    # not spelled out by SPEC Section 4.1): the H3 split runs first, and the
    # long-text/paragraph split runs *after*, per resulting H3 piece -- not
    # on the raw H2 text. This preserves the H3 rule's own rationale (never
    # mix distinct points into one chunk) instead of a length-first cut that
    # could straddle two H3 points. Checked against the real 19-article
    # corpus (manual verification, checkpoint 1): the corpus's one >2000-char
    # H2 also has 4 H3 headings, so with this precedence it is fully
    # resolved by the H3 split and the long-text branch never fires on real
    # data today -- it is exercised only by the synthetic tests, exactly as
    # SPEC Section 4.1 anticipates ("将来記事のための安全域").
    rows: list[dict] = []
    seq = 0
    for h2_title, h2_text in split_h2_sections(text):
        for h3_title, sub_text in split_h3_subsections(h2_text):
            if h3_title is None:
                section_title = h2_title
                prefix = f"{article_title} > {h2_title}"
            else:
                section_title = f"{h2_title} > {h3_title}"
                prefix = f"{article_title} > {h2_title} > {h3_title}"

            for piece in split_long_text(sub_text):
                seq += 1
                rows.append(
                    {
                        "chunk_id": f"{Path(filename).stem}__{seq:03d}",
                        "filename": filename,
                        "article_title": article_title,
                        "section_title": section_title,
                        "chunk_text": f"{prefix}\n\n{piece}".strip(),
                        "published_date": published_date,
                    }
                )
    return rows


def chunk_published_articles(published_dir) -> ChunkResult:
    """Glob published_dir/*.md at call time (never hardcode a count -- SPEC
    Section 2 / Section 4.2) and chunk every matching article. Filenames
    that do not match the YYYYMMDD_slug.md convention are skipped
    explicitly and reported, mirroring loader.extract_articles."""
    published_dir = Path(published_dir)
    rows: list[dict] = []
    skipped: list[str] = []

    if not published_dir.exists():
        logger.warning("published_dir does not exist: %s", published_dir)
        return ChunkResult(rows=rows, skipped_files=skipped)

    paths = sorted(published_dir.glob("*.md"))
    logger.info("found %d markdown file(s) in %s", len(paths), published_dir)

    for path in paths:
        match = FILENAME_PATTERN.match(path.name)
        if not match:
            skipped.append(path.name)
            continue
        raw_date, _slug = match.groups()
        published_date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        text = path.read_text(encoding="utf-8")
        rows.extend(chunk_article_text(text, path.name, published_date))

    logger.info(
        "chunked %d article(s) into %d chunk(s); skipped %d filename-convention violation(s): %s",
        len(paths) - len(skipped),
        len(rows),
        len(skipped),
        ", ".join(skipped) if skipped else "none",
    )
    return ChunkResult(rows=rows, skipped_files=skipped)


# ---------------------------------------------------------------------------
# 2. Embedding generation (SPEC Section 4.2 / Section 6-2)
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    """Raised when the Gemini embedding API returns HTTP 429."""


def build_gemini_client(api_key: str):
    """Thin factory around google.genai.Client. Imported lazily so that
    chunking / ndjson / bq-load-command tests never need google-genai
    importable to load this module (SPEC Section 6: those stages stay
    network-free and dependency-free)."""
    from google import genai

    return genai.Client(api_key=api_key)


def call_embedding_api(
    client,
    text: str,
    model: str = EMBEDDING_MODEL,
    task_type: str | None = None,
) -> list[float]:
    """One real call to the Gemini embedding API -- no retry logic here.
    embed_chunk_with_retry owns the retry loop, so tests mock this single
    call directly instead of reimplementing backoff (SPEC Section 4.2 /
    Section 6-2).

    task_type (Phase 3.5 / Plan agent review, SPEC Section 9 phase 3.5):
    the Gemini embedding API optimizes the vector differently depending on
    whether the text being embedded is a document to be indexed
    ("RETRIEVAL_DOCUMENT", passed by build_embeddings' indexing side) or a
    search query ("RETRIEVAL_QUERY", passed by api.main.embed_question) --
    leaving this unset (the pre-Phase-3.5 default) silently degrades
    retrieval quality without erroring. None (the default here) omits the
    config entirely, matching pre-Phase-3.5 behavior for any caller that
    does not yet pass a task_type."""
    from google.genai import errors, types

    config = types.EmbedContentConfig(task_type=task_type) if task_type else None
    try:
        response = client.models.embed_content(model=model, contents=text, config=config)
    except errors.APIError as exc:
        if getattr(exc, "code", None) == 429:
            raise RateLimitError(str(exc)) from exc
        raise
    return response.embeddings[0].values


def embed_chunk_with_retry(
    embed_fn: Callable[[str], list[float]],
    chunk_text: str,
    *,
    max_retries: int = MAX_EMBED_RETRIES,
    initial_backoff_seconds: float = INITIAL_BACKOFF_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[float] | None:
    """Call embed_fn(chunk_text), retrying up to max_retries times with
    exponential backoff on RateLimitError. Returns None (never raises)
    once retries are exhausted -- callers treat None as "skip this chunk,
    log it, keep going" (SPEC Section 4.2). Any other exception propagates
    immediately (not retried): only rate limiting is in scope here.
    """
    backoff = initial_backoff_seconds
    for attempt in range(1, max_retries + 1):
        try:
            return embed_fn(chunk_text)
        except RateLimitError as exc:
            if attempt == max_retries:
                logger.warning(
                    "embedding failed after %d attempt(s), skipping chunk: %r",
                    max_retries,
                    exc,
                )
                return None
            logger.info("429 on attempt %d/%d, backing off %.1fs", attempt, max_retries, backoff)
            sleep_fn(backoff)
            backoff *= 2
    return None  # pragma: no cover -- loop always returns/raises above


def embed_chunks(
    chunk_rows: list[dict],
    embed_fn: Callable[[str], list[float]],
    **retry_kwargs,
) -> tuple[list[dict], list[str]]:
    """Embed every chunk row's chunk_text. Returns (embedded_rows,
    skipped_chunk_ids) -- a chunk that fails all retries is dropped from
    embedded_rows entirely, not emitted with a null/empty embedding (SPEC
    Section 4.2: "該当チャンクをスキップしログに記録し、処理は継続する")."""
    embedded_rows: list[dict] = []
    skipped_ids: list[str] = []
    for row in chunk_rows:
        vector = embed_chunk_with_retry(embed_fn, row["chunk_text"], **retry_kwargs)
        if vector is None:
            skipped_ids.append(row["chunk_id"])
            continue
        embedded_rows.append({**row, "embedding": vector})
    if skipped_ids:
        logger.warning(
            "skipped %d chunk(s) after exhausting retries: %s", len(skipped_ids), skipped_ids
        )
    return embedded_rows, skipped_ids


# ---------------------------------------------------------------------------
# 3. ndjson output (SPEC Section 4.3 / Section 6-3). Stamping loaded_at is
#    reused from loader.emit.stamp_loaded_at (not reimplemented -- SPEC
#    Section 3: "既存資産の再利用を優先する"); build_ndjson_rows is the new
#    piece: selecting/validating exactly the raw_article_chunks schema
#    fields before loader.emit.write_ndjson serializes them to disk.
# ---------------------------------------------------------------------------


def build_ndjson_rows(embedded_rows: list[dict], loaded_at: str | None = None) -> list[dict]:
    """Convert embedded chunk rows into rows matching the raw_article_chunks
    BQ schema field-for-field (terraform/schemas/raw_article_chunks.json),
    stamping loaded_at via loader.emit.stamp_loaded_at. Raises KeyError
    eagerly if a row is missing a required schema field, rather than
    silently emitting a row `bq load`'s REQUIRED-mode columns would reject
    later, far away from this function."""
    stamped = stamp_loaded_at(embedded_rows, loaded_at=loaded_at)
    rows: list[dict] = []
    for row in stamped:
        missing = [name for name in SCHEMA_FIELDS if name not in row]
        if missing:
            raise KeyError(
                f"chunk row {row.get('chunk_id', '?')!r} missing schema field(s): {missing}"
            )
        rows.append({name: row[name] for name in SCHEMA_FIELDS})
    return rows


# ---------------------------------------------------------------------------
# 4. `bq load` command construction (SPEC Section 4.2 / Section 6-4) --
#    returns argv only, mirrors loader.bq_merge.bq_cli_runner's
#    load_staging branch; never executes anything.
# ---------------------------------------------------------------------------


def build_bq_load_args(
    project: str,
    dataset: str,
    source_uri: str,
    table: str = BQ_TABLE,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
) -> list[str]:
    """Build the `bq load` argv for a full-replace load of article_chunks
    (SPEC Section 4.2: "実行のたびに対象テーブルを--replaceで全再構築する").
    Returns the argument list only -- never calls subprocess (Section 6-4)."""
    return [
        "bq",
        "load",
        "--source_format=NEWLINE_DELIMITED_JSON",
        "--replace",
        f"--schema={schema_path}",
        f"{project}:{dataset}.{table}",
        source_uri,
    ]


def build_bq_append_args(
    project: str,
    dataset: str,
    source_uri: str,
    table: str = BQ_TABLE,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
) -> list[str]:
    """Build the `bq load` argv for an append-only load of article_chunks
    (SPEC-index-on-publish_design.md FR-2/FR-7: differential updates never
    carry --replace, so existing chunk rows are never deleted/replaced).
    Returns the argument list only -- never calls subprocess."""
    return [
        "bq",
        "load",
        "--source_format=NEWLINE_DELIMITED_JSON",
        f"--schema={schema_path}",
        f"{project}:{dataset}.{table}",
        source_uri,
    ]


# ---------------------------------------------------------------------------
# 6. Incremental (differential) update mode -- SPEC-index-on-publish.
#    chunk_published_articles() above still runs over every published
#    article (chunking is pure/offline and cheap); only the *embedding*
#    step is restricted to the chunk_ids scripts.index_diff says are
#    missing from the index (FR-1/FR-6), so the Gemini API is called only
#    for the differential chunks, not the full corpus.
# ---------------------------------------------------------------------------

LOCK_STALE_SECONDS = 30 * 60
MAX_PENDING_REPASSES = 3  # bounds the pending re-run loop against a
# runaway commit-storm (design Section "データモデル・インターフェース")
DEFAULT_ENV_FILE = Path.home() / ".config" / "gemini" / "env"


def append_ndjson(rows: list[dict], path: str | Path) -> None:
    """Append rows as NDJSON lines to path (never truncates -- unlike
    loader.emit.write_ndjson's "w"-mode, which would erase prior audit
    history on every run; design Section "既存アーキテクチャとの整合")."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")


def _parse_run_note(note: str) -> dict[str, str]:
    if not isinstance(note, str):
        return {}
    return dict(item.split("=", 1) for item in note.split(";") if "=" in item)


def detect_interrupted_run(runs_path: str | Path) -> str | None:
    """Scan index_update_runs.ndjson for a phase=start row with no matching
    phase=complete row for the same run_id (scenario 9). Returns the first
    such run_id, or None if every recorded run completed (or the file does
    not exist yet)."""
    runs_path = Path(runs_path)
    if not runs_path.exists():
        return None
    open_runs: dict[str, bool] = {}
    for line in runs_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # An interrupted append is exactly what this function exists
            # to detect -- a half-written trailing line must not kill the
            # detector itself (review P1).
            logger.warning("skipping corrupt audit line in %s", runs_path)
            continue
        if not isinstance(row, dict):
            continue
        note = _parse_run_note(row.get("exclusions_note", ""))
        run_id = note.get("run_id")
        if run_id is None:
            continue
        if note.get("phase") == "start":
            open_runs[run_id] = True
        elif note.get("phase") == "complete":
            open_runs.pop(run_id, None)
    return next(iter(open_runs), None)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def acquire_lock(lock_path: str | Path, now: float | None = None) -> bool:
    """Single-execution lock (design: "ロック+pending"). A lock is valid
    (blocks a new run) only while its pid is alive AND it was acquired
    less than LOCK_STALE_SECONDS ago -- anything else is stale and gets
    overwritten. Returns True iff this call acquired the lock."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    payload = json.dumps({"pid": os.getpid(), "acquired_at": now})
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass  # exists -- fall through to the staleness check
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            return True
        parsed = False
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            parsed = isinstance(data, dict)
        except (OSError, json.JSONDecodeError):
            data = {}
        if parsed:
            pid = data.get("pid")
            acquired_at = data.get("acquired_at")
            alive = isinstance(pid, int) and _pid_alive(pid)
            fresh = (
                isinstance(acquired_at, (int, float))
                and (now - acquired_at) < LOCK_STALE_SECONDS
            )
            if alive and fresh:
                return False
            # dead pid or stale timestamp -> reclaim below
        else:
            # Unparseable file: a lock caught between O_EXCL create and
            # write looks exactly like this. If its mtime is fresh it is
            # probably a live acquire in progress -- do not steal it;
            # only an old corrupt file is reclaimed (review P2 race).
            try:
                if (now - lock_path.stat().st_mtime) < LOCK_STALE_SECONDS:
                    return False
            except OSError:
                return False  # vanished mid-check or unreadable: give up this round
        # Stale or corrupt: remove and retry the atomic create.
        # O_EXCL makes the re-create race-free -- two processes that both
        # unlink still let only one win the create (review P1).
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def release_lock(lock_path: str | Path) -> None:
    Path(lock_path).unlink(missing_ok=True)


def mark_pending(pending_path: str | Path) -> None:
    pending_path = Path(pending_path)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text("1", encoding="utf-8")


def consume_pending(pending_path: str | Path) -> bool:
    pending_path = Path(pending_path)
    if pending_path.exists():
        pending_path.unlink()
        return True
    return False


def resolve_api_key(api_key_env: str, env_file: str | Path) -> str | None:
    """Env var first, then --env-file (default ~/.config/gemini/env),
    matching the existing with-gemini zsh-helper convention referenced in
    main()'s SystemExit message below. Returns None (never raises) when
    neither source has the key -- callers decide whether that is fatal
    (it is only fatal once the differential chunk set is non-empty;
    scenario 2 must reach 0 API calls without ever needing a key)."""
    key = os.environ.get(api_key_env)
    if key:
        return key
    env_file = Path(env_file)
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        # Real env files (incl. ~/.config/gemini/env) use `export KEY=...`
        # -- a bare-name match misses them and kills every hook-launched
        # run, since the hook environment has no exported key (review P0).
        if name.startswith("export "):
            name = name[len("export "):].strip()
        if name == api_key_env:
            return value.strip().strip('"').strip("'")
    return None


def run_incremental(args: argparse.Namespace) -> int:
    """Differential index update (scenarios 1-4/6/7/9). Never raises past
    this function for anything except a missing API key when the
    differential chunk set is non-empty (SystemExit, matching the
    full-rebuild path's existing failure mode) -- a post-commit/post-merge
    hook caller must never see this process block or crash the commit
    (FR-6/scenario 6), which is why on_published_commit.sh always launches
    this via `nohup ... &` rather than inline."""
    # Imported lazily (not at module import time) to avoid a circular
    # import: scripts.index_diff imports scripts.build_embeddings at
    # module load time for chunk_published_articles(); by the time this
    # function actually runs, both modules are already fully initialized.
    from scripts.index_diff import compute_index_diff

    out_dir = Path(args.out)
    lock_path = out_dir / ".index_update.lock"
    pending_path = out_dir / ".index_update.pending"
    runs_path = out_dir / "index_update_runs.ndjson"

    interrupted = detect_interrupted_run(runs_path)
    if interrupted:
        logger.warning("previous index_update run %s did not complete", interrupted)

    try:
        acquired = acquire_lock(lock_path)
    except OSError as exc:
        # e.g. out/ permissions: under nohup this would otherwise die
        # silently -- log and exit cleanly (the commit must never block).
        logger.error("could not acquire index-update lock: %s", exc)
        return 0
    if not acquired:
        mark_pending(pending_path)
        logger.info("index update already running; marked pending and exiting")
        return 0

    total_loaded = 0
    total_skipped: list[str] = []
    try:
        passes = 0
        while True:
            passes += 1
            run_id = uuid.uuid4().hex[:8]
            run_audit_rows: list[dict] = []

            def record_audit(row: dict) -> None:
                run_audit_rows.append(row)
                append_ndjson([row], runs_path)

            def ship_audit() -> None:
                """Append this run's audit rows to raw.load_runs via a
                dedicated per-run file -- already-shipped history rows in
                index_update_runs.ndjson are never re-appended (design
                P1-3). Runs that found no diff ship their start+complete
                pair too: it is the evidence the hook fired at all."""
                if not args.execute:
                    return
                audit_path = out_dir / f"load_runs_{run_id}.ndjson"
                write_ndjson(run_audit_rows, audit_path)
                audit_args = build_bq_append_args(
                    project=args.project,
                    dataset=args.dataset,
                    table="load_runs",
                    source_uri=str(audit_path),
                    schema_path=DEFAULT_LOAD_RUNS_SCHEMA_PATH,
                )
                audit_proc = subprocess.run(
                    audit_args, capture_output=True, text=True, timeout=120
                )
                if audit_proc.returncode != 0:
                    logger.error(
                        "bq load (load_runs append) failed: %s",
                        audit_proc.stderr.strip()[:200],
                    )

            record_audit(
                build_load_run(
                    "index_update",
                    0,
                    f"run_id={run_id};phase=start;trigger={args.trigger}",
                )
            )

            diff, expected_by_file = compute_index_diff(args.published_dir)
            repair_ids = set(diff.repair_chunk_ids(expected_by_file))

            if not repair_ids:
                # scenario 2: zero diff -> zero embedding calls, no API
                # key resolution at all.
                record_audit(
                    build_load_run(
                        "index_update",
                        0,
                        f"run_id={run_id};phase=complete;trigger={args.trigger};"
                        "load=not_executed;verify=skipped;"
                        "skipped_ids=none;verify_missing=none",
                    )
                )
                ship_audit()
                break

            chunk_rows = [
                row
                for file_chunks in expected_by_file.values()
                for cid, row in file_chunks.items()
                if cid in repair_ids
            ]

            api_key = resolve_api_key(args.api_key_env, args.env_file)
            if not api_key:
                raise SystemExit(
                    f"{args.api_key_env} is not set and not found in {args.env_file}; "
                    "cannot call the Gemini embedding API."
                )
            client = build_gemini_client(api_key)

            def embed_fn(text: str, _client=client) -> list[float]:
                return call_embedding_api(_client, text, task_type="RETRIEVAL_DOCUMENT")

            embedded_rows, skipped_ids = embed_chunks(chunk_rows, embed_fn)
            rows = build_ndjson_rows(embedded_rows)
            # Chunk rows and audit rows live in differently-named per-run
            # files: the delta file feeds raw.article_chunks, the
            # load_runs file feeds raw.load_runs. Naming either just
            # "load_runs" while it held chunk rows would be a silent
            # schema-mismatch trap for a later manual load.
            delta_path = out_dir / f"article_chunks_delta_{run_id}.ndjson"
            n = write_ndjson(rows, delta_path) if rows else 0

            bq_args = build_bq_append_args(
                project=args.project,
                dataset=args.dataset,
                source_uri=str(delta_path),
                schema_path=args.schema_path,
            )
            # load/verify are recorded as explicit states, not collapsed
            # into the id list -- a durable audit row must distinguish
            # "verified: nothing missing" from "could not verify"
            # (review P2).
            verify_state = "skipped"
            verify_missing: list[str] = []
            if not rows:
                # Every chunk skipped after retries -> empty delta. Loading
                # an empty file would record load=failed on a run that
                # simply had nothing to load (review P2).
                load_status = "skipped_empty"
                logger.info("no embedded rows -- skipping bq load")
            elif args.execute:
                proc = subprocess.run(bq_args, capture_output=True, text=True, timeout=120)
                if proc.returncode != 0:
                    load_status = "failed"
                    logger.error("bq load (append) failed: %s", proc.stderr.strip()[:200])
                else:
                    load_status = "ok"
                    # Grounding check (design: "--executeはload後にロードした
                    # chunk_idが索引に存在することを再クエリで検証"): the
                    # load returning success is thin evidence -- re-query
                    # the index and confirm the ids are actually there.
                    loaded_ids = {r["chunk_id"] for r in rows}
                    try:
                        from scripts.index_diff import fetch_indexed_rows

                        verified_ids = {r["chunk_id"] for r in fetch_indexed_rows()}
                        verify_missing = sorted(loaded_ids - verified_ids)
                        verify_state = "ok"
                    except Exception as exc:  # noqa: BLE001 -- verify is diagnostic
                        verify_state = "unverified"
                        logger.warning("post-load index verify failed: %s", exc)
                    if verify_missing:
                        logger.warning(
                            "post-load verify: %d chunk_id(s) absent from index: %s",
                            len(verify_missing),
                            verify_missing,
                        )
            else:
                load_status = "not_executed"
                logger.info("bq load command (not executed): %s", " ".join(bq_args))
                print(" ".join(bq_args))

            total_loaded += n
            total_skipped.extend(skipped_ids)
            skipped_note = ", ".join(skipped_ids) if skipped_ids else "none"
            verify_note = ", ".join(verify_missing) if verify_missing else "none"
            complete_row = build_load_run(
                "index_update",
                n,
                f"run_id={run_id};phase=complete;trigger={args.trigger};"
                f"load={load_status};verify={verify_state};"
                f"skipped_ids={skipped_note};verify_missing={verify_note}",
            )
            record_audit(complete_row)
            ship_audit()

            # Check the pass cap BEFORE consuming pending: a marker that
            # arrives during the final pass must survive for the next run
            # rather than being consumed and dropped (review P2).
            if passes >= MAX_PENDING_REPASSES:
                logger.info("pass cap reached (%d); leaving pending for next run", passes)
                break
            if not consume_pending(pending_path):
                break
    finally:
        release_lock(lock_path)

    logger.info(
        "index update complete: %d row(s) loaded, %d skipped: %s",
        total_loaded,
        len(total_skipped),
        total_skipped or "none",
    )
    return 0


# ---------------------------------------------------------------------------
# 5. main() -- wires 1-4 together (SPEC Section 9 phase 2)
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_embeddings",
        description=(
            "Chunk note-articles/published/*.md, embed each chunk via "
            "gemini-embedding-001, and write raw_article_chunks.ndjson plus "
            "the `bq load` command to load it (never executed here)."
        ),
    )
    parser.add_argument(
        "--published-dir",
        default=str(DEFAULT_PUBLISHED_DIR),
        help="Path to a published/ dir of note-articles (default: ~/Developer/note-articles/published).",
    )
    parser.add_argument("--out", default="out", help="Output directory for the NDJSON file.")
    parser.add_argument(
        "--project", required=True, help="GCP project id (for the bq load command)."
    )
    parser.add_argument("--dataset", default="raw", help="BigQuery dataset id (default: raw).")
    parser.add_argument(
        "--schema-path",
        default=str(DEFAULT_SCHEMA_PATH),
        help="Path to the raw_article_chunks.json BQ schema file.",
    )
    parser.add_argument(
        "--api-key-env",
        default="GEMINI_API_KEY",
        help="Environment variable holding the Gemini API key (default: GEMINI_API_KEY).",
    )
    parser.add_argument(
        "--dry-run-chunks",
        action="store_true",
        help="Chunk and log only -- skip embedding calls and ndjson/bq-load output entirely.",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "Differential mode (SPEC-index-on-publish): embed only the chunk_ids "
            "scripts.index_diff reports missing/partial, then `bq load` (append, "
            "no --replace). Mutually exclusive in practice with --dry-run-chunks."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="With --incremental, actually run the append `bq load` via subprocess.",
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help=(
            "With --incremental, fallback source for the Gemini API key when "
            "--api-key-env is not set in the environment (default: "
            "~/.config/gemini/env)."
        ),
    )
    parser.add_argument(
        "--trigger",
        default="manual",
        choices=["manual", "hook"],
        help="With --incremental, recorded in the audit row's exclusions_note.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> list[dict] | int:
    """Chunk -> embed -> ndjson -> (unexecuted) bq load command (SPEC
    Section 9 phase 2). Never calls the real Gemini API or `bq` itself in
    this repo's own test/dev runs -- only when an operator runs this file
    directly with a real GEMINI_API_KEY set (post-CEO-confirmation,
    SPEC Section 7.1).

    --incremental (SPEC-index-on-publish) dispatches to run_incremental()
    instead and returns an int exit code, rather than the row list every
    other mode returns -- callers (tests, on_published_commit.sh) branch
    on args.incremental to know which shape to expect."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    if args.incremental:
        return run_incremental(args)

    result = chunk_published_articles(args.published_dir)
    if args.dry_run_chunks:
        return result.rows

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(
            f"{args.api_key_env} is not set; cannot call the Gemini embedding API.\n"
            "Local runs: the key lives in ~/.config/gemini/env (not exported globally).\n"
            "  with-gemini .venv/bin/python scripts/build_embeddings.py ...   (zsh helper)\n"
            "  or: (set -a; . ~/.config/gemini/env; set +a; .venv/bin/python scripts/build_embeddings.py ...)"
        )

    client = build_gemini_client(api_key)

    def embed_fn(text: str) -> list[float]:
        # RETRIEVAL_DOCUMENT: the indexing side (SPEC Section 9 phase 3.5;
        # api.main.embed_question is the RETRIEVAL_QUERY counterpart).
        return call_embedding_api(client, text, task_type="RETRIEVAL_DOCUMENT")

    embedded_rows, skipped_ids = embed_chunks(result.rows, embed_fn)

    rows = build_ndjson_rows(embedded_rows)
    out_path = Path(args.out) / "raw_article_chunks.ndjson"
    n = write_ndjson(rows, out_path)
    logger.info(
        "wrote %d row(s) to %s (skipped %d chunk(s) after retries: %s)",
        n,
        out_path,
        len(skipped_ids),
        skipped_ids,
    )

    bq_args = build_bq_load_args(
        project=args.project,
        dataset=args.dataset,
        source_uri=str(out_path),
        schema_path=args.schema_path,
    )
    logger.info("bq load command (not executed): %s", " ".join(bq_args))
    print(" ".join(bq_args))

    # raw.load_runs audit trail (Phase 3.5 / SPEC Section 8 risk table:
    # "埋め込みロード時の欠落...がログにしか残らず、BigQuery上で追跡でき
    # ない"). Mirrors loader.__main__.run()'s build_load_run(source,
    # rows_loaded, note) + write_ndjson(..., "raw_load_runs.ndjson")
    # pattern -- reused, not reimplemented (SPEC Section 3). Both skip
    # categories are named explicitly by count so an operator (or a future
    # dashboard over raw.load_runs) does not have to go back to the log
    # output to know what was excluded.
    skipped_files_note = ", ".join(result.skipped_files) if result.skipped_files else "none"
    skipped_ids_note = ", ".join(skipped_ids) if skipped_ids else "none"
    note = (
        f"skipped_files={len(result.skipped_files)} (filename-convention "
        f"violations: {skipped_files_note}); "
        f"skipped_ids={len(skipped_ids)} (429 retry exhausted: {skipped_ids_note})"
    )
    load_run = build_load_run("raw_article_chunks", n, note)
    write_ndjson([load_run], Path(args.out) / "raw_load_runs.ndjson")

    return rows


if __name__ == "__main__":
    main()
