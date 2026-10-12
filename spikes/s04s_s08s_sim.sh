#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-04s (CL-1, ADR-004) and S-08s (CL-5): simulated logout, run as the spike user (uid 1000).
# No root, no sudo, no xbps, no --privileged.
#
# The simulation stands in for what elogind does at the last logout of a user: it removes the
# user's runtime directory. $SIM_RUNTIME stands in for /run/user/<uid>/ and "logout" removes it.
# $SIM_GINN stands in for the ginn runtime root and is not removed. KillUserProcesses is unset
# (default no), so no process is killed. This is not a real logout: no PAM session closes and
# no elogind session is involved.
#
#   build       compile the static probe client (uclient); copy the catatonit init stand-in into the store
#   setup       spike storage: pull alpine into variant A's store, save it, load it into variant B's
#   start       s04a (variant A), s04b (variant B), s08c (variant A flags, socket bind mount), gen1 server.
#               With GINN_S08_KEEPID=1: no s04a/s04b; s08c gets the realm base argv instead
#               (--runtime crun --userns keep-id --init --init-path <stand-in> --network host)
#   logout      simulated logout: stop gen1, remove $SIM_RUNTIME, wait 15 s, check pinned processes
#   login       simulated login: recreate $SIM_RUNTIME empty (0700), start gen2 at the same path
#   s04-post    podman ps -a, inspect, exec and stop per variant with exact flags
#   s08-probe   probe /run/s08.sock inside s08c (argument: a label)
#   s08-rebind  the six S-08s attempts on s08c (as recorded in results/S-08.md)
#   s08-try V   one rebind attempt on s08c with the namespace listing and probes around it (S-08t).
#               V = row5: podman unshare, helper with --unmount-first (user ns entered when different)
#               V = b:    as row5, plus --no-setns-user (enter only the mount namespace)
#               V = row3: podman unshare, helper without umount (control)
#   reap        podman rm with exact flags, then kill leftovers that match their pins
#   teardown    podman unshare rm -rf the spike storage; remove $SIM_RUNTIME, $SIM_GINN and the store
#
# Every podman call passes --root, --runroot and --tmpdir at spike-owned paths, and --runtime crun.
# Nothing here touches ~/.local/share/containers, /run/user/<uid>/containers, /run/user/<uid>/libpod
# or another spike's directory. Logs go to $GINN_SPIKE_OUT (default ~/.cache/ginn-spike/s04s).
set -euo pipefail

SELF_DIR=$(dirname "$(readlink -f "$0")")
NAME=ginn-spike-s04sim
UID_NUM=$(id -u)
STORE=$HOME/.cache/$NAME
OUT=${GINN_SPIKE_OUT:-$HOME/.cache/ginn-spike/s04s}
ROOT_A=$STORE/a/root
ROOT_B=$STORE/b/root
SIM_RUNTIME=/run/user/$UID_NUM/$NAME
SIM_GINN=/dev/shm/$NAME-ginn
SOCK=$SIM_RUNTIME/s08.sock
IMAGE=docker.io/library/alpine:3
TAR=$STORE/alpine.tar
CLIENT=$STORE/uclient
# catatonit stand-in: this host has no catatonit at a path podman searches (S-05, S-05b). The copy
# comes from the S-07c spike store once, in cmd_build, and every later run uses $INIT in the store.
INIT_SRC=$HOME/.cache/ginn-spike/s07c/catatonit
INIT=$STORE/catatonit
UCLIENT_C=$SELF_DIR/s04s_s08s_uclient.c
SERVER_PY=$SELF_DIR/s04_s08_sockserver.py
REBIND_PY=$SELF_DIR/s04s_s08s_rebind.py
PINS=$OUT/pins.txt

mkdir -p "$OUT"
exec > >(tee -a "$OUT/run.log") 2>&1

die() { echo "error: $*" >&2; exit 1; }
say() { echo "$(date +%T) $*"; }
guard() {
  [[ $HOME == /home/* ]] || die "unexpected HOME: $HOME"
  [[ $SIM_RUNTIME == "/run/user/$UID_NUM/$NAME" ]] || die "unexpected SIM_RUNTIME: $SIM_RUNTIME"
  [[ $SIM_GINN == "/dev/shm/$NAME-ginn" ]] || die "unexpected SIM_GINN: $SIM_GINN"
  [[ $STORE == "$HOME/.cache/$NAME" ]] || die "unexpected STORE: $STORE"
}
# run_rc CMD...: run a command, print its exit status, and keep going.
run_rc() { local rc=0; "$@" || rc=$?; echo "rc=$rc"; }

# pa / pb: the only podman entry points. Variant A keeps its runroot and tmpdir in $SIM_GINN;
# variant B keeps them under $SIM_RUNTIME. Each has its own --root, so the stores are independent.
pa() {
  echo "$(date -Is) [A] podman --root $ROOT_A --runroot $SIM_GINN/podman --tmpdir $SIM_GINN/tmp --runtime crun $*" >>"$OUT/commands.log"
  env XDG_RUNTIME_DIR="$SIM_GINN" podman --root "$ROOT_A" --runroot "$SIM_GINN/podman" \
    --tmpdir "$SIM_GINN/tmp" --runtime crun "$@"
}
pb() {
  echo "$(date -Is) [B] podman --root $ROOT_B --runroot $SIM_RUNTIME/podman --tmpdir $SIM_RUNTIME/tmp --runtime crun $*" >>"$OUT/commands.log"
  env XDG_RUNTIME_DIR="$SIM_RUNTIME" podman --root "$ROOT_B" --runroot "$SIM_RUNTIME/podman" \
    --tmpdir "$SIM_RUNTIME/tmp" --runtime crun "$@"
}

proc_uid() { stat -c %u "/proc/$1" 2>/dev/null || true; }
proc_cmd() { { tr '\0' ' ' <"/proc/$1/cmdline"; } 2>/dev/null | sed 's/ $//' || true; }
proc_env() { { tr '\0' '\n' <"/proc/$1/environ"; } 2>/dev/null || true; }
# proc_stat PID: "state ppid starttime" (stat fields 3, 4, 22); fails when the process is gone.
proc_stat() {
  local s f
  s=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
  s=${s##*) }
  read -r -a f <<<"$s"
  echo "${f[0]} ${f[1]} ${f[19]}"
}
# proc_alive PID START: still exists, not a zombie, same start time (so the pid was not reused).
proc_alive() {
  local s state ppid start
  s=$(proc_stat "$1") || return 1
  read -r state ppid start <<<"$s"
  [[ $state != Z && $start == "$2" ]]
}

# inventory: "pid ppid starttime label command" for this user's processes that belong to the
# simulation. A: XDG_RUNTIME_DIR=$SIM_GINN, or a command line naming $SIM_GINN. B: the same for
# $SIM_RUNTIME. S: the socket server. Other spikes use other paths and never match.
inventory() {
  local d p cmd env label s state ppid start
  for d in /proc/[0-9]*; do
    p=${d#/proc/}
    [[ $(proc_uid "$p") == "$UID_NUM" ]] || continue
    cmd=$(proc_cmd "$p")
    [[ -n $cmd ]] || continue
    env=$(proc_env "$p")
    label=
    # Matched by environment (exact XDG_RUNTIME_DIR) or by a podman/conmon/server command line.
    # A shell whose command text merely mentions a path is never matched.
    if grep -qxF "XDG_RUNTIME_DIR=$SIM_GINN" <<<"$env" || [[ $cmd == /usr/libexec/podman/conmon* && $cmd == *"$SIM_GINN/"* ]]; then label=A; fi
    if grep -qxF "XDG_RUNTIME_DIR=$SIM_RUNTIME" <<<"$env" || [[ $cmd == /usr/libexec/podman/conmon* && $cmd == *"$SIM_RUNTIME/"* ]]; then label=B; fi
    if [[ $cmd == "python3 -I "*"$SERVER_PY $SOCK "* ]]; then label=S; fi
    [[ -n $label ]] || continue
    s=$(proc_stat "$p") || continue
    read -r state ppid start <<<"$s"
    echo "$p $ppid $start $label $cmd"
  done
}

# pin ROLE PID: record the process by pid, start time and command line.
pin() {
  local s start
  s=$(proc_stat "$2") || die "pin $1: pid $2 is not running"
  read -r _ _ start <<<"$s"
  echo "$1 $2 $start $(proc_cmd "$2")" >>"$PINS"
}
report_pins() {  # report_pins LABEL
  local role pid start cmd state
  echo "== pinned processes: $1"
  [[ -s $PINS ]] || { echo "  (no pins)"; return 0; }
  while read -r role pid start cmd; do
    if proc_alive "$pid" "$start" && [[ $(proc_cmd "$pid") == "$cmd" ]]; then state=alive; else state=gone; fi
    echo "  $role pid=$pid $state"
  done <"$PINS"
}
ps_pins() {
  local list
  list=$(awk '{print $2}' "$PINS" | paste -sd, -)
  [[ -n $list ]] && ps -o pid=,ppid=,stat=,etime=,args= -p "$list" 2>/dev/null | cut -c1-180 || true
}

start_server() {  # start_server GEN: detached socket server at $SOCK, pid kept in $OUT/GEN.pid
  local gen=$1 pid
  setsid python3 -I "$SERVER_PY" "$SOCK" "$gen" >>"$OUT/$gen.log" 2>&1 </dev/null &
  pid=$!
  echo "$pid" >"$OUT/$gen.pid"
  sleep 1
  [[ $(proc_cmd "$pid") == "python3 -I $SERVER_PY $SOCK $gen" ]] \
    || die "$gen did not start as expected: $(cat "$OUT/$gen.log" 2>/dev/null)"
  say "server $gen pid=$pid at $SOCK ($(stat -c 'inode %i' "$SOCK"))"
}
stop_server() {  # stop_server GEN: SIGTERM when the pinned pid still matches
  local gen=$1 line pid start cmd
  line=$(grep "^$gen " "$PINS") || die "no pin for $gen"
  read -r _ pid start cmd <<<"$line"
  if proc_alive "$pid" "$start" && [[ $(proc_cmd "$pid") == "$cmd" ]]; then
    kill -TERM "$pid"
    say "stopped $gen pid=$pid"
  else
    say "$gen pid=$pid was not running"
  fi
}

cmd_build() {
  guard
  mkdir -p -m 0700 "$STORE"
  gcc -static -O2 -Wall -Wextra -Werror -o "$CLIENT" "$UCLIENT_C"
  ls -l "$CLIENT"
  [[ -x $INIT_SRC ]] || die "no catatonit stand-in at $INIT_SRC"
  install -m 0755 "$INIT_SRC" "$INIT"
  sha256sum "$INIT" | tee -a "$OUT/run.log" >&2
  "$INIT" --version | head -1
}

cmd_setup() {
  guard
  mkdir -p -m 0700 "$STORE" "$STORE/a" "$STORE/b"
  mkdir -p -m 0700 "$ROOT_A" "$ROOT_B"
  mkdir -p -m 0700 "$SIM_RUNTIME" "$SIM_RUNTIME/podman" "$SIM_RUNTIME/tmp"
  mkdir -p -m 0700 "$SIM_GINN" "$SIM_GINN/podman" "$SIM_GINN/tmp"
  pa image exists "$IMAGE" || pa pull "$IMAGE"
  [[ -s $TAR ]] || pa save -o "$TAR" "$IMAGE"
  pb image exists "$IMAGE" || pb load -i "$TAR"
  pa images
  pb images
}

write_pins() {  # before the logout: init and conmon of each container, gen1, and the inventory
  : >"$PINS"
  if pa container exists s04a; then
    pin "init:s04a" "$(pa inspect -f '{{.State.Pid}}' s04a)"
    pin "conmon:s04a" "$(pa inspect -f '{{.State.ConmonPid}}' s04a)"
  fi
  if pb container exists s04b; then
    pin "init:s04b" "$(pb inspect -f '{{.State.Pid}}' s04b)"
    pin "conmon:s04b" "$(pb inspect -f '{{.State.ConmonPid}}' s04b)"
  fi
  pin "init:s08c" "$(pa inspect -f '{{.State.Pid}}' s08c)"
  pin "conmon:s08c" "$(pa inspect -f '{{.State.ConmonPid}}' s08c)"
  pin "gen1" "$(cat "$OUT/gen1.pid")"
  local p pp st label cmd
  while read -r p pp st label cmd; do
    pin "inv:$label" "$p"
  done < <(inventory)
}

cmd_start() {
  guard
  [[ -x $CLIENT ]] || die "run build first"
  [[ ! -s $PINS ]] || die "$PINS exists: a run is already started"
  [[ -d $SIM_RUNTIME && -d $SIM_GINN ]] || die "run setup first"
  start_server gen1
  if [[ ${GINN_S08_KEEPID:-0} == 1 ]]; then
    # Realm base argv (S-08t): keep-id gives the container its own child user namespace, and
    # --init runs catatonit as PID 1. --init-path is the only addition, because of INIT (see above).
    [[ -x $INIT ]] || die "run build first: no $INIT"
    pa run -d --name s08c --runtime crun --userns keep-id --init --init-path "$INIT" --network host \
      -v "$SOCK:/run/s08.sock" -v "$CLIENT:/opt/uclient:ro" "$IMAGE" sleep infinity
  else
    pa run -d --name s04a --cgroups disabled --network none "$IMAGE" sleep infinity
    pb run -d --name s04b --cgroups disabled --network none "$IMAGE" sleep infinity
    pa run -d --name s08c --cgroups disabled --network none \
      -v "$SOCK:/run/s08.sock" -v "$CLIENT:/opt/uclient:ro" "$IMAGE" sleep infinity
  fi
  echo "== baseline probe from inside s08c (before logout)"
  run_rc pa exec s08c /opt/uclient /run/s08.sock
  write_pins
  report_pins "before logout"
  echo "== podman view before logout"
  run_rc pa ps -a --format '{{.Names}} {{.State}} {{.Status}}'
  run_rc pb ps -a --format '{{.Names}} {{.State}} {{.Status}}'
}

cmd_logout() {
  guard
  [[ -s $PINS ]] || die "no pins: run start first"
  [[ -d $SIM_RUNTIME ]] || die "$SIM_RUNTIME is absent"
  stop_server gen1
  say "before logout, $SIM_RUNTIME holds: $(ls -A "$SIM_RUNTIME" | tr '\n' ' ')"
  say "mounts under $SIM_RUNTIME: [$(findmnt -rn -o TARGET | grep -F "$SIM_RUNTIME" | tr '\n' ' ' || true)]"
  rm -rf -- "$SIM_RUNTIME"
  [[ ! -e $SIM_RUNTIME ]] || die "$SIM_RUNTIME still exists after removal"
  say "logout: $SIM_RUNTIME removed; $SIM_GINN still holds: $(ls -A "$SIM_GINN" | tr '\n' ' ')"
  say "waiting 15 s"
  sleep 15
  report_pins "after logout, 15 s later"
  ps_pins
}

cmd_login() {
  guard
  [[ ! -e $SIM_RUNTIME ]] || die "$SIM_RUNTIME exists: the simulated logout did not happen"
  mkdir -m 0700 "$SIM_RUNTIME"
  say "login: $SIM_RUNTIME recreated empty (0700), as elogind creates the runtime dir"
  start_server gen2
  pin gen2 "$(cat "$OUT/gen2.pid")"
}

cmd_s04_post() {
  guard
  report_pins "after login, before any podman call"
  say "SIM_RUNTIME before the first B call: [$(ls -A "$SIM_RUNTIME" | tr '\n' ' ')]"
  say "SIM_GINN before any call: [$(ls -A "$SIM_GINN" | tr '\n' ' ')]"
  echo "== variant B, first podman call after login (debug log: $OUT/b-first-call-debug.log)"
  local rc=0
  pb --log-level=debug ps -a >"$OUT/b-first-call-debug.log" 2>&1 || rc=$?
  echo "rc=$rc"
  grep -iE 'refresh|reboot|boot|alive|reset|runroot|tmpdir' "$OUT/b-first-call-debug.log" | cut -c1-220 | head -30 || true
  say "alive files: A tmp [$(ls -A "$SIM_GINN/tmp" | tr '\n' ' ')]  B tmp [$(ls -A "$SIM_RUNTIME/tmp" | tr '\n' ' ')]"
  run_rc pb ps -a --format '{{.Names}} {{.State}} {{.Status}}'
  run_rc pb inspect -f '{{.Name}} state={{.State.Status}} pid={{.State.Pid}} conmon={{.State.ConmonPid}}' s04b
  run_rc pb exec s04b echo exec-ok
  run_rc pb stop -t 2 s04b
  report_pins "after B's podman calls"
  ps_pins
  echo "== variant A, after login"
  run_rc pa ps -a --format '{{.Names}} {{.State}} {{.Status}}'
  run_rc pa inspect -f '{{.Name}} state={{.State.Status}} pid={{.State.Pid}} conmon={{.State.ConmonPid}}' s04a
  run_rc pa exec s04a echo exec-ok
  run_rc pa stop -t 2 s04a
  run_rc pa ps -a --format '{{.Names}} {{.State}} {{.Status}}'
  report_pins "after A's podman calls"
}

cmd_s08_probe() {
  guard
  echo "== s08 probe: ${1:-probe}"
  run_rc pa exec s08c /opt/uclient /run/s08.sock
}

cmd_s08_rebind() {
  guard
  local ipid
  ipid=$(pa inspect -f '{{.State.Pid}}' s08c)
  say "s08c init pid=$ipid; host socket now $(stat -c 'inode %i' "$SOCK")"
  echo "== user namespaces"
  echo "plain session user ns:   $(readlink /proc/self/ns/user)"
  echo "container init user ns:  $(readlink /proc/$ipid/ns/user)"
  echo "container init mnt ns:   $(readlink /proc/$ipid/ns/mnt)"
  echo "podman unshare user ns:  $(pa unshare readlink /proc/self/ns/user)"
  echo "podman unshare mnt ns:   $(pa unshare readlink /proc/self/ns/mnt)"
  echo "== attempt 1: podman unshare + nsenter --mount + mount --bind of the host socket"
  run_rc pa unshare nsenter --mount --target "$ipid" mount --bind "$SOCK" /run/s08.sock
  cmd_s08_probe "after attempt 1"
  echo "== attempt 2: podman unshare + nsenter --user --mount + mount --bind"
  run_rc pa unshare nsenter --user --mount --target "$ipid" mount --bind "$SOCK" /run/s08.sock
  cmd_s08_probe "after attempt 2"
  echo "== attempt 3: podman unshare + rebind helper, no unmount (stack on the stale mount)"
  run_rc pa unshare python3 -I "$REBIND_PY" "$ipid" "$SOCK" /run/s08.sock
  cmd_s08_probe "after attempt 3"
  echo "== attempt 4: plain session + rebind helper, no unmount"
  run_rc python3 -I "$REBIND_PY" "$ipid" "$SOCK" /run/s08.sock
  cmd_s08_probe "after attempt 4"
  echo "== attempt 5: podman unshare + rebind helper with --unmount-first"
  run_rc pa unshare python3 -I "$REBIND_PY" "$ipid" "$SOCK" /run/s08.sock --unmount-first
  cmd_s08_probe "after attempt 5"
  echo "== attempt 6: plain session + rebind helper with --unmount-first"
  run_rc python3 -I "$REBIND_PY" "$ipid" "$SOCK" /run/s08.sock --unmount-first
  cmd_s08_probe "after attempt 6"
}

# ns_listing PID: user and mount namespace inodes of the plain session, of podman unshare and of PID
# (the container init), plus the uid maps of the last two.
ns_listing() {
  local ipid=$1
  echo "plain session:           user=$(readlink /proc/self/ns/user) mnt=$(readlink /proc/self/ns/mnt)"
  echo "podman unshare:          user=$(pa unshare readlink /proc/self/ns/user) mnt=$(pa unshare readlink /proc/self/ns/mnt)"
  echo "container init pid=$ipid: user=$(readlink "/proc/$ipid/ns/user") mnt=$(readlink "/proc/$ipid/ns/mnt")"
  echo "uid_map podman unshare:  $(pa unshare cat /proc/self/uid_map | sed 's/^ *//; s/  */ /g' | paste -sd';' -)"
  echo "uid_map container init:  $(sed 's/^ *//; s/  */ /g' "/proc/$ipid/uid_map" | paste -sd';' -)"
}

# cmd_s08_try VARIANT: one rebind attempt on s08c, with the namespace listing, the pid and StartedAt
# before and after, and a probe before and after. Pass: the probe after reports gen2 and the pid and
# StartedAt are unchanged. Run it on a freshly staled container only: a successful attempt consumes
# the stale mount.
cmd_s08_try() {
  guard
  local variant=${1:-} ipid before after
  local -a extra
  case $variant in
    row5) extra=(--unmount-first) ;;
    b)    extra=(--unmount-first --no-setns-user) ;;
    row3) extra=() ;;
    *) die "usage: $0 s08-try {row5|b|row3}" ;;
  esac
  ipid=$(pa inspect -f '{{.State.Pid}}' s08c)
  before=$(pa inspect -f '{{.State.Pid}} {{.State.StartedAt}}' s08c)
  say "variant $variant: s08c init pid and StartedAt before: $before; host socket inode $(stat -c %i "$SOCK")"
  echo "== namespaces before the attempt"
  ns_listing "$ipid"
  cmd_s08_probe "before $variant"
  echo "== variant $variant: podman unshare python3 rebind ${extra[*]:-(no umount)}"
  run_rc pa unshare python3 -I "$REBIND_PY" "$ipid" "$SOCK" /run/s08.sock "${extra[@]}"
  cmd_s08_probe "after $variant"
  after=$(pa inspect -f '{{.State.Pid}} {{.State.StartedAt}}' s08c)
  if [[ $before == "$after" ]]; then
    echo "init pid and StartedAt unchanged: $after"
  else
    echo "init pid or StartedAt CHANGED: $before -> $after"
  fi
}

kill_pins() {  # SIGTERM, then SIGKILL, for each pinned process whose pid, start time and command still match
  local sig role pid start cmd
  for sig in TERM KILL; do
    while read -r role pid start cmd; do
      proc_alive "$pid" "$start" && [[ $(proc_cmd "$pid") == "$cmd" ]] || continue
      say "$sig $role pid=$pid: $(echo "$cmd" | cut -c1-120)"
      kill -"$sig" "$pid" 2>/dev/null || true
    done <"$PINS"
    sleep 3
  done
}

# kill_leftovers: any process still labelled A, B or S by the inventory (for example a rootless
# pause process that podman starts for a storage root) gets SIGTERM, then SIGKILL after 2 s.
kill_leftovers() {
  local p pp st label cmd
  while read -r p pp st label cmd; do
    say "leftover $label pid=$p: $(echo "$cmd" | cut -c1-120); SIGTERM"
    kill -TERM "$p" 2>/dev/null || true
  done < <(inventory)
  sleep 2
  while read -r p pp st label cmd; do
    say "leftover $label pid=$p still alive; SIGKILL"
    kill -KILL "$p" 2>/dev/null || true
  done < <(inventory)
}

cmd_reap() {
  guard
  echo "== podman rm with exact flags"
  run_rc pa rm -f s08c s04a
  run_rc pb rm -f s04b
  report_pins "after podman rm"
  kill_pins
  kill_leftovers
  echo "== spike processes after reap: $(inventory | wc -l)"
}

# fallback_rm PATH...: plain rm only inside a ginn-spike-s04sim path with every entry owned by us.
fallback_rm() {
  local p
  for p in "$@"; do
    [[ -e $p ]] || continue
    [[ $p == *ginn-spike-s04sim* ]] || die "refusing fallback outside a ginn-spike-s04sim path: $p"
    if [[ -z $(find "$p" ! -user "$UID_NUM" 2>/dev/null) ]]; then
      rm -rf -- "$p"
      say "fallback removed $p"
    else
      say "left $p: entries not owned by us"
    fi
  done
}

cmd_teardown() {
  guard
  [[ -z $(inventory) ]] || die "spike processes are still alive: run reap first"
  echo "== podman unshare rm -rf, exact flags per variant"
  run_rc pa unshare rm -rf -- "$ROOT_A" "$SIM_GINN/podman" "$SIM_GINN/tmp"
  run_rc pb unshare rm -rf -- "$ROOT_B" "$SIM_RUNTIME/podman" "$SIM_RUNTIME/tmp"
  kill_leftovers
  fallback_rm "$ROOT_A" "$ROOT_B" "$SIM_GINN/podman" "$SIM_GINN/tmp" "$SIM_RUNTIME/podman" "$SIM_RUNTIME/tmp"
  rm -rf -- "$SIM_RUNTIME" "$SIM_GINN" "$STORE"
  echo "== after teardown"
  local p
  for p in "$STORE" "$SIM_RUNTIME" "$SIM_GINN"; do
    if [[ -e $p ]]; then echo "still present: $p"; else echo "absent: $p"; fi
  done
  echo "spike processes: $(inventory | wc -l)"
  echo "mounts naming the spike: $(findmnt -rn -o TARGET | grep -c 'ginn-spike-s04sim' || true)"
}

case "${1:-}" in
  build)      cmd_build ;;
  setup)      cmd_setup ;;
  start)      cmd_start ;;
  logout)     cmd_logout ;;
  login)      cmd_login ;;
  s04-post)   cmd_s04_post ;;
  s08-probe)  cmd_s08_probe "${2:-probe}" ;;
  s08-rebind) cmd_s08_rebind ;;
  s08-try)    cmd_s08_try "${2:-}" ;;
  reap)       cmd_reap ;;
  teardown)   cmd_teardown ;;
  *) die "usage: $0 {build|setup|start|logout|login|s04-post|s08-probe [label]|s08-rebind|s08-try {row5|b|row3}|reap|teardown}" ;;
esac
