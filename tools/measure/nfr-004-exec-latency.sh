#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# NFR-004 (T10.4): added launch latency of an app in an already-running realm, p95 at most 300 ms.
#
#   tools/measure/nfr-004-exec-latency.sh [--from-results FILE]
#
# Default: runs the host-gate test test_e_exec_p95_over_100_launches on this Void host. It makes
# 100 enter calls through runtime/enter.py, each with the ADR-008 pre-check, and times them in
# process. That excludes the start-up of the ginn CLI, which the SRS wording does not name. The
# test needs rootless podman, crun and make (native/), and it builds its own storage
# (tests/integration/conftest.py). Its records go to a temporary GINN_T49_RESULTS file.
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
    results=$(mktemp "${TMPDIR:-/tmp}/ginn-nfr-004.XXXXXX")
    trap 'rm -f -- "$results"' EXIT
    run_gate_test test_e_exec_p95_over_100_launches "$results"
fi

status=0
python3 -I "$here/emit.py" gate --results "$results" --item e-exec-p95 --field p95_ms \
    --nfr NFR-004 \
    --metric "launch latency of an app in a running realm (enter_realm in process, crun, debian)" \
    --unit ms --target "p95 <= 300 ms over 100 launches" --max 300 \
    --note "read this on an idle host; the run at load 24 in spikes/results/T4.9-lifecycle-real.md had p95 453 ms" \
    || status=$?
exit "$status"
