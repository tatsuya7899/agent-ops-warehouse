#!/bin/sh
# install_publish_hook.sh -- installs the index-on-publish hook call into
# the note-articles git hooks directory (post-commit and post-merge;
# SPEC-index-on-publish_design.md row; task 3).
#
# Usage: install_publish_hook.sh [NOTE_ARTICLES_DIR]
#   NOTE_ARTICLES_DIR defaults to <warehouse>/../note-articles (sibling
#   checkout). Pass explicitly for re-clones at other paths / tests.
#
# Behavior:
#   - verifies the target repo exists BEFORE writing (a moved/renamed
#     warehouse leaves a dead absolute path inside the hook -- that break
#     is detected here and by --check's hooks_installed report)
#   - preserves any existing hook body; our call is wrapped in marker
#     lines so re-runs are idempotent, and a stale body-script path left
#     by a warehouse move is refreshed in place
#   - creates the state dir so the body script's nohup redirect never
#     dies on a missing directory
#
# The generated hook block is:
#   # >>> index-on-publish (agent-ops-warehouse)
#   "absolute/scripts/path/on_published_commit.sh" <hook-name>; true
#   # <<< index-on-publish (agent-ops-warehouse)
# The trailing "; true" pins exit 0 -- hooks must never fail a commit
# (scenario 6). The block is 3 lines; a fresh hook file gets a shebang
# line prepended.
#
# NOTE: no pipe characters in this file -- the Studio write-guard rejects
# them in .sh content.

set -eu

SELF_DIR=$(dirname -- "$0")
SCRIPT_DIR=$(cd -P "$SELF_DIR" && pwd)
WAREHOUSE_DIR=$(dirname "$SCRIPT_DIR")
BODY_SH="$SCRIPT_DIR/on_published_commit.sh"
DEFAULT_REPO=$(dirname "$WAREHOUSE_DIR")/note-articles
NOTE_ARTICLES="${1:-$DEFAULT_REPO}"
HOOKS_DIR="$NOTE_ARTICLES/.git/hooks"
LOG_DIR="${AOW_INDEX_UPDATE_LOG_DIR:-$HOME/.local/state/index-on-publish}"
MARK_BEGIN="# >>> index-on-publish (agent-ops-warehouse)"
MARK_END="# <<< index-on-publish (agent-ops-warehouse)"

fail() {
    echo "install_publish_hook: $*" >&2
    exit 1
}

if [ ! -d "$NOTE_ARTICLES/.git" ]; then
    fail "note-articles repo not found at $NOTE_ARTICLES (pass the repo path as arg 1)"
fi
if [ ! -f "$BODY_SH" ]; then
    fail "missing $BODY_SH -- run from inside agent-ops-warehouse/scripts"
fi
chmod +x "$BODY_SH"
if [ ! -x "$WAREHOUSE_DIR/.venv/bin/python" ]; then
    echo "warning: $WAREHOUSE_DIR/.venv/bin/python is missing; hook launches will no-op" >&2
fi
mkdir -p "$HOOKS_DIR" "$LOG_DIR"

install_one() {
    name="$1"
    hook="$HOOKS_DIR/$name"
    call_line="\"$BODY_SH\" $name; true"
    if [ -f "$hook" ] && grep -qF "$MARK_BEGIN" "$hook"; then
        if grep -qF "$call_line" "$hook"; then
            echo "$name: already installed"
            return 0
        fi
        # Marker present but the recorded path differs -- a warehouse move
        # left a stale absolute path; refresh the call line in place. On
        # awk failure the temp file is left for manual inspection rather
        # than removed (rm plus a variable path trips the write-guard).
        tmp="$hook.tmp.$$"
        if awk -v begin="$MARK_BEGIN" -v end="$MARK_END" -v newline="$call_line" '
            index($0, begin) { print; print newline; skip=1; next }
            index($0, end) { print; skip=0; next }
            !skip { print }
        ' "$hook" > "$tmp"; then
            mv "$tmp" "$hook"
            echo "$name: refreshed stale call line in $hook"
        else
            fail "could not refresh $hook (temp left at $tmp)"
        fi
        return 0
    fi
    if [ ! -f "$hook" ]; then
        printf '#!/bin/sh\n' > "$hook"
    fi
    printf '%s\n%s\n%s\n' "$MARK_BEGIN" "$call_line" "$MARK_END" >> "$hook"
    chmod +x "$hook"
    echo "$name: installed -> $hook"
}

install_one post-commit
install_one post-merge
echo "install_publish_hook: done. Re-run after re-clone or warehouse path moves."
