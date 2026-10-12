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
#   spikes/s05b_catatonit_pause.sh prepare    extract Debian's catatonit into the scratch bin dir
#   spikes/s05b_catatonit_pause.sh probe      stop the spike pause, start a fresh one, report its comm and exe
#   spikes/s05b_catatonit_pause.sh knobs      stop the spike pause, retry with [engine] init_path set, report comm
#   spikes/s05b_catatonit_pause.sh teardown   remove spike containers, images and storage, stop the spike pause
#
# Environment:
#   GINN_S05B_OUT   scratch dir for bin, conf and logs (default ~/.cache/ginn-spike/s05b)
#
# Storage is the spike's own: root ~/.local/share/ginn-spike-s05b/podman, runroot
# /run/user/<uid>/ginn-spike-s05b, and XDG_RUNTIME_DIR /run/user/<uid>/ginn-spike-s05b/xdg.
# Podman reads and writes the rootless pause.pid under $XDG_RUNTIME_DIR/libpod/tmp, not under
# --tmpdir, so XDG_RUNTIME_DIR is set to the spike's own directory. The user's default storage and
# /run/user/<uid>/libpod are never touched. The only process the script stops is the spike pause,
# found by the exact pid in its own pause.pid, after its environ shows the spike XDG_RUNTIME_DIR.
set -euo pipefail

UIDN=$(id -u)
OUT="${GINN_S05B_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s05b}"
BIN="$OUT/bin"
CONF="$OUT/conf"
LOG="$OUT/logs"
DEBIAN=docker.io/library/debian:stable
ROOT="$HOME/.local/share/ginn-spike-s05b/podman"
RUNROOT="/run/user/$UIDN/ginn-spike-s05b"
SXDG="$RUNROOT/xdg"
TMPD="$SXDG/libpod/tmp"
PIDF="$TMPD/pause.pid"
export XDG_RUNTIME_DIR="$SXDG"
PM=(podman --root "$ROOT" --runroot "$RUNROOT" --tmpdir "$TMPD")

log() { mkdir -p "$LOG"; printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$LOG/run.log" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
pm() { "${PM[@]}" "$@"; }

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

make_dirs() { mkdir -p -m 700 "$RUNROOT" "$TMPD"; }

# pid recorded in the spike's own pause.pid, empty when there is none
pause_pid() { if [[ -r $PIDF ]]; then cat "$PIDF"; fi; }

# Stop one pause pid if it belongs to the spike. Returns 1 when it refuses or survives.
stop_spike_pause() {
  local p=$1 env_txt i
  [[ -n $p ]] || return 0
  kill -0 "$p" 2>/dev/null || return 0
  env_txt=$(tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null) || env_txt=""
  if ! grep -qx "XDG_RUNTIME_DIR=$SXDG" <<<"$env_txt"; then
    log "pid $p environ has no XDG_RUNTIME_DIR=$SXDG, not stopped"
    return 1
  fi
  kill -TERM "$p"
  for i in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
  if kill -0 "$p" 2>/dev/null; then log "pid $p alive after SIGTERM"; return 1; fi
  log "stopped spike pause pid $p"
}

pause_report() { # pid, prints comm, exe and argv of the spike pause
  local p=$1
  log "spike pause pid=$p comm='$(ps -o comm= -p "$p" | sed 's/ *$//')' exe=$(readlink "/proc/$p/exe")"
  ps -o pid=,ppid=,comm=,args=,rss= -p "$p" | tee -a "$LOG/run.log" >&2
}

cmd_prepare() {
  write_confs; make_dirs; use_conf containers.conf
  log "versions: $(podman --version), $(crun --version | head -1), conmon $(conmon --version | head -1)"
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
  write_confs; make_dirs; use_conf containers.conf
  local p; p=$(pause_pid)
  if [[ -n $p ]]; then stop_spike_pause "$p" || die "could not stop old spike pause $p"; fi
  pm run --rm "$DEBIAN" true >"$LOG/probe.txt" 2>&1 || die "podman run failed, see $LOG/probe.txt"
  p=$(pause_pid); [[ -n $p ]] || die "no pause.pid after run"
  pause_report "$p"
}

cmd_knobs() {
  write_confs; make_dirs; use_conf containers-initpath.conf
  local p; p=$(pause_pid)
  if [[ -n $p ]]; then stop_spike_pause "$p" || die "could not stop spike pause $p"; fi
  pm run --rm "$DEBIAN" true >"$LOG/knobs.txt" 2>&1 || die "podman run failed, see $LOG/knobs.txt"
  p=$(pause_pid); [[ -n $p ]] || die "no pause.pid after run"
  pause_report "$p"
}

cmd_teardown() {
  write_confs; make_dirs; use_conf containers.conf
  local p; p=$(pause_pid)
  # A live pause keeps podman from spawning a new one for unshare. Start one if none is alive.
  if [[ -z $p ]] || ! kill -0 "$p" 2>/dev/null; then pm unshare true >/dev/null 2>&1 || true; p=$(pause_pid); fi
  if [[ -d $ROOT || -d $RUNROOT ]]; then
    pm rm -af >/dev/null 2>&1 || true
    pm rmi -af >/dev/null 2>&1 || true
    pm unshare rm -rf "$ROOT" "$RUNROOT" >/dev/null 2>&1 || true
  fi
  stop_spike_pause "$p" || log "spike pause $p left running, check by hand"
  rm -rf "$ROOT" "$RUNROOT" 2>/dev/null || log "storage removal incomplete"
  rmdir "$(dirname "$ROOT")" 2>/dev/null || true
  local left
  left=$(ps -u "$UIDN" -o pid=,comm= | awk '$2 ~ /^podman/ {print $1}' || true)
  log "podman processes after teardown (uid $UIDN): $(echo ${left:-none})"
}

case ${1:-} in
  prepare) cmd_prepare ;;
  probe) cmd_probe ;;
  knobs) cmd_knobs ;;
  teardown) cmd_teardown ;;
  *) die "usage: $0 prepare|probe|knobs|teardown" ;;
esac
