#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-07e (FR-062 AC-3, FR-013, CR-007, NFR-SEC-002): can `podman run --rm <image> true` (AC-3) pass inside a
# Development-style outer container when the realm user has no subordinate-ID range, no setcap, no libcap, and
# inner rootless podman runs single-uid with ignore_chown_errors? The only image change is removing the realm
# user's /etc/subuid and /etc/subgid lines (Dockerfile.s07e).
#
#   spikes/s07e_no_subuid.sh build        base image (Dockerfile.s07, unchanged), no-subuid image (Dockerfile.s07e),
#                                         catatonit, seccomp profiles (s07e_seccomp.py), image state
#   spikes/s07e_no_subuid.sh run          build, then cases E0 E1 E2 E3 E4 E4c E5a E5b E5c in order
#   spikes/s07e_no_subuid.sh case <id>    build, then one case (E0 E1 E2 E3 E4 E4c E5a E5b E5c)
#   spikes/s07e_no_subuid.sh teardown     remove containers and storage under the spike roots only
#
# Every host podman call passes --root, --runroot and --tmpdir at spike-owned directories. The inner podman runs
# inside an outer container and gets the same three flags at container paths; its storage is the outer
# container's own filesystem under the spike root. No --privileged, no sudo, no xbps, no host package changes.
# Logs, the attempts table and generated profiles stay in the spike output directory, outside the repository.
set -euo pipefail

SELF="$(readlink -f "$0")"
SPIKE_DIR="$(dirname "$SELF")"
ROOT_DIR="${GINN_SPIKE_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/ginn-spike-s07e/podman}"
RUN_DIR="${GINN_SPIKE_RUNROOT:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ginn-spike-s07e}"
OUT="${GINN_SPIKE_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s07e}"
PM=(podman --root "$ROOT_DIR" --runroot "$RUN_DIR" --tmpdir "$RUN_DIR/libpod-tmp")
BASE_IMG=localhost/ginn-s07e:base
REALM_IMG=localhost/ginn-s07e:nosubuid
ALPINE=docker.io/library/alpine:3
TABLE="$OUT/attempts.tsv"

# Outer options shared by every case (the S-07 and S-07d shape). --init-path is a host copy of catatonit.
SHARED=(--runtime crun --userns keep-id --network host --init --init-path "$OUT/catatonit")
# Minimal outer set under test: devices and the proc unmask. The seccomp option is added per profile.
DEV_ALL=(--device /dev/fuse --device /dev/net/tun --security-opt 'unmask=/proc/*')

# Inner podman, as the realm user inside the outer container. Storage and run/tmp dirs are container paths.
IPM_ARG="podman --root /home/dev/.local/share/containers/storage --runroot /tmp/s07e-run --tmpdir /tmp/s07e-tmp"
# Inner storage settings. ISTOR_CONF passes nothing: the storage.conf file alone (the brief's form). It does not
# take effect when --root is on the command line (S-07e finding, see the results section), so the cases that
# pass need ISTOR_OVERLAY or ISTOR_VFS on the command line. Placeholder @ISTOR@ in the bodies takes these.
ISTOR_CONF=""
ISTOR_OVERLAY="--storage-driver overlay --storage-opt overlay.mount_program=/usr/bin/fuse-overlayfs --storage-opt overlay.ignore_chown_errors=true"
ISTOR_VFS="--storage-driver vfs --storage-opt vfs.ignore_chown_errors=true"
INNER_PRE='export XDG_RUNTIME_DIR=/tmp/s07e-xdg
mkdir -p -m 700 "$XDG_RUNTIME_DIR" /tmp/s07e-run /tmp/s07e-tmp
IPM="podman --root /home/dev/.local/share/containers/storage --runroot /tmp/s07e-run --tmpdir /tmp/s07e-tmp"'

STORAGE_OVERLAY='[storage]
driver = "overlay"

[storage.options.overlay]
mount_program = "/usr/bin/fuse-overlayfs"
ignore_chown_errors = "true"'

STORAGE_VFS='[storage]
driver = "vfs"

[storage.options.vfs]
ignore_chown_errors = "true"'

CONTAINERS_CONF='[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"'

read -r -d '' BODY_RUN <<'BODY' || true
echo "== id"; id
echo "== inner podman version and OCI runtime"; $IPM --version; command -v crun runc
echo "== inner podman info (full)"; $IPM @ISTOR@ info; echo "info rc=$?"
echo "== inner run"; $IPM @ISTOR@ run --rm docker.io/library/alpine:3 true; rc=$?; echo "INNER_RC=$rc"; exit $rc
BODY

read -r -d '' BODY_REALM_CHECK <<'BODY' || true
echo "== id"; id
echo "== /etc/subuid"; cat /etc/subuid; echo "(end)"
echo "== /etc/subgid"; cat /etc/subgid; echo "(end)"
echo "== setuid helpers"; ls -ln /usr/bin/newuidmap /usr/bin/newgidmap
for t in getcap setcap; do command -v $t || echo "$t absent"; done
if dpkg -s libcap2-bin >/dev/null 2>&1; then echo "libcap2-bin: installed"; else echo "libcap2-bin: absent"; fi
echo "== outer uid_map"; cat /proc/self/uid_map
BODY

read -r -d '' BODY_UIDMAP <<'BODY' || true
echo "== inner podman unshare: uid_map and gid_map"
$IPM @ISTOR@ unshare sh -c 'echo uid_map; cat /proc/self/uid_map; echo gid_map; cat /proc/self/gid_map'; echo "unshare rc=$?"
echo "== inner podman info: idMappings"; $IPM @ISTOR@ info | grep -i -A8 'idmappings'
echo "== inner debug run: newuidmap or newgidmap invoked?"
$IPM @ISTOR@ --log-level=debug run --rm docker.io/library/alpine:3 true > /tmp/s07e-debug.log 2>&1; echo "debug run rc=$?"
echo "debug log lines: $(wc -l < /tmp/s07e-debug.log)"
echo "newuidmap or newgidmap lines: $(grep -c -E 'newuidmap|newgidmap' /tmp/s07e-debug.log)"
echo "single-mapping or subid lines:"; grep -E 'single mapping|cannot find UID|subuid|subgid' /tmp/s07e-debug.log | cut -c1-240 | head -n 5
BODY

read -r -d '' BODY_E6 <<'BODY' || true
echo "== E6 single-uid chown inside an inner container"
$IPM @ISTOR@ run --rm docker.io/library/alpine:3 sh -c 'id; touch /x; echo "touch rc=$?"; chown 100:100 /x; echo "chown rc=$?"; ls -ln /x; echo "ls rc=$?"'
rc=$?; echo "E6_RC=$rc"; exit $rc
BODY

read -r -d '' BODY_SYSCALL_PROBE <<'BODY' || true
echo "== id"; id
echo "== unshare -r -u, no-op"; unshare -r -u sh -c 'true'; echo "unshare-true rc=$?"
echo "== sethostname only"; unshare -r -u sh -c 'hostname s07e-probe'; echo "hostname rc=$?"
echo "== setdomainname only"; unshare -r -u sh -c 'domainname s07e-probe'; echo "domainname rc=$?"
echo "== outer hostname unchanged: $(cat /proc/sys/kernel/hostname)"
BODY

log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
join() { local IFS=' '; printf '%s' "$*"; }

# spike_path_ok <path>: absolute, has a ginn-spike-s07e component, not $HOME, not an ancestor of $HOME,
# no symlink in the path. Used before any rm -rf.
spike_path_ok() {
  local p=$1
  [[ $p == /* ]] || return 1
  case $p in
    */ginn-spike-s07e | */ginn-spike-s07e/*) ;;
    *) return 1 ;;
  esac
  [[ $p != / && $p != "$HOME" && $HOME != "$p"/* ]] || return 1
  [[ ! -L $p ]] || return 1
  [[ "$(readlink -m -- "$p")" == "$p" ]] || return 1
}

check_roots() {
  spike_path_ok "$ROOT_DIR" || die "spike root fails the path check: $ROOT_DIR"
  spike_path_ok "$RUN_DIR" || die "spike run dir fails the path check: $RUN_DIR"
}

init_out() {
  check_roots
  mkdir -p "$OUT/logs" "$OUT/ctx" "$ROOT_DIR" "$RUN_DIR/libpod-tmp"
  if [[ ! -s $TABLE ]]; then
    printf 'id\tnote\timage\tprofile\tdriver\touter argv\tinner argv\touter rc\tinner rc\tfirst error\n' > "$TABLE"
  fi
  if [[ ! -s $OUT/env.txt ]]; then
    {
      echo "podman: $(podman --version)"
      echo "crun: $(crun --version | head -1)"
      echo "kernel: $(uname -r)"
      echo "host user: $(id)"
      echo "bash: $BASH_VERSION"
    } > "$OUT/env.txt"
  fi
}

build_images() {
  init_out
  if ! "${PM[@]}" image exists "$BASE_IMG"; then
    "${PM[@]}" build -t "$BASE_IMG" -f "$SPIKE_DIR/Dockerfile.s07" "$OUT/ctx" \
      > "$OUT/logs/build-base.log" 2>&1 || die "base build failed, see $OUT/logs/build-base.log"
  fi
  "${PM[@]}" build -t "$REALM_IMG" -f "$SPIKE_DIR/Dockerfile.s07e" "$OUT/ctx" \
    > "$OUT/logs/build-nosubuid.log" 2>&1 || die "no-subuid build failed, see $OUT/logs/build-nosubuid.log"
  if [[ ! -s $OUT/catatonit ]]; then
    if "${PM[@]}" container exists s07e-ext; then "${PM[@]}" rm -f s07e-ext >/dev/null; fi
    "${PM[@]}" create --name s07e-ext "$BASE_IMG" >/dev/null
    "${PM[@]}" cp s07e-ext:/usr/bin/catatonit "$OUT/catatonit"
    "${PM[@]}" rm -f s07e-ext >/dev/null
    chmod 0755 "$OUT/catatonit"
  fi
  python3 -I "$SPIKE_DIR/s07e_seccomp.py" /usr/share/containers/seccomp.json \
    "$OUT/seccomp-s07e-both.json" sethostname setdomainname | tee -a "$OUT/logs/seccomp.log"
  python3 -I "$SPIKE_DIR/s07e_seccomp.py" /usr/share/containers/seccomp.json \
    "$OUT/seccomp-s07e-host.json" sethostname | tee -a "$OUT/logs/seccomp.log"
  : > "$OUT/image-state.txt"
  local img
  for img in "$BASE_IMG" "$REALM_IMG"; do
    {
      echo "== image $img"
      "${PM[@]}" run --rm --runtime crun "$img" sh -c 'echo "-- /etc/subuid"; cat /etc/subuid; echo "-- /etc/subgid"; cat /etc/subgid; echo "-- setuid helpers"; ls -ln /usr/bin/newuidmap /usr/bin/newgidmap; for t in getcap setcap; do command -v $t || echo "$t absent"; done; dpkg -s libcap2-bin >/dev/null 2>&1; echo "libcap2-bin dpkg rc=$? (non-zero means absent)"'
    } >> "$OUT/image-state.txt" 2>&1
  done
  log "build: images $BASE_IMG $REALM_IMG; catatonit, seccomp profiles and image-state.txt written"
}

profile_args() {
  case $1 in
    none) PROF_ARGS=() ;;
    both) PROF_ARGS=(--security-opt "seccomp=$OUT/seccomp-s07e-both.json") ;;
    host) PROF_ARGS=(--security-opt "seccomp=$OUT/seccomp-s07e-host.json") ;;
    *) die "unknown profile $1" ;;
  esac
}

# start_outer <name> <image> <profile> <outer options...>: sets OUTER_ARGV and OUTER_RC.
start_outer() {
  local name=$1 img=$2 prof=$3
  shift 3
  profile_args "$prof"
  if "${PM[@]}" container exists "$name"; then "${PM[@]}" rm -f "$name" >/dev/null; fi
  OUTER_ARGV=("${PM[@]}" run -d --name "$name" "${SHARED[@]}" "$@" "${PROF_ARGS[@]}" "$img" sleep infinity)
  OUTER_RC=0
  "${OUTER_ARGV[@]}" > "$OUT/logs/$name.outer.log" 2>&1 || OUTER_RC=$?
  log "$name: outer rc=$OUTER_RC"
}

cleanup() {
  if "${PM[@]}" container exists "$1"; then "${PM[@]}" rm -f "$1" >/dev/null; fi
}

write_inner_config() {
  local name=$1 drv=$2 storage
  if [[ $drv == overlay ]]; then storage=$STORAGE_OVERLAY; else storage=$STORAGE_VFS; fi
  printf '%s\n' "$storage" | "${PM[@]}" exec -i --user 1000:1000 -e HOME=/home/dev "$name" \
    sh -c 'mkdir -p /home/dev/.config/containers && cat > /home/dev/.config/containers/storage.conf'
  printf '%s\n' "$CONTAINERS_CONF" | "${PM[@]}" exec -i --user 1000:1000 -e HOME=/home/dev "$name" \
    sh -c 'mkdir -p /home/dev/.config/containers && cat > /home/dev/.config/containers/containers.conf'
}

# inner_exec <name> <tag> <body> <istor>: inner body as the realm user, with the inner prelude and the storage
# flags substituted for @ISTOR@. Sets INNER_RC.
inner_exec() {
  local name=$1 tag=$2 body=$3 istor=$4
  INNER_RC=0
  body=${body//@ISTOR@/"$istor"}
  "${PM[@]}" exec --user 1000:1000 -e HOME=/home/dev "$name" sh -c "$INNER_PRE"$'\n'"$body" \
    > "$OUT/logs/$tag.inner.log" 2>&1 || INNER_RC=$?
}

# realm_exec <name> <tag> <body>: outer body as the realm user, no inner prelude. Sets PROBE_RC.
realm_exec() {
  local name=$1 tag=$2 body=$3
  PROBE_RC=0
  "${PM[@]}" exec --user 1000:1000 -e HOME=/home/dev "$name" sh -c "$body" \
    > "$OUT/logs/$tag.probe.log" 2>&1 || PROBE_RC=$?
}

# first_error <log>: first line that reports a failure, cut to 240 characters. The "cannot find UID/GID" line is
# podman's single-uid fallback (expected when the user has no subordinate range), not a failure, so it is skipped.
first_error() {
  local line
  if [[ ! -s $1 ]]; then echo none; return; fi
  line=$(grep -v 'cannot find UID/GID' "$1" | grep -m1 -E 'level=(error|fatal)|^Error:|^error:|crun:|rror: |not permitted|Permission denied|not allowed|Invalid argument|rc=[1-9]' || true)
  if [[ -z $line ]]; then echo none; else printf '%s' "${line:0:240}"; fi
}

# record <id> <note> <image> <profile> <driver> <outer argv> <inner argv> <outer rc> <inner rc> <log>
record() {
  local err
  err=$(first_error "${10}")
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "$err" >> "$TABLE"
  log "$1: outer rc=$8 inner rc=$9 first error: $err"
}

# case_run <id> <note> <image> <profile> <driver label> <istor> <outer options...>: one outer container and the
# inner body BODY_RUN (inner run of $ALPINE true). The extra realm and inner checks run in the E1o container.
case_run() {
  local id=$1 note=$2 img=$3 prof=$4 drv=$5 istor=$6 name
  shift 6
  name="s07e-$id"
  start_outer "$name" "$img" "$prof" "$@"
  local outer_s inner_s
  outer_s=$(join "${OUTER_ARGV[@]}")
  inner_s="$IPM_ARG${istor:+ $istor} run --rm $ALPINE true"
  if (( OUTER_RC != 0 )); then
    record "$id" "$note" "$img" "$prof" "$drv" "$outer_s" "$inner_s" "$OUTER_RC" "not run" "$OUT/logs/$name.outer.log"
    cleanup "$name"
    return 0
  fi
  write_inner_config "$name" "$drv"
  inner_exec "$name" "$name" "$BODY_RUN" "$istor"
  record "$id" "$note" "$img" "$prof" "$drv" "$outer_s" "$inner_s" 0 "$INNER_RC" "$OUT/logs/$name.inner.log"
  if [[ $id == E1o ]]; then
    realm_exec "$name" "$name.realm" "$BODY_REALM_CHECK"
    record "E1o-realm" "outer realm image state: subuid lines, helpers, libcap" "$img" "$prof" "$drv" "$outer_s" \
      "cat /etc/subuid /etc/subgid; ls -ln /usr/bin/newuidmap /usr/bin/newgidmap; command -v getcap setcap; dpkg -s libcap2-bin" \
      0 "$PROBE_RC" "$OUT/logs/$name.realm.probe.log"
    inner_exec "$name" "$name.uidmap" "$BODY_UIDMAP" "$istor"
    record "E1o-uidmap" "inner uid_map single entry; newuidmap or newgidmap mentions in an inner debug run" "$img" "$prof" \
      "$drv" "$outer_s" "$IPM_ARG${istor:+ $istor} unshare ...; $IPM_ARG${istor:+ $istor} --log-level=debug run --rm $ALPINE true" 0 "$INNER_RC" \
      "$OUT/logs/$name.uidmap.inner.log"
    inner_exec "$name" "$name.e6" "$BODY_E6" "$istor"
    record "E6" "single-uid chown inside an inner container" "$img" "$prof" "$drv" "$outer_s" \
      "$IPM_ARG${istor:+ $istor} run --rm $ALPINE sh -c 'id; touch /x; chown 100:100 /x; ls -ln /x'" 0 "$INNER_RC" \
      "$OUT/logs/$name.e6.inner.log"
  fi
  cleanup "$name"
}

# case_syscall: outer probes of sethostname and setdomainname under three profiles (no inner podman).
case_syscall() {
  local prof name outer_s h d
  for prof in none both host; do
    name="s07e-E4-$prof"
    start_outer "$name" "$REALM_IMG" "$prof" "${DEV_ALL[@]}"
    outer_s=$(join "${OUTER_ARGV[@]}")
    if (( OUTER_RC != 0 )); then
      record "E4-$prof" "syscall probe, profile $prof" "$REALM_IMG" "$prof" "none" "$outer_s" \
        "unshare -r -u sh -c 'hostname ...' and 'domainname ...'" "$OUTER_RC" "not run" "$OUT/logs/$name.outer.log"
    else
      realm_exec "$name" "$name" "$BODY_SYSCALL_PROBE"
      h=$(sed -n 's/^hostname rc=//p' "$OUT/logs/$name.probe.log")
      d=$(sed -n 's/^domainname rc=//p' "$OUT/logs/$name.probe.log")
      record "E4-$prof" "syscall probe, profile $prof: unshare -r -u with hostname, then domainname" "$REALM_IMG" "$prof" \
        "none" "$outer_s" "unshare -r -u sh -c 'hostname s07e-probe' ; unshare -r -u sh -c 'domainname s07e-probe'" \
        0 "hostname rc=$h domainname rc=$d" "$OUT/logs/$name.probe.log"
    fi
    cleanup "$name"
  done
}

run_case_id() {
  case $1 in
    E0) case_run E0 "control: stock base image (ships dev:100000:65536), storage.conf only" "$BASE_IMG" both overlay "$ISTOR_CONF" "${DEV_ALL[@]}" ;;
    E1) case_run E1 "AC-3 as briefed: no-subuid image, both-names profile, storage.conf only (overlay, fuse-overlayfs, ignore_chown_errors), inner --root on the CLI" \
          "$REALM_IMG" both overlay "$ISTOR_CONF" "${DEV_ALL[@]}" ;;
    E1o) case_run E1o "AC-3 candidate: E1 with the overlay storage options on the inner CLI (--storage-driver, --storage-opt)" \
          "$REALM_IMG" both overlay "$ISTOR_OVERLAY" "${DEV_ALL[@]}" ;;
    E2) case_run E2 "E1o with vfs driver and vfs.ignore_chown_errors" "$REALM_IMG" both vfs "$ISTOR_VFS" "${DEV_ALL[@]}" ;;
    E3) case_run E3 "E1o with the default seccomp profile (no --security-opt seccomp)" "$REALM_IMG" none overlay "$ISTOR_OVERLAY" "${DEV_ALL[@]}" ;;
    E4) case_syscall ;;
    E4c) case_run E4c "E1o with a profile that ungates sethostname only (setdomainname stays gated)" \
           "$REALM_IMG" host overlay "$ISTOR_OVERLAY" "${DEV_ALL[@]}" ;;
    E5a) case_run E5a "E1o minus /dev/net/tun" "$REALM_IMG" both overlay "$ISTOR_OVERLAY" \
           --device /dev/fuse --security-opt 'unmask=/proc/*' ;;
    E5b) case_run E5b "E1o minus unmask=/proc/*" "$REALM_IMG" both overlay "$ISTOR_OVERLAY" \
           --device /dev/fuse --device /dev/net/tun ;;
    E5c) case_run E5c "E2 (vfs) minus /dev/fuse" "$REALM_IMG" both vfs "$ISTOR_VFS" \
           --device /dev/net/tun --security-opt 'unmask=/proc/*' ;;
    *) die "unknown case $1" ;;
  esac
}

cmd_teardown() {
  check_roots
  if [[ -e $ROOT_DIR || -e $RUN_DIR ]]; then
    local count
    count=$("${PM[@]}" ps -aq | wc -l)
    log "teardown: containers under spike roots before removal: $count"
    if (( count > 0 )); then "${PM[@]}" rm -af; fi
    if [[ -e $ROOT_DIR ]]; then "${PM[@]}" rmi -af; fi
  fi
  if [[ -e $ROOT_DIR ]]; then
    "${PM[@]}" unshare rm -rf -- "$ROOT_DIR" || log "teardown: unshare rm exited non-zero; checking what remains"
  fi
  if [[ -e $ROOT_DIR ]]; then
    local foreign
    foreign=$(find "$ROOT_DIR" ! -user "$(id -u)" -print)
    if [[ -n $foreign ]]; then
      printf '%s\n' "$foreign" | head -n 20 >&2
      die "entries not owned by the host user remain under $ROOT_DIR; not removing them"
    fi
    spike_path_ok "$ROOT_DIR" || die "path check failed before rm: $ROOT_DIR"
    rm -rf -- "$ROOT_DIR"
  fi
  if [[ -e $RUN_DIR ]]; then
    spike_path_ok "$RUN_DIR" || die "path check failed before rm: $RUN_DIR"
    rm -rf -- "$RUN_DIR"
  fi
  local parent
  parent=$(dirname "$ROOT_DIR")
  if [[ $(basename "$parent") == ginn-spike-s07e ]] && spike_path_ok "$parent" && [[ -d $parent ]]; then
    rmdir -- "$parent"
  fi
  log "teardown: spike roots removed: $ROOT_DIR $RUN_DIR"
}

main() {
  local cmd=${1:-}
  shift || true
  case $cmd in
    build) init_out; build_images ;;
    run)
      build_images
      for id in E0 E1 E1o E2 E3 E4 E4c E5a E5b E5c; do run_case_id "$id"; done
      ;;
    case) build_images; run_case_id "${1:?case id: E0 E1 E1o E2 E3 E4 E4c E5a E5b E5c}" ;;
    teardown) cmd_teardown ;;
    *) die "usage: $0 build|run|case <id>|teardown" ;;
  esac
}

main "$@"
