#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-05 (NFR-002, NFR-003, FR-068, FR-082): idle RSS of a services-off realm
# (init plus an idle process), of a services-on realm (systemd from the S-01
# image) with the mitigations in spikes/s05/, and of the xdg-dbus-proxy process.
#
#   spikes/s05_idle_rss.sh prepare      pull the base, extract catatonit, build images, record packages
#   spikes/s05_idle_rss.sh off          services-off sweep and dbus-proxy RSS
#   spikes/s05_idle_rss.sh root-setup   S-01 root steps for the services-on realms (sudo, once)
#   spikes/s05_idle_rss.sh on           services-on sweep; moves this process into the launch leaf
#   spikes/s05_idle_rss.sh run          prepare, off, dbus, root-setup, on, summary (resumable)
#   spikes/s05_idle_rss.sh summary      median, min and max per config from runs.tsv
#   spikes/s05_idle_rss.sh nfr001       list conmon, podman, pasta and catatonit processes, attributed
#   spikes/s05_idle_rss.sh teardown     remove S-05 containers, images, storage and S-05 cgroup leaves
#
# Environment:
#   GINN_SPIKE_OUT       raw logs, runs.tsv, per-run JSON (default ~/.cache/ginn-spike/s05)
#   GINN_SPIKE_ROOT      podman storage (default ~/.local/share/ginn-spike-s05/podman)
#   GINN_SPIKE_RUNROOT   podman runroot (default $XDG_RUNTIME_DIR/ginn-spike-s05)
#   GINN_S05_RUNS        runs per config (default 10)
#   GINN_S05_WINDOW      idle seconds before sampling (default 60)
#
# Every podman call uses the spike storage. The user's default storage is never
# written. Root steps run only through sudo: the S-01 helper for cgroups, and
# rmdir for S-05's own leaves in teardown. Each root step is logged.
set -euo pipefail

SELF="$(readlink -f "$0")"
HERE="$(dirname "$SELF")"
S05="$HERE/s05"
S01="$HERE/s01_systemd_rootless.sh"
DEBIAN=docker.io/library/debian:stable
IMG=localhost/ginn-s05
UIDN=$(id -u)
GIDN=$(id -g)
RUNS=${GINN_S05_RUNS:-10}
WINDOW=${GINN_S05_WINDOW:-60}
OUT="${GINN_SPIKE_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s05}"
GINN_SPIKE_ROOT="${GINN_SPIKE_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/ginn-spike-s05/podman}"
GINN_SPIKE_RUNROOT="${GINN_SPIKE_RUNROOT:-${XDG_RUNTIME_DIR:-/run/user/$UIDN}/ginn-spike-s05}"
GINN_SPIKE_TMPDIR="$GINN_SPIKE_RUNROOT/libpod-tmp"
export GINN_SPIKE_ROOT GINN_SPIKE_RUNROOT GINN_SPIKE_TMPDIR
# Every podman call carries its own root, runroot and tmpdir, so nothing under the
# user's default storage or the shared /run/user/<uid>/libpod tree is touched.
PM=(podman --root "$GINN_SPIKE_ROOT" --runroot "$GINN_SPIKE_RUNROOT" --tmpdir "$GINN_SPIKE_TMPDIR")
CG_BASE="/sys/fs/cgroup/ginn/$UIDN"
NA_ROW=$'-\t-\t-\t-\t-\t-\t-\t-\t-\t-'  # ten metric columns, no measurement
TSV_HEADER=$'cfg\trun\tstatus\timage\tboot_s\tstate\tfailed_units\tconmon_rss_kib\trealm_rss_kib\tpause_rss_kib\ttotal_rss_kib\trealm_pss_kib\tpause_pss_kib\ttotal_pss_kib\tnproc_container\tswap_kib\tpss_missing'

# name | image tag | extra run flags (no spaces inside a value)
ON_CONFIGS=(
  "base|base|"
  "a|a|"
  "b|b|"
  "bl|bl|"
  "d|base|-e SYSTEMD_LOG_LEVEL=warning"
  "norec|recommends|"
  "ab|ab|"
  "abd|ab|-e SYSTEMD_LOG_LEVEL=warning"
  "abld|abl|-e SYSTEMD_LOG_LEVEL=warning"
)

log() { mkdir -p "$OUT"; printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
pm() { "${PM[@]}" "$@"; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN {printf "%.1f", b - a}'; }
now() { date +%s.%N; }

init_out() {
  mkdir -p "$OUT"/{bin,logs,raw,probe}
  [[ -f $OUT/runs.tsv ]] || printf '%s\n' "$TSV_HEADER" > "$OUT/runs.tsv"
}

done_run() { # cfg run: true when runs.tsv already has the row
  awk -F'\t' -v c="$1" -v r="$2" '$1 == c && $2 == r {f = 1} END {exit !f}' "$OUT/runs.tsv" 2>/dev/null
}

append_row() { # cfg run status image boot state failed metrics(tab-joined)
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" >> "$OUT/runs.tsv"
}

# ------------------------------------------------------------------ prepare

extract_catatonit() {
  # Host catatonit and podman's bundled copy are both absent. Take Debian's
  # catatonit package from inside a throwaway container in the spike storage.
  [[ -x $OUT/bin/catatonit ]] && return 0
  mkdir -p "$OUT/bin"
  pm run --rm -v "$OUT/bin:/out" "$DEBIAN" sh -c \
    'apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq catatonit >/dev/null 2>&1 && cp /usr/bin/catatonit /out/catatonit && dpkg-query -W catatonit > /out/catatonit.pkg && /usr/bin/catatonit --version > /out/catatonit.version 2>&1' \
    >"$OUT/logs/catatonit-extract.txt" 2>&1 || die "catatonit extraction failed, see $OUT/logs/catatonit-extract.txt"
  if command -v file >/dev/null 2>&1; then file "$OUT/bin/catatonit" > "$OUT/bin/catatonit.file"; fi
}

build_image() { # tag, then podman build arguments
  local tag=$1
  shift
  pm build --network host -q -t "$IMG:$tag" "$@" >"$OUT/logs/build-$tag.txt" 2>&1 \
    || die "build $tag failed, see $OUT/logs/build-$tag.txt"
}

record_packages() { # tag
  local n size
  n=$(pm run --rm --entrypoint /bin/sh "$IMG:$1" -c 'dpkg-query -W | wc -l' 2>/dev/null | tail -1)
  size=$(pm image inspect --format '{{.Size}}' "$IMG:$1")
  printf '%s\t%s\t%s\n' "$1" "$n" "$size" >> "$OUT/images.tsv"
}

cmd_prepare() {
  init_out
  pm image exists "$DEBIAN" 2>/dev/null || pm pull -q "$DEBIAN" >"$OUT/logs/pull.txt" 2>&1
  extract_catatonit
  log "catatonit: $(cat "$OUT/bin/catatonit.version" 2>/dev/null | head -1)"
  # Record the failure of --init without --init-path, as the procedure asks.
  if pm create --name s05init-probe --init "$DEBIAN" true >"$OUT/probe/init-no-path.txt" 2>&1; then
    pm rm -f s05init-probe >/dev/null 2>&1 || true
    printf 'unexpected: create without --init-path succeeded\n' >> "$OUT/probe/init-no-path.txt"
  fi
  : > "$OUT/images.tsv"
  build_image base -f "$HERE/Dockerfile.s01" "$HERE"
  build_image recommends -f "$S05/Dockerfile.recommends" "$S05"
  build_image a --build-arg JOURNAL=1 --build-arg MASKS=none -f "$S05/Dockerfile.mit" "$S05"
  build_image b --build-arg JOURNAL=0 --build-arg MASKS=base -f "$S05/Dockerfile.mit" "$S05"
  build_image bl --build-arg JOURNAL=0 --build-arg MASKS=logind -f "$S05/Dockerfile.mit" "$S05"
  build_image ab --build-arg JOURNAL=1 --build-arg MASKS=base -f "$S05/Dockerfile.mit" "$S05"
  build_image abl --build-arg JOURNAL=1 --build-arg MASKS=logind -f "$S05/Dockerfile.mit" "$S05"
  for t in base recommends a b bl ab abl; do record_packages "$t"; done
  log "images recorded in $OUT/images.tsv"
}

# ------------------------------------------------------------------ measure

measure_realm() { # container con init raw-json: prints the metric columns
  local con=$2 init=$3 raw=$4 pause=""
  # The rootless pause process is per user, not per storage root: the spike joins the
  # user's existing one. Find it by its comm ("podman pause") with ps, which only reads.
  pause=$(ps -u "$UIDN" -o pid=,comm= | awk '$2 == "podman" && $3 == "pause" {print $1; exit}')
  python3 -I "$S05/measure.py" --conmon "$con" --init "$init" --pause-pid "$pause" \
    --json-out "$raw" --podman-root "$GINN_SPIKE_ROOT" --podman-runroot "$GINN_SPIKE_RUNROOT" \
    --podman-tmpdir "$GINN_SPIKE_TMPDIR"
}

# ------------------------------------------------------------------ services off

cmd_off() {
  init_out
  [[ -x $OUT/bin/catatonit ]] || die "run prepare first"
  local r name=s05off con init row
  for r in $(seq 1 "$RUNS"); do
    done_run off "$r" && continue
    pm rm -f "$name" >/dev/null 2>&1 || true
    if ! pm run -d --name "$name" --userns keep-id --network host --init \
         --init-path "$OUT/bin/catatonit" "$DEBIAN" sleep infinity >"$OUT/logs/off-$r.run" 2>&1; then
      append_row off "$r" "fail-run" "$DEBIAN" - - - "$NA_ROW"
      continue
    fi
    con=$(pm inspect --format '{{.State.ConmonPid}}' "$name")
    init=$(pm inspect --format '{{.State.Pid}}' "$name")
    sleep "$WINDOW"
    row=$(measure_realm "$name" "$con" "$init" "$OUT/raw/off-$r.json") || row="$NA_ROW"
    pm rm -f "$name" >/dev/null 2>&1 || true
    append_row off "$r" ok "$DEBIAN" - - - "$row"
    log "off run $r: $row"
  done
}

cmd_dbus() {
  # FR-068: resident cost of one filtered proxy, measured at 60 s with no client.
  init_out
  local sock="$XDG_RUNTIME_DIR/ginn-spike-s05/dbus-proxy.sock" pid r rss pss swap
  mkdir -p "$(dirname "$sock")"
  [[ -n ${DBUS_SESSION_BUS_ADDRESS:-} ]] || die "DBUS_SESSION_BUS_ADDRESS unset"
  : > "$OUT/dbus.tsv"
  printf 'run\trss_kib\tpss_kib\tswap_kib\n' >> "$OUT/dbus.tsv"
  for r in 1 2 3; do
    rm -f "$sock"
    # xdg-dbus-proxy takes ADDRESS PATH first, then the options for that proxy.
    xdg-dbus-proxy "$DBUS_SESSION_BUS_ADDRESS" "$sock" \
      --filter --talk=org.freedesktop.Notifications --talk='org.freedesktop.portal.*' \
      >"$OUT/logs/dbus-$r.txt" 2>&1 &
    pid=$!
    sleep "$WINDOW"
    rss=$(awk '/^VmRSS:/ {print $2}' "/proc/$pid/status")
    swap=$(awk '/^VmSwap:/ {print $2}' "/proc/$pid/status")
    pss=$(awk '/^Pss:/ {print $2}' "/proc/$pid/smaps_rollup")
    printf '%s\t%s\t%s\t%s\n' "$r" "$rss" "$pss" "$swap" >> "$OUT/dbus.tsv"
    log "dbus-proxy run $r: rss=${rss}KiB pss=${pss}KiB"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  rm -f "$sock"
}

# ------------------------------------------------------------------ services on

cmd_root_setup() {
  # S-01 root steps (FR-001 model). Skipped when the launch leaf already exists.
  if [[ -d $CG_BASE/launch ]]; then
    log "root-setup: $CG_BASE/launch already present, skipping"
    return 0
  fi
  mkdir -p "$OUT/root"
  log "root-setup (sudo): $S01 --root-setup $UIDN $GIDN $OUT/root"
  sudo "$S01" --root-setup "$UIDN" "$GIDN" "$OUT/root"
}

probe_cmd='
systemctl list-units --type=service --state=running --no-legend --plain
echo "--- timers"; systemctl list-timers --all --no-legend
echo "--- masked unit files"; systemctl list-unit-files --state=masked --no-legend
echo "--- failed"; systemctl --failed --no-legend
echo "--- journal lines (boot)"; journalctl -b --no-pager | wc -l
echo "--- pid1 environ"; tr "\0" "\n" < /proc/1/environ | grep SYSTEMD || true
echo "--- dpkg packages"; dpkg-query -W | wc -l'

cmd_on() {
  init_out
  [[ -d $CG_BASE/launch ]] || die "run root-setup first"
  # The realm cgroups live under /ginn/<uid>, which only a leaf can write into.
  mkdir -p "$OUT/root"
  log "moving this process ($$) into the launch leaf (sudo)"
  sudo "$S01" --root-move "$UIDN" "$$" "$OUT/root"
  local spec cfg tag flags name img r fa st state failed boot t0 t1 con init row
  for spec in "${ON_CONFIGS[@]}"; do
    IFS='|' read -r cfg tag flags <<<"$spec"
    img="$IMG:$tag"
    name="s05-$cfg"
    for r in $(seq 1 "$RUNS"); do
      done_run "on-$cfg" "$r" && continue
      pm rm -f "$name" >/dev/null 2>&1 || true
      rmdir "$CG_BASE/s05-$cfg" 2>/dev/null || true
      read -ra fa <<<"$flags"
      if ! pm run -d --name "$name" --systemd=always --cgroupns private \
           --cgroup-parent "/ginn/$UIDN/s05-$cfg" --userns keep-id --user 0:0 \
           --network host --stop-signal SIGRTMIN+3 "${fa[@]}" "$img" /sbin/init \
           >"$OUT/logs/on-$cfg-$r.run" 2>&1; then
        append_row "on-$cfg" "$r" fail-run "$img" - - - "$NA_ROW"
        continue
      fi
      t0=$(now)
      st=$(timeout 120 "${PM[@]}" exec "$name" sh -c \
           'systemctl is-system-running --wait >/dev/null 2>&1; systemctl is-system-running; systemctl --failed --no-legend | wc -l' \
           2>/dev/null || true)
      t1=$(now)
      state=$(sed -n 1p <<<"$st")
      failed=$(sed -n 2p <<<"$st")
      boot=$(elapsed "$t0" "$t1")
      if [[ $r == 1 ]]; then
        timeout 120 "${PM[@]}" exec "$name" sh -c "$probe_cmd" >"$OUT/probe/on-$cfg.txt" 2>&1 || true
      fi
      con=$(pm inspect --format '{{.State.ConmonPid}}' "$name")
      init=$(pm inspect --format '{{.State.Pid}}' "$name")
      sleep "$WINDOW"
      row=$(measure_realm "$name" "$con" "$init" "$OUT/raw/on-$cfg-$r.json") \
        || row="$NA_ROW"
      pm rm -f "$name" >/dev/null 2>&1 || true
      rmdir "$CG_BASE/s05-$cfg" 2>/dev/null || true
      append_row "on-$cfg" "$r" ok "$img" "$boot" "${state:-none}" "${failed:-none}" "$row"
      log "on-$cfg run $r: state=${state:-none} boot=${boot}s $row"
    done
  done
}

# ------------------------------------------------------------------ reporting

cmd_summary() {
  [[ -f $OUT/runs.tsv ]] || die "no runs.tsv"
  python3 -I "$S05/measure.py" --summarize "$OUT/runs.tsv"
}

cmd_nfr001() {
  echo "raw count, comm matches conmon|podman|pasta|catatonit: $(ps -u "$UIDN" -o comm= | grep -cE 'conmon|podman|pasta|catatonit' || true)"
  # Match on the process name, so a shell whose command line mentions these words is not counted.
  ps -u "$UIDN" -o pid=,comm=,args= | awk '$2 ~ /^(conmon|podman|pasta|passt|catatonit)/' | while IFS= read -r line; do
    local cat name
    case "$line" in
      *ginn-spike-s05*) cat=S-05 ;;
      *ginn-spike-s0[0-9]*|*ginn-spike-s1[0-9]*|*ginn-spike-s2*) cat=other-spike ;;
      *"podman pause"*) cat=shared-user-pause ;;
      *containers/storage*) cat=user-default-storage ;;
      *pasta*) cat=pasta-helper ;;
      *) cat=other ;;
    esac
    name=$(sed -n 's/.* -n \([^ ]*\) .*/\1/p' <<<"$line")
    printf '%-20s %s %s\n' "$cat" "$(awk '{print $1}' <<<"$line")" "${name:+name=$name}"
  done
  echo "S-05 attributed count: $(ps -u "$UIDN" -o comm=,args= | awk '$1 ~ /^(conmon|podman|pasta|passt|catatonit)/ && /ginn-spike-s05/' | wc -l) (must be 0 after teardown)"
}

# ------------------------------------------------------------------ teardown

cmd_teardown() {
  if [[ -d $GINN_SPIKE_ROOT ]] || [[ -d $GINN_SPIKE_RUNROOT ]]; then
    pm rm -af >/dev/null 2>&1 || true
    pm rmi -af >/dev/null 2>&1 || true
    pm unshare rm -rf "$GINN_SPIKE_ROOT" "$GINN_SPIKE_RUNROOT" 2>/dev/null || true
    # Leftovers that the rootless namespace could not remove: plain rm of the spike dirs only.
    rm -rf "$GINN_SPIKE_ROOT" "$GINN_SPIKE_RUNROOT" 2>/dev/null || log "storage removal incomplete under $GINN_SPIKE_ROOT"
  fi
  rm -f "$XDG_RUNTIME_DIR/ginn-spike-s05/dbus-proxy.sock" 2>/dev/null || true
  # Remove only the leaves S-05 created: the launch leaf and per-config realm leaves.
  # /sys/fs/cgroup/ginn, /ginn/<uid> and root subtree_control stay as S-01 documents them.
  local d
  for d in "$CG_BASE"/s05-* "$CG_BASE/launch"; do
    [[ -d $d ]] || continue
    log "rmdir cgroup leaf (sudo): $d"
    sudo rmdir "$d" || log "rmdir failed, leaf not empty: $d"
  done
  log "teardown done"
}

cmd_run() {
  init_out
  cmd_prepare
  cmd_off
  cmd_dbus
  cmd_root_setup
  cmd_on
  cmd_summary
}

case ${1:-} in
  prepare) cmd_prepare ;;
  off) cmd_off ;;
  dbus) cmd_dbus ;;
  root-setup) cmd_root_setup ;;
  on) cmd_on ;;
  run) cmd_run ;;
  summary) cmd_summary ;;
  nfr001) cmd_nfr001 ;;
  teardown) cmd_teardown ;;
  *) die "usage: $0 prepare|off|dbus|root-setup|on|run|summary|nfr001|teardown" ;;
esac
