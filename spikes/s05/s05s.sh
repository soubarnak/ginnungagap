#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-05s (NFR-003, ADR-007 mitigations a+b+d in their create-time form): idle RSS of a services-on
# realm whose journald drop-in, unit masks and log level are create-time mounts and environment,
# not the image layers that S-05r measured. Everything else is S-05r: the same base image
# (spikes/Dockerfile.s01 on docker.io/library/debian:stable), the same argv, the same settle
# (spikes/s05/settle.sh), the same memory gate and idle window, and the same accounting
# (spikes/s05/measure.py). The only variable is how the mitigations are delivered.
#
#   XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s05s-xrd spikes/s05/s05s.sh <command>
#
#   check            first podman calls under the pin; the spike pause must have comm catatonit
#   prepare          pull the base image, build localhost/ginn-s05s:base, record its pins
#   new-sweep        start a sweep id (s05s-<time>); rows carry it, and sweeps read only it
#   on-trial CFG     one services-on start (abd|base) under the launch leaf, with a probe; it
#                    gates the sweeps (passes only when settled with 0 failed units)
#   on CFG           services-on sweep (abd|base); needs the launcher inside s05s-launch
#   on-leg           the whole services-on leg: the launcher moves itself into s05s-launch, then
#                    check, prepare, new-sweep, trial abd, sweep abd, control sweep base, teardown
#   summary [SWEEP]  median, min and max per config for SWEEP (default: the current sweep id)
#   split            RssAnon, RssFile and RssShmem per raw sample, into split.tsv
#   nfr001           list catatonit, conmon, podman and crun processes, tagged by environ
#   teardown         remove containers, images, storage, the spike pause and the spike XDG dir
#
# Environment:
#   GINN_SPIKE_OUT       logs, runs.tsv, sweep.id, probes, raw JSON (default ~/.cache/ginn-spike/s05s)
#   GINN_SPIKE_ROOT      podman storage (default ~/.local/share/ginn-spike-s05s/podman)
#   GINN_SPIKE_RUNROOT   podman runroot (default /run/user/<uid>/ginn-spike-s05s/runroot)
#   GINN_S05_RUNS        counted runs per config (default 10)
#   GINN_S05_WINDOW      idle seconds before sampling (default 60)
#   GINN_S05_SETTLE      settle limit in seconds for services-on (default 180)
#
# Create-time forms of ADR-007 (CR-007: ginn writes no file into the realm's /etc):
#   (a) spikes/s05/journald-ginn.conf is bind-mounted read-only at /etc/systemd/journald.conf.d/ginn.conf
#   (b) /dev/null is bind-mounted read-only over each unit path in spikes/s05/masks-base.txt
#   (d) SYSTEMD_LOG_LEVEL=warning is passed with -e
# The base config passes none of them. It is the control, and its probe shows what an unmasked
# unit reports, so the mask check is shown to tell the two apart.
#
# Pinning. pm() is the only podman entry point in this script. It sets XDG_RUNTIME_DIR to the
# spike dir and passes --root, --runroot, --tmpdir and --runtime crun. The pause pid comes from
# $SPIKE_TMPDIR/pause.pid, which podman writes under $XDG_RUNTIME_DIR/libpod/tmp. Without the pin,
# podman joins the user's own pause. The guard refuses any storage path outside ginn-spike-s05s/.
set -euo pipefail

# ------------------------------------------------------------------ podman wrapper

# pm ARGS...: podman with the S-05s pin. PM_TIMEOUT=secs bounds the call.
pm() {
  spike_guard || return 1
  local -a cmd=(env "XDG_RUNTIME_DIR=$SPIKE_XRD" podman --runtime crun
    --root "$GINN_SPIKE_ROOT" --runroot "$GINN_SPIKE_RUNROOT" --tmpdir "$SPIKE_TMPDIR")
  if [[ -n ${PM_TIMEOUT:-} ]]; then
    timeout "$PM_TIMEOUT" "${cmd[@]}" "$@"
  else
    "${cmd[@]}" "$@"
  fi
}

# in_realm NAME ARGV...: one command in a realm, bounded to 60 s, stdin closed.
in_realm() {
  local name=$1
  shift
  PM_TIMEOUT=60 pm exec "$name" "$@" </dev/null
}

# under_s05s PATH: true for an absolute path inside a ginn-spike-s05s directory, no traversal.
under_s05s() {
  [[ $1 == /* && $1 != *..* && $1 == */ginn-spike-s05s/* ]]
}

# spike_guard: refuse unless every storage path and the XDG dir is spike-owned.
spike_guard() {
  [[ $SPIKE_XRD == "/run/user/$UID/ginn-spike-s05s-xrd" ]] \
    || { printf 'refusing: XDG dir %s\n' "$SPIKE_XRD" >&2; return 1; }
  [[ $SPIKE_TMPDIR == "$SPIKE_XRD/libpod/tmp" ]] \
    || { printf 'refusing: tmpdir %s\n' "$SPIKE_TMPDIR" >&2; return 1; }
  under_s05s "$GINN_SPIKE_ROOT" \
    || { printf 'refusing: root %s is not under ginn-spike-s05s/\n' "$GINN_SPIKE_ROOT" >&2; return 1; }
  under_s05s "$GINN_SPIKE_RUNROOT" \
    || { printf 'refusing: runroot %s is not under ginn-spike-s05s/\n' "$GINN_SPIKE_RUNROOT" >&2; return 1; }
}

# ------------------------------------------------------------------ constants

SELF="$(readlink -f "$0")"
S05="$(dirname "$SELF")"
SPIKES="$(dirname "$S05")"
DEBIAN=docker.io/library/debian:stable
IMG=localhost/ginn-s05s
CATATONIT=/usr/libexec/podman/catatonit
RUNS=${GINN_S05_RUNS:-10}
WINDOW=${GINN_S05_WINDOW:-60}
SETTLE_LIMIT=${GINN_S05_SETTLE:-180}
SPIKE_XRD="/run/user/$UID/ginn-spike-s05s-xrd"
SPIKE_TMPDIR="$SPIKE_XRD/libpod/tmp"
GINN_SPIKE_ROOT="${GINN_SPIKE_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/ginn-spike-s05s/podman}"
GINN_SPIKE_RUNROOT="${GINN_SPIKE_RUNROOT:-/run/user/$UID/ginn-spike-s05s/runroot}"
OUT="${GINN_SPIKE_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s05s}"
CG_LEAF_BASE="/ginn/$UID"
CG_DIR_BASE="/sys/fs/cgroup/ginn/$UID"
MASKS_FILE="$S05/masks-base.txt"
JOURNAL_CONF="$S05/journald-ginn.conf"
# Eleven metric fields, in the order measure.py prints them.
NA_ROW=$'-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-'
TSV_HEADER=$'sweep_id\tcfg\trun\tstatus\timage\tsettle\tboot_s\tstate\tfailed_units\tconmon_rss_kib\trealm_rss_kib\tpause_rss_kib\ttotal_rss_kib\trealm_pss_kib\tpause_pss_kib\ttotal_pss_kib\tnproc_container\tswap_kib\tpss_missing\tpause_pid'
TRIAL_HEADER=$'cfg\toutcome\tsettle\tboot_s\tstate\tfailed_units\tdetail'
MEM_HEADER=$'sweep_id\tcfg\trun\ttime\tMemAvailable_kB\tpsi_memory_some_avg10\tpsi_memory_full_avg10'
ONSAMPLE_HEADER=$'sweep_id\tcfg\tattempt\ttime\tMemAvailable_kB\tpsi_some_avg10\tpsi_full_avg10\trealm_vmswap_kib\twindow_ok\twindow_min_MemAvailable_kB\twindow_max_psi_some\tstatus'
# Services-on memory gate, checked before each run and every 10 s in its idle window (as S-05r).
MEM_MIN_KIB=1572864     # 1.5 GiB of MemAvailable
PSI_MAX=1.0             # /proc/pressure/memory some avg10 must stay below this
GATE_TIMEOUT_S=900      # wait at most 15 min for the gate, re-check every 30 s
ON_FLAGS=()
ON_MOUNTS=()

# shellcheck source=spikes/s05/settle.sh
source "$S05/settle.sh"

# CAP_CMD: fixed script for the cap check. 3000 messages of 4000 bytes is 12 MB against an 8 MiB
# cap. It stays under journald's default rate limit (10000 messages per 30 s per unit).
CAP_CMD='line=$(head -c 4000 /dev/zero | tr "\000" x); yes "$line" | head -n 3000 | systemd-cat -t ginn-s05s-cap; sleep 3; echo "entries tagged ginn-s05s-cap: $(journalctl -t ginn-s05s-cap --no-pager | wc -l)"'

# ------------------------------------------------------------------ helpers

log() { mkdir -p "$OUT"; printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

init_out() {
  mkdir -p "$OUT"/{logs,raw,probe}
  [[ -f $OUT/runs.tsv ]] || printf '%s\n' "$TSV_HEADER" > "$OUT/runs.tsv"
  [[ -f $OUT/trial.tsv ]] || printf '%s\n' "$TRIAL_HEADER" > "$OUT/trial.tsv"
  [[ -f $OUT/images.tsv ]] || : > "$OUT/images.tsv"
  [[ -f $OUT/mem.tsv ]] || printf '%s\n' "$MEM_HEADER" > "$OUT/mem.tsv"
  [[ -f $OUT/on-samples.tsv ]] || printf '%s\n' "$ONSAMPLE_HEADER" > "$OUT/on-samples.tsv"
}

make_dirs() {
  mkdir -p "$SPIKE_XRD"
  chmod 700 "$SPIKE_XRD"
  mkdir -p "$(dirname "$GINN_SPIKE_RUNROOT")" "$GINN_SPIKE_RUNROOT" "$SPIKE_TMPDIR" \
    "$GINN_SPIKE_ROOT"
  chmod 700 "$(dirname "$GINN_SPIKE_RUNROOT")" "$GINN_SPIKE_RUNROOT"
}

# require_pin: the script must run with the spike XDG dir pinned.
require_pin() {
  [[ ${XDG_RUNTIME_DIR:-} == "$SPIKE_XRD" ]] \
    || die "refusing: run with XDG_RUNTIME_DIR=$SPIKE_XRD (got '${XDG_RUNTIME_DIR:-unset}')"
}

# append_row FIELDS...: one tab-joined row to runs.tsv
append_row() {
  local IFS=$'\t'
  printf '%s\n' "$*" >> "$OUT/runs.tsv"
}

# done_run SWEEP CFG RUN: true when runs.tsv has a counted row (status ok) for this run.
done_run() {
  awk -F'\t' -v s="$1" -v c="$2" -v r="$3" \
    '$1 == s && $2 == c && $3 == r && $4 == "ok" {f = 1} END {exit !f}' \
    "$OUT/runs.tsv" 2>/dev/null
}

current_sweep() {
  [[ -s $OUT/sweep.id ]] || die "no sweep id; run new-sweep first"
  cat "$OUT/sweep.id"
}

# env_has_xdg PID: true when the process environ carries the spike XDG dir
env_has_xdg() {
  local envtxt
  envtxt=$(tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null || true)
  grep -qx "XDG_RUNTIME_DIR=$SPIKE_XRD" <<<"$envtxt"
}

rss_kib() { awk '/^VmRSS:/ {print $2}' "/proc/$1/status" 2>/dev/null; }

pause_file_pid() { cat "$SPIKE_TMPDIR/pause.pid" 2>/dev/null || true; }

# stop_spike_pause PID: TERM one pid, only after its environ shows the spike XDG dir
stop_spike_pause() {
  local p=$1 i
  [[ -n $p ]] || return 0
  kill -0 "$p" 2>/dev/null || { log "pause $p not running"; return 0; }
  env_has_xdg "$p" || { log "pid $p environ is not the spike XDG, not signalled"; return 1; }
  kill -TERM "$p"
  for i in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
  if kill -0 "$p" 2>/dev/null; then log "pid $p alive after TERM"; return 1; fi
  log "stopped spike pause pid $p"
}

# rm_spike_tree PATH: rm -rf only inside the S-05s dirs
rm_spike_tree() {
  local p=$1
  if under_s05s "$p" || [[ $p == "$SPIKE_XRD" ]]; then
    rm -rf -- "$p"
  else
    log "refusing rm of $p"
    return 1
  fi
}

# root_log ARGV...: one line per root command: timestamp, launcher pid, argv
root_log() {
  mkdir -p "$OUT"
  printf '%s\tpid=%s\t%s\n' "$(date -Is)" "$BASHPID" "$*" >> "$OUT/root-commands.log"
}

# self_in_launch_leaf: this launcher subshell is inside /ginn/<uid>/..., so child realms can be
# placed there. $BASHPID, not $$: inside a subshell $$ is the parent's pid.
self_in_launch_leaf() {
  local path
  path=$(sed -n 's/^0:://p' "/proc/$BASHPID/cgroup")
  [[ $path == "$CG_LEAF_BASE"/* ]]
}

# move_self_into_leaf LEAF: moves this launcher subshell ($BASHPID, checked to carry the spike XDG
# dir) into the spike leaf LEAF under /sys/fs/cgroup/ginn/<uid>/. The unprivileged write is tried
# first. The cgroup root's cgroup.procs is root-owned, so a launcher in the session cgroup is
# expected to be refused (cgroup v2 checks write access to the common ancestor). Only that refusal
# leads to the one root command, sudo -n sh -c 'echo <pid> > .../cgroup.procs'. Then this is the
# gate: podman starts only after /proc/$BASHPID/cgroup shows the leaf.
move_self_into_leaf() {
  local leaf=$1 me=$BASHPID i cur=""
  case $leaf in
    "$CG_DIR_BASE"/s05s-*) ;;
    *) die "refusing move into $leaf: not an s05s- leaf under $CG_DIR_BASE" ;;
  esac
  [[ $me =~ ^[0-9]+$ ]] || die "launcher pid '$me' is not numeric"
  env_has_xdg "$me" || die "launcher $me does not carry XDG_RUNTIME_DIR=$SPIKE_XRD; not moved"
  if { echo "$me" > "$leaf/cgroup.procs"; } 2>"$OUT/logs/unprivileged-move.err"; then
    log "launcher $me moved into $leaf without root"
  else
    log "unprivileged move refused: $(cat "$OUT/logs/unprivileged-move.err")"
    root_log "sudo -n sh -c 'echo $me > $leaf/cgroup.procs'"
    if ! sudo -n sh -c "echo $me > $leaf/cgroup.procs"; then
      root_log "  rc=1 root move failed"
      die "root move of launcher $me into $leaf failed"
    fi
    root_log "  rc=0"
  fi
  for (( i = 0; i < 20; i++ )); do
    cur=$(sed -n 's/^0:://p' "/proc/$me/cgroup")
    if [[ $cur == "$CG_LEAF_BASE/${leaf##*/}" ]]; then
      log "gate open: launcher $me in $cur"
      return 0
    fi
    sleep 0.5
  done
  die "gate timeout: launcher $me still in '$cur', expected $CG_LEAF_BASE/${leaf##*/}"
}

# mem_line: "MemAvailable_kB psi_some_avg10 psi_full_avg10" from the host now
mem_line() {
  local avail some full
  avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  some=$(awk '$1 == "some" {sub("avg10=", "", $2); print $2}' /proc/pressure/memory)
  full=$(awk '$1 == "full" {sub("avg10=", "", $2); print $2}' /proc/pressure/memory)
  printf '%s %s %s\n' "$avail" "${some:-na}" "${full:-na}"
}

# gate_open LINE: true when MemAvailable >= MEM_MIN_KIB and PSI some avg10 < PSI_MAX
gate_open() {
  local avail some _
  read -r avail some _ <<<"$1"
  awk -v a="$avail" -v s="$some" -v m="$MEM_MIN_KIB" -v p="$PSI_MAX" \
    'BEGIN {exit !(a >= m && s < p)}'
}

# wait_gate: before each services-on run. Re-check every 30 s, up to GATE_TIMEOUT_S, then die.
wait_gate() {
  local t=0 line
  while :; do
    line=$(mem_line)
    if gate_open "$line"; then
      log "gate open before run: MemAvailable_kB psi_some psi_full = $line"
      return 0
    fi
    if (( t >= GATE_TIMEOUT_S )); then
      die "memory gate still closed after ${GATE_TIMEOUT_S} s: $line"
    fi
    log "memory gate closed ($line), waiting 30 s"
    sleep 30
    t=$(( t + 30 ))
  done
}

# idle_window SECONDS: sleep the idle window, watching the gate every 10 s. Sets WIN_OK (1 when
# the gate held at every check), WIN_MIN_AVAIL (kB) and WIN_MAX_PSI (some avg10).
idle_window() {
  local end=$(( SECONDS + $1 )) line avail some
  WIN_OK=1
  WIN_MIN_AVAIL=
  WIN_MAX_PSI=0
  while (( SECONDS < end )); do
    sleep 10
    line=$(mem_line)
    read -r avail some _ <<<"$line"
    if [[ -z $WIN_MIN_AVAIL || $avail -lt $WIN_MIN_AVAIL ]]; then WIN_MIN_AVAIL=$avail; fi
    WIN_MAX_PSI=$(awk -v a="$WIN_MAX_PSI" -v b="$some" 'BEGIN {print (b > a ? b : a)}')
    if ! gate_open "$line"; then
      WIN_OK=0
      log "memory gate closed inside the idle window: $line"
    fi
  done
}

# count_ok SWEEP CFG: counted rows (status ok) for one config
count_ok() {
  awk -F'\t' -v s="$1" -v c="$2" '$1 == s && $2 == c && $4 == "ok" {n++} END {print n + 0}' \
    "$OUT/runs.tsv"
}

# realm_swap_kib RAWJSON: VmSwap summed over the realm's processes (conmon and container), not the pause
realm_swap_kib() {
  python3 -I -c 'import json, sys
d = json.load(open(sys.argv[1]))
print(sum(p["swap_kib"] for p in d["procs"] if p["role"] != "pause"))' "$1"
}

# mask_units: the unit names in masks-base.txt, comments and blank lines removed
mask_units() {
  grep -hv '^#' "$MASKS_FILE" | sed '/^[[:space:]]*$/d'
}

# on_spec CFG: sets ON_FLAGS and ON_MOUNTS. abd is the create-time a+b+d form; base is the control.
on_spec() {
  local unit
  case $1 in
    base)
      ON_FLAGS=()
      ON_MOUNTS=()
      ;;
    abd)
      ON_FLAGS=(-e SYSTEMD_LOG_LEVEL=warning)
      ON_MOUNTS=(-v "$JOURNAL_CONF:/etc/systemd/journald.conf.d/ginn.conf:ro")
      while IFS= read -r unit; do
        ON_MOUNTS+=(-v "/dev/null:/etc/systemd/system/$unit:ro")
      done < <(mask_units)
      ;;
    *) die "unknown services-on config '$1' (abd or base)" ;;
  esac
}

# start_on CFG NAME LEAF LOGFILE: one services-on realm with the S-05r flags plus the CFG mounts
start_on() {
  on_spec "$1"
  pm run -d --name "$2" --systemd=always --cgroupns private --cgroup-parent "$3" \
    --userns keep-id --user 0:0 --network host --stop-signal SIGRTMIN+3 \
    "${ON_FLAGS[@]}" "${ON_MOUNTS[@]}" "$IMG:base" /sbin/init >"$4" 2>&1
}

# cleanup_realm NAME LEAF: remove the container, then rmdir its leaf when empty (no sudo)
cleanup_realm() {
  pm rm -f "$1" >/dev/null 2>&1 || true
  case $2 in
    "$CG_LEAF_BASE"/s05s-*)
      # crun creates a libpod-<id> child under the leaf. Remove empty children, then the leaf.
      local c
      shopt -s nullglob
      for c in "/sys/fs/cgroup$2"/libpod-*; do rmdir "$c" 2>/dev/null || true; done
      shopt -u nullglob
      rmdir "/sys/fs/cgroup$2" 2>/dev/null || true
      ;;
  esac
}

# measure_realm CONMON INIT RAWJSON: prints the 11 metric fields for the realm
measure_realm() {
  python3 -I "$S05/measure.py" --conmon "$1" --init "$2" \
    --pause-file "$SPIKE_TMPDIR/pause.pid" --spike-xdg "$SPIKE_XRD" --json-out "$3" \
    --podman-root "$GINN_SPIKE_ROOT" --podman-runroot "$GINN_SPIKE_RUNROOT" \
    --podman-tmpdir "$SPIKE_TMPDIR" --podman-runtime crun
}

# probe_realm NAME BASE: one probe of a settled realm. Raw output goes to BASE.txt, the mask
# verdicts to BASE-masks.tsv. The commands take argv only, except the fixed cap check.
probe_realm() {
  local name=$1 base=$2
  local out="$base.txt"
  : > "$out"
  {
    echo "## systemd --version"
    in_realm "$name" systemctl --version | head -n 1 || true
    echo "## /etc/debian_version"
    in_realm "$name" cat /etc/debian_version || true
    echo "## systemctl is-system-running"
    in_realm "$name" systemctl is-system-running || true
    echo "## systemctl --failed"
    in_realm "$name" systemctl --failed --no-legend || true
    echo "## running services"
    in_realm "$name" systemctl list-units --type=service --state=running --no-legend --plain || true
    echo "## systemd-analyze cat-config systemd/journald.conf"
    in_realm "$name" systemd-analyze cat-config systemd/journald.conf || true
    echo "## journalctl -b -u systemd-journald"
    in_realm "$name" journalctl -b -u systemd-journald --no-pager || true
    echo "## findmnt on the drop-in"
    in_realm "$name" findmnt -no OPTIONS,SOURCE,TARGET /etc/systemd/journald.conf.d/ginn.conf || true
    echo "## write attempt on the drop-in (expect a read-only error)"
    in_realm "$name" touch /etc/systemd/journald.conf.d/ginn.conf || true
    echo "## journal directories"
    in_realm "$name" ls -la /run/log/journal /var/log/journal || true
    echo "## journalctl --disk-usage"
    in_realm "$name" journalctl --disk-usage || true
    echo "## journal lines since boot"
    in_realm "$name" journalctl -b --no-pager | wc -l || true
    echo "## podman diff (what podman wrote into the container layer)"
    pm diff "$name" || true
  } >> "$out" 2>&1
  mask_check "$name" "$out" "$base-masks.tsv"
}

# mask_check NAME RAW TSV: systemctl show -p LoadState,ActiveState for every unit in masks-base.txt.
# The raw output is appended to RAW. A unit passes only when it prints masked and inactive.
mask_check() {
  local name=$1 raw=$2 tsv=$3 unit text load active verdict
  printf 'unit\tLoadState\tActiveState\tverdict\n' > "$tsv"
  while IFS= read -r unit; do
    text=$(in_realm "$name" systemctl show -p LoadState,ActiveState "$unit" 2>&1 || true)
    { echo "## systemctl show -p LoadState,ActiveState $unit"; printf '%s\n' "$text"; } >> "$raw"
    load=$(sed -n 's/^LoadState=//p' <<<"$text")
    active=$(sed -n 's/^ActiveState=//p' <<<"$text")
    verdict=FAIL
    [[ $load == masked && $active == inactive ]] && verdict=PASS
    printf '%s\t%s\t%s\t%s\n' "$unit" "${load:-none}" "${active:-none}" "$verdict" >> "$tsv"
  done < <(mask_units)
}

# cap_check NAME OUT: pushes 12 MB through systemd-cat and records the journal's disk usage.
cap_check() {
  local name=$1 out=$2
  {
    echo "## cap check (before)"
    in_realm "$name" journalctl --disk-usage || true
    echo "## cap check (push)"
    in_realm "$name" sh -c "$CAP_CMD" || true
    echo "## cap check (after)"
    in_realm "$name" journalctl --disk-usage || true
  } >> "$out" 2>&1
}

# ------------------------------------------------------------------ check, prepare

cmd_check() {
  init_out
  # The first podman calls under the pin. The rootless namespace start creates ginn's pause
  # when none is alive under this XDG dir.
  log "podman: $(pm --version)"
  pm unshare true
  local p comm
  p=$(pause_file_pid)
  [[ -n $p ]] || die "no pause.pid under $SPIKE_TMPDIR after the first podman call"
  ps -o pid,comm,args,rss -p "$p" | tee -a "$OUT/run.log" >&2
  comm=$(ps -o comm= -p "$p" | tr -d ' ')
  [[ $comm == catatonit ]] || die "pause $p comm is '$comm', not catatonit: stop and report"
  env_has_xdg "$p" || die "pause $p does not carry XDG_RUNTIME_DIR=$SPIKE_XRD"
  log "pause pid $p comm=$comm rss_kib=$(rss_kib "$p")"
  {
    printf 'podman: %s\n' "$(pm --version)"
    printf 'crun: %s\n' "$(crun --version | head -n 1)"
    printf 'conmon: %s\n' "$(conmon --version | head -n 1)"
    printf 'catatonit: %s (%s, sha256 %s)\n' "$("$CATATONIT" --version | head -n 1)" \
      "$(readlink -f "$CATATONIT")" "$(sha256sum "$(readlink -f "$CATATONIT")" | cut -d' ' -f1)"
    printf 'kernel: %s\n' "$(uname -r)"
    printf 'launcher cgroup: %s\n' "$(sed -n 's/^0:://p' "/proc/$BASHPID/cgroup")"
    printf 'cgroup2 mount: %s\n' "$(findmnt -no FSTYPE,OPTIONS /sys/fs/cgroup)"
  } > "$OUT/versions.txt"
  cat "$OUT/versions.txt" >&2
}

# cmd_prepare: the base image only. The services-on mitigations are mounts and environment, so
# no layered image is built.
cmd_prepare() {
  init_out
  pm pull -q "$DEBIAN" >"$OUT/logs/pull.txt" 2>&1 || die "pull failed, see $OUT/logs/pull.txt"
  {
    printf 'debian pull: %s\n' "$(pm image inspect --format '{{.Id}} {{index .RepoDigests 0}}' "$DEBIAN")"
  } > "$OUT/pins.txt"
  build_image base -f "$SPIKES/Dockerfile.s01" "$SPIKES"
  printf 'base image: %s\n' "$(pm image inspect --format '{{.Id}} {{.Size}}' "$IMG:base")" >> "$OUT/pins.txt"
  record_packages base
  log "prepare done: image $IMG:base"
}

build_image() { # tag, then podman build arguments
  local tag=$1
  shift
  pm build --network host -q -t "$IMG:$tag" "$@" >"$OUT/logs/build-$tag.txt" 2>&1 \
    || die "build $tag failed, see $OUT/logs/build-$tag.txt"
}

record_packages() { # tag
  local n size
  n=$(pm run --rm --entrypoint /bin/sh "$IMG:$1" -c 'dpkg-query -W | wc -l' 2>/dev/null | tail -n 1)
  size=$(pm image inspect --format '{{.Size}}' "$IMG:$1")
  printf '%s\t%s\t%s\n' "$1" "$n" "$size" >> "$OUT/images.tsv"
}

# ------------------------------------------------------------------ sweeps

cmd_new_sweep() {
  init_out
  local id
  id="s05s-$(date +%Y%m%dT%H%M%S)"
  printf '%s\n' "$id" > "$OUT/sweep.id"
  log "sweep id $id"
}

# on-trial CFG: one services-on start with the probe, then the realm is removed. Writes trial.tsv.
# The byte-cap check runs here only, and only when journald printed no max line.
cmd_on_trial() {
  init_out
  local cfg=${1:-} name leaf logf out outcome detail
  on_spec "$cfg"
  name="s05s-trial-$cfg"
  leaf="$CG_LEAF_BASE/s05s-trial-$cfg"
  logf="$OUT/logs/trial-$cfg.run"
  cleanup_realm "$name" "$leaf"
  wait_gate
  if start_on "$cfg" "$name" "$leaf" "$logf"; then
    out=$(settle_realm "$name" "$SETTLE_LIMIT")
    outcome=started
    detail=-
    probe_realm "$name" "$OUT/probe/trial-$cfg"
    if ! grep -qE 'Runtime Journal .*max' "$OUT/probe/trial-$cfg.txt"; then
      log "no journald max line in the trial; running the cap check"
      cap_check "$name" "$OUT/probe/trial-$cfg-cap.txt"
    fi
  else
    out=$'-\t-\t-\t-'
    outcome=podman-run-failed
    detail=$(tr '\t\n' '  ' < "$logf" | cut -c1-300)
  fi
  printf '%s\t%s\t%s\t%s\n' "$cfg" "$outcome" "$out" "$detail" >> "$OUT/trial.tsv"
  log "on-trial $cfg: $outcome | $out | $detail"
  cleanup_realm "$name" "$leaf"
  # The trial gates the sweeps: it passes only when the start settled with 0 failed units.
  local s_settle s_failed
  IFS=$'\t' read -r s_settle _ _ s_failed <<<"$out"
  [[ $outcome == started && $s_settle == ok && $s_failed == 0 ]]
}

# on CFG: services-on sweep. Attempts repeat until RUNS rows are counted (status ok). An attempt
# is counted only when the gate held through its idle window, realm VmSwap is 0, and the realm
# settled with 0 failed units.
cmd_on() {
  init_out
  local cfg=${1:-} sweep attempt=0 max_attempts ok_n name leaf logf
  local settle boot state failed con init raw row swap status line avail some full
  sweep=$(current_sweep)
  self_in_launch_leaf || die "services-on needs this launcher inside $CG_LEAF_BASE/s05s-launch; run on-leg"
  on_spec "$cfg"
  name="s05s-on-$cfg"
  leaf="$CG_LEAF_BASE/s05s-$cfg"
  max_attempts=$(( RUNS + 5 ))
  while :; do
    ok_n=$(count_ok "$sweep" "on-$cfg")
    (( ok_n >= RUNS )) && break
    attempt=$(( attempt + 1 ))
    (( attempt > max_attempts )) && die "on-$cfg: $ok_n counted rows after $max_attempts attempts; stopping"
    wait_gate
    cleanup_realm "$name" "$leaf"
    logf="$OUT/logs/on-$cfg-a$attempt.run"
    if ! start_on "$cfg" "$name" "$leaf" "$logf"; then
      append_row "$sweep" "on-$cfg" "$attempt" fail-run "$IMG:base" na - - - $NA_ROW
      cleanup_realm "$name" "$leaf"
      continue
    fi
    IFS=$'\t' read -r settle boot state failed < <(settle_realm "$name" "$SETTLE_LIMIT")
    if [[ $settle != ok || $failed != 0 ]]; then
      append_row "$sweep" "on-$cfg" "$attempt" unsettled "$IMG:base" "$settle" "$boot" "$state" "$failed" $NA_ROW
      log "on-$cfg attempt $attempt not settled: settle=$settle state=$state failed=$failed"
      cleanup_realm "$name" "$leaf"
      continue
    fi
    # The first settled attempt of each config is probed (journald, drop-in, unit masks).
    if [[ ! -s $OUT/probe/on-$cfg.txt ]]; then
      probe_realm "$name" "$OUT/probe/on-$cfg"
    fi
    idle_window "$WINDOW"
    line=$(mem_line)
    read -r avail some full <<<"$line"
    con=$(pm inspect --format '{{.State.ConmonPid}}' "$name")
    init=$(pm inspect --format '{{.State.Pid}}' "$name")
    raw="$OUT/raw/on-$cfg-a$attempt.json"
    row=$(measure_realm "$con" "$init" "$raw") \
      || die "measurement invalid for on-$cfg attempt $attempt (pause check or accounting)"
    swap=$(realm_swap_kib "$raw")
    if (( WIN_OK == 1 )) && [[ $swap == 0 ]]; then status=ok; else status=excluded-pressure; fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$sweep" "on-$cfg" "$attempt" \
      "$(date +%T)" "$avail" "$some" "$full" "$swap" "$WIN_OK" "$WIN_MIN_AVAIL" "$WIN_MAX_PSI" \
      "$status" >> "$OUT/on-samples.tsv"
    append_row "$sweep" "on-$cfg" "$attempt" "$status" "$IMG:base" "$settle" "$boot" "$state" "$failed" $row
    cleanup_realm "$name" "$leaf"
    log "on-$cfg attempt $attempt: status=$status settle=$settle boot=${boot}s state=$state failed=$failed realm_vmswap_kib=$swap window_ok=$WIN_OK $row"
  done
}

# on-leg: the whole services-on leg. The launcher subshell moves itself into s05s-launch (the root
# write only if the unprivileged move is refused), then runs check, prepare, a new sweep, the
# a+b+d trial, the a+b+d sweep and the base control sweep. The parent tears down afterwards.
cmd_on_leg() {
  init_out
  local leaf="$CG_DIR_BASE/s05s-launch" rc=0
  mkdir -p "$leaf"
  (
    move_self_into_leaf "$leaf"
    cmd_check
    cmd_prepare
    cmd_new_sweep
    cmd_on_trial abd || { log "services-on trial did not settle; no sweep run"; exit 3; }
    cmd_on abd
    cmd_on base
  ) || rc=$?
  log "services-on leg exit status $rc; tearing down"
  cmd_teardown || log "teardown reported a problem, see the log"
  return "$rc"
}

# settle_realm is in settle.sh. The realm settles at running or degraded with 0 failed units.

# ------------------------------------------------------------------ reporting, teardown

cmd_summary() { # [SWEEP_ID]: defaults to the current sweep
  [[ -f $OUT/runs.tsv ]] || die "no runs.tsv"
  local sweep=${1:-}
  [[ -n $sweep ]] || sweep=$(current_sweep)
  python3 -I "$S05/measure.py" --summarize "$OUT/runs.tsv" --sweep "$sweep"
}

# split: RssAnon, RssFile and RssShmem per realm and pause for every raw sample
cmd_split() {
  shopt -s nullglob
  local raws=("$OUT"/raw/*.json)
  shopt -u nullglob
  (( ${#raws[@]} > 0 )) || die "no raw samples under $OUT/raw"
  python3 -I "$S05/measure.py" --split-raw "${raws[@]}" > "$OUT/split.tsv"
  log "split: ${#raws[@]} raw samples -> $OUT/split.tsv"
}

cmd_nfr001() {
  local p comm tag
  printf 'pid\tcomm\ttag\n'
  for p in $(ps -u "$UID" -o pid=); do
    comm=$(ps -o comm= -p "$p" 2>/dev/null | tr -d ' ') || continue
    case $comm in
      conmon|podman*|catatonit|crun|runc|pasta|passt|rootlessport|netavark|aardvark-dns) ;;
      *) continue ;;
    esac
    if env_has_xdg "$p"; then tag=s05s; else tag=other; fi
    printf '%s\t%s\t%s\n' "$p" "$comm" "$tag"
  done
  return 0
}

cmd_teardown() {
  under_s05s "$GINN_SPIKE_ROOT" && under_s05s "$GINN_SPIKE_RUNROOT" \
    || die "teardown refused: GINN_SPIKE_ROOT and GINN_SPIKE_RUNROOT must be under .../ginn-spike-s05s/"
  local p d c left
  if [[ -d $GINN_SPIKE_ROOT || -d $GINN_SPIKE_RUNROOT ]]; then
    pm rm -af >/dev/null 2>&1 || true
    pm rmi -af >/dev/null 2>&1 || true
    # Runs while the pause is alive, so unshare does not need to start one.
    if [[ -d $GINN_SPIKE_ROOT ]]; then
      pm unshare rm -rf "$GINN_SPIKE_ROOT" >/dev/null 2>&1 || true
    fi
  fi
  # Stop the spike pause by exact pid, after its environ check.
  p=$(pause_file_pid)
  if [[ -n $p ]]; then stop_spike_pause "$p" || log "spike pause $p left running, check by hand"; fi
  if [[ -e $GINN_SPIKE_ROOT ]]; then rm_spike_tree "$GINN_SPIKE_ROOT" || true; fi
  if [[ -e $GINN_SPIKE_RUNROOT ]]; then rm_spike_tree "$GINN_SPIKE_RUNROOT" || true; fi
  d=$(dirname "$GINN_SPIKE_RUNROOT")
  rmdir "$d" 2>/dev/null || true
  rmdir "$(dirname "$GINN_SPIKE_ROOT")" 2>/dev/null || true
  if [[ -e $SPIKE_XRD ]]; then rm_spike_tree "$SPIKE_XRD" || true; fi
  # Leaves this run created under /sys/fs/cgroup/ginn/<uid>. rmdir only, no sudo.
  shopt -s nullglob
  for d in "$CG_DIR_BASE"/s05s-*; do
    for c in "$d"/libpod-*; do rmdir "$c" 2>/dev/null || true; done
    if rmdir "$d" 2>/dev/null; then log "removed cgroup leaf $d"; else log "leaf kept, not empty: $d"; fi
  done
  shopt -u nullglob
  left=$(cmd_nfr001 | awk -F'\t' '$3 == "s05s"' | wc -l)
  log "teardown done: S-05s processes left = $left"
  [[ $left == 0 ]] || die "S-05s processes remain (see nfr001)"
}

# ------------------------------------------------------------------ dispatch

main() {
  case ${1:-} in
    check|prepare|new-sweep|on-trial|on|on-leg|summary|split|nfr001|teardown) ;;
    *) die "usage: XDG_RUNTIME_DIR=$SPIKE_XRD $0 check|prepare|new-sweep|on-trial abd|base|on abd|base|on-leg|summary [SWEEP]|split|nfr001|teardown" ;;
  esac
  if [[ $1 != summary ]]; then
    require_pin
  fi
  case $1 in
    check|prepare|on-trial|on|on-leg) make_dirs ;;
  esac
  case $1 in
    check) cmd_check ;;
    prepare) cmd_prepare ;;
    new-sweep) cmd_new_sweep ;;
    on-trial) cmd_on_trial "${2:-}" ;;
    on) cmd_on "${2:-}" ;;
    on-leg) cmd_on_leg ;;
    summary) cmd_summary "${2:-}" ;;
    split) cmd_split ;;
    nfr001) cmd_nfr001 ;;
    teardown) cmd_teardown ;;
  esac
}

main "$@"
