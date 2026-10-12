#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# S-01 crun leg (risk R-001): can systemd run as PID 1 under podman --runtime crun, with the
# container cgroup under the root-created delegated subtree /sys/fs/cgroup/ginn/<uid>?
# Debian only: same Dockerfile (spikes/Dockerfile.s01) and cgroupfs manager as the runc leg.
#
#   XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s01c-xrd spikes/s01c_crun.sh run
#       Pull and build the image, record host facts, then try the roadmap services-on argv
#       from the unmoved launcher. Exit 3 when podman refuses: the launcher is not in a leaf
#       that its podman can write into, so no boot is measured.
#   XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s01c-xrd spikes/s01c_crun.sh teardown
#       Remove spike containers, image, storage, the pinned dir and the leaves this script makes.
#
# Root: this script never runs sudo. Moving the launcher into $LAUNCH_LEAF needs one root write
# to $LAUNCH_LEAF/cgroup.procs. The brief does not allow that write, so it is not run here.
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
LAUNCH_LEAF="$CG_USER/s01crun-launch"
IMAGE=localhost/ginn-s01:debian-stable
SPIKE_XRD="/run/user/$MY_UID/ginn-spike-s01c-xrd"
STORE="${GINN_S01C_STORE:-${XDG_DATA_HOME:-$HOME/.local/share}/ginn-spike-s01c/podman}"
RUNROOT="$SPIKE_XRD/runroot"
TMPDIR_P="$SPIKE_XRD/libpod-tmp"
OUT="${GINN_S01C_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s01c}"

log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

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
    free -m
  } >"$OUT/host-facts.txt" 2>&1
}

# remove_leaves: rmdir only the leaves this script creates, deepest first. Other leaves are left alone.
remove_leaves() {
  local leaf
  for leaf in "$CG_USER/s01crun" "$LAUNCH_LEAF"; do
    [[ -d $leaf ]] || continue
    find "$leaf" -mindepth 1 -depth -type d -exec rmdir {} + 2>>"$OUT/teardown.err" || true
    if rmdir "$leaf" 2>>"$OUT/teardown.err"; then log "rmdir $leaf"; else log "rmdir $leaf failed (teardown.err)"; fi
  done
  return 0
}

# boot <name>: the roadmap services-on argv with --runtime crun. Output in $OUT/<name>.{out,err,rc}.
boot() {
  local name=$1 rc=0
  "$SELF" --podman rm -f "$name" >/dev/null 2>&1 || true
  "$SELF" --podman run -d --name "$name" --runtime crun \
    --systemd=always --cgroupns private --cgroup-parent "/ginn/$MY_UID/$name" \
    --user 0:0 --stop-signal SIGRTMIN+3 --stop-timeout 30 \
    "$IMAGE" /sbin/init >"$OUT/$name.out" 2>"$OUT/$name.err" || rc=$?
  echo "$rc" >"$OUT/$name.rc"
  return "$rc"
}

# measure <name>: run only after boot succeeded. Not exercised in the blocked run.
measure() {
  local name=$1 d="$OUT/$1" t0 t1 pid wrc=0
  mkdir -p "$d"
  timeout 60 "$SELF" --podman exec "$name" systemctl is-system-running --wait >"$d/wait.txt" 2>&1 || wrc=$?
  echo "$wrc" >"$d/wait.rc"
  "$SELF" --podman exec "$name" systemctl is-system-running >"$d/state.txt" 2>&1 || true
  "$SELF" --podman exec "$name" systemctl --failed --no-legend >"$d/failed.txt" 2>&1 || true
  "$SELF" --podman exec "$name" systemd-analyze time >"$d/systemd-analyze.txt" 2>&1 || true
  "$SELF" --podman exec "$name" cat /proc/1/cgroup >"$d/pid1-cgroup.txt" 2>&1 || true
  "$SELF" --podman exec "$name" systemctl --version >"$d/systemd-version.txt" 2>&1 || true
  pid=$("$SELF" --podman inspect --format '{{.State.Pid}}' "$name" 2>/dev/null || echo 0)
  echo "host init pid $pid: $(cat "/proc/$pid/cgroup" 2>/dev/null || echo gone)" >"$d/host-cgroup.txt"
  t0=$(date +%s.%N)
  "$SELF" --podman stop "$name" >"$d/stop.txt" 2>&1 || true
  t1=$(date +%s.%N)
  "$SELF" --podman inspect --format '{{.State.ExitCode}} oomkilled={{.State.OOMKilled}}' "$name" \
    >"$d/exit.txt" 2>&1 || true
  printf 'stop elapsed %ss\n' "$(awk -v a="$t0" -v b="$t1" 'BEGIN {printf "%.1f", b - a}')" >>"$d/exit.txt"
  log "measure $name: state=$(head -1 "$d/state.txt") failed=$(grep -c . "$d/failed.txt" || true)"
}

cmd_run() {
  local rc=0
  require_caller_xdg
  mkdir -p "$OUT" "$STORE" "$SPIKE_XRD" "$RUNROOT" "$TMPDIR_P"
  chmod 700 "$SPIKE_XRD"
  host_facts
  [[ -d $CG_USER && -O $CG_USER ]] || die "$CG_USER missing or not owned by uid $MY_UID"
  # User mkdir under ginn/<uid> is allowed: no root.
  mkdir -p "$LAUNCH_LEAF"
  log "leaf $LAUNCH_LEAF owner $(stat -c %U "$LAUNCH_LEAF")"
  log "pull docker.io/library/debian:stable"
  "$SELF" --podman pull docker.io/library/debian:stable >"$OUT/pull.txt" 2>&1
  log "build $IMAGE from spikes/Dockerfile.s01"
  "$SELF" --podman build --network host -t "$IMAGE" -f "$SPIKE_DIR/Dockerfile.s01" "$SPIKE_DIR" \
    >"$OUT/build.txt" 2>&1
  "$SELF" --podman image inspect --format '{{.Id}} {{.RepoDigests}}' "$IMAGE" >"$OUT/image.txt" 2>&1 || true
  log "boot s01crun from the unmoved launcher (no root)"
  if boot s01crun; then
    log "boot accepted, measuring"
    measure s01crun
  else
    rc=$?
    log "BLOCKED: podman run exit $rc: $(head -c 300 "$OUT/s01crun.err" | tr '\n' ' ')"
    log "not run, outside the brief's root allowance: sudo sh -c 'echo <pid of podman launcher> > $LAUNCH_LEAF/cgroup.procs'"
    return 3
  fi
  return "$rc"
}

cmd_teardown() {
  require_caller_xdg
  mkdir -p "$OUT"
  if [[ -d $SPIKE_XRD ]]; then
    "$SELF" --podman rm -af >/dev/null 2>&1 || true
    "$SELF" --podman rmi -af >/dev/null 2>&1 || true
    # unshare maps subuid-owned storage files so rm can delete them; see spikes/s01_systemd_rootless.sh.
    "$SELF" --podman unshare rm -rf "$STORE" 2>/dev/null || true
  fi
  rm -rf "$STORE" 2>/dev/null || log "storage removal incomplete: $STORE"
  rmdir "$(dirname "$STORE")" 2>/dev/null || true
  kill_spike_procs
  remove_leaves
  rm -rf "$SPIKE_XRD"
  log "verify pinned dir: $(ls -d "$SPIKE_XRD" 2>&1)"
  log "verify storage: $(ls -d "$STORE" 2>&1)"
  log "verify leaves: $(ls -d "$LAUNCH_LEAF" "$CG_USER/s01crun" 2>&1 | tr '\n' ' ')"
  log "verify spike pids: [$(spike_pids | tr '\n' ' ')]"
  log "teardown done; logs kept in $OUT"
}

case ${1:-} in
  run) cmd_run ;;
  teardown) cmd_teardown ;;
  --podman) shift; podman_pinned "$@" ;;
  *) echo "usage: XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s01c-xrd $0 run | teardown" >&2; exit 2 ;;
esac
