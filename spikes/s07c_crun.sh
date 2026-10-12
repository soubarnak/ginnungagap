#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-07c (FR-062 AC-3 and AC-4, NFR-SEC-002, AS-005): re-run of S-07 with the outer
# OCI runtime set to crun. Which outer flags does rootless podman need inside a
# Development-style outer container, is CAP_SYS_ADMIN still required, and does
# /dev/kvm open read-write for the realm user with keep-groups?
#
#   spikes/s07c_crun.sh run       run the cases; raw logs and attempts.tsv go to GINN_SPIKE_OUT
#   spikes/s07c_crun.sh teardown  remove spike containers, image and storage
#
# Every host podman call uses the spike storage (--root, --runroot and --tmpdir) and XDG_RUNTIME_DIR pinned to the spike dir. Inner podman calls run inside the realm and use its own storage, never the
# default storage or the shared libpod tmpdir. Every outer run passes --runtime crun.
# No root, no sudo, no host package installs, no xbps. Raw logs stay outside the repo.
# Run with XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s07c-xrd; the wrapper refuses any other value.
set -euo pipefail

SELF="$(readlink -f "$0")"
SPIKE_DIR="$(dirname "$SELF")"
ROOT_DIR="${GINN_SPIKE_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/ginn-spike-s07c/podman}"
RUN_DIR="/run/user/$(id -u)/ginn-spike-s07c"
OUT="${GINN_SPIKE_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s07c}"
IMAGE=localhost/ginn-s07c:outer
ALPINE=docker.io/library/alpine:3
DEBIAN=docker.io/library/debian:stable
RUNTIME=crun
SPIKE_XRD="/run/user/$(id -u)/ginn-spike-s07c-xrd"
PM=(env XDG_RUNTIME_DIR="$SPIKE_XRD" podman --root "$ROOT_DIR" --runroot "$RUN_DIR" --tmpdir "$RUN_DIR/libpod-tmp")
TABLE="$OUT/attempts.tsv"
ONE_RANGE='dev:1001:64000'
PULL="podman run --rm $ALPINE true"
PROBE="podman --version; podman info --format 'runtime={{.Host.OCIRuntime.Name}} {{.Host.OCIRuntime.Version}} rootless={{.Host.Security.Rootless}}'; command -v crun runc; true"
KVM_TEST='id; grep "^Groups" /proc/self/status; ls -ln /dev/kvm; if (exec 3<>/dev/kvm); then echo open-rw-ok; else echo open-rw-denied; exit 1; fi'
declare -A RC=()

log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# The only way podman runs in this script. require_xdg refuses unless XDG_RUNTIME_DIR is the spike dir, and pm
# passes that value to every call. Podman's rootless pause.pid lives under $XDG_RUNTIME_DIR/libpod/tmp, and
# --tmpdir does not move it, so an unpinned call would join the user's own pause process.
require_xdg() {
  [[ ${XDG_RUNTIME_DIR:-} == "$SPIKE_XRD" ]] \
    || die "refusing: XDG_RUNTIME_DIR='${XDG_RUNTIME_DIR:-unset}', must be $SPIKE_XRD"
}
pm() {
  require_xdg
  "${PM[@]}" "$@"
}
# ensure_xrd: create the spike XDG dir with mode 0700, owned by this user.
ensure_xrd() {
  mkdir -p -m 700 "$SPIKE_XRD"
  [[ $(stat -c '%a %u' "$SPIKE_XRD") == "700 $(id -u)" ]] \
    || die "spike XDG dir is not 0700 and owned by this user: $SPIKE_XRD"
}
# stop_spike_pause: stop the rootless pause process under the spike XDG, by exact pid, and only when its environ
# carries XDG_RUNTIME_DIR equal to the spike dir. Call it after the last podman call.
stop_spike_pause() {
  local pidf="$SPIKE_XRD/libpod/tmp/pause.pid" pid env_txt
  [[ -f $pidf ]] || return 0
  pid=$(cat "$pidf")
  [[ $pid =~ ^[0-9]+$ ]] || die "pause.pid does not hold a pid: $pidf"
  [[ -r /proc/$pid/environ ]] || return 0
  env_txt=$(tr '\0' '\n' < "/proc/$pid/environ")
  grep -qx "XDG_RUNTIME_DIR=$SPIKE_XRD" <<<"$env_txt" \
    || die "pid $pid does not carry the spike XDG; not signalled"
  kill -TERM "$pid"
  log "stopped spike pause pid $pid"
}
mem_ok() { awk '/MemAvailable/ { exit !($2 >= 262144) }' /proc/meminfo || die "MemAvailable below 256 MiB"; }

# outer_run <name> [options...]: start the outer stand-in. The shared options come first,
# then the case options. --init uses a host copy of catatonit (the host has none).
outer_run() {
  local name=$1; shift
  pm rm -f "$name" >/dev/null 2>&1 || true
  pm run -d --name "$name" --runtime "$RUNTIME" --userns keep-id --network host \
    --init --init-path "$OUT/catatonit" "$@" "$IMAGE" sleep infinity
}

# write_subids <name> <lines>: /etc/subuid and /etc/subgid as container root.
write_subids() {
  pm exec -u 0 -e SUB="$2" "$1" sh -c '
    if [ -n "$SUB" ]; then printf "%s\n" "$SUB" > /etc/subuid; else : > /etc/subuid; fi
    cp /etc/subuid /etc/subgid'
}

# setup_s07 <name>: the S-07 inner harness, used by C1 (cgroups disabled, default exec user).
setup_s07() {
  write_subids "$1" "$ONE_RANGE"
  pm exec -i "$1" sh -c 'mkdir -p ~/.config/containers ~/.local/share/containers
    cat > ~/.config/containers/storage.conf' <<'CONF'
[storage]
driver = "overlay"

[storage.options.overlay]
mount_program = "/usr/bin/fuse-overlayfs"
CONF
  pm exec -i "$1" sh -c 'cat > ~/.config/containers/containers.conf' <<'CONF'
[containers]
cgroups = "disabled"

[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
CONF
}

# setup_c2 <name>: the C2 inner configuration, written from containers.conf(5) and
# containers-storage.conf(5). The inner podman runs as the realm user (uid 1000).
setup_c2() {
  write_subids "$1" "$ONE_RANGE"
  pm exec -i --user 1000:1000 -e HOME=/home/dev "$1" sh -c 'mkdir -p ~/.config/containers ~/.local/share/containers
    cat > ~/.config/containers/storage.conf' <<'CONF'
[storage]
driver = "overlay"

[storage.options.overlay]
mount_program = "/usr/bin/fuse-overlayfs"
CONF
  pm exec -i --user 1000:1000 -e HOME=/home/dev "$1" sh -c 'cat > ~/.config/containers/containers.conf' <<'CONF'
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
CONF
}

# inner <mode> <name> <command>: run the inner command in the outer container. Modes c2 and
# none run as uid 1000 with HOME=/home/dev set explicitly; mode s07 uses the default user.
inner() {
  local mode=$1 name=$2 cmd=$3 who=()
  if [[ $mode == c2 || $mode == none ]]; then who=(--user 1000:1000 -e HOME=/home/dev); fi
  pm exec "${who[@]}" "$name" sh -c "export XDG_RUNTIME_DIR=/tmp/s07c-run
    mkdir -p -m 700 \"\$XDG_RUNTIME_DIR\"; id; echo \"HOME=\$HOME\"; $cmd"
}

# first_error <log>: the first line that reports a failure, cut to 300 characters.
first_error() {
  grep -m1 -iE 'error|fail|denied|not permitted|invalid|no such|cannot|unable|nsexec' "$1" | cut -c1-300 || true
}

# record <id> <note> <mode> <subuid> <outer runtime> <outer argv> <inner argv> <outer rc> <inner rc> <first error>
record() {
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$@" >> "$TABLE"
  log "case $1: outer rc=$8 inner rc=$9 [$2] ${10}"
}

# case_run <id> <note> <mode> <subuid> <inner command> [--] <outer options...>
# Starts a fresh outer container with the shared options and the case options, runs the
# inner command, records one TSV row and removes the container. RC[id] holds the outer rc
# when the outer start failed, otherwise the inner rc.
case_run() {
  local id=$1 note=$2 mode=$3 subs=$4 cmd=$5 orc=0 irc=not-run
  local name="s07c-$id" log_file="$OUT/$id.log" outer_rt="" outer_argv errs
  shift 5
  if [[ ${1:-} == -- ]]; then shift; fi
  printf -v outer_argv '%s ' pm run -d --name "$name" --runtime "$RUNTIME" \
    --userns keep-id --network host --init --init-path "$OUT/catatonit" "$@" "$IMAGE" sleep infinity
  mem_ok
  : > "$log_file"
  outer_run "$name" "$@" >>"$log_file" 2>&1 || orc=$?
  if (( orc == 0 )); then
    outer_rt=$(pm inspect --format '{{.OCIRuntime}}' "$name" 2>/dev/null || true)
    pm exec "$name" sh -c 'grep -E "^Cap(Bnd|Eff|Amb):" /proc/self/status; grep -E "^Cap(Bnd|Eff):" /proc/1/status' \
      > "$OUT/$id.caps" 2>&1 || true
    case $mode in
      s07) setup_s07 "$name" ;;
      c2) setup_c2 "$name" ;;
    esac >>"$log_file" 2>&1 || true
    irc=0
    inner "$mode" "$name" "$cmd" >>"$log_file" 2>&1 || irc=$?
  fi
  pm rm -f "$name" >/dev/null 2>&1 || true
  errs=$(first_error "$log_file")
  if (( orc != 0 )); then RC[$id]=$orc; else RC[$id]=$irc; fi
  record "$id" "$note" "$mode" "$subs" "$outer_rt" "${outer_argv% }" "$cmd" "$orc" "$irc" "${errs:-none}"
}

# extract_catatonit: copy the init binary out of the image (the host has no catatonit).
extract_catatonit() {
  pm rm -f s07c-ctr >/dev/null 2>&1 || true
  pm create --name s07c-ctr "$IMAGE" >/dev/null
  pm cp s07c-ctr:/usr/bin/catatonit "$OUT/catatonit"
  pm rm -f s07c-ctr >/dev/null
}

# ---------------------------------------------------------------- run

cmd_run() {
  mkdir -p "$OUT" "$OUT/ctx" "$ROOT_DIR" "$RUN_DIR/libpod-tmp"
  ensure_xrd
  : > "$OUT/run.log"
  printf 'id\tnote\tmode\tsubuid\touter runtime\touter argv\tinner argv\touter rc\tinner rc\tfirst error\n' > "$TABLE"
  {
    echo "podman: $(pm --version)"
    echo "crun: $(crun --version | head -1)"
    echo "runc: $(runc --version | head -1)"
    echo "kernel: $(uname -r)"
    echo "host user: $(id)"
  } > "$OUT/env.txt"
  log "S-07c start: root=$ROOT_DIR run=$RUN_DIR out=$OUT"

  # Same Dockerfile as S-07 (unpinned debian:stable base). RUN steps use the default runtime;
  # only the outer run and create calls are pinned to crun.
  pm build -t "$IMAGE" -f "$SPIKE_DIR/Dockerfile.s07" "$OUT/ctx" >"$OUT/build.log" 2>&1 \
    || die "image build failed, see $OUT/build.log"
  {
    echo "outer image id: $(pm image inspect --format '{{.Id}}' "$IMAGE")"
    echo "base image digest: $(pm image inspect --format '{{index .RepoDigests 0}}' "$DEBIAN" 2>/dev/null || echo unknown)"
  } >> "$OUT/env.txt"
  extract_catatonit

  local fuse=(--device /dev/fuse) label=(--security-opt label=disable)
  local unmask_all=(--security-opt unmask=ALL) tun=(--device /dev/net/tun)
  local sysadmin=(--cap-add SYS_ADMIN) kvm=(--device /dev/kvm)
  local chosen=("${fuse[@]}" "${label[@]}" "${unmask_all[@]}" "${tun[@]}")

  log "E0: environment probe, shared options only"
  case_run E0 'inner podman version and OCI runtime; shared options only' c2 "$ONE_RANGE" "$PROBE" --

  log "C: outer flag minimisation"
  case_run C1 'S-07 A1 flags with unmask=ALL and tun, no SYS_ADMIN; S-07 inner harness' s07 "$ONE_RANGE" "$PULL" -- "${chosen[@]}"
  case_run C2 'C1 with the inner podman as uid 1000 and the C2 inner config' c2 "$ONE_RANGE" "$PULL" -- "${chosen[@]}"
  if (( RC[C2] == 0 )); then
    case_run C3a 'C2 minus /dev/net/tun' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${label[@]}" "${unmask_all[@]}"
    case_run C3b 'C2 minus unmask' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${label[@]}" "${tun[@]}"
    case_run C3c 'C2 minus label=disable' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${unmask_all[@]}" "${tun[@]}"
    case_run C3d 'C2 minus /dev/fuse' c2 "$ONE_RANGE" "$PULL" -- "${label[@]}" "${unmask_all[@]}" "${tun[@]}"
  else
    log "C3 skipped: C2 did not pass"
  fi
  if (( RC[C1] != 0 && RC[C2] != 0 )); then
    case_run C4 'C2 plus SYS_ADMIN (run because C1 and C2 both failed)' c2 "$ONE_RANGE" "$PULL" -- "${chosen[@]}" "${sysadmin[@]}"
  else
    log "C4 skipped: C1 or C2 passed"
  fi

  log "K: /dev/kvm"
  case_run K1 'FR-062 AC-4: keep-groups; realm user opens /dev/kvm read-write' none '' "$KVM_TEST" -- "${kvm[@]}" --group-add keep-groups
  case_run K2 'contrast: /dev/kvm without keep-groups' none '' "$KVM_TEST" -- "${kvm[@]}"
  log "S-07c run finished"
}

# spike_path_ok PATH: teardown may only remove this spike's own storage. PATH must be absolute and
# not a symlink, must have a path component that starts with ginn-spike-s07c, must not contain . or
# .. components, and must not be $HOME, an ancestor of $HOME, or /.
spike_path_ok() {
  local path="$1" part hit=0
  local -a parts=()
  [[ $path == /* ]] || return 1
  # Each component, not only the leaf, must not be a symlink. The walk tests every prefix.
  local cur="" comp
  local -a comps=()
  IFS=/ read -r -a comps <<< "$path"
  for comp in "${comps[@]}"; do
    if [[ -n $comp ]]; then
      cur="$cur/$comp"
      if [[ -L $cur ]]; then return 1; fi
    fi
  done
  IFS=/ read -r -a parts <<< "$path"
  for part in "${parts[@]}"; do
    case "$part" in
      . | ..) return 1 ;;
      ginn-spike-s07c*) hit=1 ;;
    esac
  done
  (( hit )) || return 1
  local home="${HOME%/}"
  [[ -n $home && $path != "$home" && $home != "$path"/* ]] || return 1
  return 0
}

# guard_spike_paths PATH...: stop before any teardown step runs if a path is not the spike's own.
guard_spike_paths() {
  local p
  for p in "$@"; do
    spike_path_ok "$p" || die "refusing teardown of $p: not an absolute, non-symlink ginn-spike-s07c path, and not \$HOME or above it"
  done
}

# teardown: containers, image and spike storage under the explicit --root. Logs in
# GINN_SPIKE_OUT stay for the record.
cmd_teardown() {
  guard_spike_paths "$ROOT_DIR" "$RUN_DIR" "$SPIKE_XRD"
  # Only the spike's own --root, --runroot and --tmpdir. Never the shared libpod tmpdir.
  pm rm -af >/dev/null 2>&1 || true
  echo "teardown: containers left under spike roots: $(pm ps -aq 2>/dev/null | wc -l)"
  pm rmi -af >/dev/null 2>&1 || true
  pm unshare rm -rf "$ROOT_DIR" "$RUN_DIR" >/dev/null 2>&1 || true
  stop_spike_pause
  # podman's storage init bind-mounts $ROOT_DIR/overlay onto itself inside the unshare
  # namespace, so rm -rf there fails with EBUSY. What remains is removed directly only when
  # every entry is owned by this user (no subordinate-ID files, so no layer data).
  local -a existing=()
  local foreign parent
  if [[ -e $ROOT_DIR ]]; then existing+=("$ROOT_DIR"); fi
  if [[ -e $RUN_DIR ]]; then existing+=("$RUN_DIR"); fi
  if (( ${#existing[@]} > 0 )); then
    foreign="$(find "${existing[@]}" ! -user "$(id -un)")" || die "find failed under the spike roots; nothing removed"
    if [[ -z $foreign ]]; then
      rm -rf -- "${existing[@]}"
    fi
  fi
  if [[ -e $ROOT_DIR || -e $RUN_DIR ]]; then die "spike storage left behind: $ROOT_DIR $RUN_DIR"; fi
  parent="$(dirname "$ROOT_DIR")"
  if spike_path_ok "$parent"; then rmdir "$parent" 2>/dev/null || true; fi
  if [[ -e $SPIKE_XRD ]]; then
    spike_path_ok "$SPIKE_XRD" || die "refusing to remove $SPIKE_XRD"
    rm -rf -- "$SPIKE_XRD"
  fi
  echo "teardown: removed $ROOT_DIR and $RUN_DIR"
}

require_xdg
case "${1:-}" in
  run) cmd_run ;;
  teardown) cmd_teardown ;;
  *) die "usage: $0 run|teardown" ;;
esac
