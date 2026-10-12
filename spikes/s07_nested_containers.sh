#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-07 (FR-062 AC-3, FR-085, NFR-SEC-002, AS-005): can rootless podman run inside
# a services-off Development-style outer container, which extra outer flags are
# needed, what capability and device set results, and does /dev/kvm open?
#
#   spikes/s07_nested_containers.sh run       run the attempt matrix and final checks
#   spikes/s07_nested_containers.sh teardown  remove spike containers, image and storage
#
# Every podman call uses the spike storage (--root/--runroot) and the spike XDG pin (XRD
# below), never the default storage. No root, no sudo, no host package installs. Raw logs
# go to GINN_SPIKE_OUT (default ~/.cache/ginn-spike/s07), outside the repository.
set -euo pipefail

SELF="$(readlink -f "$0")"
SPIKE_DIR="$(dirname "$SELF")"
ROOT_DIR="${GINN_SPIKE_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/ginn-spike-s07/podman}"
RUN_DIR="${GINN_SPIKE_RUNROOT:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ginn-spike-s07}"
OUT="${GINN_SPIKE_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s07}"
IMAGE=localhost/ginn-s07:part-a
ALPINE=docker.io/library/alpine:3
DEBIAN=docker.io/library/debian:stable
PM=(spike_podman)
TABLE="$OUT/attempts.tsv"

log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
mem_ok() { awk '/MemAvailable/ { exit !($2 >= 262144) }' /proc/meminfo || die "MemAvailable below 256 MiB"; }

# XDG pin. PM names spike_podman, so every host podman call (the "${PM[@]}" uses below) runs
# with XDG_RUNTIME_DIR set to the spike-owned directory XRD. The spike refuses to run podman
# unless XRD is a real directory, owned by this user, with mode 0700.
XRD=/run/user/1000/ginn-spike-s07-xrd
spike_xrd_check() {
  [[ $(id -u) == 1000 ]] || die "refusing to run: the S-07 XDG pin is for uid 1000"
  [[ -e $XRD ]] || mkdir -m 0700 "$XRD" || die "cannot create $XRD (is /run/user/1000 present?)"
  [[ -d $XRD && ! -L $XRD && -O $XRD && $(stat -c %a "$XRD") == 700 ]] \
    || die "refusing to run: $XRD is not a 0700 directory owned by this user"
}
spike_podman() {
  spike_xrd_check
  XDG_RUNTIME_DIR="$XRD" podman --root "$ROOT_DIR" --runroot "$RUN_DIR" \
    --tmpdir "$RUN_DIR/libpod-tmp" "$@"
}

# outer_run <name> [podman run options...]: the outer stand-in with the shared options.
# --init uses a host copy of catatonit (the host has none; see extract_catatonit).
outer_run() {
  local name=$1; shift
  "${PM[@]}" rm -f "$name" >/dev/null 2>&1 || true
  "${PM[@]}" run -d --name "$name" --userns keep-id --network host \
    --init --init-path "$OUT/catatonit" \
    "$@" "$IMAGE" sleep infinity >/dev/null
}

# inner_setup <name> <subuid lines>: write the realm user's /etc/subuid and /etc/subgid
# as root in the outer container, plus the inner storage and containers configuration.
# An empty argument leaves no subordinate range (podman then uses a single mapping).
inner_setup() {
  local name=$1 subs=$2
  "${PM[@]}" exec -u 0 -e SUB="$subs" "$name" sh -c '
    if [ -n "$SUB" ]; then printf "%s\n" "$SUB" > /etc/subuid; else : > /etc/subuid; fi
    cp /etc/subuid /etc/subgid'
  "${PM[@]}" exec -i "$name" sh -c 'mkdir -p ~/.config/containers ~/.local/share/containers
    cat > ~/.config/containers/storage.conf' <<'CONF'
[storage]
driver = "overlay"

[storage.options.overlay]
mount_program = "/usr/bin/fuse-overlayfs"
CONF
  "${PM[@]}" exec -i "$name" sh -c 'cat > ~/.config/containers/containers.conf' <<'CONF'
[containers]
cgroups = "disabled"

[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
CONF
}

# inner <name> <command>: run a shell command as the realm user (keep-id uid 1000).
inner() {
  local name=$1 cmd=$2
  "${PM[@]}" exec "$name" sh -c "export XDG_RUNTIME_DIR=/tmp/s07-run
    mkdir -p -m 700 \"\$XDG_RUNTIME_DIR\"; $cmd"
}

# record <id> <outer options> <subuid> <note> <rc> <first error>: one row per attempt.
record() {
  printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$1" "$2" "${3//$'\n'/;}" "$4" "$5" "$6" >> "$TABLE"
  log "attempt $1 rc=$5 [$4] $6"
}

# attempt_raw <id> <subuid> <note> <inner command> -- <outer options...>
# Starts a fresh outer container with exactly the given options.
attempt_raw() {
  local id=$1 subs=$2 note=$3 cmd=$4 name rc=0 log_file errs opts
  shift 4
  if [[ ${1:-} == -- ]]; then shift; fi
  name="s07-$id"
  log_file="$OUT/$id.log"
  opts="$*"
  mem_ok
  if ! outer_run "$name" "$@" >"$log_file" 2>&1; then
    record "$id" "$opts" "$subs" "$note" "outer-failed" "$(tail -1 "$log_file")"
    return 0
  fi
  inner_setup "$name" "$subs" >>"$log_file" 2>&1
  inner "$name" "$cmd" >>"$log_file" 2>&1 || rc=$?
  errs=$(grep -E '^Error:|newuidmap:|pasta failed|lchown|Failed to open|read-only|Permission denied' "$log_file" \
    | head -2 | tr '\n' ' ' || true)
  record "$id" "$opts" "$subs" "$note" "$rc" "${errs:-none}"
  "${PM[@]}" rm -f "$name" >/dev/null 2>&1 || true
}

# run_attempt: attempt_raw with the spec base (/dev/fuse, label=disable) prepended.
run_attempt() {
  local id=$1 subs=$2 note=$3 cmd=$4
  shift 4
  if [[ ${1:-} == -- ]]; then shift; fi
  attempt_raw "$id" "$subs" "$note" "$cmd" -- --device /dev/fuse --security-opt label=disable "$@"
}

# extract_catatonit: copy the init binary out of the image (the host has no catatonit).
extract_catatonit() {
  "${PM[@]}" rm -f s07-ctr >/dev/null 2>&1 || true
  "${PM[@]}" create --name s07-ctr "$IMAGE" >/dev/null
  "${PM[@]}" cp s07-ctr:/usr/bin/catatonit "$OUT/catatonit"
  "${PM[@]}" rm -f s07-ctr >/dev/null
}

# dump <name> <label>: capability, device, mapping and seccomp state of a container.
dump() {
  local name=$1 label=$2
  {
    echo "== $label ($name)"
    echo "-- process view (realm user)"
    "${PM[@]}" exec "$name" sh -c 'id; grep -E "^(Cap|NoNewPrivs|Seccomp)" /proc/self/status' || true
    echo "-- init (PID 1)"
    "${PM[@]}" exec "$name" sh -c 'grep -E "^(Cap(Bnd|Eff)|NoNewPrivs|Seccomp):" /proc/1/status' || true
    echo "-- uid_map (container -> parent userns)"
    "${PM[@]}" exec "$name" sh -c 'cat /proc/1/uid_map' || true
    echo "-- gid_map"
    "${PM[@]}" exec "$name" sh -c 'cat /proc/1/gid_map' || true
    echo "-- character and block devices in /dev"
    "${PM[@]}" exec "$name" sh -c 'ls -l /dev | grep -E "^[cb]" || true' || true
    "${PM[@]}" exec "$name" sh -c 'ls -l /dev/net 2>&1' || true
  } >>"$OUT/caps.txt" 2>&1
}

# ---------------------------------------------------------------- run

# A rows: the attempt matrix. N rows: inner networking on the chosen set (A5).
# M rows: minimality, one flag removed or narrowed from the chosen set. K rows: the
# capability dump, /dev/kvm and keep-groups. Each row starts a fresh outer container
# unless noted. Raw logs are $OUT/<id>.log.
cmd_run() {
  mkdir -p "$OUT" "$ROOT_DIR" "$RUN_DIR/libpod-tmp" "$OUT/ctx"
  : > "$OUT/run.log"
  : > "$OUT/caps.txt"
  : > "$OUT/kvm.txt"
  printf 'id\touter options\tsubuid\tnote\trc\tfirst error\n' > "$TABLE"
  log "S-07 start: root=$ROOT_DIR run=$RUN_DIR out=$OUT"

  "${PM[@]}" build -t "$IMAGE" -f "$SPIKE_DIR/Dockerfile.s07" "$OUT/ctx" >"$OUT/build.log" 2>&1 \
    || die "image build failed, see $OUT/build.log"
  extract_catatonit

  local unmask=(--security-opt 'unmask=/proc/*')
  local tun=(--device /dev/net/tun)
  local sysadmin=(--cap-add SYS_ADMIN)
  local fuse=(--device /dev/fuse)
  local label=(--security-opt label=disable)
  local one_range='dev:1001:64000'
  local two_ranges=$'dev:1:999\ndev:1001:64536'
  local pull="podman run --rm $ALPINE true"
  local chosen=("${unmask[@]}" "${sysadmin[@]}" "${tun[@]}")

  log "A: attempt matrix (inner: $pull)"
  run_attempt A1 "$one_range" 'spec flags, default seccomp, no SYS_ADMIN' "$pull" -- "${unmask[@]}"
  run_attempt A2 "$one_range" 'A1 + seccomp=unconfined (informative only)' "$pull" -- \
    "${unmask[@]}" --security-opt seccomp=unconfined
  run_attempt A3 "$one_range" 'A1 + /dev/net/tun' "$pull" -- "${unmask[@]}" "${tun[@]}"
  run_attempt A4 "$one_range" 'A1 + SYS_ADMIN, no tun' "$pull" -- "${unmask[@]}" "${sysadmin[@]}"
  run_attempt A5 "$one_range" 'A1 + SYS_ADMIN + tun' "$pull" -- "${chosen[@]}"
  run_attempt A6 "$two_ranges" 'A5 with two subuid lines (inner 0..65535)' "$pull" -- "${chosen[@]}"
  run_attempt A7 "" 'A1 + tun with no subuid (single mapping)' "$pull" -- "${unmask[@]}" "${tun[@]}"
  run_attempt A8 "$one_range" 'A5 without unmask' "$pull" -- "${sysadmin[@]}" "${tun[@]}"

  log "N: inner networking on the chosen set (A5)"
  local probe="sh -c 'echo ok; id -u'"
  run_attempt N1 "$one_range" 'inner default network (pasta)' \
    "podman run --rm $ALPINE $probe" -- "${chosen[@]}"
  run_attempt N2 "$one_range" 'inner --network host' \
    "podman run --rm --network host $ALPINE $probe" -- "${chosen[@]}"
  run_attempt N3 "$one_range" 'inner --network none' \
    "podman run --rm --network none $ALPINE $probe" -- "${chosen[@]}"
  run_attempt N4 "$one_range" 'inner --network slirp4netns' \
    "podman run --rm --network slirp4netns $ALPINE $probe" -- "${chosen[@]}"
  run_attempt N5 "$one_range" 'chosen minus tun, inner --network host' \
    "podman run --rm --network host $ALPINE $probe" -- "${unmask[@]}" "${sysadmin[@]}"

  log "M: minimality of the chosen set"
  attempt_raw M1 "$one_range" 'chosen minus label=disable' "$pull" -- "${fuse[@]}" "${chosen[@]}"
  attempt_raw M2 "$one_range" 'chosen minus /dev/fuse' "$pull" -- "${label[@]}" "${chosen[@]}"
  attempt_raw M3 "$one_range" 'chosen with unmask=/proc/sys instead of /proc/*' "$pull" -- \
    "${fuse[@]}" "${label[@]}" --security-opt 'unmask=/proc/sys' "${sysadmin[@]}" "${tun[@]}"
  attempt_raw M4 "$one_range" 'chosen with unmask=/proc/sys/net instead of /proc/*' "$pull" -- \
    "${fuse[@]}" "${label[@]}" --security-opt 'unmask=/proc/sys/net' "${sysadmin[@]}" "${tun[@]}"

  attempt_raw M5 "$one_range" 'chosen with no-new-privileges' "$pull" -- "${fuse[@]}" "${label[@]}" "${chosen[@]}" --security-opt no-new-privileges
  log "K: capability and device dump, /dev/kvm, keep-groups"
  outer_run s07-cap-plain >/dev/null
  dump s07-cap-plain "plain: keep-id, host network, init only"
  "${PM[@]}" rm -f s07-cap-plain >/dev/null 2>&1 || true
  outer_run s07-cap-base "${fuse[@]}" "${label[@]}" "${unmask[@]}" >/dev/null
  dump s07-cap-base "spec base: fuse device, label=disable, unmask=/proc/*"
  "${PM[@]}" rm -f s07-cap-base >/dev/null 2>&1 || true
  outer_run s07-chosen "${fuse[@]}" "${label[@]}" "${chosen[@]}" --device /dev/kvm >"$OUT/chosen.log" 2>&1
  inner_setup s07-chosen "$one_range"
  dump s07-chosen "chosen: spec base + SYS_ADMIN + tun + kvm"
  {
    echo '-- /dev/kvm as realm user (uid 1000)'
    inner s07-chosen 'ls -l /dev/kvm; (exec 3<>/dev/kvm) && echo open-ok || echo open-denied' || true
    echo '-- /dev/kvm as container root (uid 0)'
    "${PM[@]}" exec -u 0 s07-chosen sh -c '(exec 3<>/dev/kvm) && echo open-ok || echo open-denied' || true
  } >>"$OUT/kvm.txt" 2>&1
  "${PM[@]}" rm -f s07-chosen >/dev/null 2>&1 || true
  {
    echo '-- keep-groups: realm user groups and /dev/kvm open (crun not installed; runc used)'
    if outer_run s07-kg "${fuse[@]}" "${label[@]}" "${chosen[@]}" --device /dev/kvm --group-add keep-groups \
      >"$OUT/kg.log" 2>&1; then
      inner_setup s07-kg "$one_range"
      "${PM[@]}" exec s07-kg sh -c 'id; ls -l /dev/kvm; (exec 3<>/dev/kvm) && echo open-ok || echo open-denied' || true
    else
      echo "keep-groups run failed: $(tail -1 "$OUT/kg.log")"
    fi
    "${PM[@]}" rm -f s07-kg >/dev/null 2>&1 || true
  } >>"$OUT/kvm.txt" 2>&1
  log "K: done, see caps.txt and kvm.txt"

  log "B: Docker daemon in a realm (services on)"
  local uid cg
  uid=$(id -u)
  cg="/sys/fs/cgroup/ginn/$uid"
  if [[ -w "$cg/cgroup.procs" ]]; then
    echo "DEFERRED: $cg is user-writable but created by a concurrent root step (another spike); README asks for one spike at a time, so part B was not run" > "$OUT/partb.txt"
  else
    echo "SKIPPED: $cg absent or not user-writable; needs S-01 root setup (Host Setup); not run concurrently" > "$OUT/partb.txt"
  fi
  log "B: $(cat "$OUT/partb.txt")"
  mem_ok
  "${PM[@]}" run --rm "$DEBIAN" sh -c 'apt-get update -qq >/dev/null 2>&1; apt-cache policy docker.io rootlesskit slirp4netns | grep -E "^[a-z]|Candidate"' \
    > "$OUT/partb-probe.txt" 2>&1 || true
  log "B: Debian package candidates in $OUT/partb-probe.txt"
  log "S-07 run finished"
}

# teardown: containers, image and spike storage under the explicit --root. Logs in
# GINN_SPIKE_OUT stay for the record.
cmd_teardown() {
  # Only the spike's own --root, --runroot and --tmpdir. Never the shared libpod tmpdir.
  "${PM[@]}" rm -af >/dev/null 2>&1 || true
  "${PM[@]}" rmi -af >/dev/null 2>&1 || true
  "${PM[@]}" unshare rm -rf "$ROOT_DIR" "$RUN_DIR" >/dev/null 2>&1 || true
  rm -rf "$ROOT_DIR" "$RUN_DIR"
  rmdir "$(dirname "$ROOT_DIR")" 2>/dev/null || true
  echo "teardown: removed $ROOT_DIR and $RUN_DIR"
}

case "${1:-}" in
  run) spike_xrd_check; cmd_run ;;
  teardown) spike_xrd_check; cmd_teardown ;;
  *) die "usage: $0 run|teardown" ;;
esac
