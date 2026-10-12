#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Provenance scan (NFR-063): fails when ginn source shares a code fragment with a
# reference source. The reference repositories are not named here; CI passes them in.
#
# Usage: PROVENANCE_REPOS="URL[@REF] ..." tools/provenance-scan.sh
#   PROVENANCE_REPOS   required. Whitespace-separated clone URLs. Append @REF (branch,
#                      tag or commit SHA) to pin a reference; without it the remote HEAD
#                      is scanned. The resolved commit of each reference is printed.
#   PROVENANCE_REPORT  optional. Copy the HTML report to this path. The report shows
#                      reference code, so do not publish it.
set -euo pipefail

COPYDETECT_VERSION=0.5.0
NOISE_THRESHOLD=25
GUARANTEE_THRESHOLD=30
DISPLAY_THRESHOLD=0.0
SCAN_EXTENSIONS=py,sh,c,h
SCAN_DIRS=(src tools native)

repo_root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
export GIT_TERMINAL_PROMPT=0

# Clone one reference entry ("URL" or "URL@REF") at depth 1 into the directory $2.
fetch_reference() {
    local entry=$1 dest=$2 url ref
    case "$entry" in
        *@*) url=${entry%@*}; ref=${entry##*@} ;;
        *) url=$entry; ref=HEAD ;;
    esac
    mkdir -p "$dest"
    git -C "$dest" init --quiet
    git -C "$dest" fetch --quiet --depth 1 "$url" "$ref"
    git -C "$dest" checkout --quiet FETCH_HEAD
    echo "reference: $url at $(git -C "$dest" rev-parse HEAD)"
}

: "${PROVENANCE_REPOS:?set PROVENANCE_REPOS to the reference repository URLs}"
read -r -a repos <<<"$PROVENANCE_REPOS"
if [ "${#repos[@]}" -eq 0 ]; then
    echo "PROVENANCE_REPOS names no reference source" >&2
    exit 2
fi

index=0
for entry in "${repos[@]}"; do
    index=$((index + 1))
    fetch_reference "$entry" "$scratch/ref-$index"
done

test_dirs=()
for dir in "${SCAN_DIRS[@]}"; do
    # Skip directories that do not exist yet, for example native/ before M5.
    if [ -d "$repo_root/$dir" ]; then
        test_dirs+=("$repo_root/$dir")
    fi
done
if [ "${#test_dirs[@]}" -eq 0 ]; then
    echo "no source directories to scan under $repo_root" >&2
    exit 2
fi

python3 -m venv "$scratch/venv"
"$scratch/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    "copydetect==$COPYDETECT_VERSION"

report="$scratch/report.html"
status=0
# The Python API returns the matched pairs directly, so the decision does not depend on
# the layout of the command-line summary. Only pairs whose reference side lies under
# $scratch/ref-* count; test-versus-test pairs inside this repository are not provenance.
"$scratch/venv/bin/python" - "$scratch" "$report" "$NOISE_THRESHOLD" \
    "$GUARANTEE_THRESHOLD" "$DISPLAY_THRESHOLD" "$SCAN_EXTENSIONS" "${test_dirs[@]}" <<'PY' || status=$?
import glob
import os
import sys

from copydetect import CopyDetector

scratch, report, noise, guarantee, display, extensions, *test_dirs = sys.argv[1:]
refs = sorted(glob.glob(os.path.join(scratch, "ref-*")))
ref_prefix = os.path.realpath(scratch) + os.sep + "ref-"

detector = CopyDetector(
    test_dirs=test_dirs,
    ref_dirs=refs,
    extensions=extensions.split(","),
    noise_t=int(noise),
    guarantee_t=int(guarantee),
    display_t=float(display),
    autoopen=False,
    silent=True,
    out_file=report,
)
detector.run()
detector.generate_html_report()

hits = []
for test_sim, ref_sim, test_file, ref_file, *_ in detector.get_copied_code_list():
    similarity = max(test_sim, ref_sim)
    if similarity > 0 and os.path.realpath(ref_file).startswith(ref_prefix):
        hits.append((test_file, ref_file, similarity))

for test_file, ref_file, similarity in hits:
    print(f"match: {test_file} shares a fragment with {ref_file} ({similarity:.0%})")
print(f"provenance: {len(hits)} match(es) against {len(refs)} reference source(s)")
sys.exit(1 if hits else 0)
PY

if [ -n "${PROVENANCE_REPORT:-}" ] && [ -f "$report" ]; then
    cp -- "$report" "$PROVENANCE_REPORT"
fi
if [ "$status" -ne 0 ]; then
    echo "provenance scan FAILED (status $status)" >&2
    exit "$status"
fi
echo "provenance scan passed"
