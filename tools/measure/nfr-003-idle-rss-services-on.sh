#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# NFR-003 (T10.4): idle host memory of a services-on realm, at most 40 MiB RSS (median of 10).
#
#   tools/measure/nfr-003-idle-rss-services-on.sh
#
# Reads the latest S-05r sweep of the spike (spikes/s05_idle_rss.sh, configuration `on-abd`: the
# a+b+d mitigations that ADR-007 adopts as the default). The spike measures and this script does
# not. The base configuration (`on-base`, no mitigations) is in the same sweep. It fails at the
# maximum in S-05r (see spikes/results/S-05.md), and the script does not read it for the verdict.
#
# The services-on leg of the spike needs a delegated launch leaf, which its on-leg step prepares
# with one root write. That step is not run by this script. Run it yourself first.
#
# Environment:
#   GINN_SPIKE_OUT     the spike output directory (default ~/.cache/ginn-spike/s05r, as the spike)
#   GINN_MEASURE_SWEEP the sweep id to read (default: the spike's sweep.id)
#
# Prints one JSON line (tools/measure/emit.py). Exit: 0 PASS, 1 FAIL, 2 NOT RUN or usage.
set -eu

here=$(cd "$(dirname "$0")" && pwd)

case ${1:-} in
    '') ;;
    -h | --help)
        printf 'usage: %s   (reads GINN_SPIKE_OUT and GINN_MEASURE_SWEEP)\n' "$0"
        exit 0
        ;;
    *)
        printf 'usage: %s   (takes no arguments)\n' "$0" >&2
        exit 2
        ;;
esac

out=${GINN_SPIKE_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s05r}
sweep=${GINN_MEASURE_SWEEP:-$(cat "$out/sweep.id" 2>/dev/null || true)}

status=0
python3 -I "$here/emit.py" sweep --runs "$out/runs.tsv" --sweep "$sweep" --cfg on-abd \
    --nfr NFR-003 \
    --metric "idle RSS, services-on realm (a+b+d mitigations) with its pause process (S-05r sweep ${sweep:-none})" \
    --target "<= 40 MiB, median of 10 runs" --max 40 \
    --note "a+b+d is the ADR-007 default; the unmitigated base configuration is not the verdict" \
    || status=$?
exit "$status"
