#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Release helper for the ginn Void package (T0.6, T10.6, AS-003, NFR-SEC-003).
#
# This script runs only in the CI build container (.github/workflows/ci.yaml, package job) or
# by the maintainer. The test suite never runs it, and ginn never runs it at runtime: it calls
# xbps-rindex, and FR-004 is a runtime rule for ginn.
#
#   check                      template version equal to the pyproject version, no tag yet, clean tree
#   checksum URL_OR_FILE       write the sha256 of the release tarball into the template
#   repo BINPKGS_DIR OUT_DIR   build the ginn repository in OUT_DIR, one index per architecture found
#                              in BINPKGS_DIR, with SHA256SUMS and the rendered traceability matrix
#                              (traceability.md). Unsigned unless GINN_SIGNING_KEY names a private
#                              key; with a key the packages and every index are signed
#   cut                        run check, create the annotated tag v<version>, print next steps.
#                              It never pushes
#
# The signing key is read from GINN_SIGNING_KEY. Keys are never committed; *.pem and
# *.key are listed in .gitignore.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
template="$here/packaging/void/srcpkgs/ginn/template"
placeholder='@CHECKSUM@'
signer='Bongbetic <soubarna@live.in>'
# Temporary download directory. Global, so the EXIT trap set by cmd_checksum can read it.
tmp=""

die() {
    echo "release.sh: $*" >&2
    exit 1
}

# Value of a top-level NAME=value line in the template, with quotes removed.
template_field() {
    awk -F= -v key="$1" '$1 == key { gsub(/"/, "", $2); print $2; exit }' "$template"
}

# Target architecture encoded in a package file name: ginn-VERSION_REV.ARCH.xbps.
pkg_arch() {
    local base=${1##*/}
    base=${base%.xbps}
    printf '%s\n' "${base##*.}"
}

project_version() {
    python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' \
        "$here/pyproject.toml"
}

cmd_check() {
    local tv pv
    tv=$(template_field version)
    pv=$(project_version)
    # A release uses the exact project version, so a .dev0 marker in pyproject.toml fails here.
    if [ "$tv" != "$pv" ]; then
        die "template version $tv differs from pyproject version $pv (cut releases without .dev0)"
    fi
    if git -C "$here" rev-parse --quiet --verify "refs/tags/v$tv" >/dev/null; then
        die "tag v$tv already exists"
    fi
    if [ -n "$(git -C "$here" status --porcelain)" ]; then
        die "working tree has uncommitted changes"
    fi
    echo "check passed: ginn $tv"
}

cmd_checksum() {
    local src=${1:?usage: release.sh checksum URL_OR_FILE}
    local file sum
    if [[ "$src" == https://* ]]; then
        tmp=$(mktemp -d)
        trap 'rm -rf -- "$tmp"' EXIT
        file="$tmp/source.tar.gz"
        curl --fail --location --silent --show-error --output "$file" "$src"
    else
        file=$src
    fi
    [ -f "$file" ] || die "not a file: $file"
    sum=$(sha256sum "$file" | cut -d' ' -f1)
    sed -i "s/^checksum=.*/checksum=$sum/" "$template"
    echo "checksum=$sum written to packaging/void/srcpkgs/ginn/template"
}

cmd_repo() {
    local bins=${1:?usage: release.sh repo BINPKGS_DIR OUT_DIR}
    local out=${2:?usage: release.sh repo BINPKGS_DIR OUT_DIR}
    local key=${GINN_SIGNING_KEY:-}
    local mode=unsigned pkg count=0
    if [ -n "$key" ]; then
        [ -f "$key" ] || die "signing key not found: $key"
        mode=signed
    fi
    # The index must not mix with files from an earlier run, so OUT_DIR has to be empty.
    if [ -d "$out" ] && [ -n "$(ls -A -- "$out")" ]; then
        die "output directory $out is not empty; give a new or empty directory"
    fi
    mkdir -p "$out"
    out=$(cd "$out" && pwd)
    for pkg in "$bins"/ginn*.xbps; do
        [ -f "$pkg" ] || continue
        cp -- "$pkg" "$out/"
        count=$((count + 1))
    done
    [ "$count" -gt 0 ] || die "no ginn*.xbps packages in $bins"
    # A release ships only when every Must requirement is covered or deferred (NFR-061).
    if ! python3 -I "$here/tools/traceability.py" --check; then
        die "traceability check failed; fix the markers or docs/traceability-deferred.toml first"
    fi
    # Render the traceability matrix and ship it with the packages (NFR-061, Appendix C).
    # It is generated and not committed, so it is rendered here before it is copied.
    python3 -I "$here/tools/traceability.py" --write
    cp -- "$here/docs/traceability.md" "$out/traceability.md"
    if [ "$mode" = signed ]; then
        # Sign each package before indexing: the index records the file hash.
        for pkg in "$out"/ginn*.xbps; do
            xbps-rindex --sign-pkg --signedby "$signer" --privkey "$key" "$pkg"
        done
    fi
    # xbps keeps one repodata index per architecture, selected by XBPS_TARGET_ARCH.
    # Index each package under its own architecture, then sign every index found.
    local archs=() arch
    for pkg in "$out"/ginn*.xbps; do
        arch=$(pkg_arch "$pkg")
        XBPS_TARGET_ARCH=$arch xbps-rindex -a "$pkg"
        case " ${archs[*]} " in
            *" $arch "*) ;;
            *) archs+=("$arch") ;;
        esac
    done
    if [ "$mode" = signed ]; then
        for arch in "${archs[@]}"; do
            XBPS_TARGET_ARCH=$arch xbps-rindex --sign --signedby "$signer" --privkey "$key" "$out"
        done
    else
        # xbps refuses an unsigned remote repository, so users read this one from a local
        # directory (docs/packaging.md, Install from the release).
        echo "release.sh: GINN_SIGNING_KEY is not set; the repository is UNSIGNED" >&2
    fi
    # SHA256SUMS lets a download be checked without a signature.
    (cd "$out" && sha256sum -- ginn*.xbps ./*-repodata > SHA256SUMS)
    echo "$mode repository: $out ($count package(s), architectures: ${archs[*]})"
}

cmd_cut() {
    cmd_check
    local tv
    tv=$(template_field version)
    git -C "$here" tag -a "v$tv" -m "ginn $tv"
    cat <<EOF
Tag v$tv created locally. Next steps:
  1. git push origin v$tv
  2. release.sh checksum https://github.com/soubarnak/ginnungagap/archive/refs/tags/v$tv.tar.gz
     then commit the template change on the void branch.
  3. tools/xbps-build.sh <void-packages-dir>   (the musl target is not built for 1.0.0)
  4. tools/release.sh repo <void-packages-dir>/hostdir/binpkgs <out-dir>
     (unsigned; set GINN_SIGNING_KEY=<key.pem> to sign)
  5. Create GitHub release v$tv and upload every file in <out-dir>.
EOF
}

main() {
    local cmd=${1:-}
    if [ $# -gt 0 ]; then
        shift
    fi
    case "$cmd" in
        check) cmd_check ;;
        checksum) cmd_checksum "$@" ;;
        repo) cmd_repo "$@" ;;
        cut) cmd_cut ;;
        *) die "usage: release.sh {check|checksum URL_OR_FILE|repo BINPKGS_DIR OUT_DIR|cut}" ;;
    esac
}

main "$@"
