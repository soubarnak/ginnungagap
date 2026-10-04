#!/bin/bash
# Build the Spaces xbps packages with void-packages' xbps-src, from this checkout.
#
# Usage: void/tools/xbps-build.sh [options] [package ...]
#
#   package ...        what to build (default: every template in void/srcpkgs,
#                      dependencies first)
#   --committed        build HEAD instead of the working tree (default: the working
#                      tree, including uncommitted and untracked files, honouring
#                      .gitignore: nothing is stashed or modified)
#   --revision N       override `revision=` of the spaces template (testing upgrades)
#   --check            run the unit tests during the build (xbps-src -Q)
#   --no-bootstrap     do not clone/bootstrap void-packages when it is missing
#   --lint             only run xlint on the templates and stop
#   -h, --help
#
# Environment: VOID_PACKAGES  void-packages checkout (default
#   ~/.local/share/ginnungagap/void-packages, shallow-cloned on the first run).
#
# How it works: void/srcpkgs/* are copied into the checkout's srcpkgs/. The
# `spaces` template normally downloads the tagged release tarball
# (https://github.com/soubarnak/ginnungagap/archive/refs/tags/v${version}.tar.gz) and carries
# a placeholder checksum. Here the tarball is made from THIS tree (git archive, prefix
# ginnungagap-VERSION/ as GitHub's), its sha256 is rendered into a copy of the template in
# the checkout (the committed template is not touched), and the tarball is put in
# xbps-src's source cache (hostdir/sources/by_sha256/SHA_spaces-VERSION.tar.gz), so
# nothing is downloaded for it. The packages end up in hostdir/binpkgs of the checkout;
# the script prints that path, the repository to give to xbps-install:
#   sudo xbps-install -R <printed path> -S spaces
# Not for root: xbps-src runs as a member of the xbuilder group.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
VP=${VOID_PACKAGES:-$HOME/.local/share/ginnungagap/void-packages}
VP_URL=https://github.com/void-linux/void-packages
COMMITTED=0
REVISION=
CHECK=0
BOOTSTRAP=1
LINT_ONLY=0
PACKAGES=()

while [ $# -gt 0 ]; do
    case "$1" in
        --committed) COMMITTED=1 ;;
        --revision) REVISION=${2:?--revision needs a number}; shift ;;
        --check) CHECK=1 ;;
        --no-bootstrap) BOOTSTRAP=0 ;;
        --lint) LINT_ONLY=1 ;;
        -h|--help) sed -n '2,/^set -e/p' "$0" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) PACKAGES+=("$1") ;;
    esac
    shift
done
if [ "$(id -u)" -eq 0 ]; then
    echo "xbps-build.sh must not run as root (xbps-src uses the xbuilder group)" >&2
    exit 1
fi
log() { printf '==> %s\n' "$*" >&2; }

# Dependencies first; unknown (future) templates are appended.
ORDER=(ubuntu-keyring spaces-rankmirrors spaces-arch-install-scripts spaces-archlinux-keyring spaces)
if [ "${#PACKAGES[@]}" -eq 0 ]; then
    for name in "${ORDER[@]}"; do [ -f "$ROOT/void/srcpkgs/$name/template" ] && PACKAGES+=("$name"); done
    for dir in "$ROOT"/void/srcpkgs/*/; do
        name=$(basename "$dir")
        case " ${PACKAGES[*]} " in *" $name "*) ;; *) PACKAGES+=("$name") ;; esac
    done
fi

# ------------------------------------------------------- void-packages checkout
if [ ! -d "$VP/.git" ]; then
    [ "$BOOTSTRAP" -eq 1 ] || { echo "no void-packages checkout at $VP" >&2; exit 1; }
    log "cloning $VP_URL (shallow) into $VP"
    mkdir -p "$(dirname "$VP")"
    git clone --depth 1 "$VP_URL" "$VP"
fi
if [ ! -d "$VP/masterdir-x86_64" ] && [ ! -d "$VP/masterdir" ]; then
    [ "$BOOTSTRAP" -eq 1 ] || { echo "no masterdir in $VP: run ./xbps-src binary-bootstrap" >&2; exit 1; }
    log "xbps-src binary-bootstrap (first run, downloads about 300 MB)"
    (cd "$VP" && ./xbps-src binary-bootstrap)
fi

# ---------------------------------------------------------------- templates
# Copies of the template directories of the repository (xbps-src reads a symlinked
# srcpkgs/NAME as a subpackage of the package it points to, so symlinks cannot be
# used). The `spaces` template is rendered, see below.
version=$(sed -n 's/^version=//p' "$ROOT/void/srcpkgs/spaces/template")
revision=${REVISION:-$(sed -n 's/^revision=//p' "$ROOT/void/srcpkgs/spaces/template")}
for dir in "$ROOT"/void/srcpkgs/*/; do
    name=$(basename "$dir")
    target=$VP/srcpkgs/$name
    [ "$name" = spaces ] && continue
    rm -rf "$target"
    cp -a "${dir%/}" "$target"
done

render_spaces() {
    local tree sha tarball cache tmp_index
    if [ "$COMMITTED" -eq 1 ]; then
        tree=$(git -C "$ROOT" rev-parse "HEAD^{tree}")
        log "spaces: building HEAD ($(git -C "$ROOT" rev-parse --short HEAD))"
    else
        tmp_index=$(mktemp -u /tmp/spaces-index.XXXXXX)
        GIT_INDEX_FILE=$tmp_index git -C "$ROOT" read-tree HEAD
        GIT_INDEX_FILE=$tmp_index git -C "$ROOT" add -A
        tree=$(GIT_INDEX_FILE=$tmp_index git -C "$ROOT" write-tree)
        rm -f "$tmp_index"
        log "spaces: building the working tree (tree $tree)"
    fi
    cache=$VP/hostdir/sources
    tarball=spaces-$version.tar.gz
    mkdir -p "$cache/by_sha256" "$cache/spaces-$version"
    git -C "$ROOT" archive --format=tar --prefix="ginnungagap-$version/" "$tree" | gzip -n >"$cache/$tarball.new"
    sha=$(sha256sum "$cache/$tarball.new" | cut -d' ' -f1)
    # A distfile from an earlier build has another checksum: drop it, then seed the cache.
    rm -f "$cache/spaces-$version/$tarball"
    mv -f "$cache/$tarball.new" "$cache/by_sha256/${sha}_$tarball"
    log "spaces: $tarball sha256 $sha"

    local out=$VP/srcpkgs/spaces
    rm -rf "$out"
    mkdir "$out"
    cp -a "$ROOT"/void/srcpkgs/spaces/. "$out/"
    sed -e "s/^checksum=.*/checksum=$sha/" \
        -e "s/^revision=.*/revision=$revision/" \
        "$ROOT/void/srcpkgs/spaces/template" >"$out/template"
    SPACES_SHA=$sha
}
render_spaces

cd "$VP"
log "xlint"
lint_failed=0
for name in "${PACKAGES[@]}"; do
    # xlint rejects every users.noreply.github.com maintainer ("needs a valid address for
    # sending mail"): a Void-upstream submission needs a real address, a private repository
    # does not. That one message is accepted, anything else fails the lint.
    out=$(xlint "srcpkgs/$name/template" || true)
    out=$(printf '%s\n' "$out" | grep -v 'maintainer needs a valid address' || true)
    if [ -n "$out" ]; then printf '%s\n' "$out" >&2; lint_failed=1; fi
done
[ "$LINT_ONLY" -eq 1 ] && exit "$lint_failed"
[ "$lint_failed" -eq 0 ] || log "xlint reported problems (see above); building anyway"

opts=()
[ "$CHECK" -eq 1 ] && opts+=(-Q)
for name in "${PACKAGES[@]}"; do
    log "xbps-src pkg $name"
    ./xbps-src "${opts[@]}" pkg "$name"
done
repo=$VP/hostdir/binpkgs
log "packages built:"
for name in "${PACKAGES[@]}"; do
    ls -1 "$repo"/"$name"-[0-9]*.xbps 2>/dev/null | tail -n 1 >&2 || true
done
echo "$repo"
