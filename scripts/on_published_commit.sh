#!/bin/sh
# on_published_commit.sh -- body invoked from note-articles'
# .git/hooks/{post-commit,post-merge} (SPEC-index-on-publish_design.md
# row; requirements scenarios 1/6).
#
# Contract: never block or fail the git operation that invoked it. Every
# failure path exits 0; the actual index update is launched detached via
# nohup ... & with all stdio redirected, so a slow/hung embedding run can
# never hold the commit open.
#
# Sync part: detect published/ adds via git --name-status output. The
# argv is fixed by the design doc. Rename detection -M is mandatory --
# ready/ -> published/ moves classify as R, which an A-only diff-filter
# would let pass. post-merge diffs ORIG_HEAD..HEAD so ff-pulls do not
# drop intermediate commits; manual invocation without ORIG_HEAD falls
# back to HEAD~1. Any non-empty output means a published/ add happened
# (the -- published/ pathspec already scopes R lines to their dst).
#
# Async part: launch the repo-pinned .venv/bin/python on
# scripts/build_embeddings.py --incremental --execute (bare python3 would
# ImportError -- google-genai lives in the api extra only).
#
# Injection seams for tests (unused in production):
#   AOW_INDEX_UPDATE_PYTHON   -- interpreter to launch
#   AOW_INDEX_UPDATE_LOG_DIR  -- state dir
#
# NOTE: this file intentionally contains no pipe characters -- the Studio
# write-guard rejects them in .sh content, so all conditionals use
# if-forms and pipelines are single-command (awk alone).

set -u

KIND="${1:-post-commit}"
SELF_DIR=$(dirname -- "$0")
SCRIPT_DIR=$(cd -P "$SELF_DIR" && pwd)
WAREHOUSE_DIR=$(dirname "$SCRIPT_DIR")
LOG_DIR="${AOW_INDEX_UPDATE_LOG_DIR:-$HOME/.local/state/index-on-publish}"
LOG_FILE="$LOG_DIR/runs.log"
PYTHON_BIN="${AOW_INDEX_UPDATE_PYTHON:-$WAREHOUSE_DIR/.venv/bin/python}"

# The log dir must exist before nohup redirects into it -- a missing dir
# would kill the redirect itself (design section 非機能/ログ).
mkdir -p "$LOG_DIR" 2>/dev/null

# Hooks run at the repo root; for manual invocation from a subdir, anchor
# at the work-tree top so the published/ pathspec resolves correctly.
TOP=$(git rev-parse --show-toplevel 2>/dev/null)
TOP=${TOP:-.}
if ! cd "$TOP"; then
    exit 0
fi

# Surface the previous run's final status line so a dead/failed earlier
# launch is visible on the terminal at commit time (design 非機能).
if [ -f "$LOG_FILE" ]; then
    prev=$(awk 'END { print substr($0, 1, 200) }' "$LOG_FILE" 2>/dev/null)
    if [ -n "$prev" ]; then
        echo "prev run: $prev" >&2
    fi
fi

CHANGED=""
case "$KIND" in
    post-merge)
        BASE="ORIG_HEAD"
        if ! git rev-parse --verify --quiet ORIG_HEAD >/dev/null 2>&1; then
            BASE="HEAD~1"
        fi
        CHANGED=$(git diff --name-status --diff-filter=AMR -M "$BASE" HEAD -- published/ 2>/dev/null)
        ;;
    *)
        CHANGED=$(git diff-tree -r -m -M --root --no-commit-id --name-status --diff-filter=AMR HEAD -- published/ 2>/dev/null)
        ;;
esac

if [ -z "$CHANGED" ]; then
    echo "index update: no published adds ($KIND)" >&2
    exit 0
fi

if [ ! -x "$PYTHON_BIN" ]; then
    echo "index update: $PYTHON_BIN missing, skipping" >&2
    exit 0
fi

# "launched" only prints when cd actually succeeded -- the & belongs to
# nohup alone, so a cd failure short-circuits to else (POSIX: `a && b &`
# would background the whole AND-list and always exit 0 here).
if cd "$WAREHOUSE_DIR"; then
    nohup "$PYTHON_BIN" scripts/build_embeddings.py \
        --incremental --execute --trigger=hook \
        --out out --project agent-ops-warehouse --dataset raw \
        >>"$LOG_FILE" 2>&1 </dev/null &
    echo "index update launched (trigger=$KIND)" >&2
else
    echo "index update skipped: could not enter $WAREHOUSE_DIR" >&2
fi
exit 0
