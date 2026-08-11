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
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loader.emit import stamp_loaded_at, write_ndjson
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
    cut mid-sentence."""
    if len(text) <= max_chars:
        return [text]

    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    groups: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        para_len = len(para)
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


def call_embedding_api(client, text: str, model: str = EMBEDDING_MODEL) -> list[float]:
    """One real call to the Gemini embedding API -- no retry logic here.
    embed_chunk_with_retry owns the retry loop, so tests mock this single
    call directly instead of reimplementing backoff (SPEC Section 4.2 /
    Section 6-2)."""
    from google.genai import errors

    try:
        response = client.models.embed_content(model=model, contents=text)
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> list[dict]:
    """Chunk -> embed -> ndjson -> (unexecuted) bq load command (SPEC
    Section 9 phase 2). Never calls the real Gemini API or `bq` itself in
    this repo's own test/dev runs -- only when an operator runs this file
    directly with a real GEMINI_API_KEY set (post-CEO-confirmation,
    SPEC Section 7.1)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    result = chunk_published_articles(args.published_dir)
    if args.dry_run_chunks:
        return result.rows

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"{args.api_key_env} is not set; cannot call the Gemini embedding API.")

    client = build_gemini_client(api_key)

    def embed_fn(text: str) -> list[float]:
        return call_embedding_api(client, text)

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
    return rows


if __name__ == "__main__":
    main()
