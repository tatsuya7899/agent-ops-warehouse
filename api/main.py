"""FastAPI RAG service: GET /health, POST /query (RAG API phase 3 --
SPEC-agent-ops-warehouse-rag-api_20260811.md Section 4.4).

Checkpoint 2 laid down routing, Bearer auth against the API_TOKEN env var,
and request-body validation. Checkpoint 3 (this file, current state) wires
execute_query() to a real Gemini embedding call (reusing
scripts.build_embeddings' client/call functions -- SPEC Section 3: "既存
資産の再利用を優先する") + api.query.build_vector_search_sql +
google-cloud-bigquery, with RetrievalError as the one exception type the
route maps to HTTP 503 (SPEC Section 4.4: "黙って200を返さない"). Every
test still monkeypatches at a function boundary (embed_question,
run_vector_search_query, summarize_answer, or execute_query itself) --
never a real Gemini/BigQuery call in this repo's own test suite.

Phase 3.6 (SPEC Section 9 phase 3.6 / Section 8 risk table -- "課金の数学
的上限設計") adds two independent spend caps on top of the above: a
BigQuery-side maximum_bytes_billed hard limit per query
(run_vector_search_query/build_query_job_config, this file) and an
application-side daily request-count limit (require_daily_request_limit,
this file). Neither replaces the other -- the byte cap bounds a single
query's worst case, the daily limit bounds how many queries can run at
all in a day.
"""
from __future__ import annotations

import logging
import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from api.query import build_vector_search_sql
from scripts.build_embeddings import build_gemini_client, call_embedding_api

logger = logging.getLogger(__name__)

app = FastAPI(
    title="agent-ops-warehouse RAG API",
    # shipping-reviewer retroactive audit (2026-08-12): FastAPI's
    # auto-generated /docs, /redoc, /openapi.json were reachable
    # unauthenticated (allUsers invoker, no Depends on those routes),
    # contradicting the README's claim that /health is the only
    # unauthenticated surface. No secrets leak through them, but the
    # claim must be true. Disabled rather than gated -- they add no
    # value to this single-endpoint API.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GCP_PROJECT_ENV = "GCP_PROJECT_ID"
BQ_DATASET_ENV = "BQ_DATASET"
DEFAULT_BQ_DATASET = "raw"
# Lightweight/cheap generation model for the optional 1-2 sentence
# summarize=true path (SPEC Section 4.4). Never called in this repo's own
# tests -- summarize_answer is always monkeypatched (task constraint: no
# real Gemini API call anywhere in the test suite).
SUMMARY_MODEL = "gemini-3.5-flash"
# Bounds (Phase 3.5 / SPEC Section 8 risk table: a leaked single Bearer
# token has no rate limit today, so an unbounded question/top_k is a
# self-DoS vector against the Gemini free-tier RPM/day limit and BigQuery's
# free query tier; SPEC Section 9 phase 3.5). Values match the SPEC's own
# examples: "妥当な最大長(例: 2,000字...)" / "top_kに妥当な上限(例: 20)".
MAX_QUESTION_LENGTH = 2000
MAX_TOP_K = 20
# Phase 3.6 / SPEC Section 9 phase 3.6, Section 8 risk table: a leaked
# Bearer token has no rate limit that stops BigQuery's brute-force
# VECTOR_SEARCH from scanning the whole article_chunks table on every
# request. maximum_bytes_billed makes BigQuery synchronously *reject* (not
# just warn about) any query whose estimated scan exceeds this many bytes,
# before it runs -- 100MB is the SPEC's own number; the real table is
# ~7MB, so this only ever fires for a runaway/malformed query, never a
# normal one.
MAXIMUM_BYTES_BILLED = 100 * 1024 * 1024

# Daily request-count cap (Phase 3.6 / SPEC Section 9 phase 3.6, Section 8
# risk table): maximum_bytes_billed above bounds one query's worst case;
# this is a *best-effort burst brake* on top of it, not a second guaranteed
# ceiling -- see the honesty note below. The 293GB/month (~28.6% of
# BigQuery's 1TiB/month free query tier) figure in the SPEC and README
# assumes this counter holds for a full day; it is not the load-bearing
# guarantee. The load-bearing guarantee is MAXIMUM_BYTES_BILLED alone,
# which is synchronous and per-query, independent of process lifetime.
DAILY_REQUEST_LIMIT_ENV = "DAILY_REQUEST_LIMIT"
DEFAULT_DAILY_REQUEST_LIMIT = 100

# In-memory, module-level counter keyed by UTC calendar date.
#
# Two separate correctness conditions, both required, only one of which
# Phase 3.6 originally documented:
#   1. max_instance_count=1 (Cloud Run, Phase 4, SPEC Section 4.5) -- a
#      single instance means only one copy of this dict exists at a time.
#      Raising max_instance_count silently multiplies the effective limit
#      by instance count. Don't bump it without revisiting this.
#   2. HONESTY NOTE (found in shipping-reviewer's retroactive audit,
#      2026-08-12, after the SPEC's original "worst case 293GB/month" math
#      was already written and published): Cloud Run is also deployed with
#      min_instance_count=0 (cost -- SPEC Section 4.5). When the service is
#      idle long enough to scale to zero, this dict is destroyed with the
#      process; the next request starts a fresh instance with a fresh,
#      empty dict. A caller who paces requests to ride out cold starts can
#      reset the count arbitrarily many times per day. This counter is
#      therefore a burst brake against accidental/naive over-calling, not
#      a guaranteed daily ceiling -- do not describe it as one in docs.
#      Making it a true ceiling would need a shared store (BigQuery/
#      Firestore counter row) or min_instance_count=1 (real cost); neither
#      is justified by this project's actual traffic, so this limitation
#      is accepted and documented rather than engineered away.
_daily_request_counts: dict[str, int] = {}


def _today_utc_key() -> str:
    """UTC calendar date as a stable dict key (e.g. "2026-08-12"). A
    function, not an inlined call, so tests can monkeypatch it to move
    the "day" instead of waiting on (or mocking) a real clock."""
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


def require_daily_request_limit() -> None:
    """Reject the request with 429 once today's (UTC) request count has
    reached DAILY_REQUEST_LIMIT (env DAILY_REQUEST_LIMIT_ENV, default
    DEFAULT_DAILY_REQUEST_LIMIT) -- SPEC Section 9 phase 3.6.

    Design decision (task instruction: record which side of validation
    this runs on): the /query route calls this *after* question/top_k
    validation (checkpoint 2's 400s), not before. A structurally invalid
    request never reaches embed_question/run_vector_search_query -- it
    costs nothing -- so counting it here too would let a flood of garbage
    400s burn through the day's budget without ever touching the Gemini/
    BigQuery pipeline this limit exists to bound, denying legitimate
    requests for the rest of the day for zero savings. Counting only
    validated requests keeps this limit's math (100/day x 100MB, SPEC
    Section 9 phase 3.6) an accurate bound on the costly path specifically.
    """
    limit = int(os.environ.get(DAILY_REQUEST_LIMIT_ENV, DEFAULT_DAILY_REQUEST_LIMIT))
    key = _today_utc_key()
    count = _daily_request_counts.get(key, 0)
    if count >= limit:
        raise HTTPException(status_code=429, detail="daily request limit exceeded")
    _daily_request_counts[key] = count + 1


class QueryRequest(BaseModel):
    question: str
    top_k: int = 5
    summarize: bool = False


class ChunkResult(BaseModel):
    article_title: str
    url: str
    excerpt: str
    similarity_score: float


class QueryResponse(BaseModel):
    results: list[ChunkResult]
    answer: str | None = None


def require_api_token(authorization: str | None = Header(default=None)) -> str:
    """Bearer-token auth against the API_TOKEN env var (SPEC Section 4.4:
    "単一の静的Bearerトークン"). Read via os.environ at request time, not
    at module import time, so tests can monkeypatch.setenv/delenv per
    case without reimporting this module (mirrors
    scripts.build_embeddings.main's os.environ.get(args.api_key_env)).

    An unset API_TOKEN rejects every request rather than silently
    accepting any token or crashing -- "黙って200を返さない" (SPEC Section
    4.4) applies to misconfiguration too, not just bad input.

    .strip() on the expected side (Phase 3.5 / SPEC Section 9 phase 3.5:
    "Secret Manager登録時の空白混入事故対策") -- the Bearer header side
    already stripped (`token = ...strip()` below); a Secret Manager value
    saved with a stray trailing newline/leading space previously rejected
    every otherwise-correct request with no way to tell why.
    """
    expected = (os.environ.get("API_TOKEN") or "").strip()
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")
    return token


@app.get("/health")
def health() -> dict:
    """Unauthenticated liveness check (SPEC Section 4.4: "認証なし・課金
    にも繋がらない軽量応答")."""
    return {"status": "ok"}


class RetrievalError(Exception):
    """Raised when embedding generation, the BigQuery VECTOR_SEARCH call,
    or (when requested) the Gemini summarize call fails, or when required
    config (GCP_PROJECT_ID / GEMINI_API_KEY) is missing. The route handler
    maps every RetrievalError to HTTP 503 -- one exception type, one
    mapping (SPEC Section 4.4: "黙って200を返さない")."""


def embed_question(question: str) -> list[float]:
    """One Gemini embedding call for the query text. Reuses
    scripts.build_embeddings.build_gemini_client/call_embedding_api (SPEC
    Section 3: 既存資産の再利用を優先する) instead of a second Gemini
    client implementation. Any failure -- missing key, network, quota --
    is wrapped in RetrievalError so execute_query has one exception type
    to catch."""
    api_key = os.environ.get(GEMINI_API_KEY_ENV)
    if not api_key:
        raise RetrievalError(f"{GEMINI_API_KEY_ENV} is not set")
    try:
        client = build_gemini_client(api_key)
        # RETRIEVAL_QUERY: the query side (SPEC Section 9 phase 3.5;
        # scripts.build_embeddings' embed_fn is the RETRIEVAL_DOCUMENT
        # counterpart on the indexing side).
        return call_embedding_api(client, question, task_type="RETRIEVAL_QUERY")
    except Exception as exc:
        # Deliberately broad: every embedding-call failure (network, auth,
        # quota, a raw RateLimitError bubbling up because this single call
        # has no retry loop of its own -- retries are build_embeddings'
        # batch-load concern, not a synchronous query request's) is a
        # retrieval failure from this API's point of view, and all of them
        # map to the same 503.
        raise RetrievalError(f"embedding call failed: {exc}") from exc


def build_bigquery_client():
    """Thin factory around google.cloud.bigquery.Client, imported lazily
    so importing api.main never requires google-cloud-bigquery importable
    for tests that never reach this function (mirrors
    scripts.build_embeddings.build_gemini_client's lazy-import contract).
    """
    from google.cloud import bigquery

    return bigquery.Client()


def build_query_job_config():
    """Thin factory around google.cloud.bigquery.QueryJobConfig, lazily
    imported for the same reason as build_bigquery_client (importing
    api.main never requires google-cloud-bigquery importable for tests
    that never reach this function). Kept separate from
    build_bigquery_client because the byte cap is a per-query setting,
    not a per-client one (Phase 3.6)."""
    from google.cloud import bigquery

    return bigquery.QueryJobConfig(maximum_bytes_billed=MAXIMUM_BYTES_BILLED)


def run_vector_search_query(sql: str) -> list[dict]:
    """Execute a VECTOR_SEARCH SQL string (from
    api.query.build_vector_search_sql) against BigQuery and return the
    result rows as plain dicts. Any failure -- auth, network, malformed
    SQL, or BigQuery's own pre-execution rejection of a query whose
    estimated scan exceeds maximum_bytes_billed (Phase 3.6) -- is wrapped
    in RetrievalError; the byte cap needs no special-casing here, it is
    just one more way this call can fail."""
    try:
        client = build_bigquery_client()
        job_config = build_query_job_config()
        query_job = client.query(sql, job_config=job_config)
        return [dict(row) for row in query_job.result()]
    except Exception as exc:
        raise RetrievalError(f"BigQuery VECTOR_SEARCH failed: {exc}") from exc


def _row_to_chunk_result(row: dict) -> ChunkResult:
    """SPEC Section 4.4: URL falls back to filename verbatim. No
    filename -> note.com public-URL mapping exists anywhere in this repo
    today (checked scripts/build_embeddings.py's SCHEMA_FIELDS,
    loader/extract_articles.py, note-articles/README.md's publish
    workflow) -- the task's own instruction is explicit that this is not
    to be invented speculatively ("公開URL変換の仕組みが存在しないなら無
    理に作らない"), so filename is returned as-is.

    similarity_score = 1 - distance: for distance_type COSINE, BigQuery's
    VECTOR_SEARCH distance is `1 - cosine_similarity` (0 = identical
    direction), so this recovers the more intuitive "higher is more
    similar" score for the API response without changing what SQL asks
    BigQuery to compute.
    """
    return ChunkResult(
        article_title=row["article_title"],
        url=row["filename"],
        excerpt=row["chunk_text"],
        similarity_score=1.0 - row["distance"],
    )


def summarize_answer(question: str, results: list[ChunkResult]) -> str:
    """One Gemini generate_content call producing a 1-2 sentence answer
    grounded only in the retrieved excerpts (SPEC Section 4.4:
    summarize=true option). summarize=true is an explicit part of the
    request, so -- same posture as embed_question/run_vector_search_query
    -- a failed summary is a failed request (503), not a silently
    degraded 200 with a missing `answer` field."""
    api_key = os.environ.get(GEMINI_API_KEY_ENV)
    if not api_key:
        raise RetrievalError(f"{GEMINI_API_KEY_ENV} is not set")
    context = "\n\n".join(result.excerpt for result in results)
    prompt = (
        "以下の記事抜粋だけを根拠に、質問へ日本語で1〜2文で答えてください。\n\n"
        f"質問: {question}\n\n抜粋:\n{context}"
    )
    try:
        client = build_gemini_client(api_key)
        response = client.models.generate_content(model=SUMMARY_MODEL, contents=prompt)
        return response.text
    except Exception as exc:
        raise RetrievalError(f"summarize call failed: {exc}") from exc


def execute_query(question: str, top_k: int, summarize: bool) -> QueryResponse:
    """Embed the question -> VECTOR_SEARCH on BigQuery -> map rows to
    ChunkResult -> optionally summarize (SPEC Section 4.3/4.4). Every
    external call in this pipeline raises RetrievalError on failure,
    never a bare exception the route handler would have to guess about.
    """
    project = os.environ.get(GCP_PROJECT_ENV)
    if not project:
        raise RetrievalError(f"{GCP_PROJECT_ENV} is not set")
    dataset = os.environ.get(BQ_DATASET_ENV, DEFAULT_BQ_DATASET)

    try:
        embedding = embed_question(question)
        sql = build_vector_search_sql(
            project=project, dataset=dataset, embedding=embedding, top_k=top_k
        )
        rows = run_vector_search_query(sql)
        results = [_row_to_chunk_result(row) for row in rows]
        answer = summarize_answer(question, results) if summarize and results else None
    except RetrievalError:
        # embed_question/run_vector_search_query/summarize_answer already
        # wrap their own failures -- re-raise unchanged rather than
        # double-wrapping the message.
        raise
    except Exception as exc:
        # Safety net for any exception that reaches this far unwrapped
        # (e.g. a monkeypatched stand-in in a test, or a future call site
        # added here without its own try/except) -- still one exception
        # type, one 503 mapping, never a bare 500 (SPEC Section 4.4).
        raise RetrievalError(f"query pipeline failed: {exc}") from exc

    return QueryResponse(results=results, answer=answer)


@app.post("/query", response_model=QueryResponse)
def query(payload: QueryRequest, _token: str = Depends(require_api_token)) -> QueryResponse:
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")
    if len(question) > MAX_QUESTION_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"question must be at most {MAX_QUESTION_LENGTH} characters",
        )
    if payload.top_k <= 0:
        raise HTTPException(status_code=400, detail="top_k must be a positive integer")
    if payload.top_k > MAX_TOP_K:
        raise HTTPException(status_code=400, detail=f"top_k must be at most {MAX_TOP_K}")
    # After validation, before execute_query -- see
    # require_daily_request_limit's docstring for why (a 400 must not
    # consume the day's budget).
    require_daily_request_limit()
    try:
        return execute_query(question, payload.top_k, payload.summarize)
    except RetrievalError as exc:
        # shipping-reviewer retroactive audit (2026-08-12): the raw
        # exception string (e.g. a BigQuery "table not found" error) can
        # include the fully-qualified project/dataset/table reference.
        # Log the detail server-side (visible in Cloud Run logs to the
        # operator) and return a generic message to the caller.
        logger.warning("retrieval failed: %s", exc)
        raise HTTPException(status_code=503, detail="retrieval temporarily unavailable") from exc
