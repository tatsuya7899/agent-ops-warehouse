#!/usr/bin/env python3
"""eval/run_eval.py — golden query regression harness for the RAG API
(SPEC: _ops/specs/SPEC-rag-eval-harness_20261008.md).

Throws each entry of eval/golden_queries.json at the live POST /query and
checks, deterministically, whether the expected article comes back inside
top_k (positive queries) or whether every returned score stays under the
noise ceiling (negative control queries). Verdict classes are separated
so that "expected file was never indexed" (coverage gap) is reported
apart from "indexed but not retrieved" (retrieval failure) — the same
retrieval/generation separation principle the spec borrows, applied
one level up (index vs retrieval).

Usage:
    export RAG_API_URL="https://rag-api-xxxxx-uc.a.run.app"
    export RAG_API_TOKEN="$(gcloud secrets versions access latest \
        --secret=API_TOKEN --project=agent-ops-warehouse)"
    python3 eval/run_eval.py [--top-k 5] [--check-index] [--json]

Manual run only by design (no unattended automation). Each run costs
one /query call per golden entry against the API's daily request cap.

Exit codes: 0 = all pass / 1 = quality regression (miss, coverage gap,
or negative-control violation) / 2 = could not execute (env, auth,
transport, malformed golden file).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_queries.json"
DEFAULT_API_URL = "https://rag-api-kgjvrywpmq-uc.a.run.app"
DEFAULT_TOP_K = 5
TIMEOUT_SEC = 60
# Standard SQL resolves the fully-qualified table name without
# backtick-quoting — see the NOTE on indexed_filenames for why the
# literal ` form must be avoided in this environment.
BQ_INDEX_SQL = (
    "SELECT DISTINCT filename "
    "FROM agent-ops-warehouse.raw.article_chunks"
)

# Score bands mirror the article-search SKILL's measured ranges
# (hit 0.71-0.84 / noise 0.53-0.55). 0.60-0.70 is the grey zone:
# below it a score is treated as noise, above it as a candidate hit.
NOISE_CEILING = 0.60
HIT_FLOOR = 0.70

VERDICTS = ("hit", "miss", "not_indexed", "pass", "violation", "grey")


class EvalRuntimeError(Exception):
    """The eval itself could not run (env/transport/auth/golden shape).
    Distinct from a quality regression — see main()'s exit codes."""


def post_query(api_url: str, token: str, question: str, top_k: int,
               timeout: int = TIMEOUT_SEC) -> dict:
    """One POST /query call. Injectable boundary — tests never reach
    the real API (repo convention: no live calls in the test suite)."""
    body = json.dumps(
        {"question": question, "top_k": top_k, "summarize": False}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}/query",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise EvalRuntimeError(f"/query returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise EvalRuntimeError(f"/query call failed: {exc}") from exc
    if not isinstance(payload, dict) or "results" not in payload:
        raise EvalRuntimeError("/query response missing 'results'")
    return payload


def indexed_filenames() -> set[str]:
    """Distinct filenames present in raw.article_chunks via bq.
    Injectable boundary — tests inject a fake set instead.

    NOTE: the SQL deliberately avoids backtick-quoting the table path —
    a literal ` inside a shell command trips the Studio guard's
    command-substitution pattern (cmdname-via-subst). Standard SQL
    resolves the fully-qualified name without it."""
    proc = subprocess.run(
        [
            "bq", "query", "--project_id=agent-ops-warehouse",
            "--use_legacy_sql=false", "--format=json", BQ_INDEX_SQL,
        ],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise EvalRuntimeError(f"bq index check failed: {proc.stderr.strip()[:200]}")
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise EvalRuntimeError("bq index check returned non-JSON") from exc
    return {row["filename"] for row in rows if "filename" in row}


def coverage_check(published_dir: Path, indexed: set[str]) -> list[str]:
    """Published files absent from the index — the full-corpus version
    of the per-expect not_indexed verdict. A missing file means the
    loader never embedded it: search can NEVER return it, which is a
    different failure from 'indexed but not retrieved'.
    Returns sorted filenames; [] when the dir is absent or empty."""
    if not published_dir.is_dir():
        return []
    published = {p.name for p in published_dir.glob("*.md")}
    return sorted(published - indexed)


def judge_positive(entry: dict, results: list[dict], top_k: int,
                   indexed: set[str] | None) -> dict:
    """Verdict for one positive query.

    'url' in a result is the published filename verbatim (see api/main.py
    _row_to_chunk_result). Verdict classes:
      hit         — expect found inside top_k (records rank+score)
      miss        — expect indexed (or index unchecked) but absent
      not_indexed — --check-index proved the expect is missing from the
                    index: coverage gap, NOT a retrieval failure
    """
    expect = entry["expect"]
    if indexed is not None and expect not in indexed:
        return {"id": entry["id"], "verdict": "not_indexed",
                "expect": expect, "rank": None, "score": None}
    returned = results[:top_k]
    for rank, row in enumerate(returned, start=1):
        if row.get("url") == expect:
            return {"id": entry["id"], "verdict": "hit",
                    "expect": expect, "rank": rank,
                    "score": row.get("similarity_score")}
    top_score = max((r.get("similarity_score") or 0.0 for r in returned),
                    default=None)
    # missの帯を分離(SPEC §3.2): トップ返却が何に化けたかで原因を診断する
    #   low  — 近い記事すら返らない=検索そのものの失敗
    #   grey — グレーゾーンの別記事がトップ=順位競合の可能性
    #   high — 0.70超の別記事がトップ=誤ヒット級のミスランク疑い
    if top_score is None or top_score < NOISE_CEILING:
        band = "low"
    elif top_score < HIT_FLOOR:
        band = "grey"
    else:
        band = "high"
    return {"id": entry["id"], "verdict": "miss", "expect": expect,
            "rank": None, "score": top_score, "band": band}


def judge_negative(entry: dict, results: list[dict], top_k: int) -> dict:
    """Verdict for one negative control query (SPEC §3.2):
      pass      — top score below NOISE_CEILING (noise band)
      grey      — 0.60-0.70: 警告のみ。判定には使わない(誤ヒット疑いで
                  個別報告するがregressionにはしない)
      violation — at/above HIT_FLOOR: ヒット級スコアを誤返却=退行"""
    returned = results[:top_k]
    top_score = max((r.get("similarity_score") or 0.0 for r in returned),
                    default=0.0)
    if top_score < NOISE_CEILING:
        verdict = "pass"
    elif top_score < HIT_FLOOR:
        verdict = "grey"
    else:
        verdict = "violation"
    return {"id": entry["id"], "verdict": verdict, "expect": None,
            "rank": None, "score": top_score}


def run_eval(queries: list[dict], call_fn, top_k: int,
             indexed: set[str] | None) -> dict:
    """Drive the whole golden set through call_fn and collect verdicts.
    call_fn(question, top_k) -> dict shaped like the /query response."""
    rows = []
    for entry in queries:
        resp = call_fn(entry["question"], top_k)
        results = resp.get("results", [])
        if entry["type"] == "positive":
            rows.append(judge_positive(entry, results, top_k, indexed))
        else:
            rows.append(judge_negative(entry, results, top_k))
    return {"queries": rows, "top_k": top_k}


def summarize(report: dict) -> dict:
    positives = [r for r in report["queries"]
                 if r["verdict"] in ("hit", "miss")]
    hits = [r for r in positives if r["verdict"] == "hit"]
    coverage_gaps = [r for r in report["queries"]
                     if r["verdict"] == "not_indexed"]
    negatives = [r for r in report["queries"]
                 if r["verdict"] in ("pass", "grey", "violation")]
    neg_max = max((r["score"] for r in negatives), default=None)
    judged = len(positives)
    return {
        "recall_at_k": f"{len(hits)}/{judged}",
        "recall_ratio": (len(hits) / judged) if judged else None,
        "coverage_gaps": [r["expect"] for r in coverage_gaps],
        "negative_max_score": neg_max,
        "regressions": [r for r in report["queries"]
                        if r["verdict"] in ("miss", "not_indexed",
                                            "violation")],
        # グレー帯(negative 0.60-0.70)は警告として分離 — 判定に使わない
        "warnings": [r for r in report["queries"]
                     if r["verdict"] == "grey"],
    }


def load_golden(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalRuntimeError(f"golden file unreadable: {exc}") from exc
    queries = data.get("queries")
    if not isinstance(queries, list) or not queries:
        raise EvalRuntimeError("golden file has no 'queries' list")
    for q in queries:
        if q.get("type") not in ("positive", "negative"):
            raise EvalRuntimeError(
                f"{q.get('id', '?')}: type must be positive|negative")
        if q["type"] == "positive" and not q.get("expect"):
            raise EvalRuntimeError(f"{q['id']}: positive needs 'expect'")
    return queries


def render_table(report: dict, summary: dict) -> str:
    lines = [f"{'id':<6} {'verdict':<12} {'rank':>4} {'score':>6}  expect/detail"]
    for r in report["queries"]:
        score = f"{r['score']:.2f}" if isinstance(r["score"], (int, float)) else "-"
        rank = str(r["rank"]) if r["rank"] is not None else "-"
        detail = r.get("expect") or ""
        if r.get("band"):
            detail = f"{detail} [band={r['band']}]".strip()
        lines.append(f"{r['id']:<6} {r['verdict']:<12} {rank:>4} {score:>6}  "
                     f"{detail}")
    s = summary
    neg = (f"{s['negative_max_score']:.2f}"
           if s["negative_max_score"] is not None else "-")
    lines.append(
        f"\nrecall@{report['top_k']}: {s['recall_at_k']}  "
        f"| coverage gaps: {len(s['coverage_gaps'])}  "
        f"| negative max: {neg}  "
        f"| warnings: {len(s.get('warnings', []))}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Golden query regression harness for the RAG API")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--check-index", action="store_true",
                        help="bqで索引カバレッジを照合(未索引と検索失敗を分離)")
    parser.add_argument("--published-dir", default=None,
                        help="published/のパス。--check-indexと併用で全記事の"
                             "未索引を検出(既定: 兄弟リポジトリがあれば自動)")
    parser.add_argument("--json", action="store_true",
                        help="機械可読JSONのみ出力")
    parser.add_argument("--golden", default=str(GOLDEN_PATH))
    args = parser.parse_args(argv)

    api_url = os.environ.get("RAG_API_URL", DEFAULT_API_URL)
    token = os.environ.get("RAG_API_TOKEN", "")
    if not token.strip():
        print("error: RAG_API_TOKEN is not set", file=sys.stderr)
        return 2
    if args.top_k <= 0 or args.top_k > 20:
        print("error: --top-k must be 1..20", file=sys.stderr)
        return 2

    try:
        queries = load_golden(Path(args.golden))
    except EvalRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    indexed = None
    unindexed_published: list[str] | None = None
    if args.check_index:
        try:
            indexed = indexed_filenames()
            pub_dir = Path(args.published_dir) if args.published_dir else (
                Path(__file__).resolve().parents[2]
                / "note-articles" / "published")
            unindexed_published = coverage_check(pub_dir, indexed)
        except EvalRuntimeError as exc:
            # fail soft — coverage separation is diagnostic, not a gate
            print(f"warning: index check skipped ({exc})", file=sys.stderr)

    def call_fn(question: str, top_k: int) -> dict:
        return post_query(api_url, token, question, top_k)

    try:
        report = run_eval(queries, call_fn, args.top_k, indexed)
    except EvalRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary = summarize(report)
    out = {"summary": summary, "queries": report["queries"],
           "top_k": report["top_k"]}
    if unindexed_published is not None:
        out["unindexed_published"] = unindexed_published
        summary["unindexed_published"] = unindexed_published
        for f in unindexed_published:
            summary["regressions"].append(
                {"id": "-", "verdict": "unindexed_published", "expect": f,
                 "rank": None, "score": None})
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render_table(report, summary))
        if unindexed_published:
            print(f"\n未索引の公開記事 {len(unindexed_published)}件:")
            for f in unindexed_published:
                print(f"  - {f}")

    return 1 if summary["regressions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
