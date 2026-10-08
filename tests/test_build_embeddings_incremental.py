"""Tests for scripts.build_embeddings --incremental (SPEC-index-on-publish
_requirements.md scenarios 1-4/6/7/9). No real bq/Gemini call anywhere --
scripts.index_diff.fetch_indexed_rows and build_gemini_client/
call_embedding_api are always monkeypatched."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from scripts import build_embeddings


def _write_article(published_dir, name, body):
    published_dir.mkdir(parents=True, exist_ok=True)
    (published_dir / name).write_text(body, encoding="utf-8")


TWO_SECTIONS = "# 記事\n\n## 一番目\n\n本文1。\n\n## 二番目\n\n本文2。\n"


def _args(tmp_path, published_dir, **overrides):
    base = dict(
        published_dir=str(published_dir),
        out=str(tmp_path / "out"),
        project="p",
        dataset="raw",
        schema_path=str(build_embeddings.DEFAULT_SCHEMA_PATH),
        api_key_env="GEMINI_API_KEY",
        env_file=str(tmp_path / "no-such-env"),
        trigger="manual",
        execute=False,
    )
    base.update(overrides)

    class Args:
        pass

    ns = Args()
    for k, v in base.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# scenario 2: zero diff -> zero embedding calls, no API key needed at all
# ---------------------------------------------------------------------------


def test_incremental_zero_diff_calls_no_embedding(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)

    import scripts.index_diff as index_diff_mod

    def fake_fetch():
        rows = build_embeddings.chunk_published_articles(published_dir).rows
        return [
            {"chunk_id": r["chunk_id"], "chunk_text": r["chunk_text"], "filename": r["filename"]}
            for r in rows
        ]

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", fake_fetch)

    def boom_client(api_key):
        raise AssertionError("build_gemini_client must not be called when diff is empty")

    monkeypatch.setattr(build_embeddings, "build_gemini_client", boom_client)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    args = _args(tmp_path, published_dir)
    exit_code = build_embeddings.run_incremental(args)

    assert exit_code == 0
    runs_path = tmp_path / "out" / "index_update_runs.ndjson"
    lines = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2  # start + complete
    assert lines[1]["rows_loaded"] == 0
    assert "phase=complete" in lines[1]["exclusions_note"]


# ---------------------------------------------------------------------------
# scenario 1/4: differential add -- only the missing chunk gets embedded,
# existing chunk_ids are not re-embedded or duplicated
# ---------------------------------------------------------------------------


def test_incremental_embeds_only_the_missing_chunk(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)

    import scripts.index_diff as index_diff_mod

    def fake_fetch():
        # Only the first chunk ("一番目") is already indexed -- the second
        # ("二番目") is the differential add.
        rows = build_embeddings.chunk_published_articles(published_dir).rows
        first = rows[0]
        return [{"chunk_id": first["chunk_id"], "chunk_text": first["chunk_text"], "filename": first["filename"]}]

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", fake_fetch)
    monkeypatch.setattr(build_embeddings, "build_gemini_client", lambda api_key: object())

    calls = []

    def fake_call(client, text, model=build_embeddings.EMBEDDING_MODEL, task_type=None):
        calls.append(text)
        return [0.1]

    monkeypatch.setattr(build_embeddings, "call_embedding_api", fake_call)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    args = _args(tmp_path, published_dir)
    build_embeddings.run_incremental(args)

    assert len(calls) == 1
    assert "二番目" in calls[0]
    assert "一番目" not in calls[0]


# ---------------------------------------------------------------------------
# scenario 3: one chunk's embedding fails all retries -> reported as
# skipped, successful chunks still complete
# ---------------------------------------------------------------------------


def test_incremental_partial_embedding_failure_reports_skipped_ids(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)

    import scripts.index_diff as index_diff_mod

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", lambda: [])
    monkeypatch.setattr(build_embeddings, "build_gemini_client", lambda api_key: object())

    def fake_call(client, text, model=build_embeddings.EMBEDDING_MODEL, task_type=None):
        if "一番目" in text:
            raise build_embeddings.RateLimitError("429")
        return [0.5]

    monkeypatch.setattr(build_embeddings, "call_embedding_api", fake_call)
    monkeypatch.setattr(build_embeddings.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    args = _args(tmp_path, published_dir)
    build_embeddings.run_incremental(args)

    runs_path = tmp_path / "out" / "index_update_runs.ndjson"
    lines = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines()]
    complete = lines[-1]
    assert complete["rows_loaded"] == 1
    assert "skipped_ids=" in complete["exclusions_note"]
    assert "20260701_a__001" in complete["exclusions_note"]


# ---------------------------------------------------------------------------
# scenario 6: hook caller must never block on a missing API key when the
# differential set is non-empty -- SystemExit, not a hang/crash loop
# ---------------------------------------------------------------------------


def test_incremental_raises_systemexit_when_key_missing_and_diff_nonempty(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)

    import scripts.index_diff as index_diff_mod

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", lambda: [])
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    args = _args(tmp_path, published_dir)
    with pytest.raises(SystemExit):
        build_embeddings.run_incremental(args)


# ---------------------------------------------------------------------------
# scenario 7: append load never carries --replace
# ---------------------------------------------------------------------------


def test_build_bq_append_args_has_no_replace_flag():
    args = build_embeddings.build_bq_append_args(
        project="p", dataset="raw", source_uri="/tmp/x.ndjson"
    )
    assert "--replace" not in args
    assert args[0] == "bq"
    assert args[1] == "load"


# ---------------------------------------------------------------------------
# lock / pending (concurrent run handling)
# ---------------------------------------------------------------------------


def test_incremental_lock_held_marks_pending_and_writes_no_audit_rows(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    lock_path = out_dir / ".index_update.lock"
    build_embeddings.acquire_lock(lock_path)  # simulate another run holding it

    import scripts.index_diff as index_diff_mod

    def boom_fetch():
        raise AssertionError("must not compute a diff while locked")

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", boom_fetch)

    args = _args(tmp_path, published_dir)
    exit_code = build_embeddings.run_incremental(args)

    assert exit_code == 0
    assert (out_dir / ".index_update.pending").exists()
    assert not (out_dir / "index_update_runs.ndjson").exists()


def test_acquire_lock_overwrites_stale_lock(tmp_path):
    lock_path = tmp_path / ".lock"
    lock_path.write_text(json.dumps({"pid": 999999999, "acquired_at": 0}), encoding="utf-8")
    assert build_embeddings.acquire_lock(lock_path, now=10_000.0) is True


def test_acquire_lock_blocks_when_pid_alive_and_fresh(tmp_path):
    import os

    lock_path = tmp_path / ".lock"
    now = 1000.0
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "acquired_at": now}), encoding="utf-8"
    )
    assert build_embeddings.acquire_lock(lock_path, now=now + 60) is False


# ---------------------------------------------------------------------------
# scenario 9: interrupted-run detection
# ---------------------------------------------------------------------------


def test_detect_interrupted_run_flags_start_without_complete(tmp_path):
    runs_path = tmp_path / "runs.ndjson"
    build_embeddings.append_ndjson(
        [build_embeddings.build_load_run("index_update", 0, "run_id=abcd1234;phase=start;trigger=manual")],
        runs_path,
    )
    assert build_embeddings.detect_interrupted_run(runs_path) == "abcd1234"


def test_detect_interrupted_run_none_when_every_run_completed(tmp_path):
    runs_path = tmp_path / "runs.ndjson"
    build_embeddings.append_ndjson(
        [
            build_embeddings.build_load_run("index_update", 0, "run_id=abcd1234;phase=start;trigger=manual"),
            build_embeddings.build_load_run(
                "index_update", 1, "run_id=abcd1234;phase=complete;trigger=manual;skipped_ids=none"
            ),
        ],
        runs_path,
    )
    assert build_embeddings.detect_interrupted_run(runs_path) is None


def test_detect_interrupted_run_missing_file_returns_none(tmp_path):
    assert build_embeddings.detect_interrupted_run(tmp_path / "missing.ndjson") is None


# ---------------------------------------------------------------------------
# main() dispatch: --incremental returns an int, not the row list
# ---------------------------------------------------------------------------


def test_main_incremental_flag_dispatches_to_run_incremental(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)
    out_dir = tmp_path / "out"

    import scripts.index_diff as index_diff_mod

    def fake_fetch():
        rows = build_embeddings.chunk_published_articles(published_dir).rows
        return [
            {"chunk_id": r["chunk_id"], "chunk_text": r["chunk_text"], "filename": r["filename"]}
            for r in rows
        ]

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", fake_fetch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    result = build_embeddings.main(
        [
            "--published-dir", str(published_dir),
            "--out", str(out_dir),
            "--project", "p",
            "--incremental",
        ]
    )

    assert result == 0
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Task 3: on_published_commit.sh / install_publish_hook.sh E2E (scenarios
# 1 and 6). Real git commands in tmp repos -- never mocked. The actual
# embedding launch is intercepted via the AOW_INDEX_UPDATE_PYTHON injection
# seam (a stub recording its argv), so no real bq/Gemini call can happen.
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(build_embeddings.__file__).resolve().parent
ON_PUBLISHED_SH = SCRIPTS_DIR / "on_published_commit.sh"
INSTALL_SH = SCRIPTS_DIR / "install_publish_hook.sh"


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    _git(path, "commit", "-q", "--allow-empty", "-m", "base")
    return path


def _commit_file(repo: Path, rel: str, content: str, msg: str, env: dict | None = None) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", rel, env=env)
    _git(repo, "commit", "-q", "-m", msg, env=env)


def _make_stub_python(tmp_path: Path) -> tuple[Path, Path]:
    """A fake .venv/bin/python that records its argv instead of running
    build_embeddings -- the injection boundary that keeps real bq/Gemini
    calls out of the E2E."""
    marker = tmp_path / "launch_argv.txt"
    stub = tmp_path / "fake_python"
    stub.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" >> "' + str(marker) + '"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, marker


def _hook_env(tmp_path: Path, stub: Path) -> dict:
    return {
        **os.environ,
        "AOW_INDEX_UPDATE_PYTHON": str(stub),
        "AOW_INDEX_UPDATE_LOG_DIR": str(tmp_path / "state"),
    }


def _wait_for_marker(marker: Path, timeout: float = 10.0) -> str:
    """The launch is async (nohup &) -- poll briefly for the stub's argv
    record instead of racing it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker.exists():
            return marker.read_text(encoding="utf-8")
        time.sleep(0.05)
    return ""


def _install(tmp_path: Path, repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(INSTALL_SH), str(repo)],
        capture_output=True, text=True, env=_hook_env(tmp_path, _make_stub_python(tmp_path)[0]),
    )


def test_install_hook_creates_post_commit_and_post_merge(tmp_path):
    repo = _init_repo(tmp_path / "note-articles")
    proc = _install(tmp_path, repo)
    assert proc.returncode == 0, proc.stderr
    post_commit = repo / ".git" / "hooks" / "post-commit"
    post_merge = repo / ".git" / "hooks" / "post-merge"
    assert post_commit.exists() and post_merge.exists()
    body = post_commit.read_text(encoding="utf-8")
    assert "on_published_commit.sh" in body
    assert "post-commit" in body
    assert os.access(post_commit, os.X_OK)


def test_install_hook_is_idempotent_and_preserves_existing(tmp_path):
    repo = _init_repo(tmp_path / "note-articles")
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text('#!/bin/sh\necho "pre-existing hook"\n', encoding="utf-8")
    assert _install(tmp_path, repo).returncode == 0
    assert _install(tmp_path, repo).returncode == 0  # second run: no duplication
    body = hook.read_text(encoding="utf-8")
    assert 'echo "pre-existing hook"' in body
    assert body.count("index-on-publish") == 2  # begin+end markers, once


def test_install_hook_fails_when_repo_missing(tmp_path):
    proc = _install(tmp_path, tmp_path / "no-such-repo")
    assert proc.returncode != 0


def test_post_commit_fires_incremental_on_published_add(tmp_path):
    repo = _init_repo(tmp_path / "note-articles")
    stub, marker = _make_stub_python(tmp_path)
    assert _install(tmp_path, repo).returncode == 0
    env = _hook_env(tmp_path, stub)
    _commit_file(repo, "published/20260701_a.md", TWO_SECTIONS, "publish a", env=env)
    out = _wait_for_marker(marker)
    assert "--incremental" in out
    assert "--execute" in out
    assert "--trigger=hook" in out


def test_post_commit_rename_into_published_fires(tmp_path):
    # ready/ -> published/ moves are classified R by --name-status; the
    # -M flag plus last-field parsing is what catches them (design).
    repo = _init_repo(tmp_path / "note-articles")
    stub, marker = _make_stub_python(tmp_path)
    assert _install(tmp_path, repo).returncode == 0
    env = _hook_env(tmp_path, stub)
    _commit_file(repo, "ready/20260702_b.md", TWO_SECTIONS, "draft b", env=env)
    (repo / "published").mkdir(exist_ok=True)
    _git(repo, "mv", "ready/20260702_b.md", "published/20260702_b.md", env=env)
    _git(repo, "commit", "-q", "-m", "publish via rename", env=env)
    assert "--incremental" in _wait_for_marker(marker)


def test_post_commit_does_not_fire_on_unrelated_commit(tmp_path):
    repo = _init_repo(tmp_path / "note-articles")
    stub, marker = _make_stub_python(tmp_path)
    assert _install(tmp_path, repo).returncode == 0
    env = _hook_env(tmp_path, stub)
    _commit_file(repo, "drafts/20260703_c.md", TWO_SECTIONS, "draft only", env=env)
    time.sleep(0.5)
    assert not marker.exists()


def test_post_merge_fires_on_merge_adding_published(tmp_path):
    repo = _init_repo(tmp_path / "note-articles")
    stub, marker = _make_stub_python(tmp_path)
    assert _install(tmp_path, repo).returncode == 0
    env = _hook_env(tmp_path, stub)
    _git(repo, "checkout", "-q", "-b", "side", env=env)
    _commit_file(repo, "published/20260704_d.md", TWO_SECTIONS, "publish d", env=env)
    _git(repo, "checkout", "-q", "-", env=env)
    _git(repo, "merge", "--no-ff", "-m", "merge side", "side", env=env)
    assert "--incremental" in _wait_for_marker(marker)


def test_post_merge_fires_on_ff_pull(tmp_path):
    # ff merges must also fire -- post-merge uses ORIG_HEAD as the base so
    # intermediate commits of a fast-forward are not missed (design).
    repo = _init_repo(tmp_path / "note-articles")
    stub, marker = _make_stub_python(tmp_path)
    assert _install(tmp_path, repo).returncode == 0
    env = _hook_env(tmp_path, stub)
    _git(repo, "checkout", "-q", "-b", "side", env=env)
    _commit_file(repo, "published/20260705_e.md", TWO_SECTIONS, "publish e", env=env)
    _git(repo, "checkout", "-q", "-", env=env)
    _git(repo, "merge", "-q", "side", env=env)  # fast-forward
    assert "--incremental" in _wait_for_marker(marker)


def test_hook_failure_does_not_block_commit(tmp_path):
    # scenario 6: a failing post-commit hook can never fail the commit
    # (structural git guarantee) -- and install's `; true` is a second belt.
    repo = _init_repo(tmp_path / "note-articles")
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _commit_file(repo, "published/20260706_f.md", TWO_SECTIONS, "publish f")
    proc = _git(repo, "log", "--oneline", "-1")
    assert "publish f" in proc.stdout


def test_post_merge_manual_fallback_without_orig_head(tmp_path):
    # Manual invocation (no ORIG_HEAD) falls back to HEAD~1 (design:
    # "git rev-parse --verify ORIG_HEAD"判定).
    repo = _init_repo(tmp_path / "note-articles")
    stub, marker = _make_stub_python(tmp_path)
    env = _hook_env(tmp_path, stub)
    _commit_file(repo, "published/20260707_g.md", TWO_SECTIONS, "publish g", env=env)
    proc = subprocess.run(
        ["sh", str(ON_PUBLISHED_SH), "post-merge"],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert "--incremental" in _wait_for_marker(marker)


# ---------------------------------------------------------------------------
# --execute path: append loads (never --replace), audit rows ship to
# raw.load_runs, post-load verification flags ids still absent
# ---------------------------------------------------------------------------


def _fake_bq(calls, fail=False):
    """subprocess.run stand-in: records argv, returns rc 0/1. Every bq
    invocation in a test goes through here -- nothing real is executed."""

    def fake_run(argv, **kwargs):
        calls.append(list(argv))

        class Proc:
            returncode = 1 if fail else 0
            stdout = ""
            stderr = "boom" if fail else ""

        return Proc()

    return fake_run


def test_incremental_execute_loads_chunks_then_ships_audit(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)

    import scripts.index_diff as index_diff_mod

    rows = build_embeddings.chunk_published_articles(published_dir).rows
    state = {"calls": 0}

    def fake_fetch():
        state["calls"] += 1
        if state["calls"] == 1:
            return []  # diff pass: nothing indexed yet
        # post-load verify pass: everything just loaded is present
        return [
            {
                "chunk_id": r["chunk_id"],
                "chunk_text": r["chunk_text"],
                "filename": r["filename"],
            }
            for r in rows
        ]

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", fake_fetch)
    monkeypatch.setattr(build_embeddings, "build_gemini_client", lambda api_key: object())
    monkeypatch.setattr(build_embeddings, "call_embedding_api", lambda *a, **k: [0.1])
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    bq_calls = []
    monkeypatch.setattr(build_embeddings.subprocess, "run", _fake_bq(bq_calls))

    args = _args(tmp_path, published_dir, execute=True)
    assert build_embeddings.run_incremental(args) == 0

    # Two appends: delta chunks -> article_chunks, then this run's audit
    # pair -> load_runs. Neither may carry --replace.
    assert len(bq_calls) == 2
    for argv in bq_calls:
        assert "--replace" not in argv
    assert "p:raw.article_chunks" in bq_calls[0]
    assert "p:raw.load_runs" in bq_calls[1]

    # The per-run audit file holds exactly this run's start+complete pair
    # (already-shipped history rows are not re-appended).
    audit_files = list((tmp_path / "out").glob("load_runs_*.ndjson"))
    assert len(audit_files) == 1
    audit = [
        json.loads(line)
        for line in audit_files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert len(audit) == 2
    assert "phase=start" in audit[0]["exclusions_note"]
    assert "phase=complete" in audit[1]["exclusions_note"]
    assert "load=ok" in audit[1]["exclusions_note"]
    assert "verify=ok" in audit[1]["exclusions_note"]
    assert "verify_missing=none" in audit[1]["exclusions_note"]

    # Chunk rows went to the separately-named delta file, not the audit file.
    delta_files = list((tmp_path / "out").glob("article_chunks_delta_*.ndjson"))
    assert len(delta_files) == 1


def test_incremental_execute_zero_diff_ships_audit_only(tmp_path, monkeypatch):
    """A clean-diff --execute run still ships its start+complete pair to
    raw.load_runs -- the pair is the evidence the hook fired at all."""
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)

    import scripts.index_diff as index_diff_mod

    def fake_fetch():
        rows = build_embeddings.chunk_published_articles(published_dir).rows
        return [
            {
                "chunk_id": r["chunk_id"],
                "chunk_text": r["chunk_text"],
                "filename": r["filename"],
            }
            for r in rows
        ]

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", fake_fetch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    bq_calls = []
    monkeypatch.setattr(build_embeddings.subprocess, "run", _fake_bq(bq_calls))

    args = _args(tmp_path, published_dir, execute=True)
    assert build_embeddings.run_incremental(args) == 0

    assert len(bq_calls) == 1
    assert "p:raw.load_runs" in bq_calls[0]
    assert "--replace" not in bq_calls[0]


def test_incremental_execute_records_chunk_load_failure(tmp_path, monkeypatch):
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)

    import scripts.index_diff as index_diff_mod

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", lambda: [])
    monkeypatch.setattr(build_embeddings, "build_gemini_client", lambda api_key: object())
    monkeypatch.setattr(build_embeddings, "call_embedding_api", lambda *a, **k: [0.1])
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    bq_calls = []
    monkeypatch.setattr(build_embeddings.subprocess, "run", _fake_bq(bq_calls, fail=True))

    args = _args(tmp_path, published_dir, execute=True)
    build_embeddings.run_incremental(args)

    runs_path = tmp_path / "out" / "index_update_runs.ndjson"
    complete = [
        json.loads(line)
        for line in runs_path.read_text(encoding="utf-8").splitlines()
    ][-1]
    assert "load=failed" in complete["exclusions_note"]


def test_incremental_execute_flags_chunks_absent_after_load(tmp_path, monkeypatch):
    """Post-load verify: ids still absent from the index after a
    successful `bq load` are named in the complete audit row."""
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)

    import scripts.index_diff as index_diff_mod

    # Index stays empty even after the (fake) load -- the grounding
    # check must catch that bq reported success but nothing landed.
    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", lambda: [])
    monkeypatch.setattr(build_embeddings, "build_gemini_client", lambda api_key: object())
    monkeypatch.setattr(build_embeddings, "call_embedding_api", lambda *a, **k: [0.1])
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    bq_calls = []
    monkeypatch.setattr(build_embeddings.subprocess, "run", _fake_bq(bq_calls))

    args = _args(tmp_path, published_dir, execute=True)
    build_embeddings.run_incremental(args)

    runs_path = tmp_path / "out" / "index_update_runs.ndjson"
    complete = [
        json.loads(line)
        for line in runs_path.read_text(encoding="utf-8").splitlines()
    ][-1]
    assert "verify_missing=20260701_a__001" in complete["exclusions_note"]
    assert "20260701_a__002" in complete["exclusions_note"]


# ---------------------------------------------------------------------------
# adversarial-review regressions: resolve_api_key export form (P0),
# corrupt audit line (P1), atomic lock (P1), pending cap (P2),
# empty-delta load skip (P2)
# ---------------------------------------------------------------------------


def test_resolve_api_key_reads_export_form(tmp_path, monkeypatch):
    """Real ~/.config/gemini/env uses `export KEY=...`; a bare-name
    parser misses it and every hook-launched run dies with SystemExit."""
    env_file = tmp_path / "env"
    env_file.write_text("export GEMINI_API_KEY='sk-from-file'\n", encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert build_embeddings.resolve_api_key("GEMINI_API_KEY", env_file) == "sk-from-file"


def test_resolve_api_key_env_var_wins_over_file(tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_text("export GEMINI_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    assert build_embeddings.resolve_api_key("GEMINI_API_KEY", env_file) == "env-key"


def test_resolve_api_key_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert build_embeddings.resolve_api_key("GEMINI_API_KEY", tmp_path / "nope") is None


def test_detect_interrupted_run_survives_truncated_line(tmp_path):
    """The detector must not die on the very corruption it exists to
    notice: an interrupted append leaves a half-written trailing line."""
    runs = tmp_path / "index_update_runs.ndjson"
    runs.write_text(
        json.dumps({"exclusions_note": "run_id=aa;phase=start"}) + "\n"
        + '{"exclusions_note": "run_id=aa;phase=comple',  # truncated tail
        encoding="utf-8",
    )
    assert build_embeddings.detect_interrupted_run(runs) == "aa"


def test_acquire_lock_blocks_second_caller_while_holder_alive(tmp_path):
    lock = tmp_path / "out" / ".index_update.lock"
    assert build_embeddings.acquire_lock(lock)
    # our own pid is alive -- a second caller must lose, not overwrite
    assert not build_embeddings.acquire_lock(lock)


def test_acquire_lock_reclaims_old_corrupt_lock_file(tmp_path):
    lock = tmp_path / "out" / ".index_update.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("not-json{{{", encoding="utf-8")
    # age it past the stale window -- a corrupt file this old is garbage,
    # not a live acquire in progress
    old = time.time() - build_embeddings.LOCK_STALE_SECONDS - 60
    os.utime(lock, (old, old))
    assert build_embeddings.acquire_lock(lock)


def test_acquire_lock_treats_fresh_corrupt_file_as_in_progress(tmp_path):
    """A lock file caught between O_EXCL create and write is unparseable
    but has a fresh mtime -- stealing it would undo the atomicity."""
    lock = tmp_path / "out" / ".index_update.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("", encoding="utf-8")  # mid-create placeholder
    assert not build_embeddings.acquire_lock(lock)


def test_pending_marker_survives_pass_cap(tmp_path, monkeypatch):
    """A marker arriving during the final allowed pass must remain for
    the next run -- consuming it before the cap check would drop it."""
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)

    import scripts.index_diff as index_diff_mod

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", lambda: [])
    monkeypatch.setattr(build_embeddings, "build_gemini_client", lambda api_key: object())
    calls = []
    monkeypatch.setattr(
        build_embeddings,
        "call_embedding_api",
        lambda *a, **k: calls.append(1) or [0.1],
    )
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    out_dir = tmp_path / "out"
    pending_path = out_dir / ".index_update.pending"
    real_consume = build_embeddings.consume_pending
    consume_calls = []

    def consume_then_rearm(path):
        # simulate a commit landing *during* every pass: the marker that
        # was pending gets consumed, and a fresh one arrives right after
        consume_calls.append(1)
        existed = real_consume(path)
        Path(path).write_text("1", encoding="utf-8")
        return existed or True

    monkeypatch.setattr(build_embeddings, "consume_pending", consume_then_rearm)
    args = _args(tmp_path, published_dir)
    assert build_embeddings.run_incremental(args) == 0

    # 3 passes ran (2 chunks each). consume_pending is called only on
    # passes 1-2 -- under the old order (consume-then-cap) it would be
    # called a 3rd time and the pass-3 marker would be dropped.
    assert len(calls) == 2 * build_embeddings.MAX_PENDING_REPASSES
    assert len(consume_calls) == build_embeddings.MAX_PENDING_REPASSES - 1
    assert pending_path.exists()


def test_incremental_execute_skips_load_when_all_embeds_failed(tmp_path, monkeypatch):
    """All chunks skipped after retries -> empty delta -> no chunk load;
    only the audit pair is shipped and load=skipped_empty is recorded."""
    published_dir = tmp_path / "published"
    _write_article(published_dir, "20260701_a.md", TWO_SECTIONS)

    import scripts.index_diff as index_diff_mod

    monkeypatch.setattr(index_diff_mod, "fetch_indexed_rows", lambda: [])
    monkeypatch.setattr(build_embeddings, "build_gemini_client", lambda api_key: object())

    def always_rate_limited(client, text, model=build_embeddings.EMBEDDING_MODEL, task_type=None):
        raise build_embeddings.RateLimitError("429")

    monkeypatch.setattr(build_embeddings, "call_embedding_api", always_rate_limited)
    monkeypatch.setattr(build_embeddings.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    bq_calls = []
    monkeypatch.setattr(build_embeddings.subprocess, "run", _fake_bq(bq_calls))

    args = _args(tmp_path, published_dir, execute=True)
    build_embeddings.run_incremental(args)

    # No article_chunks load -- only the audit pair to raw.load_runs.
    assert len(bq_calls) == 1
    assert "p:raw.load_runs" in bq_calls[0]

    runs_path = tmp_path / "out" / "index_update_runs.ndjson"
    complete = [
        json.loads(line)
        for line in runs_path.read_text(encoding="utf-8").splitlines()
    ][-1]
    assert "load=skipped_empty" in complete["exclusions_note"]
    assert complete["rows_loaded"] == 0
