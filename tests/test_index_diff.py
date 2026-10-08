"""Tests for scripts.index_diff (SPEC-index-on-publish_requirements.md
scenarios 5/8/9 + design's 6-category classification and repair-target
gate). No bq/Gemini call anywhere in this file -- fetch_fn is always
injected (FR-4/FR-8: differences must be computable without API keys)."""
from __future__ import annotations

import json

from scripts.index_diff import (
    IndexDiff,
    IndexDiffError,
    build_expected_by_file,
    check_hooks_installed,
    classify,
    compute_index_diff,
    main,
    render_report,
)


def _write_article(published_dir, name, body):
    published_dir.mkdir(parents=True, exist_ok=True)
    (published_dir / name).write_text(body, encoding="utf-8")


ONE_SECTION = "# 記事\n\n## セクション\n\n本文。\n"


# ---------------------------------------------------------------------------
# build_expected_by_file
# ---------------------------------------------------------------------------


def test_build_expected_by_file_includes_empty_dict_for_h2_less_article(tmp_path):
    _write_article(tmp_path, "20260701_a.md", "# 見出しのみ、H2なし\n")
    expected, invalid = build_expected_by_file(tmp_path)
    assert expected["20260701_a.md"] == {}
    assert invalid == []


def test_build_expected_by_file_reports_invalid_filenames(tmp_path):
    _write_article(tmp_path, "no-date-prefix.md", ONE_SECTION)
    expected, invalid = build_expected_by_file(tmp_path)
    assert "no-date-prefix.md" not in expected
    assert invalid == ["no-date-prefix.md"]


# ---------------------------------------------------------------------------
# classify: 6 categories (design "触るファイル" row for index_diff.py)
# ---------------------------------------------------------------------------


def test_classify_missing_filename_entirely_absent_from_index():
    expected = {"a.md": {"a__001": {"chunk_text": "x"}}}
    diff = classify(expected, [], [])
    assert diff.missing == ["a.md"]
    assert diff.missing_empty == []
    assert diff.partial == []
    assert diff.changed == []


def test_classify_missing_empty_for_zero_chunk_article():
    expected = {"a.md": {}}
    diff = classify(expected, [], [])
    assert diff.missing == []
    assert diff.missing_empty == ["a.md"]


def test_classify_partial_index_text_matches_on_common_ids():
    expected = {
        "a.md": {
            "a__001": {"chunk_text": "one"},
            "a__002": {"chunk_text": "two"},
        }
    }
    indexed_rows = [{"chunk_id": "a__001", "chunk_text": "one", "filename": "a.md"}]
    diff = classify(expected, [], indexed_rows)
    assert diff.partial == [{"filename": "a.md", "missing_chunk_ids": ["a__002"]}]
    assert diff.changed == []


def test_classify_changed_when_common_chunk_text_mismatches():
    expected = {"a.md": {"a__001": {"chunk_text": "new text"}}}
    indexed_rows = [{"chunk_id": "a__001", "chunk_text": "old text", "filename": "a.md"}]
    diff = classify(expected, [], indexed_rows)
    assert diff.changed == ["a.md"]
    assert diff.partial == []


def test_classify_changed_when_index_has_id_outside_expected():
    expected = {"a.md": {"a__001": {"chunk_text": "one"}}}
    indexed_rows = [
        {"chunk_id": "a__001", "chunk_text": "one", "filename": "a.md"},
        {"chunk_id": "a__999", "chunk_text": "stale", "filename": "a.md"},
    ]
    diff = classify(expected, [], indexed_rows)
    assert diff.changed == ["a.md"]
    assert diff.partial == []


def test_classify_deleted_for_filename_only_in_index():
    indexed_rows = [{"chunk_id": "b__001", "chunk_text": "x", "filename": "b.md"}]
    diff = classify({}, [], indexed_rows)
    assert diff.deleted == ["b.md"]


def test_classify_deleted_excludes_invalid_filenames():
    indexed_rows = [{"chunk_id": "x", "chunk_text": "x", "filename": "no-date.md"}]
    diff = classify({}, ["no-date.md"], indexed_rows)
    assert diff.deleted == []
    assert diff.invalid_filenames == ["no-date.md"]


def test_classify_duplicates_detected_by_repeated_chunk_id():
    indexed_rows = [
        {"chunk_id": "a__001", "chunk_text": "x", "filename": "a.md"},
        {"chunk_id": "a__001", "chunk_text": "x", "filename": "a.md"},
    ]
    diff = classify({"a.md": {"a__001": {"chunk_text": "x"}}}, [], indexed_rows)
    assert diff.duplicates == ["a__001"]


def test_classify_healthy_file_produces_no_category():
    expected = {"a.md": {"a__001": {"chunk_text": "x"}}}
    indexed_rows = [{"chunk_id": "a__001", "chunk_text": "x", "filename": "a.md"}]
    diff = classify(expected, [], indexed_rows)
    assert diff.is_clean()


# ---------------------------------------------------------------------------
# repair_chunk_ids: FR-6 gate (missing + partial only)
# ---------------------------------------------------------------------------


def test_repair_chunk_ids_includes_missing_and_partial_only():
    expected_by_file = {
        "a.md": {"a__001": {"chunk_text": "x"}, "a__002": {"chunk_text": "y"}},
        "b.md": {"b__001": {"chunk_text": "z"}},
        "c.md": {"c__001": {"chunk_text": "w"}},
    }
    diff = IndexDiff(
        missing=["b.md"],
        partial=[{"filename": "a.md", "missing_chunk_ids": ["a__002"]}],
        changed=["c.md"],
    )
    ids = diff.repair_chunk_ids(expected_by_file)
    assert sorted(ids) == ["a__002", "b__001"]
    assert "c__001" not in ids  # changed is report-only, never auto-repaired


# ---------------------------------------------------------------------------
# compute_index_diff: injectable fetch_fn, no bq/API call (scenario 8)
# ---------------------------------------------------------------------------


def test_compute_index_diff_uses_injected_fetch_fn_without_network(tmp_path):
    _write_article(tmp_path, "20260701_a.md", ONE_SECTION)
    calls = []

    def fake_fetch():
        calls.append(1)
        return []

    diff, expected_by_file = compute_index_diff(tmp_path, fetch_fn=fake_fetch)
    assert calls == [1]
    assert diff.missing == ["20260701_a.md"]
    assert "20260701_a.md" in expected_by_file


# ---------------------------------------------------------------------------
# check_hooks_installed (design: "フック存在検査")
# ---------------------------------------------------------------------------


def test_check_hooks_installed_reports_false_when_absent(tmp_path):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    result = check_hooks_installed(tmp_path)
    assert result == {"post-commit": False, "post-merge": False}


def test_check_hooks_installed_requires_the_marker_block(tmp_path):
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    # A hook file that exists for an unrelated purpose is NOT "installed"
    # -- the marker line is what proves the index-on-publish call is wired.
    (hooks_dir / "post-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    result = check_hooks_installed(tmp_path)
    assert result["post-commit"] is False
    assert result["post-merge"] is False


def test_check_hooks_installed_reports_true_when_marker_present(tmp_path):
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "post-commit").write_text(
        "#!/bin/sh\n# >>> index-on-publish (agent-ops-warehouse)\n",
        encoding="utf-8",
    )
    result = check_hooks_installed(tmp_path)
    assert result["post-commit"] is True
    assert result["post-merge"] is False


# ---------------------------------------------------------------------------
# render_report / main CLI
# ---------------------------------------------------------------------------


def test_render_report_lists_each_category_count():
    diff = IndexDiff(missing=["a.md"], duplicates=["x__001"])
    out = render_report(diff)
    assert "missing(追加漏れ): 1" in out
    assert "  - a.md" in out
    assert "duplicates(重複): 1" in out


def test_render_report_warns_about_missing_hooks():
    diff = IndexDiff()
    out = render_report(diff, hooks={"post-commit": False, "post-merge": True})
    assert "warning: hooks not installed: post-commit" in out


def test_main_check_exits_0_when_clean(tmp_path, monkeypatch):
    import scripts.index_diff as mod

    _write_article(tmp_path, "20260701_a.md", ONE_SECTION)
    note_dir = tmp_path.parent
    (note_dir / "note-articles").mkdir(exist_ok=True)

    def fake_fetch():
        from scripts.build_embeddings import chunk_published_articles

        rows = chunk_published_articles(tmp_path).rows
        return [{"chunk_id": r["chunk_id"], "chunk_text": r["chunk_text"], "filename": r["filename"]} for r in rows]

    monkeypatch.setattr(mod, "fetch_indexed_rows", fake_fetch)
    exit_code = main(["--published-dir", str(tmp_path), "--note-articles-dir", str(tmp_path)])
    assert exit_code == 0


def test_main_check_exits_1_when_dirty(tmp_path, monkeypatch):
    import scripts.index_diff as mod

    _write_article(tmp_path, "20260701_a.md", ONE_SECTION)
    monkeypatch.setattr(mod, "fetch_indexed_rows", lambda: [])
    exit_code = main(["--published-dir", str(tmp_path), "--note-articles-dir", str(tmp_path)])
    assert exit_code == 1


def test_main_json_output_contains_all_categories(tmp_path, monkeypatch, capsys):
    import scripts.index_diff as mod

    _write_article(tmp_path, "20260701_a.md", ONE_SECTION)
    monkeypatch.setattr(mod, "fetch_indexed_rows", lambda: [])
    main(["--published-dir", str(tmp_path), "--note-articles-dir", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    for key in (
        "missing",
        "missing_empty",
        "partial",
        "changed",
        "deleted",
        "invalid_filenames",
        "duplicates",
        "hooks_installed",
    ):
        assert key in out


def test_main_returns_2_on_index_diff_error(tmp_path, monkeypatch):
    import scripts.index_diff as mod

    def boom():
        raise IndexDiffError("bq not authenticated")

    monkeypatch.setattr(mod, "fetch_indexed_rows", boom)
    exit_code = main(["--published-dir", str(tmp_path)])
    assert exit_code == 2


# ---------------------------------------------------------------------------
# Task 4: eval/run_eval.py --check-index delegates to scripts.index_diff
# (design "触るファイル" row: shared diff checker as backstop / 1実装2導線;
# RAG_API_TOKENゲートはcheck-indexブロックより前で据え置き)
# ---------------------------------------------------------------------------


def _golden(tmp_path, queries):
    path = tmp_path / "golden.json"
    path.write_text(json.dumps({"queries": queries}), encoding="utf-8")
    return path


def test_eval_check_index_delegates_to_index_diff(tmp_path, monkeypatch, capsys):
    """--check-index must call scripts.index_diff.compute_index_diff (the
    shared checker), not the legacy DISTINCT-filename bq path. The diff
    result drives both the per-query `indexed` set and the
    `unindexed_published` list (output compat with the old shape)."""
    import eval.run_eval as eval_mod
    import scripts.index_diff as diff_mod

    monkeypatch.setenv("RAG_API_TOKEN", "dummy")
    published = tmp_path / "published"
    _write_article(published, "20260701_a.md", ONE_SECTION)
    _write_article(published, "20260702_b.md", ONE_SECTION)
    golden = _golden(tmp_path, [
        {"id": "q1", "type": "positive", "question": "q",
         "expect": "20260701_a.md"},
        {"id": "q2", "type": "positive", "question": "q",
         "expect": "20260702_b.md"},
    ])

    calls = []

    def fake_compute(published_dir, **kw):
        calls.append(published_dir)
        return (
            IndexDiff(
                missing=["20260702_b.md"],
                indexed_filenames={"20260701_a.md"},
            ),
            {"20260701_a.md": {"a__001": {}}, "20260702_b.md": {"b__001": {}}},
        )

    monkeypatch.setattr(diff_mod, "compute_index_diff", fake_compute)
    monkeypatch.setattr(
        eval_mod, "post_query",
        lambda *a, **k: {"results": [
            {"article_title": "a", "url": "20260701_a.md", "excerpt": "…",
             "similarity_score": 0.8}]},
    )
    rc = eval_mod.main([
        "--golden", str(golden), "--check-index",
        "--published-dir", str(published), "--json",
    ])
    assert calls == [str(published)]
    out = json.loads(capsys.readouterr().out)
    verdicts = {r["id"]: r["verdict"] for r in out["queries"]}
    assert verdicts == {"q1": "hit", "q2": "not_indexed"}
    assert out["summary"]["unindexed_published"] == ["20260702_b.md"]
    assert rc == 1


def test_eval_check_index_soft_fails_on_index_diff_error(
        tmp_path, monkeypatch, capsys):
    """IndexDiffError during --check-index is a warning, not an eval
    failure -- same fail-soft contract as the old bq path."""
    import eval.run_eval as eval_mod
    import scripts.index_diff as diff_mod

    monkeypatch.setenv("RAG_API_TOKEN", "dummy")
    published = tmp_path / "published"
    _write_article(published, "20260701_a.md", ONE_SECTION)
    golden = _golden(tmp_path, [
        {"id": "q1", "type": "positive", "question": "q",
         "expect": "20260701_a.md"},
    ])

    def boom(published_dir, **kw):
        raise IndexDiffError("bq not authenticated")

    monkeypatch.setattr(diff_mod, "compute_index_diff", boom)
    monkeypatch.setattr(
        eval_mod, "post_query",
        lambda *a, **k: {"results": [
            {"article_title": "a", "url": "20260701_a.md", "excerpt": "…",
             "similarity_score": 0.8}]},
    )
    rc = eval_mod.main([
        "--golden", str(golden), "--check-index",
        "--published-dir", str(published),
    ])
    err = capsys.readouterr().err
    assert "warning" in err
    assert rc == 0


def test_eval_token_gate_still_precedes_check_index(tmp_path, monkeypatch):
    """RAG_API_TOKEN gate stays BEFORE the check-index block (design:
    the key-free path is index_diff's own CLI, not run_eval)."""
    import eval.run_eval as eval_mod
    import scripts.index_diff as diff_mod

    monkeypatch.delenv("RAG_API_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(
        diff_mod, "compute_index_diff",
        lambda *a, **k: calls.append(1) or (IndexDiff(), {}))
    assert eval_mod.main(["--check-index"]) == 2
    assert calls == []


# ---------------------------------------------------------------------------
# Task 5: loader --check-index warning stage (design "触るファイル" row:
# 週次ローダー末尾にindex_diff --checkの警告実行を1段追加。tasks.md验收:
# loader実行時に差分警告が出る・失敗してもloader自体は完走)
# ---------------------------------------------------------------------------


def test_loader_check_index_invokes_warning_stage(tmp_path, monkeypatch, capsys):
    """--check-index on `python -m loader` runs the shared diff check as a
    final warning stage and reports its result. The loader's own return
    value (load_runs) is unaffected."""
    import loader.__main__ as loader_main

    calls = []
    monkeypatch.setattr(
        loader_main, "index_diff_check",
        lambda args: calls.append(args.articles))
    articles = tmp_path / "published"
    _write_article(articles, "20260701_a.md", ONE_SECTION)
    result = loader_main.run([
        "--articles", str(articles),
        "--check-index", "--out", str(tmp_path / "out"),
    ])
    assert calls == [str(articles)]
    assert isinstance(result, list)
    assert (tmp_path / "out" / "raw_load_runs.ndjson").exists()


def test_loader_index_check_failure_still_completes(tmp_path, monkeypatch, capsys):
    """A failing/raising index check must surface as a warning and never
    abort the loader run (the check is diagnostic, not a gate)."""
    import loader.__main__ as loader_main

    def boom(args):
        raise RuntimeError("index check blew up")

    monkeypatch.setattr(loader_main, "index_diff_check", boom)
    result = loader_main.run(["--check-index", "--out", str(tmp_path / "out")])
    assert isinstance(result, list)
    assert "warning" in capsys.readouterr().err.lower()


def test_loader_without_check_index_does_not_call_stage(tmp_path, monkeypatch):
    """No flag -> no index check: the default loader run keeps its
    no-BigQuery dependency profile (loader docstring P0 scope)."""
    import loader.__main__ as loader_main

    calls = []
    monkeypatch.setattr(
        loader_main, "index_diff_check",
        lambda args: calls.append(1))
    loader_main.run(["--out", str(tmp_path / "out")])
    assert calls == []
