#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the ginn xbps package with xbps-src (T0.6, AS-003, NFR-062).
#
# Usage: tools/xbps-build.sh [VOID_PACKAGES_DIR]
#   VOID_PACKAGES_DIR     a void-linux/void-packages checkout (default: ./void-packages)
#   GINN_MUSL=1           also build the x86_64-musl target
#   GINN_LOCAL_SOURCE=1   build the distfile from HEAD of this repository, not from the
#                         GitHub tag archive. Use it in CI and before the tag exists.
#                         The checksum is written into the copied template only.
#
# Prints the .xbps files produced in hostdir/binpkgs.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "run as a normal user: xbps-src refuses to run as root" >&2
    exit 2
fi

here=$(cd "$(dirname "$0")/.." && pwd)
void=${1:-./void-packages}
if [ ! -x "$void/xbps-src" ]; then
    echo "not a void-packages checkout: $void" >&2
    exit 2
fi
void=$(cd "$void" && pwd)

template_src="$here/packaging/void/srcpkgs"
version=$(awk -F= '$1 == "version" { print $2; exit }' "$template_src/ginn/template")
if [ -z "$version" ]; then
    echo "no version in $template_src/ginn/template" >&2
    exit 2
fi

# Copy the templates.
cp -a "$template_src/." "$void/srcpkgs/"
template="$void/srcpkgs/ginn/template"

if [ "${GINN_LOCAL_SOURCE:-0}" = 1 ]; then
    tmp=$(mktemp -d)
    trap 'rm -rf -- "$tmp"' EXIT
    archive="ginn-v$version.tar.gz"
    # The name and prefix match a GitHub archive of the tag ginn-v$version.
    git -C "$here" archive --format=tar.gz --prefix="ginnungagap-ginn-v$version/" \
        -o "$tmp/$archive" HEAD
    sum=$(sha256sum "$tmp/$archive" | cut -d' ' -f1)
    sed -i "s/^checksum=.*/checksum=$sum/" "$template"
    # xbps-src takes a distfile from its cache when the checksum already matches.
    cache="$void/hostdir/sources/ginn-$version"
    mkdir -p "$cache"
    cp -- "$tmp/$archive" "$cache/"
fi

if grep -q '^checksum=@CHECKSUM@$' "$template"; then
    echo "checksum is still @CHECKSUM@; run tools/release.sh checksum URL_OR_FILE or set GINN_LOCAL_SOURCE=1" >&2
    exit 2
fi

cd "$void"
# -Q runs the package check stage, which includes the unit tests in the release tarball.
# The native build takes no -a; -a names a cross target, here x86_64-musl.
./xbps-src binary-bootstrap
./xbps-src -Q pkg ginn
if [ "${GINN_MUSL:-0}" = 1 ]; then
    ./xbps-src -a x86_64-musl -Q pkg ginn
fi

echo "built packages:"
find "$void/hostdir/binpkgs" -maxdepth 1 -name 'ginn*.xbps' -print | sort
