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
"""
from __future__ import annotations

import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from api.query import build_vector_search_sql
from scripts.build_embeddings import build_gemini_client, call_embedding_api

app = FastAPI(title="agent-ops-warehouse RAG API")

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GCP_PROJECT_ENV = "GCP_PROJECT_ID"
BQ_DATASET_ENV = "BQ_DATASET"
DEFAULT_BQ_DATASET = "raw"
# Lightweight/cheap generation model for the optional 1-2 sentence
# summarize=true path (SPEC Section 4.4). Never called in this repo's own
# tests -- summarize_answer is always monkeypatched (task constraint: no
# real Gemini API call anywhere in the test suite).
SUMMARY_MODEL = "gemini-2.5-flash"
# Bounds (Phase 3.5 / SPEC Section 8 risk table: a leaked single Bearer
# token has no rate limit today, so an unbounded question/top_k is a
# self-DoS vector against the Gemini free-tier RPM/day limit and BigQuery's
# free query tier; SPEC Section 9 phase 3.5). Values match the SPEC's own
# examples: "妥当な最大長(例: 2,000字...)" / "top_kに妥当な上限(例: 20)".
MAX_QUESTION_LENGTH = 2000
MAX_TOP_K = 20


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


def run_vector_search_query(sql: str) -> list[dict]:
    """Execute a VECTOR_SEARCH SQL string (from
    api.query.build_vector_search_sql) against BigQuery and return the
    result rows as plain dicts. Any failure -- auth, network, malformed
    SQL -- is wrapped in RetrievalError."""
    try:
        client = build_bigquery_client()
        query_job = client.query(sql)
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
    try:
        return execute_query(question, payload.top_k, payload.summarize)
    except RetrievalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
