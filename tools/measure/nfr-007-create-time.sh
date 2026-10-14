#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# NFR-007 (T10.4): realm creation time, at most 5 min on a 100 Mbps link, with no --with option.
#
#   tools/measure/nfr-007-create-time.sh [--from-results FILE]
#
# Default: runs the host-gate test test_c_create_timing on this Void host. The test creates a
# debian realm once, with zero packages and no refresh, after removing the image so that the pull
# is inside the time (record c-create-timing). The test needs rootless podman, crun and make
# (native/), and it builds its own storage (tests/integration/conftest.py). Its records go to a
# temporary GINN_T49_RESULTS file.
#
# NFR-007 asks for three runs per distribution, with packages, on a 100 Mbps link. This script runs
# none of that. Its note says so, and the verdict is partial.
#
# --from-results FILE reads the records of an earlier run and runs nothing.
#
# Prints one JSON line (tools/measure/emit.py). Exit: 0 PASS, 1 FAIL, 2 NOT RUN or usage.
set -eu

here=$(cd "$(dirname "$0")" && pwd)

results=""
case ${1:-} in
    '') ;;
    --from-results)
        [ $# -eq 2 ] || { printf 'usage: %s [--from-results FILE]\n' "$0" >&2; exit 2; }
        results=$2
        ;;
    -h | --help)
        printf 'usage: %s [--from-results FILE]\n' "$0"
        exit 0
        ;;
    *)
        printf 'usage: %s [--from-results FILE]\n' "$0" >&2
        exit 2
        ;;
esac

# shellcheck source=tools/measure/_common.sh
. "$here/_common.sh"

if [ -z "$results" ]; then
    results=$(mktemp "${TMPDIR:-/tmp}/ginn-nfr-007.XXXXXX")
    trap 'rm -f -- "$results"' EXIT
    run_gate_test test_c_create_timing "$results"
fi

status=0
python3 -I "$here/emit.py" gate --results "$results" --item c-create-timing --field elapsed_s \
    --nfr NFR-007 \
    --metric "realm creation (debian, zero packages, no refresh, image pull included)" \
    --unit s --target "<= 300 s on a 100 Mbps link, 3 runs per distribution" --max 300 \
    --note "one run, no packages, no link speed control; partial until the packages run is measured" \
    || status=$?
exit "$status"
