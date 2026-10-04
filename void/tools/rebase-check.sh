#!/bin/bash
# Check how well the void branch follows upstream (anatase-org/spaces) without touching it.
#
# Usage: void/tools/rebase-check.sh [options]
#
#   --upstream REF   what to bring in (default: upstream/master, fetched first)
#   --branch REF     the port to test (default: void)
#   --rebase         dry-run `git rebase REF` instead of `git merge REF`
#   --no-fetch       do not fetch the upstream remote
#   --no-tests       skip the test suite after a clean merge or rebase
#   --keep           keep the temporary worktree (its path is printed)
#   -h, --help
#
# What it does: fetches the upstream remote (read only), lists the upstream commits that the
# port does not have, names the files they touch, intersects those with the files the port
# changed (the "hot" ones: launch.py, session.py, priv.py and friends), dry-runs the merge or
# rebase in a throwaway detached worktree and lists the conflicts, flags upstream changes that
# need a look at the package (native/, data/, the wheel's data-files, the templates), and runs
# the test suite on a clean result. Nothing is committed to a branch: master and void are not
# moved and nothing is pushed. Exit status: 0 clean, 1 conflicts or failing tests, 2 usage.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
UPSTREAM=upstream/master
BRANCH=void
MODE=merge
FETCH=1
TESTS=1
KEEP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --upstream) UPSTREAM=${2:?--upstream needs a ref}; shift ;;
        --branch) BRANCH=${2:?--branch needs a ref}; shift ;;
        --rebase) MODE=rebase ;;
        --no-fetch) FETCH=0 ;;
        --no-tests) TESTS=0 ;;
        --keep) KEEP=1 ;;
        -h|--help) sed -n '2,/^set -e/p' "$0" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

cd "$ROOT"
log() { printf '==> %s\n' "$*" >&2; }
git rev-parse --verify --quiet "$BRANCH^{commit}" >/dev/null || { echo "no such ref: $BRANCH" >&2; exit 2; }

if [ "$FETCH" -eq 1 ] && [ "$UPSTREAM" = upstream/master ]; then
    log "fetching upstream"
    git fetch --quiet upstream
fi
git rev-parse --verify --quiet "$UPSTREAM^{commit}" >/dev/null || { echo "no such ref: $UPSTREAM" >&2; exit 2; }

base=$(git merge-base "$BRANCH" "$UPSTREAM")
new=$(git rev-list --count "$base..$UPSTREAM" --)
ours=$(git rev-list --count "$base..$BRANCH" --)
echo "merge base:        $(git log -1 --format='%h %s' "$base")"
echo "upstream commits not in $BRANCH: $new"
echo "$BRANCH commits not in upstream: $ours"
if [ "$new" -eq 0 ]; then
    echo "nothing to bring in: $BRANCH already contains $UPSTREAM"
    exit 0
fi
echo
git log --no-decorate --format='  %h %ad %an: %s' --date=short "$base..$UPSTREAM" -- | head -50
[ "$new" -gt 50 ] && echo "  ... and $((new - 50)) more"

up_files=$(git diff --name-only "$base" "$UPSTREAM" -- | sort)
our_files=$(git diff --name-only "$base" "$BRANCH" -- | sort)
both=$(comm -12 <(printf '%s\n' "$up_files") <(printf '%s\n' "$our_files") || true)

echo
echo "files upstream changed: $(printf '%s\n' "$up_files" | grep -c . || true)"
echo "of those, files the port also changed (conflict candidates):"
if [ -n "$both" ]; then printf '%s\n' "$both" | sed 's/^/  /'; else echo "  none"; fi

echo
echo "upstream changes that need a look at the package or the port:"
watch=$(printf '%s\n' "$up_files" | grep -E '^(native/|data/|pyproject\.toml$|src/spaces/(launch|session|priv|system_bus|core)\.py$|src/spaces/overlay/|tests/test_(native_abi|packaging)\.py$)' || true)
if [ -n "$watch" ]; then
    printf '%s\n' "$watch" | sed 's/^/  /'
    echo "  (post_install of void/srcpkgs/spaces/template diffs data/portal and data/system-bridge"
    echo "   against the wheel, native/ is built there, pyproject's data-files must stay in step)"
else
    echo "  none"
fi

tmp=$(mktemp -d "${TMPDIR:-/tmp}/rebase-check.XXXXXX")
cleanup() {
    cd "$ROOT"
    if [ "$KEEP" -eq 1 ]; then
        echo "worktree kept: $tmp/wt"
    else
        git worktree remove --force "$tmp/wt" 2>/dev/null || true
        rm -rf "$tmp"
    fi
    git worktree prune
}
trap cleanup EXIT

log "dry-run $MODE of $UPSTREAM onto $BRANCH in a temporary worktree"
git worktree add --quiet --detach "$tmp/wt" "$BRANCH"
cd "$tmp/wt"
git config user.name rebase-check
git config user.email rebase-check@localhost
status=0
if [ "$MODE" = merge ]; then
    git merge --no-commit --no-ff "$UPSTREAM" >"$tmp/out" 2>&1 || status=$?
else
    git rebase "$UPSTREAM" >"$tmp/out" 2>&1 || status=$?
fi
conflicts=$(git diff --name-only --diff-filter=U | sort || true)
echo
if [ -n "$conflicts" ]; then
    echo "CONFLICTS ($(printf '%s\n' "$conflicts" | wc -l)):"
    hot='^src/spaces/(launch|session|priv)\.py$'
    while IFS= read -r file; do
        if printf '%s\n' "$file" | grep -Eq "$hot"; then
            echo "  $file   <- hot file"
        else
            echo "  $file"
        fi
    done <<<"$conflicts"
    echo
    echo "git said:"
    sed 's/^/  /' "$tmp/out" | head -30
    echo
    echo "result: $MODE does not apply cleanly; tests not run"
    exit 1
fi
if [ "$status" -ne 0 ]; then
    echo "$MODE failed without conflicts:"
    sed 's/^/  /' "$tmp/out" | head -30
    exit 1
fi
echo "result: $MODE applies cleanly"

if [ "$TESTS" -eq 0 ]; then
    echo "tests skipped (--no-tests)"
    exit 0
fi
log "running the test suite in the worktree (PYTHONPATH=src)"
if PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider >"$tmp/tests" 2>&1; then
    tail -n 1 "$tmp/tests"
    echo "result: tests pass on the merged tree"
    exit 0
fi
tail -n 25 "$tmp/tests"
echo "result: tests FAIL on the merged tree"
exit 1
