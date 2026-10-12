#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Release helper for the ginn Void package (T0.6, AS-003, NFR-SEC-003).
#
#   check                      template version equal to the pyproject version, no tag yet, clean tree
#   checksum URL_OR_FILE       write the sha256 of the release tarball into the template
#   repo BINPKGS_DIR OUT_DIR   sign the ginn packages and build the signed repository in OUT_DIR,
#                              one signed index per architecture found in BINPKGS_DIR. OUT_DIR
#                              also gets the rendered traceability matrix (traceability.md)
#   cut                        run check, create the annotated tag v<version>, print next steps
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
    local key=${GINN_SIGNING_KEY:?set GINN_SIGNING_KEY to the private key path}
    local pkg count=0
    [ -f "$key" ] || die "signing key not found: $key"
    mkdir -p "$out"
    out=$(cd "$out" && pwd)
    for pkg in "$bins"/ginn*.xbps; do
        [ -f "$pkg" ] || continue
        cp -- "$pkg" "$out/"
        count=$((count + 1))
    done
    [ "$count" -gt 0 ] || die "no ginn*.xbps packages in $bins"
    # Render the traceability matrix and ship it with the packages (NFR-061, Appendix C).
    # It is generated and not committed, so it is rendered here before it is copied.
    python3 -I "$here/tools/traceability.py" --write
    cp -- "$here/docs/traceability.md" "$out/traceability.md"
    # Sign each package before indexing: the index records the file hash.
    for pkg in "$out"/ginn*.xbps; do
        xbps-rindex --sign-pkg --signedby "$signer" --privkey "$key" "$pkg"
    done
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
    for arch in "${archs[@]}"; do
        XBPS_TARGET_ARCH=$arch xbps-rindex --sign --signedby "$signer" --privkey "$key" "$out"
    done
    echo "signed repository: $out ($count package(s), architectures: ${archs[*]})"
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
     then commit the template change on the main line.
  3. tools/xbps-build.sh <void-packages-dir>   (set GINN_MUSL=1 to add the musl target)
  4. GINN_SIGNING_KEY=<key.pem> tools/release.sh repo <void-packages-dir>/hostdir/binpkgs <out-dir>
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
