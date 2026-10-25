#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-07f (FR-062 AC-3 and AC-4, NFR-SEC-002, FR-013, CR-007, FR-021): the Development set from docs/capabilities.md and
# ADR-006, run together in one outer container. The realm user is created with useradd -K, and the Realm Home holds
# the storage and container config files. Question: does `podman run --rm docker.io/library/alpine:3 true` exit 0
# inside the decided set (AC-3)? The same container also checks id, /dev/kvm open, and the inner storage driver.
#
#   XDG_RUNTIME_DIR=/run/user/<uid>/ginn-spike-s07f-xrd spikes/s07f_development_set.sh prepare
#       pull the three base images by their trust.toml digests, build the Debian realm image and the Arch and Fedora
#       useradd images, copy catatonit out of the realm image, generate the sethostname-only profile
#   ... run       prepare, then case A (storage.conf and containers.conf), case B (storage.conf only, no
#                 containers.conf) and case C (case A plus --security-opt no-new-privileges)
#   ... teardown  remove containers, images and storage under the spike roots, stop the spike pause process by exact
#                 pid, remove the runroot and the spike XDG dir, then scan /proc for spike processes
#
# Host podman: every call goes through pm, which passes --root, --runroot, --tmpdir and XDG_RUNTIME_DIR pinned to the
# spike dir. require_xdg refuses any other XDG_RUNTIME_DIR. Inner podman runs in the realm with HOME=/home/dev only: no
# XDG_RUNTIME_DIR, and no --root, --runroot, --tmpdir, --storage-driver or --storage-opt.
# No --privileged, no SYS_ADMIN, no sudo, no xbps, no host package installs. Raw logs stay out of the repository.
set -euo pipefail

UIDN=$(id -u)
SELF="$(readlink -f "$0")"
SPIKE_DIR="$(dirname "$SELF")"
TRUST="$SPIKE_DIR/../src/ginn/trust/data/trust.toml"
SPIKE_XRD="/run/user/$UIDN/ginn-spike-s07f-xrd"
RUN_DIR="/run/user/$UIDN/ginn-spike-s07f"
ROOT_DIR="$HOME/.local/share/ginn-spike-s07f/podman"
OUT="${GINN_S07F_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s07f}"
TABLE="$OUT/attempts.tsv"
PM=(env XDG_RUNTIME_DIR="$SPIKE_XRD" podman --root "$ROOT_DIR" --runroot "$RUN_DIR" --tmpdir "$RUN_DIR/libpod-tmp")
mkdir -p "$OUT"

# Base images: the platform digests in src/ginn/trust/data/trust.toml. pull_one checks each one against the file.
DEB_DIGEST=sha256:d405c1e1d9eecbf30022d23766a0eea235e7f499aa33e357dfd29271a839f82a
ARCH_DIGEST=sha256:c556b483a8e653aa6389bb66263e1d96cd6e17c67ca5968c87495d313c4a2e93
FEDORA_DIGEST=sha256:86d2e2423cce020de50ff67579216b799e57247f5565b2891f3870189d84dd4b
DEB_BASE="docker.io/library/debian@$DEB_DIGEST"
ARCH_BASE="quay.io/archlinux/archlinux@$ARCH_DIGEST"
FEDORA_BASE="registry.fedoraproject.org/fedora-minimal@$FEDORA_DIGEST"
REALM_IMG=localhost/ginn-s07f:debian-realm
ARCH_IMG=localhost/ginn-s07f:arch-useradd
FEDORA_IMG=localhost/ginn-s07f:fedora-useradd
ALPINE=docker.io/library/alpine:3
USERADD_K="-K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0 -m -u 1000 dev"
SECCOMP_SRC=/usr/share/containers/seccomp.json
SECCOMP_SHA=$(sha256sum "$SECCOMP_SRC" | cut -c1-12)
PROFILE="$OUT/seccomp-$SECCOMP_SHA.json"

# Outer options: the Development set (docs/capabilities.md, Outer options) on the base argv. --init-path is a host
# copy of catatonit, because the host has no catatonit at the time of the first run (base argv, FR-082).
OUTER_ARGS=(--runtime crun --userns keep-id --network host --init --init-path "$OUT/catatonit"
  --device /dev/fuse --device /dev/net/tun --device /dev/kvm
  --security-opt 'unmask=/proc/*' --security-opt "seccomp=$PROFILE" --group-add keep-groups)

# Realm Home config files (ADR-006). Written as files, read by the inner podman. No storage flags on the command line.
STORAGE_CONF='[storage]
driver = "overlay"

[storage.options.overlay]
mount_program = "/usr/bin/fuse-overlayfs"
ignore_chown_errors = "true"'
CONTAINERS_CONF='[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"'

# Realm user checks, run as uid 1000 with HOME=/home/dev only. The last command's exit code is the inner run's exit code.
read -r -d '' CHECK_BODY <<'BODY' || true
echo "== id"; id
echo "== XDG_RUNTIME_DIR inside the realm: ${XDG_RUNTIME_DIR:-unset}"
echo "== status Groups"; grep '^Groups' /proc/self/status
echo "== uid_map"; cat /proc/self/uid_map
echo "== /dev/kvm"; ls -ln /dev/kvm; if (exec 3<>/dev/kvm) 2>/dev/null; then echo "KVM_OPEN_RW=ok"; else echo "KVM_OPEN_RW=denied"; fi
echo "== dev lines in /etc/subuid and /etc/subgid"; echo "subuid_lines=$(grep -c '^dev:' /etc/subuid 2>/dev/null || true)"; echo "subgid_lines=$(grep -c '^dev:' /etc/subgid 2>/dev/null || true)"
echo "== config files"
for f in /home/dev/.config/containers/storage.conf /home/dev/.config/containers/containers.conf /etc/containers/containers.conf /usr/share/containers/containers.conf; do if [ -e "$f" ]; then echo "present $f"; else echo "absent $f"; fi; done
echo "== inner podman version"; podman --version
echo "== inner fuse-overlayfs"; command -v fuse-overlayfs; fuse-overlayfs --version 2>&1 | head -2
echo "== inner podman info: driver, runtime, cgroup manager, event logger"
podman info --format 'driver={{.Store.GraphDriverName}} graphRoot={{.Store.GraphRoot}} cgroupManager={{.Host.CgroupManager}} eventLogger={{.Host.EventLogger}} ociRuntime={{.Host.OCIRuntime.Name}} {{.Host.OCIRuntime.Version}}'; echo "info_rc=$?"
echo "== inner podman info: graphOptions"; podman info --format '{{json .Store.GraphOptions}}'; echo "info_rc=$?"
echo "== inner podman info (full)"; podman info; echo "info_full_rc=$?"
echo "== inner run: podman run --rm alpine true"; podman run --rm docker.io/library/alpine:3 true; rc=$?; echo "INNER_RC=$rc"
echo "== inner alpine digest"; podman image inspect --format '{{.Digest}}' docker.io/library/alpine:3 2>&1
exit $rc
BODY

# Image state probe: the /etc/subuid and /etc/subgid lines of an image, and the dev user.
read -r -d '' STATE_BODY <<'BODY' || true
echo "-- /etc/subuid"; cat /etc/subuid 2>&1 || echo "absent"
echo "-- /etc/subgid"; cat /etc/subgid 2>&1 || echo "absent"
echo "-- dev lines in /etc/subuid: $(grep -c '^dev:' /etc/subuid 2>/dev/null || true)"
echo "-- dev lines in /etc/subgid: $(grep -c '^dev:' /etc/subgid 2>/dev/null || true)"
echo "-- id dev"; id dev 2>&1 || echo "no dev user"
BODY

# Control: plain useradd on a base image, to show what the -K flags change.
read -r -d '' CONTROL_BODY <<BODY || true
command -v useradd; useradd -m -u 1000 dev 2>&1; echo "control useradd rc=\$?"
$STATE_BODY
BODY

log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

require_xdg() {
  [[ ${XDG_RUNTIME_DIR:-} == "$SPIKE_XRD" ]] \
    || die "refusing: XDG_RUNTIME_DIR='${XDG_RUNTIME_DIR:-unset}', must be $SPIKE_XRD"
}

# The only way podman runs in this script. Guard first, then pm passes XDG_RUNTIME_DIR pinned to the spike dir.
pm() {
  require_xdg
  "${PM[@]}" "$@"
}

# spike_path_ok PATH: absolute, a component starts with ginn-spike-s07f, no . or .. components, no symlink in any
# component (not only the leaf), and not $HOME, an ancestor of $HOME or /.
spike_path_ok() {
  local path="$1" part cur="" comp hit=0
  local -a parts=() comps=()
  [[ $path == /* ]] || return 1
  IFS=/ read -r -a parts <<< "$path"
  for part in "${parts[@]}"; do
    case "$part" in
      . | ..) return 1 ;;
      ginn-spike-s07f*) hit=1 ;;
    esac
  done
  (( hit )) || return 1
  IFS=/ read -r -a comps <<< "$path"
  for comp in "${comps[@]}"; do
    if [[ -n $comp ]]; then
      cur="$cur/$comp"
      if [[ -L $cur ]]; then return 1; fi
    fi
  done
  local home="${HOME%/}"
  [[ -n $home && $path != "$home" && $home != "$path"/* ]] || return 1
  return 0
}

# stop_spike_pause: stop the rootless pause process under the spike XDG, by exact pid, and only when its environ carries
# XDG_RUNTIME_DIR equal to the spike dir. Call it after the last podman call. It records which process it found.
stop_spike_pause() {
  local pidf="$SPIKE_XRD/libpod/tmp/pause.pid" pid env_txt comm
  if [[ ! -f $pidf ]]; then log "pause: no spike pause.pid"; return 0; fi
  pid=$(cat "$pidf")
  [[ $pid =~ ^[0-9]+$ ]] || die "pause.pid does not hold a pid: $pidf"
  if [[ ! -r /proc/$pid/environ ]]; then log "pause: pid $pid not running"; return 0; fi
  env_txt=$(tr '\0' '\n' < "/proc/$pid/environ")
  grep -qx "XDG_RUNTIME_DIR=$SPIKE_XRD" <<<"$env_txt" \
    || die "pid $pid does not carry the spike XDG; not signalled"
  comm=$(tr '\0' ' ' < "/proc/$pid/cmdline")
  log "pause: stopping spike pause pid $pid ($comm)"
  kill -TERM "$pid"
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    [[ -e /proc/$pid ]] || break
    sleep 0.5
  done
  if [[ -e /proc/$pid ]]; then die "pid $pid did not exit after SIGTERM"; fi
  log "pause: pid $pid exited"
}

ensure_dirs() {
  mkdir -p "$OUT/logs" "$OUT/ctx" "$ROOT_DIR" "$RUN_DIR/libpod-tmp"
  mkdir -p -m 700 "$SPIKE_XRD"
  [[ $(stat -c '%a %u' "$SPIKE_XRD") == "700 $UIDN" ]] || die "spike XDG dir is not 0700 and owned by this user: $SPIKE_XRD"
  spike_path_ok "$ROOT_DIR" || die "spike root fails the path check: $ROOT_DIR"
  spike_path_ok "$RUN_DIR" || die "spike run dir fails the path check: $RUN_DIR"
  spike_path_ok "$SPIKE_XRD" || die "spike XDG dir fails the path check: $SPIKE_XRD"
}

record_env() {
  {
    echo "podman (host): $(pm --version)"
    echo "podman (host) info: $(pm info --format 'ociRuntime={{.Host.OCIRuntime.Name}} {{.Host.OCIRuntime.Version}} rootless={{.Host.Security.Rootless}} graphDriver={{.Store.GraphDriverName}}')"
    echo "crun (host): $(crun --version | head -1)"
    echo "fuse-overlayfs (host): $(command -v fuse-overlayfs) $(fuse-overlayfs --version 2>&1 | head -1)"
    echo "kernel: $(uname -r)"
    echo "host user: $(id)"
    echo "seccomp source: $(sha256sum "$SECCOMP_SRC")"
    echo "seccomp profile: $PROFILE"
    echo "pause process at record time: $(if [[ -f $SPIKE_XRD/libpod/tmp/pause.pid ]]; then cat "$SPIKE_XRD/libpod/tmp/pause.pid"; else echo none; fi)"
    echo "bash: $BASH_VERSION"
  } > "$OUT/env.txt"
}

# pull_one NAME REF DIGEST: pull a base image by digest and check the digest the store reports against trust.toml.
pull_one() {
  local name=$1 ref=$2 want=$3 rc=0 got=""
  grep -q "$want" "$TRUST" || die "$want is not in $TRUST"
  pm pull "$ref" > "$OUT/logs/pull-$name.log" 2>&1 || rc=$?
  if (( rc == 0 )); then got=$(pm image inspect --format '{{.Digest}}' "$ref" 2>/dev/null || true); fi
  printf '%s\t%s\t%s\t%s\n' "$name" "$rc" "$want" "${got:-none}" >> "$OUT/pull-rc.tsv"
  log "pull $name: rc=$rc store digest=${got:-none} trust.toml=$want"
  (( rc == 0 )) && [[ $got == "$want" ]]
}

pull_bases() {
  : > "$OUT/pull-rc.tsv"
  pull_one debian "$DEB_BASE" "$DEB_DIGEST" || die "debian base pull or digest check failed; see $OUT/logs/pull-debian.log"
  pull_one arch "$ARCH_BASE" "$ARCH_DIGEST" || log "arch base not usable; the arch build will record the failure"
  pull_one fedora "$FEDORA_BASE" "$FEDORA_DIGEST" || log "fedora base not usable; the fedora build will record the failure"
}

# build_one IMG DOCKERFILE TAG: build with the decided useradd form. Records the rc in build-rc.tsv.
build_one() {
  local img=$1 df=$2 tag=$3 rc=0
  pm build --build-arg "REALM_USERADD=$USERADD_K" -t "$img" -f "$df" "$OUT/ctx" \
    > "$OUT/logs/build-$tag.log" 2>&1 || rc=$?
  printf '%s\t%s\n' "$tag" "$rc" >> "$OUT/build-rc.tsv"
  log "build $tag: rc=$rc"
  return "$rc"
}

build_images() {
  : > "$OUT/build-rc.tsv"
  build_one "$REALM_IMG" "$SPIKE_DIR/Dockerfile.s07f-debian" debian-realm \
    || die "debian realm build failed; see $OUT/logs/build-debian-realm.log"
  build_one "$ARCH_IMG" "$SPIKE_DIR/Dockerfile.s07f-arch" arch-useradd || true
  build_one "$FEDORA_IMG" "$SPIKE_DIR/Dockerfile.s07f-fedora" fedora-useradd || true
}

extract_catatonit() {
  if [[ -s $OUT/catatonit ]]; then return 0; fi
  if pm container exists s07f-ext; then pm rm -f s07f-ext >/dev/null; fi
  pm create --name s07f-ext "$REALM_IMG" >/dev/null
  pm cp s07f-ext:/usr/bin/catatonit "$OUT/catatonit"
  pm rm -f s07f-ext >/dev/null
  chmod 0755 "$OUT/catatonit"
  log "catatonit copied from the realm image to $OUT/catatonit"
}

make_profile() {
  python3 -I "$SPIKE_DIR/s07e_seccomp.py" "$SECCOMP_SRC" "$PROFILE" sethostname | tee -a "$OUT/logs/seccomp.log"
  [[ -s $PROFILE ]] || die "profile not written: $PROFILE"
}

image_ids() {
  local ref
  : > "$OUT/image-ids.txt"
  for ref in "$DEB_BASE" "$ARCH_BASE" "$FEDORA_BASE" "$REALM_IMG" "$ARCH_IMG" "$FEDORA_IMG"; do
    printf '%s\n  %s\n' "$ref" "$(pm image inspect --format 'Id={{.Id}} Digest={{.Digest}}' "$ref" 2>&1 || echo 'not present')" \
      >> "$OUT/image-ids.txt"
  done
}

# state_probe LABEL IMG BODY: run BODY in IMG (--rm, crun) and append the output to useradd-state.txt.
state_probe() {
  local label=$1 img=$2 body=$3 rc=0
  if ! pm image exists "$img"; then
    printf '== %s (%s)\nnot present in the spike store\n\n' "$label" "$img" >> "$OUT/useradd-state.txt"
    return 0
  fi
  {
    printf '== %s (%s)\n' "$label" "$img"
    pm run --rm --runtime crun "$img" sh -c "$body" 2>&1 || rc=$?
    printf 'run_rc=%s\n\n' "$rc"
  } >> "$OUT/useradd-state.txt"
}

useradd_state() {
  : > "$OUT/useradd-state.txt"
  state_probe "Debian realm image, useradd -K (decided form)" "$REALM_IMG" "$STATE_BODY"
  state_probe "Arch image, useradd -K" "$ARCH_IMG" "$STATE_BODY"
  state_probe "Fedora image, useradd -K" "$FEDORA_IMG" "$STATE_BODY"
  state_probe "control: Debian base, plain useradd" "$DEB_BASE" "$CONTROL_BODY"
  state_probe "control: Arch base, plain useradd" "$ARCH_BASE" "$CONTROL_BODY"
  state_probe "control: Fedora base, plain useradd" "$FEDORA_BASE" "$CONTROL_BODY"
  state_probe "Debian realm image: system containers.conf engine values" "$REALM_IMG" \
    "grep -nE '^\[|cgroup_manager|events_logger' /usr/share/containers/containers.conf 2>&1 || echo 'no matching lines'"
}

first_error() {
  local line
  if [[ ! -s $1 ]]; then echo none; return; fi
  # The "cannot find UID/GID" and "single mapping" lines are podman's single-ID fallback (expected), not failures.
  line=$(grep -v -E 'cannot find UID/GID|Using rootless single mapping' "$1" | grep -m1 -E 'level=(error|fatal)|^Error:|^error:|crun:|rror: |not permitted|Permission denied|Invalid argument|rc=[1-9]|RC=[1-9]|KVM_OPEN_RW=denied' || true)
  if [[ -z $line ]]; then echo none; else printf '%s' "${line:0:240}"; fi
}

# record ID NOTE OUTER_ARGV OUTER_RC CHECK_RC LOG
record() {
  local err
  err=$(first_error "$6")
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" "$err" >> "$TABLE"
  log "$1: outer rc=$4 check rc=$5 first error: $err"
}

# write_config NAME both|storage: the Realm Home files, written as the realm user.
write_config() {
  local name=$1 cfg=$2
  printf '%s\n' "$STORAGE_CONF" | pm exec -i --user 1000:1000 -e HOME=/home/dev "$name" \
    sh -c 'mkdir -p /home/dev/.config/containers && cat > /home/dev/.config/containers/storage.conf'
  if [[ $cfg == both ]]; then
    printf '%s\n' "$CONTAINERS_CONF" | pm exec -i --user 1000:1000 -e HOME=/home/dev "$name" \
      sh -c 'mkdir -p /home/dev/.config/containers && cat > /home/dev/.config/containers/containers.conf'
  fi
  # Control D: also remove the image's system containers.conf (in the container's own layer, not the image).
  if [[ $cfg == none ]]; then
    pm exec --user 0:0 "$name" sh -c 'rm -f /usr/share/containers/containers.conf'
  fi
}

# case_outer ID NOTE both|storage [extra outer options]: one fresh outer container, the Realm Home files, the checks.
case_outer() {
  local id=$1 note=$2 cfg=$3 name outer_s rc_outer=0 rc_check=0
  shift 3
  name="s07f-$id"
  if pm container exists "$name"; then pm rm -f "$name" >/dev/null; fi
  OUTER_ARGV=("${PM[@]}" run -d --name "$name" "${OUTER_ARGS[@]}" "$@" "$REALM_IMG" sleep infinity)
  outer_s=$(printf '%q ' "${OUTER_ARGV[@]}")
  require_xdg
  "${OUTER_ARGV[@]}" > "$OUT/logs/$id.outer.log" 2>&1 || rc_outer=$?
  log "$id: outer rc=$rc_outer"
  if (( rc_outer != 0 )); then
    record "$id" "$note" "$outer_s" "$rc_outer" "not run" "$OUT/logs/$id.outer.log"
    if pm container exists "$name"; then pm rm -f "$name" >/dev/null; fi
    return 0
  fi
  write_config "$name" "$cfg"
  pm exec --user 1000:1000 -e HOME=/home/dev "$name" sh -c "$CHECK_BODY" \
    > "$OUT/logs/$id.check.log" 2>&1 || rc_check=$?
  record "$id" "$note" "$outer_s" 0 "$rc_check" "$OUT/logs/$id.check.log"
  if pm container exists "$name"; then pm rm -f "$name" >/dev/null; fi
}

cmd_prepare() {
  ensure_dirs
  pull_bases
  build_images
  extract_catatonit
  make_profile
  record_env
  image_ids
  useradd_state
}

cmd_run() {
  cmd_prepare
  printf 'case\tnote\touter argv\touter rc\tcheck rc\tfirst error\n' > "$TABLE"
  case_outer A "canonical: storage.conf and containers.conf, full Development set" both
  case_outer B "storage.conf only, no containers.conf (fresh container)" storage
  case_outer C "case A plus --security-opt no-new-privileges" both --security-opt no-new-privileges
  case_outer D "control: storage.conf only, and the image's system containers.conf removed" none
  log "run: done"
}

cmd_teardown() {
  local p parent
  for p in "$ROOT_DIR" "$RUN_DIR" "$SPIKE_XRD"; do
    spike_path_ok "$p" || die "refusing teardown of $p: not a spike path"
  done
  if [[ -e $ROOT_DIR || -e $RUN_DIR ]]; then
    log "teardown: containers under the spike roots before removal: $(pm ps -aq | wc -l)"
    pm rm -af >/dev/null 2>&1 || true
    pm rmi -af >/dev/null 2>&1 || true
    pm unshare rm -rf -- "$ROOT_DIR" >/dev/null 2>&1 || log "teardown: unshare rm exited non-zero; checking what remains"
  fi
  if [[ -e $ROOT_DIR ]]; then
    local foreign
    foreign=$(find "$ROOT_DIR" ! -user "$UIDN" -print) || die "find failed under $ROOT_DIR; nothing removed"
    if [[ -n $foreign ]]; then
      printf '%s\n' "$foreign" | head -n 20 >&2
      die "entries not owned by the host user remain under $ROOT_DIR; not removing them"
    fi
    rm -rf -- "$ROOT_DIR"
  fi
  stop_spike_pause
  if [[ -e $RUN_DIR ]]; then rm -rf -- "$RUN_DIR"; fi
  if [[ -e $SPIKE_XRD ]]; then rm -rf -- "$SPIKE_XRD"; fi
  parent=$(dirname "$ROOT_DIR")
  if [[ -d $parent ]] && spike_path_ok "$parent"; then rmdir -- "$parent"; fi
  verify_clean
}

# verify_clean: no process may carry the spike XDG or a spike path in its argv. Scans /proc once. Skips this shell and
# its ancestors: the caller's own command line carries the spike XDG path when it is passed on the command line.
verify_clean() {
  local p hits=0 env_txt cmd_txt a ppid
  local -A skip=()
  a=$$
  while [[ -n $a && $a -gt 1 ]]; do
    skip[$a]=1
    ppid=$(sed 's/.*) //' "/proc/$a/stat" 2>/dev/null | cut -d' ' -f2 || true)
    a=${ppid:-0}
  done
  for p in /proc/[0-9]*; do
    [[ -z ${skip[${p#/proc/}]:-} ]] || continue
    env_txt=$(tr '\0' '\n' 2>/dev/null < "$p/environ" || true)
    if grep -qx "XDG_RUNTIME_DIR=$SPIKE_XRD" <<<"$env_txt"; then log "verify: left $p (environ XDG)"; hits=$((hits + 1)); fi
    cmd_txt=$(tr '\0' ' ' 2>/dev/null < "$p/cmdline" || true)
    if grep -q 'ginn-spike-s07[f]' <<<"$cmd_txt"; then log "verify: left $p (cmdline)"; hits=$((hits + 1)); fi
  done
  log "verify: spike processes left: $hits"
  if [[ -e $ROOT_DIR || -e $RUN_DIR || -e $SPIKE_XRD ]]; then log "verify: spike dirs remain"; else log "verify: spike dirs absent"; fi
}

require_xdg
case "${1:-}" in
  prepare) cmd_prepare ;;
  run) cmd_run ;;
  teardown) cmd_teardown ;;
  *) die "usage: XDG_RUNTIME_DIR=$SPIKE_XRD $0 prepare|run|teardown" ;;
esac
