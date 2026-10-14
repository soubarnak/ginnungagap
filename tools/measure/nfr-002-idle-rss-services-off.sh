#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# NFR-002 (T10.4): idle host memory of a services-off realm, at most 8 MiB RSS (median of 10).
#
#   tools/measure/nfr-002-idle-rss-services-off.sh
#
# Reads the latest S-05r sweep of the spike (spikes/s05_idle_rss.sh, configuration `off`). The
# spike measures: it starts realms under its own pinned podman storage and XDG_RUNTIME_DIR. This
# script starts nothing and calls no podman. Its total is conmon, the container processes and the
# rootless pause process, as in spikes/s05/measure.py.
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
python3 -I "$here/emit.py" sweep --runs "$out/runs.tsv" --sweep "$sweep" --cfg off \
    --nfr NFR-002 \
    --metric "idle RSS, services-off realm with its pause process (S-05r sweep ${sweep:-none})" \
    --target "<= 8 MiB, median of 10 runs" --max 8 || status=$?
exit "$status"
