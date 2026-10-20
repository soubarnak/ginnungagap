#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Release helper for the ginn Void package (T0.6, T10.6, AS-003, NFR-SEC-003).
#
# This script runs only in the release workflow (.github/workflows/release.yml), in the CI
# package job, or by the maintainer. The test suite never runs the xbps parts, and ginn never
# runs it at runtime: it calls xbps-rindex, and FR-004 is a runtime rule for ginn.
#
# Releases are tag-driven. The tag is ginn-v<version> and must equal the version in
# pyproject.toml and in the template. Never build or upload release assets by hand.
#
#   check                      template version equal to the pyproject version, no tag yet, clean tree
#   verify-tag TAG             TAG equal to ginn-v<version> of both pyproject.toml and the template
#                              (the release workflow runs this first)
#   checksum URL_OR_FILE       write the sha256 of the release tarball into the template
#   bump TAG                   after a release: set the template version from TAG, write the sha256
#                              of the GitHub tag archive into the template, and set the package
#                              names in README.md
#   repo BINPKGS_DIR OUT_DIR   build the ginn repository in OUT_DIR: one <arch>-repodata per
#                              architecture (a noarch package is indexed under every architecture
#                              Void supports, an arch package only under its own), SHA256SUMS and
#                              the rendered traceability matrix (traceability.md). With
#                              GINN_SIGNING_KEY naming a private key, the indexes are signed and
#                              every package gets a .sig2; without it the repository is unsigned
#                              and fit only for a local check (xbps refuses it as a remote repository)
#   cut                        run check, create the annotated tag ginn-v<version>, print next
#                              steps. It never pushes
#
# The signing key is read from GINN_SIGNING_KEY. Keys are never committed; *.pem and
# *.key are listed in .gitignore.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
template="$here/packaging/void/srcpkgs/ginn/template"
repo_url='https://github.com/soubarnak/ginnungagap'
signer='Bongbetic <soubarna@live.in>'
# Architectures a noarch package is indexed under (docs/packaging.md).
noarch_archs=(x86_64 x86_64-musl i686 aarch64 aarch64-musl armv7l armv7l-musl armv6l armv6l-musl)
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
    if git -C "$here" rev-parse --quiet --verify "refs/tags/ginn-v$tv" >/dev/null; then
        die "tag ginn-v$tv already exists"
    fi
    if [ -n "$(git -C "$here" status --porcelain)" ]; then
        die "working tree has uncommitted changes"
    fi
    echo "check passed: ginn $tv"
}

cmd_verify_tag() {
    local tag=${1:?usage: release.sh verify-tag TAG}
    local tv pv
    tv=$(template_field version)
    pv=$(project_version)
    if [ "$tv" != "$pv" ]; then
        die "template version $tv differs from pyproject version $pv"
    fi
    if [ "$tag" != "ginn-v$pv" ]; then
        die "tag $tag differs from ginn-v$pv (the tag must equal the source version)"
    fi
    echo "tag check passed: $tag"
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
    # xbps keeps one repodata index per architecture, selected by XBPS_ARCH. A noarch package goes
    # into the index of every architecture; any other package only into its own.
    local archs=() arch parch
    for pkg in "$out"/ginn*.xbps; do
        parch=$(pkg_arch "$pkg")
        if [ "$parch" = noarch ]; then
            for arch in "${noarch_archs[@]}"; do
                XBPS_ARCH=$arch xbps-rindex -a "$pkg"
            done
        else
            XBPS_ARCH=$parch xbps-rindex -a "$pkg"
        fi
    done
    for pkg in "$out"/ginn*.xbps; do
        parch=$(pkg_arch "$pkg")
        if [ "$parch" = noarch ]; then
            archs=("${noarch_archs[@]}")
            break
        fi
        case " ${archs[*]-} " in
            *" $parch "*) ;;
            *) archs+=("$parch") ;;
        esac
    done
    if [ "$mode" = signed ]; then
        # Sign each index, then each package (the .sig2 files).
        for arch in "${archs[@]}"; do
            XBPS_ARCH=$arch xbps-rindex --privkey "$key" --signedby "$signer" -s "$out"
        done
        for pkg in "$out"/ginn*.xbps; do
            xbps-rindex --privkey "$key" --signedby "$signer" -S "$pkg"
        done
    else
        # xbps refuses an unsigned remote repository (docs/packaging.md).
        echo "release.sh: GINN_SIGNING_KEY is not set; the repository is UNSIGNED" >&2
    fi
    # SHA256SUMS lets a download be checked without a signature.
    (
        cd "$out"
        sums=(ginn*.xbps)
        # The .sig2 files exist only when signed; an unmatched glob would stay literal.
        if [ "$mode" = signed ]; then
            sums+=(ginn*.sig2)
        fi
        sha256sum -- "${sums[@]}" ./*-repodata > SHA256SUMS
    )
    echo "$mode repository: $out ($count package(s), architectures: ${archs[*]})"
}

cmd_bump() {
    local tag=${1:?usage: release.sh bump TAG}
    local ver=${tag#ginn-v}
    [ "$tag" != "$ver" ] || die "not a ginn tag: $tag"
    # Only the branch that carries this version is bumped; a later version is left alone.
    if [ "$(template_field version)" != "$ver" ]; then
        echo "bump skipped: template version $(template_field version) is not $ver"
        return 0
    fi
    sed -i "s/^version=.*/version=$ver/" "$template"
    cmd_checksum "$repo_url/archive/refs/tags/$tag.tar.gz"
    # The README names the packages of the current release.
    sed -i -E "s/(ginn(-gui)?)-[0-9]+(\.[0-9]+)*_1\.x86_64\.xbps/\1-${ver}_1.x86_64.xbps/g" \
        "$here/README.md"
    echo "bumped to $ver"
}

cmd_cut() {
    cmd_check
    local tv
    tv=$(template_field version)
    git -C "$here" tag -a "ginn-v$tv" -m "ginn $tv"
    cat <<EOF
Tag ginn-v$tv created locally. Next steps:
  1. git push origin void && git push origin ginn-v$tv
  2. The release workflow (.github/workflows/release.yml) runs the tests, builds the package,
     signs the repository with the Actions secret XBPS_PRIVKEY, attaches it to the GitHub
     release, and commits the template checksum to the void branch. Do not upload assets by hand.
EOF
}

main() {
    local cmd=${1:-}
    if [ $# -gt 0 ]; then
        shift
    fi
    case "$cmd" in
        check) cmd_check ;;
        verify-tag) cmd_verify_tag "$@" ;;
        bump) cmd_bump "$@" ;;
        checksum) cmd_checksum "$@" ;;
        repo) cmd_repo "$@" ;;
        cut) cmd_cut ;;
        *) die "usage: release.sh {check|verify-tag TAG|checksum URL_OR_FILE|bump TAG|repo BINPKGS_DIR OUT_DIR|cut}" ;;
    esac
}

main "$@"
