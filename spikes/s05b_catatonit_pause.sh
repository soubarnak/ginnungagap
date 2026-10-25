#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-05b (NFR-002, NFR-003): can rootless podman use catatonit as its pause process?
#
# Finding: the rootless pause execs `catatonit -P` only from the fixed paths
# /usr/libexec/podman/catatonit and /usr/bin/catatonit (podman pkg/rootless/rootless_linux.c,
# do_pause). helper_binaries_dir and init_path do not reach it. With neither path present,
# the pause is podman's own binary (comm "podman pause").
#
#   XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s05b-xrd spikes/s05b_catatonit_pause.sh prepare   extract Debian's catatonit into the scratch bin dir
#   ... probe      stop the spike pause, start a fresh one, report its comm, exe and XDG
#   ... knobs      stop the spike pause, retry with [engine] init_path set, report comm
#   ... teardown   remove spike containers, images and storage, stop the spike pause
#
# Environment:
#   XDG_RUNTIME_DIR must equal the spike dir /run/user/<uid>/ginn-spike-s05b-xrd, or the script refuses.
#   GINN_S05B_OUT   scratch dir for bin, conf and logs (default ~/.cache/ginn-spike/s05b)
#
# Podman's rootless pause.pid lives under $XDG_RUNTIME_DIR/libpod/tmp, and --tmpdir does not move it.
# So every podman call runs with XDG_RUNTIME_DIR pinned to the spike dir. Without the pin, podman joins
# the user's own pause process. The script never signals a pause process unless its pid comes from the
# spike's pause.pid and its environ shows XDG_RUNTIME_DIR equal to the spike dir.
set -euo pipefail

UIDN=$(id -u)
SPIKE_XRD="/run/user/$UIDN/ginn-spike-s05b-xrd"
OUT="${GINN_S05B_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s05b}"
BIN="$OUT/bin"
CONF="$OUT/conf"
LOG="$OUT/logs"
DEBIAN=docker.io/library/debian:stable
ROOT="$HOME/.local/share/ginn-spike-s05b/podman"
RUNROOT="/run/user/$UIDN/ginn-spike-s05b"
TMPD="$SPIKE_XRD/libpod/tmp"
PIDF="$TMPD/pause.pid"
PM=(podman --root "$ROOT" --runroot "$RUNROOT" --tmpdir "$TMPD")

log() { mkdir -p "$LOG"; printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$LOG/run.log" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

require_xdg() {
  [[ ${XDG_RUNTIME_DIR:-} == "$SPIKE_XRD" ]] \
    || die "refusing: XDG_RUNTIME_DIR='${XDG_RUNTIME_DIR:-unset}', must be $SPIKE_XRD"
}

# The only way podman runs in this script. Guard first, then pin XDG_RUNTIME_DIR for the call.
pm() {
  require_xdg
  env XDG_RUNTIME_DIR="$SPIKE_XRD" "${PM[@]}" "$@"
}

write_confs() {
  mkdir -p "$BIN" "$CONF" "$LOG"
  cat > "$CONF/containers.conf" <<CONF_EOF
[engine]
runtime = "crun"
helper_binaries_dir = ["$BIN", "/usr/libexec/podman", "/usr/lib/podman"]
CONF_EOF
  cat > "$CONF/containers-initpath.conf" <<CONF_EOF
[engine]
runtime = "crun"
init_path = "$BIN/catatonit"
helper_binaries_dir = ["$BIN", "/usr/libexec/podman", "/usr/lib/podman"]
CONF_EOF
}

use_conf() { export CONTAINERS_CONF="$CONF/$1"; }

make_dirs() { mkdir -p -m 700 "$SPIKE_XRD" "$RUNROOT" "$TMPD"; }

# pid recorded in the spike's own pause.pid, empty when there is none
pause_pid() { if [[ -r $PIDF ]]; then cat "$PIDF"; fi; }

# XDG_RUNTIME_DIR line of a process environ, or a marker when unreadable
xdg_of() { tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null | grep '^XDG_RUNTIME_DIR=' || echo 'XDG_RUNTIME_DIR=(unreadable)'; }

# Stop one pause pid if it belongs to the spike. Returns 1 when it refuses or survives.
stop_spike_pause() {
  local p=$1 env_txt i
  [[ -n $p ]] || return 0
  kill -0 "$p" 2>/dev/null || return 0
  env_txt=$(tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null) || env_txt=""
  if ! grep -qx "XDG_RUNTIME_DIR=$SPIKE_XRD" <<<"$env_txt"; then
    log "pid $p environ is not the spike XDG, not stopped"
    return 1
  fi
  kill -TERM "$p"
  for i in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
  if kill -0 "$p" 2>/dev/null; then log "pid $p alive after SIGTERM"; return 1; fi
  log "stopped spike pause pid $p"
}

# Report the spike pause, then every uid podman pause with its XDG (read only, nothing signalled).
pause_report() {
  local p=$1 q tag
  log "spike pause pid=$p comm='$(ps -o comm= -p "$p" | sed 's/ *$//')' exe=$(readlink "/proc/$p/exe")"
  xdg_of "$p" | grep -qx "XDG_RUNTIME_DIR=$SPIKE_XRD" || die "pause $p does not carry the spike XDG"
  for q in $(ps -u "$UIDN" -o pid=,comm= | awk '$2=="podman" && $3=="pause" {print $1}'); do
    if [[ $q == "$p" ]]; then tag=spike; else tag=other; fi
    log "  pause pid $q [$tag] $(xdg_of "$q")"
  done
}

cmd_prepare() {
  write_confs; use_conf containers.conf
  log "versions: $(pm --version), $(crun --version | head -1), $(conmon --version | head -1)"
  if [[ ! -x $BIN/catatonit ]]; then
    pm pull -q "$DEBIAN" >"$LOG/pull.txt" 2>&1 || die "pull failed, see $LOG/pull.txt"
    pm run --rm -v "$BIN:/out" "$DEBIAN" sh -c \
      'apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq catatonit >/dev/null 2>&1 && cp /usr/bin/catatonit /out/catatonit && dpkg-query -W catatonit > /out/catatonit.pkg && /usr/bin/catatonit --version > /out/catatonit.version 2>&1' \
      >"$LOG/extract.txt" 2>&1 || die "catatonit extraction failed, see $LOG/extract.txt"
  fi
  log "catatonit: $(head -1 "$BIN/catatonit.pkg") / $("$BIN/catatonit" --version | head -1)"
  sha256sum "$BIN/catatonit" | tee -a "$LOG/run.log" >&2
}

cmd_probe() {
  write_confs; use_conf containers.conf
  local p; p=$(pause_pid)
  if [[ -n $p ]]; then stop_spike_pause "$p" || die "could not stop old spike pause $p"; fi
  pm run --rm "$DEBIAN" true >"$LOG/probe.txt" 2>&1 || die "podman run failed, see $LOG/probe.txt"
  p=$(pause_pid); [[ -n $p ]] || die "no pause.pid after run"
  pause_report "$p"
}

cmd_knobs() {
  write_confs; use_conf containers-initpath.conf
  local p; p=$(pause_pid)
  if [[ -n $p ]]; then stop_spike_pause "$p" || die "could not stop spike pause $p"; fi
  pm run --rm "$DEBIAN" true >"$LOG/knobs.txt" 2>&1 || die "podman run failed, see $LOG/knobs.txt"
  p=$(pause_pid); [[ -n $p ]] || die "no pause.pid after run"
  pause_report "$p"
}

cmd_teardown() {
  write_confs; use_conf containers.conf
  local p; p=$(pause_pid)
  # A live pause keeps unshare from spawning a new one. Start one if none is alive.
  if [[ -z $p ]] || ! kill -0 "$p" 2>/dev/null; then pm unshare true >/dev/null 2>&1 || true; p=$(pause_pid); fi
  if [[ -d $ROOT || -d $RUNROOT ]]; then
    pm rm -af >/dev/null 2>&1 || true
    pm rmi -af >/dev/null 2>&1 || true
    pm unshare rm -rf "$ROOT" "$RUNROOT" >/dev/null 2>&1 || true
  fi
  stop_spike_pause "$p" || log "spike pause $p left running, check by hand"
  rm -rf "$ROOT" "$RUNROOT" "$SPIKE_XRD" 2>/dev/null || log "storage removal incomplete"
  rmdir "$(dirname "$ROOT")" 2>/dev/null || true
  local q
  for q in $(ps -u "$UIDN" -o pid=,comm= | awk '$2=="podman" && $3=="pause" {print $1}'); do
    log "pause pid $q remains [$(xdg_of "$q")]"
  done
  log "teardown done"
}

main() {
  case ${1:-} in
    prepare|probe|knobs|teardown) ;;
    *) die "usage: XDG_RUNTIME_DIR=$SPIKE_XRD $0 prepare|probe|knobs|teardown" ;;
  esac
  require_xdg
  make_dirs
  case $1 in
    prepare) cmd_prepare ;;
    probe) cmd_probe ;;
    knobs) cmd_knobs ;;
    teardown) cmd_teardown ;;
  esac
}

main "$@"
