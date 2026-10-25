#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# NFR-005 (T10.4): start time of a stopped realm to `running`, p95 at most 2 s.
#
#   tools/measure/nfr-005-start-time.sh [--from-results FILE]
#
# Default: runs the host-gate test test_e_start_stop_cycles on this Void host. It makes 20 start
# and stop cycles of a debian realm and records the p95 of the starts (record e-start-cycles).
# The test needs rootless podman, crun and make (native/), and it builds its own storage
# (tests/integration/conftest.py). Its records go to a temporary GINN_T49_RESULTS file.
#
# NFR-005 asks for 50 cycles per distribution. This script reports the cycles the test ran, and the
# note names what is not covered: other distributions, and the 50-cycle count.
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
    results=$(mktemp "${TMPDIR:-/tmp}/ginn-nfr-005.XXXXXX")
    trap 'rm -f -- "$results"' EXIT
    run_gate_test test_e_start_stop_cycles "$results"
fi

status=0
python3 -I "$here/emit.py" gate --results "$results" --item e-start-cycles --field p95_s \
    --count-field n --nfr NFR-005 \
    --metric "start of a stopped realm to running (debian, services off)" \
    --unit s --target "p95 <= 2 s, 50 cycles per distribution" --max 2 \
    --note "debian only; NFR-005 asks for 50 cycles on every distribution, so this is partial" \
    || status=$?
exit "$status"
