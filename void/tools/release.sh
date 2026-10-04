#!/bin/bash
# Release helper for the Void port: checks, the local tag, and the source checksums.
#
# Usage: void/tools/release.sh COMMAND [options]
#
#   check              preflight: on the void branch, clean tree, version in pyproject.toml and
#                      in the template agree, tag not taken yet, templates lint, tests pass
#   tag                create the local annotated tag v<version> (never pushes it)
#   checksum [--write] download the GitHub tarball of the PUSHED tag, print its sha256 and
#                      compare it with the `spaces` template; --write puts it into the template
#   verify             download every distfile of every template and compare the checksums
#   dist               write dist/ginnungagap-<version>.tar.gz from the tag with `git archive`
#                      (reproducible; an alternative distfile, see void/docs/release.md)
#   repo --key PATH --signedby "NAME <MAIL>" [--from DIR] [--out DIR] [--replace]
#                      assemble a signed xbps repository from the built packages: copy them
#                      into DIR (default dist/repo), index them (xbps-rindex -a), sign the
#                      repository (--sign) and every package (--sign-pkg) with the RSA key
#                      PATH. A local step only: nothing is uploaded or published. Packages
#                      come from --from (default: hostdir/binpkgs of the void-packages
#                      checkout of xbps-build.sh). The passphrase of an encrypted key is
#                      asked by xbps-rindex, or taken from XBPS_PASSPHRASE.
#   -h, --help
#
# The order matters, and is spelled out in void/docs/release.md: the checksum of GitHub's
# tarball only exists once the tag is on GitHub, and the tag's own tree cannot contain a
# checksum of itself, so the template in the tag carries the placeholder and the real value
# is committed after the tag was pushed.
#
# Environment: REPO (default soubarnak/ginnungagap), TAG_PREFIX (default v),
# DOWNLOAD_DIR (default ~/.cache/ginnungagap-release), VOID_PACKAGES (as for xbps-build.sh).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
REPO=${REPO:-soubarnak/ginnungagap}
PREFIX=${TAG_PREFIX:-v}
DOWNLOAD_DIR=${DOWNLOAD_DIR:-$HOME/.cache/ginnungagap-release}
TEMPLATE=void/srcpkgs/spaces/template
PLACEHOLDER=0000000000000000000000000000000000000000000000000000000000000000

cd "$ROOT"
log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'release.sh: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -e/p' "$0" | sed '$d;s/^# \{0,1\}//'; }

template_version() { sed -n 's/^version=//p' "$TEMPLATE"; }
pyproject_version() { sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1; }
template_checksum() { sed -n 's/^checksum=//p' "$TEMPLATE"; }
tarball_url() { echo "https://github.com/$REPO/archive/refs/tags/$PREFIX$1.tar.gz"; }

fetch() { # URL DEST
    mkdir -p "$(dirname "$2")"
    curl --fail --silent --show-error --location --retry 3 --output "$2.part" "$1" || { rm -f "$2.part"; return 1; }
    mv -f "$2.part" "$2"
}

cmd_check() {
    local version failed=0
    version=$(template_version)
    [ -n "$version" ] || die "no version= in $TEMPLATE"
    [ "$(pyproject_version)" = "$version" ] || { log "pyproject.toml says $(pyproject_version), the template says $version"; failed=1; }
    [ "$(git rev-parse --abbrev-ref HEAD)" = void ] || { log "not on the void branch"; failed=1; }
    [ -z "$(git status --porcelain)" ] || { log "the working tree is not clean"; failed=1; }
    if git rev-parse --verify --quiet "refs/tags/$PREFIX$version" >/dev/null; then
        log "tag $PREFIX$version already exists"; failed=1
    fi
    grep -q "^distfiles=\"https://github.com/$REPO/archive/refs/tags/$PREFIX\${version}.tar.gz>spaces-\${version}.tar.gz\"" "$TEMPLATE" ||
        { log "the distfiles line of $TEMPLATE does not name the GitHub tag tarball of $REPO"; failed=1; }
    log "xlint"
    "$ROOT/void/tools/xbps-build.sh" --lint || { log "xlint reported problems"; failed=1; }
    log "unit tests"
    PYTHONPATH=src python3 -m pytest -q -p no:cacheprovider || { log "tests fail"; failed=1; }
    if [ "$failed" -eq 0 ]; then
        echo "ready to tag $PREFIX$version (checksum in the template: $(template_checksum | cut -c1-12)...)"
    else
        die "not ready"
    fi
}

cmd_tag() {
    local version tag
    version=$(template_version)
    tag=$PREFIX$version
    [ "$(git rev-parse --abbrev-ref HEAD)" = void ] || die "not on the void branch"
    [ -z "$(git status --porcelain)" ] || die "the working tree is not clean"
    git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null && die "tag $tag exists"
    git tag -a "$tag" -m "Spaces for Void $version" -m "Source release of the Void port. Not pushed by release.sh: git push origin $tag"
    echo "created local tag $tag at $(git rev-parse --short "$tag^{commit}"); it has NOT been pushed"
    echo "next: git push origin void $tag   (then: void/tools/release.sh checksum --write)"
}

cmd_checksum() {
    local write=0 version url file sum current
    while [ $# -gt 0 ]; do
        case "$1" in
            --write) write=1 ;;
            *) die "unknown option: $1" ;;
        esac
        shift
    done
    version=$(template_version)
    url=$(tarball_url "$version")
    file=$DOWNLOAD_DIR/spaces-$version.tar.gz
    log "downloading $url"
    fetch "$url" "$file" || die "cannot download the tarball: is $PREFIX$version pushed to $REPO?"
    sum=$(sha256sum "$file" | cut -d' ' -f1)
    current=$(template_checksum)
    echo "sha256 of $PREFIX$version from GitHub: $sum"
    if [ "$sum" = "$current" ]; then
        echo "the template already has this checksum"
        return 0
    fi
    if [ "$current" != "$PLACEHOLDER" ]; then
        echo "the template has a different checksum: $current" >&2
        [ "$write" -eq 1 ] || return 1
        echo "overwriting it (--write); GitHub changed the tarball, or the tag was moved" >&2
    fi
    [ "$write" -eq 1 ] || { echo "run again with --write to put it into $TEMPLATE"; return 1; }
    sed -i "s/^checksum=.*/checksum=$sum/" "$TEMPLATE"
    echo "wrote the checksum into $TEMPLATE; commit it, then verify with:"
    echo "  void/tools/xbps-build.sh --release spaces"
}

cmd_verify() {
    local template name status=0 version
    for template in void/srcpkgs/*/template; do
        name=$(basename "$(dirname "$template")")
        # Source the template in a subshell: it only sets variables and defines functions.
        # shellcheck disable=SC1090
        mapfile -t entries < <(
            set +eu
            # the one site variable of xbps-src's environment that the templates use
            UBUNTU_SITE=http://archive.ubuntu.com/ubuntu/pool
            . "$template" >/dev/null 2>&1
            # distfiles and checksums are parallel, whitespace separated lists
            read -r -a urls <<<"$(echo $distfiles)"
            read -r -a sums <<<"$(echo $checksum)"
            for i in "${!urls[@]}"; do echo "${urls[$i]} ${sums[$i]:-missing}"; done
        )
        for entry in "${entries[@]}"; do
            url=${entry% *}
            sum=${entry##* }
            if [ "$name" = spaces ] && [ "$sum" = "$PLACEHOLDER" ]; then
                echo "SKIP  $name: placeholder checksum (the tag is not pushed yet)"
                continue
            fi
            file=$DOWNLOAD_DIR/verify/$name/$(basename "${url%%>*}")
            if ! fetch "${url%%>*}" "$file" 2>/dev/null; then
                echo "FAIL  $name: cannot download ${url%%>*}"; status=1; continue
            fi
            got=$(sha256sum "$file" | cut -d' ' -f1)
            if [ "$got" = "$sum" ]; then
                echo "PASS  $name: $(basename "${url%%>*}") $sum"
            else
                echo "FAIL  $name: $(basename "${url%%>*}") is $got, the template says $sum"; status=1
            fi
        done
    done
    return "$status"
}

cmd_dist() {
    local version tag out
    version=$(template_version)
    tag=$PREFIX$version
    git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null || die "tag $tag does not exist (run: release.sh tag)"
    mkdir -p dist
    out=dist/ginnungagap-$version.tar.gz
    git archive --format=tar --prefix="ginnungagap-$version/" "$tag" | gzip -n >"$out"
    echo "$out  sha256 $(sha256sum "$out" | cut -d' ' -f1)"
}

cmd_repo() {
    local key= signedby= from= out= replace=0 name file found=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --key) key=${2:?--key needs a path}; shift ;;
            --signedby) signedby=${2:?--signedby needs "Name <mail>"}; shift ;;
            --from) from=${2:?--from needs a directory}; shift ;;
            --out) out=${2:?--out needs a directory}; shift ;;
            --replace) replace=1 ;;
            *) die "unknown option: $1" ;;
        esac
        shift
    done
    [ -n "$key" ] || die "repo needs --key PATH, an RSA private key (how to make one: void/docs/release.md)"
    [ -n "$signedby" ] || die "repo needs --signedby \"Name <mail>\" (shown to users when they trust the key)"
    [ -f "$key" ] && [ -r "$key" ] || die "cannot read the key: $key"
    if [ -n "$(find "$key" -perm /077 -print 2>/dev/null)" ]; then
        log "warning: $key is readable by group or others; chmod 600 it"
    fi
    command -v xbps-rindex >/dev/null || die "xbps-rindex is not installed (package xbps)"
    key=$(realpath "$key")
    from=${from:-${VOID_PACKAGES:-$HOME/.local/share/ginnungagap/void-packages}/hostdir/binpkgs}
    [ -d "$from" ] || die "no package directory: $from (build first: void/tools/xbps-build.sh)"
    out=$(realpath -m "${out:-$ROOT/dist/repo}")
    if [ -e "$out" ] && [ -n "$(ls -A "$out" 2>/dev/null)" ]; then
        [ "$replace" -eq 1 ] || die "$out is not empty (--replace drops the packages and index in it first)"
        find "$out" -maxdepth 1 \( -name '*.xbps' -o -name '*.xbps.sig*' -o -name '*-repodata' -o -name '*-stagedata' \) -delete
    fi
    mkdir -p "$out"
    # Our own packages only (the version after the name starts with a digit, so `spaces-` does not
    # match `spaces-arch-install-scripts-...`), for every architecture that was built.
    # Only the newest version of each package per architecture (the build directory keeps the
    # packages of earlier test builds).
    for template in void/srcpkgs/*/template; do
        name=$(basename "$(dirname "$template")")
        for arch in $(ls "$from/$name"-[0-9]*.xbps 2>/dev/null | sed 's/.*\.\([^.]*\)\.xbps$/\1/' | sort -u); do
            file=$(ls "$from/$name"-[0-9]*."$arch".xbps | sort -V | tail -n 1)
            cp -f "$file" "$out/"
            found=$((found + 1))
        done
    done
    [ "$found" -gt 0 ] || die "no package of void/srcpkgs found in $from"
    log "indexing $found packages in $out"
    xbps-rindex -a "$out"/*.xbps
    log "signing the repository"
    xbps-rindex --privkey "$key" --signedby "$signedby" -s "$out"
    log "signing the packages"
    xbps-rindex --privkey "$key" -S "$out"/*.xbps
    echo "signed repository: $out ($found packages)"
    echo "nothing was published; to try it:  sudo xbps-install -S -R $out spaces"
    echo "(xbps asks to trust the key on first use; compare the fingerprint with the one you published)"
}

case "${1:-}" in
    check) shift; cmd_check "$@" ;;
    tag) shift; cmd_tag "$@" ;;
    checksum) shift; cmd_checksum "$@" ;;
    verify) shift; cmd_verify "$@" ;;
    dist) shift; cmd_dist "$@" ;;
    repo) shift; cmd_repo "$@" ;;
    -h|--help|help) usage ;;
    *) usage >&2; exit 2 ;;
esac
