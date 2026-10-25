# SPDX-License-Identifier: Apache-2.0
# S-05r settle procedure. Sourced by spikes/s05_idle_rss.sh after its pm() wrapper is defined.
#
# settle_realm NAME LIMIT_S
#   Poll the systemd manager inside container NAME until `systemctl is-system-running` reports
#   running or degraded, or until LIMIT_S seconds have passed (maintenance is not a settled
#   state for these runs). Each poll is a
#   podman exec through pm, bounded to 10 s. A non-zero exit from systemctl is not an error:
#   degraded exits 1. The state and the failed-unit count are read only after the poll ends,
#   so the row never records a state read before the bus was up.
#   Prints one tab-separated line: settle (ok|timeout), boot_s, state, failed_units.
#   The sweep records that line per row and starts the 60 s idle window after it.
settle_realm() {
  local name=$1 limit=$2 t0 now st settle=timeout boot state failed
  t0=$(date +%s.%N)
  while :; do
    st=$(PM_TIMEOUT=10 pm exec "$name" systemctl is-system-running 2>/dev/null || true)
    st=${st%%$'\n'*}
    case $st in
      running|degraded) settle=ok; break ;;
    esac
    now=$(date +%s.%N)
    if awk -v a="$t0" -v b="$now" -v l="$limit" 'BEGIN {exit !(b - a >= l)}'; then
      break
    fi
    sleep 1
  done
  now=$(date +%s.%N)
  boot=$(awk -v a="$t0" -v b="$now" 'BEGIN {printf "%.1f", b - a}')
  state=$(PM_TIMEOUT=10 pm exec "$name" systemctl is-system-running 2>/dev/null || true)
  state=${state%%$'\n'*}
  failed=$(PM_TIMEOUT=10 pm exec "$name" sh -c 'systemctl --failed --no-legend | wc -l' 2>/dev/null || true)
  failed=${failed//[[:space:]]/}
  printf '%s\t%s\t%s\t%s\n' "$settle" "$boot" "${state:-none}" "${failed:-none}"
}
