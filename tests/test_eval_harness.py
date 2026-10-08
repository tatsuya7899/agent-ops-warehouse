"""tests/test_eval_harness.py — golden query harness judgement logic.

Repo convention (SPEC Section 6-2 / every existing api test): no real
HTTP or bq call in the suite. post_query/indexed_filenames are the
injection boundaries — tests drive run_eval with fake call_fns.
"""
import json

import pytest

from eval.run_eval import (
    EvalRuntimeError,
    coverage_check,
    judge_negative,
    judge_positive,
    load_golden,
    render_table,
    run_eval,
    summarize,
    main,
)


def _row(filename, score):
    return {
        "article_title": filename.replace(".md", ""),
        "url": filename,
        "excerpt": "…",
        "similarity_score": score,
    }


POS = {"id": "q01", "type": "positive", "question": "q",
       "expect": "a.md"}
NEG = {"id": "q99", "type": "negative", "question": "curry"}


def test_positive_hit_records_rank_and_score():
    results = [_row("other.md", 0.80), _row("a.md", 0.75)]
    v = judge_positive(POS, results, top_k=5, indexed=None)
    assert v["verdict"] == "hit"
    assert v["rank"] == 2
    assert v["score"] == pytest.approx(0.75)


def test_positive_miss_reports_top_returned_score():
    results = [_row("other.md", 0.66), _row("other2.md", 0.55)]
    v = judge_positive(POS, results, top_k=5, indexed=None)
    assert v["verdict"] == "miss"
    assert v["score"] == pytest.approx(0.66)
    assert v["band"] == "grey"


def test_positive_miss_band_low_when_nothing_close():
    results = [_row("other.md", 0.50)]
    v = judge_positive(POS, results, top_k=5, indexed=None)
    assert v["verdict"] == "miss" and v["band"] == "low"


def test_positive_miss_band_high_when_wrong_article_scores_like_hit():
    results = [_row("other.md", 0.78)]
    v = judge_positive(POS, results, top_k=5, indexed=None)
    assert v["verdict"] == "miss" and v["band"] == "high"


def test_positive_beyond_top_k_is_a_miss():
    # expect sits at rank 6 — outside the top_k=5 window the API returns
    results = [_row(f"o{i}.md", 0.8) for i in range(5)] + [_row("a.md", 0.9)]
    v = judge_positive(POS, results, top_k=5, indexed=None)
    assert v["verdict"] == "miss"


def test_not_indexed_is_separate_from_miss():
    results = [_row("other.md", 0.80)]
    v = judge_positive(POS, results, top_k=5, indexed={"b.md", "c.md"})
    assert v["verdict"] == "not_indexed"


def test_indexed_set_containing_expect_still_judges_normally():
    results = [_row("a.md", 0.77)]
    v = judge_positive(POS, results, top_k=5, indexed={"a.md"})
    assert v["verdict"] == "hit"


def test_negative_pass_when_all_scores_under_ceiling():
    results = [_row("x.md", 0.55), _row("y.md", 0.40)]
    assert judge_negative(NEG, results, top_k=5)["verdict"] == "pass"


def test_negative_grey_zone_is_warning_not_violation():
    # 0.60-0.70はグレー帯 — 誤ヒット疑いとして個別報告するが退行判定には使わない
    results = [_row("x.md", 0.62)]
    assert judge_negative(NEG, results, top_k=5)["verdict"] == "grey"


def test_negative_violation_only_at_or_above_hit_floor():
    results = [_row("x.md", 0.72)]
    assert judge_negative(NEG, results, top_k=5)["verdict"] == "violation"


def test_grey_negative_is_a_warning_not_a_regression():
    report = {"queries": [
        {"id": "q9", "verdict": "grey", "expect": None, "rank": None,
         "score": 0.62},
    ], "top_k": 5}
    s = summarize(report)
    assert s["regressions"] == []
    assert [r["id"] for r in s["warnings"]] == ["q9"]
    assert s["negative_max_score"] == pytest.approx(0.62)


def test_run_eval_separates_classes_and_summary_counts():
    queries = [
        POS,
        {"id": "q02", "type": "positive", "question": "q",
         "expect": "missing.md"},
        NEG,
    ]
    responses = {
        "q": {"results": [_row("a.md", 0.8)]},
        "curry": {"results": [_row("a.md", 0.5)]},
    }
    report = run_eval(queries, lambda q, k: responses[q], top_k=5,
                      indexed={"a.md"})  # missing.md NOT indexed
    verdicts = [r["verdict"] for r in report["queries"]]
    assert verdicts == ["hit", "not_indexed", "pass"]
    s = summarize(report)
    assert s["recall_at_k"] == "1/1"
    assert s["coverage_gaps"] == ["missing.md"]
    # not_indexed is a regression signal but not a retrieval miss
    assert [r["id"] for r in s["regressions"]] == ["q02"]


def test_regression_set_includes_violation():
    report = {"queries": [
        {"id": "q1", "verdict": "hit", "expect": "a.md", "rank": 1,
         "score": 0.8},
        {"id": "q9", "verdict": "violation", "expect": None, "rank": None,
         "score": 0.72},
    ], "top_k": 5}
    s = summarize(report)
    assert [r["id"] for r in s["regressions"]] == ["q9"]


def test_load_golden_rejects_missing_expect(tmp_path):
    bad = tmp_path / "golden.json"
    bad.write_text(json.dumps({"queries": [
        {"id": "x", "type": "positive", "question": "q"}]}),
        encoding="utf-8")
    with pytest.raises(EvalRuntimeError):
        load_golden(bad)


def test_load_golden_rejects_unknown_type(tmp_path):
    bad = tmp_path / "golden.json"
    bad.write_text(json.dumps({"queries": [
        {"id": "x", "type": "maybe", "question": "q"}]}),
        encoding="utf-8")
    with pytest.raises(EvalRuntimeError):
        load_golden(bad)


def test_coverage_check_lists_published_minus_indexed(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("x", encoding="utf-8")
    (tmp_path / "c.md").write_text("x", encoding="utf-8")
    missing = coverage_check(tmp_path, indexed={"a.md"})
    assert missing == ["b.md", "c.md"]


def test_coverage_check_missing_dir_returns_empty(tmp_path):
    assert coverage_check(tmp_path / "nope", indexed={"a.md"}) == []


def test_render_table_contains_recall_line():
    report = {"queries": [
        {"id": "q1", "verdict": "hit", "expect": "a.md", "rank": 1,
         "score": 0.8},
    ], "top_k": 5}
    out = render_table(report, summarize(report))
    assert "recall@5: 1/1" in out


def test_main_missing_token_exits_2(monkeypatch):
    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    assert main([]) == 2


def test_main_clean_run_exits_0(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_API_TOKEN", "dummy")
    golden = tmp_path / "g.json"
    golden.write_text(json.dumps({"queries": [
        {"id": "q1", "type": "positive", "question": "q", "expect": "a.md"},
        {"id": "q2", "type": "negative", "question": "curry"},
    ]}), encoding="utf-8")
    import eval.run_eval as mod
    monkeypatch.setattr(
        mod, "post_query",
        lambda _u, _t, q, _k, **_kw: {
            "results": [_row("a.md", 0.8)] if q != "curry"
            else [_row("a.md", 0.5)]})
    assert main(["--golden", str(golden)]) == 0


def test_main_regression_exits_1(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_API_TOKEN", "dummy")
    golden = tmp_path / "g.json"
    golden.write_text(json.dumps({"queries": [
        {"id": "q1", "type": "positive", "question": "q", "expect": "a.md"},
    ]}), encoding="utf-8")
    import eval.run_eval as mod
    monkeypatch.setattr(mod, "post_query",
                        lambda *a, **k: {"results": [_row("b.md", 0.7)]})
    assert main(["--golden", str(golden)]) == 1


def test_main_api_failure_exits_2(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_API_TOKEN", "dummy")
    golden = tmp_path / "g.json"
    golden.write_text(json.dumps({"queries": [
        {"id": "q1", "type": "positive", "question": "q", "expect": "a.md"},
    ]}), encoding="utf-8")
    import eval.run_eval as mod

    def boom(*a, **k):
        raise EvalRuntimeError("HTTP 503")
    monkeypatch.setattr(mod, "post_query", boom)
    assert main(["--golden", str(golden)]) == 2
