#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Three-flag rule: every podman call (teardown included) passes --root, --runroot and --tmpdir,
# all inside this spike's directories. Without --tmpdir podman shares the user's libpod tmpdir.
# XDG pin: every podman call also runs with XDG_RUNTIME_DIR set to $SPIKE_XRD. The three flags do
# not move the rootless pause lookup, which follows XDG_RUNTIME_DIR. Earlier runs of this script
# did not pin it (see spikes/results/S-01.md, Disclosures).
#
# S-01 (risk R-001): can systemd run as PID 1 in a rootless podman container
# under a root-created, user-owned, controller-delegated cgroup subtree?
#
#   spikes/s01_systemd_rootless.sh run        run the procedure
#   spikes/s01_systemd_rootless.sh teardown   remove spike containers, image,
#                                             storage and cgroup directories
#
# Root steps run only as: sudo "$0" --root-setup|--root-move ...
# Every user-level podman call uses the spike storage (--root, --runroot, --tmpdir), never
# the default storage. Logs go to GINN_SPIKE_OUT (default
# ~/.cache/ginn-spike/s01).
set -euo pipefail

SELF="$(readlink -f "$0")"
SPIKE_DIR="$(dirname "$SELF")"
CG_ROOT=/sys/fs/cgroup
CTL_SET=(cpu memory pids io cpuset)
IMAGE=localhost/ginn-s01:debian-stable
SPIKE_XRD="/run/user/$(id -u)/ginn-spike-s01-xrd"
GINN_SPIKE_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/ginn-spike"
GINN_SPIKE_ROOT="${GINN_SPIKE_ROOT:-$GINN_SPIKE_BASE/podman}"
GINN_SPIKE_RUNROOT="$SPIKE_XRD/runroot"
GINN_SPIKE_TMP="$SPIKE_XRD/libpod-tmp"
OUT="${GINN_SPIKE_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s01}"
export GINN_SPIKE_ROOT
# Every podman call goes through "$SELF --podman", which runs the guard and pins XDG_RUNTIME_DIR.
PM=("$SELF" --podman)
COMMON_FLAGS=(--systemd=always --cgroupns private --userns keep-id --user 0:0
              --network host --stop-signal SIGRTMIN+3)

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
need_root() { [[ $(id -u) -eq 0 ]] || die "$1 must run as root (sudo)"; }

# require_pin: refuse unless the pinned XDG dir is this uid's own 0700 directory at the exact path.
require_pin() {
  [[ $SPIKE_XRD == "/run/user/$(id -u)/ginn-spike-s01-xrd" ]] || die "refusing: pin is '$SPIKE_XRD'"
  [[ -d $SPIKE_XRD && ! -L $SPIKE_XRD ]] || die "refusing: pinned dir $SPIKE_XRD does not exist"
  [[ $(stat -c '%u %a' "$SPIKE_XRD") == "$(id -u) 700" ]] \
    || die "refusing: $SPIKE_XRD is not uid $(id -u) with mode 700"
}

# require_caller_xdg: user-level entry points refuse unless the caller already exports the pin.
require_caller_xdg() {
  [[ ${XDG_RUNTIME_DIR:-} == "$SPIKE_XRD" ]] \
    || die "refusing: XDG_RUNTIME_DIR='${XDG_RUNTIME_DIR:-unset}', must be $SPIKE_XRD"
}

# podman_pinned: the only way podman runs here. Guard first, then pin XDG_RUNTIME_DIR for the call.
podman_pinned() {
  require_pin
  exec env XDG_RUNTIME_DIR="$SPIKE_XRD" podman --root "$GINN_SPIKE_ROOT" \
    --runroot "$GINN_SPIKE_RUNROOT" --tmpdir "$GINN_SPIKE_TMP" "$@"
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

# ---------------------------------------------------------------- root steps

# root_write <value> <file> <log>: one cgroup write; result appended to log.
root_write() {
  local value=$1 file=$2 log=$3 err
  if err=$({ printf '%s\n' "$value" > "$file"; } 2>&1); then
    printf 'OK    echo %s > %s\n' "$value" "$file" >> "$log"
  else
    printf 'FAIL  echo %s > %s : %s\n' "$value" "$file" "$err" >> "$log"
    return 1
  fi
}

# setup_root <uid> <gid> <out>: the steps Host Setup (FR-001) must perform.
setup_root() {
  need_root setup_root
  local uid=$1 gid=$2 out=$3 log c ud
  log="$out/root-steps.log"
  ud="$CG_ROOT/ginn/$uid"
  mkdir -p "$out"
  printf '== root-setup %s uid=%s gid=%s\n' "$(date -Is)" "$uid" "$gid" >> "$log"
  # Keep the original root subtree_control once, so teardown can restore it.
  [[ -e $out/root-subtree-control.orig ]] ||
    cat "$CG_ROOT/cgroup.subtree_control" > "$out/root-subtree-control.orig"
  printf 'root subtree_control before: [%s]\n' "$(cat "$CG_ROOT/cgroup.subtree_control")" >> "$log"
  printf 'root cgroup.procs lines before: %s\n' "$(wc -l < "$CG_ROOT/cgroup.procs")" >> "$log"

  for c in "${CTL_SET[@]}"; do
    root_write "+$c" "$CG_ROOT/cgroup.subtree_control" "$log" || true
  done
  mkdir -p "$CG_ROOT/ginn"
  printf 'OK    mkdir -p %s\n' "$CG_ROOT/ginn" >> "$log"
  for c in "${CTL_SET[@]}"; do
    root_write "+$c" "$CG_ROOT/ginn/cgroup.subtree_control" "$log" || true
  done
  mkdir -p "$ud" "$ud/s01-launch"
  printf 'OK    mkdir -p %s %s\n' "$ud" "$ud/s01-launch" >> "$log"
  for c in "${CTL_SET[@]}"; do
    root_write "+$c" "$ud/cgroup.subtree_control" "$log" || true
  done
  # Minimal delegation: only the directory and the three files a delegated
  # subtree must own. memory.max and other limit files stay root-owned.
  chown "$uid:$gid" "$ud" "$ud/cgroup.procs" "$ud/cgroup.threads" "$ud/cgroup.subtree_control"
  printf 'OK    chown %s:%s %s, cgroup.procs, cgroup.threads, cgroup.subtree_control\n' \
    "$uid" "$gid" "$ud" >> "$log"
  printf 'root subtree_control after: [%s]\n' "$(cat "$CG_ROOT/cgroup.subtree_control")" >> "$log"
  printf 'ginn subtree_control after: [%s]\n' "$(cat "$CG_ROOT/ginn/cgroup.subtree_control")" >> "$log"
  printf 'uid subtree_control after: [%s]\n' "$(cat "$ud/cgroup.subtree_control")" >> "$log"
  ls -ld "$ud" "$ud/s01-launch" "$ud/cgroup.procs" "$ud/cgroup.threads" \
    "$ud/cgroup.subtree_control" >> "$log"
  chown "$uid:$gid" "$log"
}

# move_launcher <uid> <pid> <out>: place the launcher shell in the user leaf.
move_launcher() {
  need_root move_launcher
  local uid=$1 pid=$2 out=$3 log
  log="$out/root-steps.log"
  root_write "$pid" "$CG_ROOT/ginn/$uid/s01-launch/cgroup.procs" "$log" || die "move failed"
  printf 'launcher pid %s now in %s\n' "$pid" "$(sed -n 's/^0:://p' "/proc/$pid/cgroup")" >> "$log"
}

# ---------------------------------------------------------------- user steps

mem_guard() {
  local avail swapfree
  avail=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
  swapfree=$(awk '/^SwapFree:/ {print int($2/1024)}' /proc/meminfo)
  log "mem guard: MemAvailable=${avail}MiB SwapFree=${swapfree}MiB"
  if (( avail < 256 )); then
    die "MemAvailable ${avail}MiB below 256 MiB; host headroom too low to continue"
  fi
}

elapsed() { awk -v a="$1" -v b="$2" 'BEGIN {printf "%.1f", b - a}'; }

# run_variant <tag> <name> <extra podman run flags...>
run_variant() {
  local tag=$1 name=$2 d rc=0 wrc=0 t0 t1 conmon pid conmon_rss ctr_rss failed state notes
  shift 2
  d="$OUT/$tag"
  mkdir -p "$d"
  "${PM[@]}" rm -f "$name" >/dev/null 2>&1 || true
  log "variant $tag ($name): podman run $*"
  t0=$(date +%s.%N)
  "${PM[@]}" run -d --name "$name" "${COMMON_FLAGS[@]}" "$@" "$IMAGE" /sbin/init \
    >"$d/run.out" 2>"$d/run.err" || rc=$?
  echo "$rc" > "$d/run.rc"
  if (( rc != 0 )); then
    printf '%s\t%s\t%s\t-\t-\t-\t-\t-\t-\tpodman run failed: %s\n' "$tag" "$name" "$rc" \
      "$(head -c 200 "$d/run.err" | tr '\n\t' '  ')" >> "$OUT/summary.tsv"
    log "variant $tag: podman run exit $rc"
    return 0
  fi
  # Up to 60 s for boot to settle; systemctl --wait returns when it does.
  timeout 60 "${PM[@]}" exec "$name" systemctl is-system-running --wait \
    >"$d/wait.txt" 2>&1 || wrc=$?
  t1=$(date +%s.%N)
  echo "$wrc" > "$d/wait.rc"
  "${PM[@]}" exec "$name" systemctl is-system-running >"$d/state.txt" 2>&1 || true
  "${PM[@]}" exec "$name" systemctl --failed --no-legend >"$d/failed.txt" 2>&1 || true
  "${PM[@]}" exec "$name" cat /proc/1/cgroup >"$d/pid1-cgroup.txt" 2>&1 || true
  "${PM[@]}" exec "$name" ls /sys/fs/cgroup/ >"$d/sys-fs-cgroup.txt" 2>&1 || true
  "${PM[@]}" exec "$name" cat /sys/fs/cgroup/cgroup.controllers >"$d/controllers.txt" 2>&1 || true
  "${PM[@]}" exec "$name" journalctl -b --no-pager -p warning >"$d/journal-warnings.txt" 2>&1 || true
  "${PM[@]}" exec "$name" journalctl -b --no-pager >"$d/journal.txt" 2>&1 || true
  grep -i 'cgroup\|elogind' "$d/journal.txt" >"$d/journal-cgroup-elogind.txt" 2>&1 || true
  "${PM[@]}" top "$name" pid,rss,comm >"$d/top.txt" 2>&1 || true
  "${PM[@]}" stats --no-stream "$name" >"$d/stats.txt" 2>&1 || true
  conmon=$("${PM[@]}" inspect --format '{{.State.ConmonPid}}' "$name" 2>/dev/null || echo 0)
  pid=$("${PM[@]}" inspect --format '{{.State.Pid}}' "$name" 2>/dev/null || echo 0)
  {
    echo "conmon pid $conmon: $(cat "/proc/$conmon/cgroup" 2>/dev/null || echo gone)"
    grep VmRSS "/proc/$conmon/status" 2>/dev/null || true
    echo "container init host pid $pid: $(cat "/proc/$pid/cgroup" 2>/dev/null || echo gone)"
  } >"$d/host-side.txt"
  conmon_rss=$(awk '/VmRSS/ {print $2}' "/proc/$conmon/status" 2>/dev/null || echo n/a)
  ctr_rss=$(awk 'NR>1 && $2 ~ /^[0-9]+$/ {s+=$2} END {print s+0}' "$d/top.txt")
  failed=$(grep -c . "$d/failed.txt" || true)
  state=$(head -1 "$d/state.txt")
  notes="wait_rc=$wrc"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$tag" "$name" "$rc" "$state" \
    "$failed" "$(tr '\n' ' ' < "$d/controllers.txt")" "$(elapsed "$t0" "$t1")" \
    "$conmon_rss" "$ctr_rss" "$notes" >> "$OUT/summary.tsv"
  log "variant $tag: state=$state failed=$failed boot=$(elapsed "$t0" "$t1")s conmonRSS=${conmon_rss}KiB ctrRSS=${ctr_rss}KiB"
  # One variant at a time: free the memory before the next one (host has little headroom).
  if [[ ${KEEP:-0} != 1 ]]; then
    "${PM[@]}" rm -f "$name" >/dev/null 2>&1 || true
  fi
}

# variant_spec <tag> <uid>: prints the container name, then one podman flag per line.
variant_spec() {
  case $1 in
    e) echo s01e ;;
    a) printf '%s\n' s01a --cgroup-parent "/ginn/$2/s01a" ;;
    b) printf '%s\n' s01b --cgroups=split --cgroup-parent "/ginn/$2/s01b" ;;
    c) printf '%s\n' s01c --cgroups=no-conmon --cgroup-parent "/ginn/$2/s01c" ;;
    d) printf '%s\n' s01d --runtime crun --cgroup-parent "/ginn/$2/s01d" ;;
  esac
}

# pick_best: first delegated variant (a..d) running or degraded with zero failed units.
pick_best() {
  awk -F'\t' 'NR>1 && $1 ~ /^[abcd]$/ && ($4=="running" || $4=="degraded") && $5==0 {print $1; exit}' \
    "$OUT/summary.tsv"
}

best_steps() {
  local name=$1 t0 t1 code
  log "cron install in $name (FR-080 AC-1)"
  "${PM[@]}" exec "$name" bash -c \
    'apt-get update && apt-get install -y cron && systemctl enable --now cron && systemctl is-active cron' \
    >"$OUT/cron-install.txt" 2>&1 || true
  "${PM[@]}" exec "$name" systemctl is-active cron >"$OUT/cron-before-restart.txt" 2>&1 || true
  "${PM[@]}" top "$name" pid,rss,comm >"$OUT/top-with-cron.txt" 2>&1 || true
  log "restart $name (FR-080 AC-2)"
  t0=$(date +%s.%N)
  "${PM[@]}" restart "$name" >"$OUT/restart.txt" 2>&1 || true
  timeout 60 "${PM[@]}" exec "$name" systemctl is-system-running --wait \
    >"$OUT/restart-wait.txt" 2>&1 || true
  t1=$(date +%s.%N)
  "${PM[@]}" exec "$name" systemctl is-active cron >"$OUT/cron-after-restart.txt" 2>&1 || true
  "${PM[@]}" exec "$name" systemctl is-enabled cron >"$OUT/cron-enabled-after-restart.txt" 2>&1 || true
  printf 'restart to settled: %ss\n' "$(elapsed "$t0" "$t1")" >>"$OUT/restart-wait.txt"
  log "stop $name with -t 30 (FR-016 AC-2)"
  t0=$(date +%s.%N)
  "${PM[@]}" stop -t 30 "$name" >"$OUT/stop.txt" 2>&1 || true
  t1=$(date +%s.%N)
  code=$("${PM[@]}" inspect --format '{{.State.ExitCode}} oomkilled={{.State.OOMKilled}}' "$name")
  printf 'stop elapsed: %ss exit: %s\n' "$(elapsed "$t0" "$t1")" "$code" >"$OUT/shutdown.txt"
  log "shutdown: $(cat "$OUT/shutdown.txt")"
}

cmd_run() {
  local uid gid orig best
  uid=$(id -u)
  gid=$(id -g)
  require_caller_xdg
  mkdir -p "$OUT" "$GINN_SPIKE_ROOT" "$SPIKE_XRD" "$GINN_SPIKE_RUNROOT" "$GINN_SPIKE_TMP"
  chmod 700 "$SPIKE_XRD"
  orig=$(sed -n 's/^0:://p' /proc/self/cgroup)
  echo "$orig" > "$OUT/orig-cgroup"
  printf 'variant\tname\trun_rc\tstate\tfailed_units\tcontrollers\tboot_s\tconmon_rss_kib\tctr_rss_kib\tnotes\n' \
    > "$OUT/summary.tsv"
  {
    echo "uid=$uid gid=$gid self=$$ orig_cgroup=$orig"
    echo "root subtree_control: [$(cat "$CG_ROOT/cgroup.subtree_control")]"
    echo "root controllers: [$(cat "$CG_ROOT/cgroup.controllers")]"
    echo "root cgroup.procs lines: $(wc -l < "$CG_ROOT/cgroup.procs")"
    echo "elogind line: $(grep elogind /proc/self/cgroup || echo none)"
    "${PM[@]}" version 2>&1 | head -3
    echo "runc: $(runc --version 2>&1 | head -1)"
    echo "crun on PATH: $(command -v crun || echo no)"
    free -m
  } > "$OUT/host-facts.txt" 2>&1
  log "host facts -> $OUT/host-facts.txt"

  mem_guard
  log "root-setup (sudo)"
  sudo "$SELF" --root-setup "$uid" "$gid" "$OUT"

  log "pull debian:stable into spike storage"
  "${PM[@]}" pull docker.io/library/debian:stable >"$OUT/pull.txt" 2>&1
  mem_guard
  log "build S-01 image (systemd, dbus, procps)"
  "${PM[@]}" build --network host -t "$IMAGE" -f "$SPIKE_DIR/Dockerfile.s01" "$SPIKE_DIR" \
    >"$OUT/build.txt" 2>&1
  mem_guard

  # Baseline first: launched from the unmoved session shell, no delegation.
  mapfile -t spec < <(variant_spec e "$uid")
  run_variant e "${spec[@]}"

  log "move launcher shell into delegated leaf (sudo)"
  sudo "$SELF" --root-move "$uid" "$$" "$OUT"

  for tag in a b c; do
    mem_guard
    mapfile -t spec < <(variant_spec "$tag" "$uid")
    run_variant "$tag" "${spec[@]}"
  done
  mem_guard
  if command -v crun >/dev/null 2>&1; then
    mapfile -t spec < <(variant_spec d "$uid")
    run_variant d "${spec[@]}"
  else
    printf 'd\ts01d\t-\t-\t-\t-\t-\t-\t-\tNOT TESTED: crun not installed; install needs user consent\n' \
      >> "$OUT/summary.tsv"
    log "variant d NOT TESTED: crun not on PATH"
  fi

  best=$(pick_best || true)
  if [[ -n $best ]]; then
    log "best delegated variant: $best; booting it again for cron steps"
    mem_guard
    mapfile -t spec < <(variant_spec "$best" "$uid")
    KEEP=1 run_variant "best-$best" "${spec[@]}"
    best_steps "${spec[0]}"
  else
    log "no delegated variant reached running/degraded with zero failed units; skipping cron steps"
  fi
  log "done. summary: $OUT/summary.tsv"
  cat "$OUT/summary.tsv"
}

# remove_own_leaves: rmdir only the leaves this spike names, deepest first. Never ginn, never ginn/<uid>,
# never a root write. A leaf that still has processes is left and logged.
remove_own_leaves() {
  local ud leaf
  ud="$CG_ROOT/ginn/$(id -u)"
  for leaf in s01a s01b s01c s01d s01-launch; do
    [[ -d $ud/$leaf ]] || continue
    find "$ud/$leaf" -mindepth 1 -depth -type d -exec rmdir {} + 2>>"$OUT/teardown.err" || true
    if rmdir "$ud/$leaf" 2>>"$OUT/teardown.err"; then log "rmdir $ud/$leaf"; else log "rmdir $ud/$leaf failed (teardown.err)"; fi
  done
  return 0
}

cmd_teardown() {
  require_caller_xdg
  mkdir -p "$OUT"
  if [[ -d $GINN_SPIKE_ROOT ]]; then
    "${PM[@]}" rm -af >/dev/null 2>&1 || true
    "${PM[@]}" rmi -af >/dev/null 2>&1 || true
    # unshare maps subuid-owned storage files so rm can delete them. Podman
    # bind-mounts its overlay dir onto itself inside its own mount namespace,
    # which makes unshare rm fail with EBUSY; the host namespace has no such
    # mount, so a plain rm removes what is left (user-owned files only).
    check_under "$GINN_SPIKE_BASE" "$GINN_SPIKE_ROOT"
    "${PM[@]}" unshare rm -rf "$GINN_SPIKE_ROOT" 2>/dev/null || true
    check_under "$GINN_SPIKE_BASE" "$GINN_SPIKE_ROOT"
    rm -rf "$GINN_SPIKE_ROOT" 2>/dev/null || log "storage removal incomplete: $GINN_SPIKE_ROOT"
  fi
  # The rootless pause process keeps the pinned XDG; stop it by exact pid before removing the dir.
  kill_spike_procs
  remove_own_leaves
  check_under "/run/user/$(id -u)/ginn-spike-s01-xrd" "$SPIKE_XRD"
  rm -rf "$SPIKE_XRD"
  log "teardown done (no root writes); logs kept in $OUT"
}

usage() {
  cat <<'EOF'
usage: XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s01-xrd s01_systemd_rootless.sh run | teardown
       s01_systemd_rootless.sh --podman <podman args>            (pinned wrapper, used internally)
       s01_systemd_rootless.sh --root-setup <uid> <gid> <out>   (root only)
       s01_systemd_rootless.sh --root-move <uid> <pid> <out>    (root only)
EOF
}

case ${1:-} in
  run) cmd_run ;;
  teardown) cmd_teardown ;;
  --podman) shift; podman_pinned "$@" ;;
  --root-setup) setup_root "${2:?uid}" "${3:?gid}" "${4:?out}" ;;
  --root-move) move_launcher "${2:?uid}" "${3:?pid}" "${4:?out}" ;;
  *) usage; exit 2 ;;
esac
