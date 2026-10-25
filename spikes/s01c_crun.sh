#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# S-01 crun leg (risk R-001) and its runc control: can systemd run as PID 1 under podman, with the
# container cgroup under the root-created delegated subtree /sys/fs/cgroup/ginn/<uid>?
# Debian only: same Dockerfile (spikes/Dockerfile.s01) and cgroupfs manager for both runtimes.
#
#   XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s01c-xrd spikes/s01c_crun.sh run
#       Pull and build the image, record host facts, then run two legs (crun, runc). Each leg
#       starts a launcher subshell that writes its $BASHPID to $OUT/<runtime>.launcher.pid and
#       waits at the gate until /proc/$BASHPID/cgroup shows /ginn/<uid>/s01<runtime>-launch.
#       Root places that pid with one logged write per leg (spikes/results/S-01.md). After the
#       gate opens, each leg boots the roadmap argv and the runc S-01 variant-a flags, measures,
#       stops, and removes its containers.
#   XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s01c-xrd spikes/s01c_crun.sh teardown
#       Kill the spike pause process, remove spike containers, image, storage, the pinned dir and
#       only the leaves named in this script. Writes nothing to the cgroup root or to ginn/<uid>.
#
# Root: none in this script. The root write is run by the operator per leg, outside this file.
#
# Pin: every podman call goes through "$0" --podman. The wrapper refuses unless the pinned XDG dir
# is this uid's, mode 0700, at the exact path, then runs podman with XDG_RUNTIME_DIR set to it.
# --root, --runroot and --tmpdir point at this spike's storage and runroot. The rootless pause
# lookup follows XDG_RUNTIME_DIR, so the pin keeps this spike off the user's pause process.
set -euo pipefail

SELF="$(readlink -f "$0")"
SPIKE_DIR="$(dirname "$SELF")"
MY_UID="$(id -u)"
CG_ROOT=/sys/fs/cgroup
CG_USER="$CG_ROOT/ginn/$MY_UID"
IMAGE=localhost/ginn-s01:debian-stable
SPIKE_XRD="/run/user/$MY_UID/ginn-spike-s01c-xrd"
STORE_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/ginn-spike-s01c"
STORE="${GINN_S01C_STORE:-$STORE_BASE/podman}"
RUNROOT="$SPIKE_XRD/runroot"
TMPDIR_P="$SPIKE_XRD/libpod-tmp"
OUT="${GINN_S01C_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s01c}"
GATE_TIMEOUT=900

log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# check_under BASE PATH: abort unless PATH is BASE or lies under it, with no ".." in it. Both
# arguments must be non-empty, so an unset variable can never widen a later rm -rf.
check_under() {
  local base=${1:?check_under: base is empty} p=${2:?check_under: path is empty}
  case $p in *..*) die "refusing rm of '$p': path contains '..'" ;; esac
  case $p in
    "$base"|"$base"/*) ;;
    *) die "refusing rm of '$p': not under $base" ;;
  esac
}

# require_pin: refuse unless the pinned XDG dir is this uid's own 0700 directory at the exact path.
require_pin() {
  [[ $SPIKE_XRD == "/run/user/$MY_UID/ginn-spike-s01c-xrd" ]] || die "refusing: pin is '$SPIKE_XRD'"
  [[ -d $SPIKE_XRD && ! -L $SPIKE_XRD ]] || die "refusing: pinned dir $SPIKE_XRD does not exist"
  [[ $(stat -c '%u %a' "$SPIKE_XRD") == "$MY_UID 700" ]] \
    || die "refusing: $SPIKE_XRD is not uid $MY_UID with mode 700"
}

# require_caller_xdg: entry points refuse unless the caller already exports the pin.
require_caller_xdg() {
  [[ ${XDG_RUNTIME_DIR:-} == "$SPIKE_XRD" ]] \
    || die "refusing: XDG_RUNTIME_DIR='${XDG_RUNTIME_DIR:-unset}', must be $SPIKE_XRD"
}

# podman_pinned: the only way podman runs here. Guard first, then pin XDG_RUNTIME_DIR for the call.
podman_pinned() {
  require_pin
  exec env XDG_RUNTIME_DIR="$SPIKE_XRD" podman --root "$STORE" \
    --runroot "$RUNROOT" --tmpdir "$TMPDIR_P" "$@"
}

# is_spike_pid <pid>: true when the process environ has the exact pinned XDG_RUNTIME_DIR.
is_spike_pid() {
  [[ -r /proc/$1/environ ]] || return 1
  tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null | grep -qx "XDG_RUNTIME_DIR=$SPIKE_XRD"
}

# spike_pids: pids of this uid carrying the pinned XDG, excluding this shell and its ancestors.
spike_pids() {
  local d p cur=$BASHPID ancestry=" $$ $BASHPID "
  while (( cur > 1 )); do
    ancestry+="$cur "
    cur=$(ps -o ppid= -p "$cur" 2>/dev/null | tr -d ' ') || break
    [[ -n $cur ]] || break
  done
  for d in /proc/[0-9]*; do
    p=${d#/proc/}
    [[ $ancestry == *" $p "* ]] && continue
    if is_spike_pid "$p"; then echo "$p"; fi
  done
  return 0
}

# kill_spike_procs: TERM, then KILL. Each exact pid is re-checked against the pinned XDG first.
kill_spike_procs() {
  local p
  for p in $(spike_pids); do
    if is_spike_pid "$p"; then log "TERM spike pid $p"; kill -TERM "$p" 2>/dev/null || true; fi
  done
  sleep 3
  for p in $(spike_pids); do
    if is_spike_pid "$p"; then log "KILL spike pid $p (alive after TERM)"; kill -KILL "$p" 2>/dev/null || true; fi
  done
  return 0
}

# host_facts: versions, cgroup state and launcher placement, written before any podman call.
host_facts() {
  {
    echo "date: $(date -Is) uid=$MY_UID"
    echo "pinned XDG: $SPIKE_XRD"
    echo "root subtree_control: [$(cat "$CG_ROOT/cgroup.subtree_control")]"
    echo "ginn/$MY_UID subtree_control: [$(cat "$CG_USER/cgroup.subtree_control")]"
    echo "ginn/$MY_UID owner and mode: $(stat -c '%U:%G %a' "$CG_USER")"
    echo "ginn/$MY_UID cgroup.procs: [$(cat "$CG_USER/cgroup.procs")]"
    echo "launcher cgroup: $(sed -n 's/^0:://p' /proc/self/cgroup)"
    echo "kernel: $(uname -r)"
    "$SELF" --podman version 2>&1 || true
    "$SELF" --podman info 2>&1 || true
    echo "crun: $(crun --version 2>&1 | head -2 | tr '\n' ' ')" || true
    echo "runc: $(runc --version 2>&1 | head -1)" || true
    echo "conmon: $(/usr/libexec/podman/conmon --version 2>&1 | head -1)" || true
    echo "pasta: $(command -v pasta || echo none)"
    free -m
  } >"$OUT/host-facts.txt" 2>&1
}

# name_for <runtime> <flavor>: container and cgroup leaf name. Each starts with s01crun or s01runc.
name_for() {
  case "$1-$2" in
    crun-roadmap) echo s01crun ;;
    crun-runca) echo s01crunm ;;
    runc-roadmap) echo s01runc ;;
    runc-runca) echo s01runcm ;;
    *) die "no name for $1 $2" ;;
  esac
}

# boot_argv <runtime> <flavor> <name>: sets BOOT_ARGV and STOP_ARGV.
#   roadmap: the services-on argv from docs/roadmap.md, with --stop-timeout 30 at create time.
#   runca:   the runc S-01 variant-a flags (--userns keep-id --network host), stop with -t 30.
boot_argv() {
  local runtime=$1 flavor=$2 name=$3
  if [[ $flavor == roadmap ]]; then
    BOOT_ARGV=(run -d --name "$name" --runtime "$runtime" --systemd=always --cgroupns private
      --cgroup-parent "/ginn/$MY_UID/$name" --user 0:0 --stop-signal SIGRTMIN+3 --stop-timeout 30
      "$IMAGE" /sbin/init)
    STOP_ARGV=(stop "$name")
  else
    BOOT_ARGV=(run -d --name "$name" --runtime "$runtime" --systemd=always --cgroupns private
      --userns keep-id --user 0:0 --network host --stop-signal SIGRTMIN+3
      --cgroup-parent "/ginn/$MY_UID/$name" "$IMAGE" /sbin/init)
    STOP_ARGV=(stop -t 30 "$name")
  fi
}

# wait_for_move <leaf>: the gate. Reads this subshell's own cgroup; podman starts only after it matches.
wait_for_move() {
  local leaf=$1 me=$BASHPID i cur=""
  for (( i = 0; i < GATE_TIMEOUT; i++ )); do
    cur=$(sed -n 's/^0:://p' "/proc/$me/cgroup")
    if [[ $cur == "/ginn/$MY_UID/$leaf" ]]; then
      log "gate open: launcher $me in $cur"
      return 0
    fi
    sleep 1
  done
  die "gate timeout: launcher $me still in '$cur', expected /ginn/$MY_UID/$leaf"
}

# measure_one <runtime> <flavor>: boot, wait, record, stop, remove. One row in summary.tsv.
measure_one() {
  local runtime=$1 flavor=$2 name d rc=0 wrc=0 t0 t1 t2 t3 pid cg state failed stop_s code
  name=$(name_for "$runtime" "$flavor")
  d="$OUT/$name"
  mkdir -p "$d"
  boot_argv "$runtime" "$flavor" "$name"
  printf '%q ' "${BOOT_ARGV[@]}" >"$d/boot-argv.txt"; echo >>"$d/boot-argv.txt"
  printf '%q ' "${STOP_ARGV[@]}" >"$d/stop-argv.txt"; echo >>"$d/stop-argv.txt"
  "$SELF" --podman rm -f "$name" >/dev/null 2>&1 || true
  log "boot $name ($runtime, $flavor)"
  t0=$(date +%s.%N)
  "$SELF" --podman "${BOOT_ARGV[@]}" >"$d/run.out" 2>"$d/run.err" || rc=$?
  echo "$rc" >"$d/run.rc"
  if (( rc != 0 )); then
    printf '%s\t%s\t%s\tboot-failed rc=%s: %s\n' "$runtime" "$flavor" "$name" "$rc" \
      "$(head -c 200 "$d/run.err" | tr '\n\t' '  ')" >>"$OUT/summary.tsv"
    log "$name: podman run exit $rc"
    return 0
  fi
  timeout 60 "$SELF" --podman exec "$name" systemctl is-system-running --wait \
    >"$d/wait.txt" 2>&1 || wrc=$?
  t1=$(date +%s.%N)
  echo "$wrc" >"$d/wait.rc"
  "$SELF" --podman exec "$name" systemctl is-system-running >"$d/state.txt" 2>&1 || true
  "$SELF" --podman exec "$name" systemctl --failed --no-legend >"$d/failed.txt" 2>&1 || true
  "$SELF" --podman exec "$name" systemd-analyze time >"$d/systemd-analyze.txt" 2>&1 || true
  "$SELF" --podman exec "$name" cat /proc/1/cgroup >"$d/pid1-cgroup.txt" 2>&1 || true
  pid=$("$SELF" --podman inspect --format '{{.State.Pid}}' "$name" 2>/dev/null || echo 0)
  cg=$(sed -n 's/^0:://p' "/proc/$pid/cgroup" 2>/dev/null || echo gone)
  echo "host init pid $pid cgroup $cg" >"$d/host-cgroup.txt"
  t2=$(date +%s.%N)
  "$SELF" --podman "${STOP_ARGV[@]}" >"$d/stop.txt" 2>&1 || true
  t3=$(date +%s.%N)
  code=$("$SELF" --podman inspect --format '{{.State.ExitCode}} oomkilled={{.State.OOMKilled}}' \
    "$name" 2>/dev/null || echo n/a)
  state=$(head -1 "$d/state.txt" 2>/dev/null || echo n/a)
  failed=$(grep -c . "$d/failed.txt" 2>/dev/null || true)
  stop_s=$(awk -v a="$t2" -v b="$t3" 'BEGIN {printf "%.1f", b - a}')
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$runtime" "$flavor" "$name" "$state" "${failed:-0}" \
    "$(awk -v a="$t0" -v b="$t1" 'BEGIN {printf "%.1f", b - a}')" "$cg" "$stop_s" "$code" \
    >>"$OUT/summary.tsv"
  log "$name: state=$state failed=${failed:-0} boot=$(awk -v a="$t0" -v b="$t1" 'BEGIN {printf "%.1f", b - a}')s stop=${stop_s}s exit=$code"
  "$SELF" --podman rm -f "$name" >/dev/null 2>&1 || true
}

# leg <runtime>: the launcher subshell. Waits at the gate, then runs both flavors under it.
leg() {
  local runtime=$1 leaf="s01${1}-launch"
  mkdir -p "$CG_USER/$leaf"
  log "leg $runtime: leaf $CG_USER/$leaf owner $(stat -c %U "$CG_USER/$leaf")"
  (
    echo "$BASHPID" >"$OUT/$runtime.launcher.pid"
    log "leg $runtime: launcher pid $BASHPID in $(sed -n 's/^0:://p' "/proc/$BASHPID/cgroup"); waiting for root move to /ginn/$MY_UID/$leaf"
    wait_for_move "$leaf"
    measure_one "$runtime" roadmap
    measure_one "$runtime" runca
  )
}

# remove_leaves: rmdir only the leaves named here, deepest first. Never ginn or ginn/<uid>.
remove_leaves() {
  local leaf
  for leaf in s01crun s01crunm s01runc s01runcm s01crun-launch s01runc-launch; do
    [[ -d $CG_USER/$leaf ]] || continue
    find "$CG_USER/$leaf" -mindepth 1 -depth -type d -exec rmdir {} + 2>>"$OUT/teardown.err" || true
    if rmdir "$CG_USER/$leaf" 2>>"$OUT/teardown.err"; then log "rmdir $CG_USER/$leaf"; else log "rmdir $CG_USER/$leaf failed (teardown.err)"; fi
  done
  return 0
}

cmd_run() {
  require_caller_xdg
  mkdir -p "$OUT" "$STORE" "$SPIKE_XRD" "$RUNROOT" "$TMPDIR_P"
  chmod 700 "$SPIKE_XRD"
  printf 'runtime\tflavor\tname\tstate\tfailed_units\tboot_s\tcontainer_cgroup\tstop_s\texit\n' \
    >"$OUT/summary.tsv"
  host_facts
  [[ -d $CG_USER && -O $CG_USER ]] || die "$CG_USER missing or not owned by uid $MY_UID"
  log "pull docker.io/library/debian:stable"
  "$SELF" --podman pull docker.io/library/debian:stable >"$OUT/pull.txt" 2>&1
  log "build $IMAGE from spikes/Dockerfile.s01"
  "$SELF" --podman build --network host -t "$IMAGE" -f "$SPIKE_DIR/Dockerfile.s01" "$SPIKE_DIR" \
    >"$OUT/build.txt" 2>&1
  "$SELF" --podman image inspect --format '{{.Id}} {{.RepoDigests}}' "$IMAGE" >"$OUT/image.txt" 2>&1 || true
  leg crun
  leg runc
  log "done; summary $OUT/summary.tsv"
  cat "$OUT/summary.tsv"
}

cmd_teardown() {
  require_caller_xdg
  mkdir -p "$OUT"
  if [[ -d $SPIKE_XRD ]]; then
    "$SELF" --podman rm -af >/dev/null 2>&1 || true
    "$SELF" --podman rmi -af >/dev/null 2>&1 || true
    # unshare maps subuid-owned storage files so rm can delete them; see spikes/s01_systemd_rootless.sh.
    check_under "$STORE_BASE" "$STORE"
    "$SELF" --podman unshare rm -rf "$STORE" 2>/dev/null || true
  fi
  check_under "$STORE_BASE" "$STORE"
  rm -rf "$STORE" 2>/dev/null || log "storage removal incomplete: $STORE"
  rmdir "$(dirname "$STORE")" 2>/dev/null || true
  # Processes first: the pause process sits in a launcher leaf, and a leaf can only be removed empty.
  kill_spike_procs
  remove_leaves
  check_under "/run/user/$MY_UID/ginn-spike-s01c-xrd" "$SPIKE_XRD"
  rm -rf "$SPIKE_XRD"
  log "verify pinned dir: $(ls -d "$SPIKE_XRD" 2>&1)"
  log "verify storage: $(ls -d "$STORE" 2>&1)"
  log "verify leaves: $(ls -d "$CG_USER"/s01crun* "$CG_USER"/s01runc* 2>&1 | tr '\n' ' ')"
  spike_pids >"$OUT/verify-pids.txt"
  log "verify spike pids: [$(tr '\n' ' ' <"$OUT/verify-pids.txt")]"
  log "teardown done (no root writes); logs kept in $OUT"
}

case ${1:-} in
  run) cmd_run ;;
  teardown) cmd_teardown ;;
  --podman) shift; podman_pinned "$@" ;;
  *) echo "usage: XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s01c-xrd $0 run | teardown" >&2; exit 2 ;;
esac
