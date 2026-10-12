#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# S-07d (FR-062 AC-3, NFR-SEC-002, AS-005): root cause of the nested newuidmap EPERM that
# S-07c hit under crun, whether SYS_ADMIN is really required, and the minimal Development-tier
# flag set once a fix is in place.
#
#   spikes/s07d_newuidmap.sh evidence         build the unfixed image (FIX=none); H1 layer check;
#                                             H1 to H4 probes with and without SYS_ADMIN; C2 baseline
#   spikes/s07d_newuidmap.sh fix <variant>    build FIX=<variant> (setuid or setcap); H1 layer check;
#                                             probe; C2 (no SYS_ADMIN); C3 removals if C2 passes
#   spikes/s07d_newuidmap.sh diag             setcap image: next blocker (sethostname) with and without SYS_ADMIN;
#                                             uts=host is diagnostic only
#   spikes/s07d_newuidmap.sh diag2            setcap image: realm-side unshare detail and inner debug log
#   spikes/s07d_newuidmap.sh seccomp          narrow seccomp (sethostname, setdomainname ungated): C2 without
#                                             SYS_ADMIN, then one-at-a-time C3 removals if it passes
#   spikes/s07d_newuidmap.sh final           control without the setcap fix; unmask=/proc/* in place of ALL;
#                                             host hostname unchanged after a realm sethostname
#   spikes/s07d_newuidmap.sh teardown        remove spike containers, images and storage
#
# Every podman call uses the spike storage (--root, --runroot and --tmpdir), never the default
# storage or the shared libpod tmpdir. Every outer run passes --runtime crun. No root, no sudo,
# no host package installs, no xbps, no --privileged. Raw logs stay outside the repository.
set -euo pipefail

SELF="$(readlink -f "$0")"
SPIKE_DIR="$(dirname "$SELF")"
ROOT_DIR="${GINN_SPIKE_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/ginn-spike-s07d/podman}"
RUN_DIR="${GINN_SPIKE_RUNROOT:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ginn-spike-s07d}"
OUT="${GINN_SPIKE_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s07d}"
ALPINE=docker.io/library/alpine:3
RUNTIME=crun
PM=(podman --root "$ROOT_DIR" --runroot "$RUN_DIR" --tmpdir "$RUN_DIR/libpod-tmp")
TABLE="$OUT/attempts.tsv"
ONE_RANGE='dev:1001:64000'
IMAGE=""
PULL="podman run --rm $ALPINE true"
read -r -d '' PROBE_BODY <<'PROBE' || true
echo "== id"; id
echo "== H1 setuid bit and file capability on the helpers (ls, stat, getcap)"
ls -ln /usr/bin/newuidmap /usr/bin/newgidmap
stat -c '%A %a uid=%u gid=%g %n' /usr/bin/newuidmap /usr/bin/newgidmap
getcap /usr/bin/newuidmap /usr/bin/newgidmap; echo "getcap rc=$?"
echo "== H2 no_new_privs and seccomp"
grep -E '^(NoNewPrivs|Seccomp):' /proc/self/status
echo "== H3 capability sets of the realm user (decoded)"
for k in CapInh CapPrm CapEff CapBnd CapAmb; do
  v=$(awk -v k="$k:" '$1 == k {print $2}' /proc/self/status)
  echo "$k $v $(capsh --decode="$v" 2>&1)"
done
echo "== H4 id maps and subordinate ranges"
echo "uid_map:"; cat /proc/self/uid_map
echo "gid_map:"; cat /proc/self/gid_map
echo "/etc/subuid:"; cat /etc/subuid
echo "/etc/subgid:"; cat /etc/subgid
echo "== mounts (nosuid would disable setuid helpers)"
cat /proc/self/mountinfo
echo "== direct newuidmap, multi-range (the inner podman request)"
unshare -U sleep 60 & p1=$!
sleep 1
newuidmap "$p1" 0 1000 1 1 1001 64000; echo "newuidmap multi-range rc=$?"
cat "/proc/$p1/uid_map" 2>/dev/null || true
kill "$p1" 2>/dev/null || true
echo "== direct newuidmap, single range (own uid only, no subuid needed)"
unshare -U sleep 60 & p2=$!
sleep 1
newuidmap "$p2" 0 1000 1; echo "newuidmap single-range rc=$?"
kill "$p2" 2>/dev/null || true
echo "== T1 caps and euid of a child exec'd from a setuid-root copy of sleep, and from a plain copy"
/usr/local/bin/suid-sleep 60 & t1=$!
sleep 1
grep -E '^(Uid|CapInh|CapPrm|CapEff|CapBnd|CapAmb):' "/proc/$t1/status"
kill "$t1" 2>/dev/null || true
/usr/local/bin/plain-sleep 60 & t2=$!
sleep 1
grep -E '^(Uid|CapEff):' "/proc/$t2/status"
kill "$t2" 2>/dev/null || true
echo "== T2 file-capability copy of sleep (setcap cap_setuid=ep, no setuid bit)"
getcap /usr/local/bin/cap-sleep; echo "getcap rc=$?"
/usr/local/bin/cap-sleep 60 & t3=$!
sleep 1
grep -E '^(Uid|CapEff):' "/proc/$t3/status"
kill "$t3" 2>/dev/null || true
true
PROBE
declare -A RC=()

log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
mem_ok() { awk '/MemAvailable/ { exit !($2 >= 262144) }' /proc/meminfo || die "MemAvailable below 256 MiB"; }

# init_out: the output directory, the attempts table header and the environment record.
init_out() {
  mkdir -p "$OUT" "$OUT/ctx" "$ROOT_DIR" "$RUN_DIR/libpod-tmp"
  [[ -s $TABLE ]] || printf 'id\tnote\tmode\tsubuid\touter runtime\touter argv\tinner argv\touter rc\tinner rc\tfirst error\n' > "$TABLE"
  if [[ ! -s $OUT/env.txt ]]; then
    {
      echo "podman: $(podman --version)"
      echo "crun: $(crun --version | head -1)"
      echo "kernel: $(uname -r)"
      echo "host user: $(id)"
    } > "$OUT/env.txt"
  fi
  log "S-07d: root=$ROOT_DIR run=$RUN_DIR out=$OUT"
}

# build_image <variant>: the S-07d image with FIX=<variant>, tagged per variant.
build_image() {
  local fix=$1
  IMAGE="localhost/ginn-s07d:$fix"
  "${PM[@]}" build --build-arg "FIX=$fix" -t "$IMAGE" -f "$SPIKE_DIR/Dockerfile.s07d" "$OUT/ctx" \
    >"$OUT/build-$fix.log" 2>&1 || die "image build failed, see $OUT/build-$fix.log"
  echo "image $IMAGE id: $("${PM[@]}" image inspect --format '{{.Id}}' "$IMAGE")" >> "$OUT/env.txt"
}

# layer_check <variant>: H1 on the image layer content, not the running container. podman
# image mount runs inside podman unshare with the same spike flags.
layer_check() {
  local fix=$1
  "${PM[@]}" unshare sh -c '
    pm=$0; root=$1; runroot=$2; tmpdir=$3; img=$4
    m=$("$pm" --root "$root" --runroot "$runroot" --tmpdir "$tmpdir" image mount "$img") || exit 1
    for f in usr/bin/newuidmap usr/bin/newgidmap; do
      echo "== layer $img: $f"
      stat -c "%A %a uid=%u gid=%g %n" "$m/$f"
      getcap "$m/$f"; echo "getcap rc=$?"
    done
    "$pm" --root "$root" --runroot "$runroot" --tmpdir "$tmpdir" image unmount "$img" >/dev/null
  ' podman "$ROOT_DIR" "$RUN_DIR" "$RUN_DIR/libpod-tmp" "$IMAGE" > "$OUT/layer-$fix.txt" 2>&1 \
    || log "layer check for $fix exited non-zero, see $OUT/layer-$fix.txt"
}

# extract_catatonit: copy the init binary out of the image (the host has no catatonit).
extract_catatonit() {
  "${PM[@]}" rm -f s07d-ctr >/dev/null 2>&1 || true
  "${PM[@]}" create --name s07d-ctr "$IMAGE" >/dev/null
  "${PM[@]}" cp s07d-ctr:/usr/bin/catatonit "$OUT/catatonit"
  "${PM[@]}" rm -f s07d-ctr >/dev/null
}

# outer_run <name> [options...]: start the outer stand-in. The shared options come first,
# then the case options. --init uses a host copy of catatonit.
outer_run() {
  local name=$1; shift
  "${PM[@]}" rm -f "$name" >/dev/null 2>&1 || true
  "${PM[@]}" run -d --name "$name" --runtime "$RUNTIME" --userns keep-id --network host \
    --init --init-path "$OUT/catatonit" "$@" "$IMAGE" sleep infinity
}

# write_subids <name> <lines>: /etc/subuid and /etc/subgid as container root.
write_subids() {
  "${PM[@]}" exec -u 0 -e SUB="$2" "$1" sh -c '
    if [ -n "$SUB" ]; then printf "%s\n" "$SUB" > /etc/subuid; else : > /etc/subuid; fi
    cp /etc/subuid /etc/subgid'
}

# setup_c2 <name>: the C2 inner configuration from s07c, written from containers.conf(5) and
# containers-storage.conf(5). The inner podman runs as the realm user (uid 1000).
setup_c2() {
  write_subids "$1" "$ONE_RANGE"
  "${PM[@]}" exec -i --user 1000:1000 -e HOME=/home/dev "$1" sh -c 'mkdir -p ~/.config/containers ~/.local/share/containers
    cat > ~/.config/containers/storage.conf' <<'CONF'
[storage]
driver = "overlay"

[storage.options.overlay]
mount_program = "/usr/bin/fuse-overlayfs"
CONF
  "${PM[@]}" exec -i --user 1000:1000 -e HOME=/home/dev "$1" sh -c 'cat > ~/.config/containers/containers.conf' <<'CONF'
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
CONF
}

# probe_prep <name>: the probe's own /etc/subuid (as S-07 does, so the multi-range request is
# judged against the spike range, not the image default), and three test binaries as root:
# a setuid-root copy of sleep, a plain copy, and a copy with cap_setuid=ep and no setuid bit.
probe_prep() {
  write_subids "$1" "$ONE_RANGE"
  "${PM[@]}" exec -u 0 "$1" sh -c 'cp /usr/bin/sleep /usr/local/bin/suid-sleep && chmod 4755 /usr/local/bin/suid-sleep
    cp /usr/bin/sleep /usr/local/bin/plain-sleep && chmod 0755 /usr/local/bin/plain-sleep
    cp /usr/bin/sleep /usr/local/bin/cap-sleep && chmod 0755 /usr/local/bin/cap-sleep
    setcap cap_setuid=ep /usr/local/bin/cap-sleep'
}

# inner <mode> <name> <command>: mode c2 runs the command as uid 1000 with HOME=/home/dev.
# mode probe runs PROBE_BODY the same way (no inner podman). The label in <command> is
# only a TSV label for probes.
inner() {
  local mode=$1 name=$2 cmd=$3
  if [[ $mode == probe ]]; then
    "${PM[@]}" exec --user 1000:1000 -e HOME=/home/dev "$name" sh -c "$PROBE_BODY"
    return
  fi
  "${PM[@]}" exec --user 1000:1000 -e HOME=/home/dev "$name" sh -c "export XDG_RUNTIME_DIR=/tmp/s07d-run
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
# Starts a fresh outer container with the shared options and the case options, runs the inner
# part, records one TSV row and removes the container. RC[id] holds the outer rc when the outer
# start failed, otherwise the inner rc.
case_run() {
  local id=$1 note=$2 mode=$3 subs=$4 cmd=$5 orc=0 irc=not-run
  local name="s07d-$id" log_file="$OUT/$id.log" outer_rt="" outer_argv errs
  shift 5
  if [[ ${1:-} == -- ]]; then shift; fi
  printf -v outer_argv '%s ' "${PM[@]}" run -d --name "$name" --runtime "$RUNTIME" \
    --userns keep-id --network host --init --init-path "$OUT/catatonit" "$@" "$IMAGE" sleep infinity
  mem_ok
  : > "$log_file"
  outer_run "$name" "$@" >>"$log_file" 2>&1 || orc=$?
  if (( orc == 0 )); then
    outer_rt=$("${PM[@]}" inspect --format '{{.OCIRuntime}}' "$name" 2>/dev/null || true)
    "${PM[@]}" exec "$name" sh -c 'grep -E "^Cap(Bnd|Eff|Amb):" /proc/self/status; grep -E "^Cap(Bnd|Eff):" /proc/1/status' \
      > "$OUT/$id.caps" 2>&1 || true
    case $mode in
      c2) setup_c2 "$name" ;;
      probe) probe_prep "$name" ;;
    esac >>"$log_file" 2>&1 || true
    irc=0
    inner "$mode" "$name" "$cmd" >>"$log_file" 2>&1 || irc=$?
  fi
  "${PM[@]}" rm -f "$name" >/dev/null 2>&1 || true
  errs=$(first_error "$log_file")
  if (( orc != 0 )); then RC[$id]=$orc; else RC[$id]=$irc; fi
  record "$id" "$note" "$mode" "$subs" "$outer_rt" "${outer_argv% }" "$cmd" "$orc" "$irc" "${errs:-none}"
}

# ---------------------------------------------------------------- stages

# cmd_evidence: H1 to H4 on the unfixed image, with and without SYS_ADMIN, plus the C2 baseline.
cmd_evidence() {
  init_out
  build_image none
  layer_check none
  extract_catatonit
  local fuse=(--device /dev/fuse) label=(--security-opt label=disable)
  local unmask_all=(--security-opt unmask=ALL) tun=(--device /dev/net/tun)
  local sysadmin=(--cap-add SYS_ADMIN)
  local chosen=("${fuse[@]}" "${label[@]}" "${unmask_all[@]}" "${tun[@]}")
  log "P1: H1 to H4 probe, chosen set, no SYS_ADMIN"
  case_run P1 'H1-H4 probe: chosen set, no SYS_ADMIN' probe "$ONE_RANGE" 'probe: H1-H4 and direct newuidmap' -- "${chosen[@]}"
  log "P2: H1 to H4 probe, chosen set plus SYS_ADMIN (contrast)"
  case_run P2 'H1-H4 probe: chosen set plus SYS_ADMIN (contrast)' probe "$ONE_RANGE" 'probe: H1-H4 and direct newuidmap' -- "${chosen[@]}" "${sysadmin[@]}"
  log "C2 baseline on the unfixed image"
  case_run C2-none 'C2 baseline, FIX=none' c2 "$ONE_RANGE" "$PULL" -- "${chosen[@]}"
  log "S-07d evidence finished"
}

# cmd_fix <variant>: one candidate fix, then the C2 check without SYS_ADMIN, then C3 if C2 passes.
cmd_fix() {
  local fix=$1
  case $fix in setuid|setcap) ;; *) die "variant must be setuid or setcap" ;; esac
  init_out
  build_image "$fix"
  layer_check "$fix"
  extract_catatonit
  local fuse=(--device /dev/fuse) label=(--security-opt label=disable)
  local unmask_all=(--security-opt unmask=ALL) tun=(--device /dev/net/tun)
  local chosen=("${fuse[@]}" "${label[@]}" "${unmask_all[@]}" "${tun[@]}")
  log "F1 ($fix): H1 to H4 probe, chosen set, no SYS_ADMIN"
  case_run "F1-$fix" "fix $fix: H1-H4 probe, chosen set, no SYS_ADMIN" probe "$ONE_RANGE" 'probe: H1-H4 and direct newuidmap' -- "${chosen[@]}"
  log "F2 ($fix): C2, chosen set, no SYS_ADMIN"
  case_run "F2-$fix" "fix $fix: C2, chosen set, no SYS_ADMIN" c2 "$ONE_RANGE" "$PULL" -- "${chosen[@]}"
  local f2="F2-$fix"
  if (( RC[$f2] == 0 )); then
    log "C3 ($fix): removals from the passing C2"
    case_run "C3a-$fix" "fix $fix: C2 minus /dev/net/tun" c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${label[@]}" "${unmask_all[@]}"
    case_run "C3b-$fix" "fix $fix: C2 minus unmask=ALL" c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${label[@]}" "${tun[@]}"
    case_run "C3c-$fix" "fix $fix: C2 minus label=disable" c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${unmask_all[@]}" "${tun[@]}"
    case_run "C3d-$fix" "fix $fix: C2 minus /dev/fuse" c2 "$ONE_RANGE" "$PULL" -- "${label[@]}" "${unmask_all[@]}" "${tun[@]}"
    case_run "C3e-$fix" "fix $fix: C2 with no extra device or security option" c2 "$ONE_RANGE" "$PULL" --
  else
    log "C3 skipped: F2 ($fix) did not pass"
  fi
  log "S-07d fix $fix finished"
}

# UTS_TEST: sethostname in isolation. unshare -r maps the realm user to root in a new user and
# UTS namespace; the exec'd sh calls sethostname. With SYS_ADMIN in the spec it succeeds (D3).
read -r -d '' UTS_TEST <<'UTS' || true
unshare -r -u sh -c 'hostname s07d-test; echo hostname-rc=$?; grep -E "^Cap(Bnd|Eff):" /proc/self/status'
UTS

# cmd_diag: the next blocker after newuidmap is fixed (sethostname). Runs on the setcap image.
cmd_diag() {
  init_out
  build_image setcap
  extract_catatonit
  local fuse=(--device /dev/fuse) label=(--security-opt label=disable)
  local unmask_all=(--security-opt unmask=ALL) tun=(--device /dev/net/tun)
  local sysadmin=(--cap-add SYS_ADMIN)
  local chosen=("${fuse[@]}" "${label[@]}" "${unmask_all[@]}" "${tun[@]}")
  log "D1: setcap image, C2 with inner --uts host (diagnostic, not AC-3 as written)"
  case_run D1 'setcap, C2 with inner --uts host (diagnostic only)' c2 "$ONE_RANGE" "podman run --rm --uts host $ALPINE true" -- "${chosen[@]}"
  log "D2: setcap image, unshare -r -u hostname, no SYS_ADMIN"
  case_run D2 'setcap, realm unshare -r -u sethostname, no SYS_ADMIN' c2 "$ONE_RANGE" "$UTS_TEST" -- "${chosen[@]}"
  log "D3: setcap image, unshare -r -u hostname, SYS_ADMIN"
  case_run D3 'setcap, realm unshare -r -u sethostname, SYS_ADMIN' c2 "$ONE_RANGE" "$UTS_TEST" -- "${chosen[@]}" "${sysadmin[@]}"
  log "D4: setcap image, C2 plus SYS_ADMIN (AC-3 as written)"
  case_run D4 'setcap, C2 plus SYS_ADMIN (AC-3 as written)' c2 "$ONE_RANGE" "$PULL" -- "${chosen[@]}" "${sysadmin[@]}"
  log "S-07d diag finished"
}

# cmd_diag2: realm-side unshare with ids and capability sets (E1, E2), and the inner podman debug
# log (E3), on the setcap image without SYS_ADMIN. E1 shows sethostname EPERM with full CapEff.
cmd_diag2() {
  init_out
  build_image setcap
  extract_catatonit
  local fuse=(--device /dev/fuse) label=(--security-opt label=disable)
  local unmask_all=(--security-opt unmask=ALL) tun=(--device /dev/net/tun)
  local chosen=("${fuse[@]}" "${label[@]}" "${unmask_all[@]}" "${tun[@]}")
  local e1='unshare -r -u sh -c '"'"'id; cat /proc/self/uid_map; grep -E "^(Uid|CapEff|CapBnd):" /proc/self/status; hostname s07d-x; echo hostname-rc=$?'"'"
  local e2='unshare -U -u sh -c '"'"'id; grep -E "^(Uid|CapEff|CapBnd):" /proc/self/status; hostname s07d-x; echo hostname-rc=$?'"'"
  log "E1: unshare -r -u detail, no SYS_ADMIN"
  case_run E1 'setcap, realm unshare -r -u with ids and caps, no SYS_ADMIN' c2 "$ONE_RANGE" "$e1" -- "${chosen[@]}"
  log "E2: unshare -U -u detail, no SYS_ADMIN"
  case_run E2 'setcap, realm unshare -U -u with ids and caps, no SYS_ADMIN' c2 "$ONE_RANGE" "$e2" -- "${chosen[@]}"
  log "E3: inner podman run with debug log, no SYS_ADMIN (crun invocation)"
  case_run E3 'setcap, C2 with --log-level=debug (crun argv and error)' c2 "$ONE_RANGE" "podman --log-level=debug run --rm $ALPINE true" -- "${chosen[@]}"
  log "S-07d diag2 finished"
}

# make_seccomp: the host default profile with sethostname and setdomainname allowed
# unconditionally. The default profile returns EPERM for them unless the container holds
# CAP_SYS_ADMIN (rule with excludes caps CAP_SYS_ADMIN). Written to the spike output, not the repo.
make_seccomp() {
  python3 - "$OUT/seccomp-s07d.json" <<'PY'
import json, sys
src = "/usr/share/containers/seccomp.json"
d = json.load(open(src))
gate = {"sethostname", "setdomainname"}
rules = []
for r in d["syscalls"]:
    names = [n for n in r.get("names", []) if n not in gate]
    if names != r.get("names", []):
        if not names:
            continue
        r["names"] = names
    rules.append(r)
rules.insert(0, {"names": sorted(gate), "action": "SCMP_ACT_ALLOW", "args": [], "includes": {}, "excludes": {}})
d["syscalls"] = rules
json.dump(d, open(sys.argv[1], "w"), indent=1)
PY
  log "seccomp profile written: $OUT/seccomp-s07d.json (from $(echo /usr/share/containers/seccomp.json))"
}

# cmd_seccomp: the sethostname blocker on the setcap image without SYS_ADMIN. G1 is the broad
# option (seccomp=unconfined, informative only); G2 is the narrow one. C3 removals run only if G2 passes.
cmd_seccomp() {
  init_out
  build_image setcap
  extract_catatonit
  local fuse=(--device /dev/fuse) label=(--security-opt label=disable)
  local unmask_all=(--security-opt unmask=ALL) tun=(--device /dev/net/tun)
  local chosen=("${fuse[@]}" "${label[@]}" "${unmask_all[@]}" "${tun[@]}")
  make_seccomp
  local secc=(--security-opt "seccomp=$OUT/seccomp-s07d.json")
  log "G1: setcap, C2, seccomp=unconfined, no SYS_ADMIN (informative, broad)"
  case_run G1 'setcap, C2, seccomp=unconfined, no SYS_ADMIN (informative)' c2 "$ONE_RANGE" "$PULL" -- "${chosen[@]}" --security-opt seccomp=unconfined
  log "G2: setcap, C2, narrow seccomp (sethostname and setdomainname ungated), no SYS_ADMIN"
  case_run G2 'setcap, C2, narrow seccomp (UTS calls ungated), no SYS_ADMIN' c2 "$ONE_RANGE" "$PULL" -- "${chosen[@]}" "${secc[@]}"
  if (( RC[G2] == 0 )); then
    log "C3 (narrow seccomp): removals from the passing G2"
    case_run C3a-g2 'G2 minus /dev/net/tun' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${label[@]}" "${unmask_all[@]}" "${secc[@]}"
    case_run C3b-g2 'G2 minus unmask=ALL' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${label[@]}" "${tun[@]}" "${secc[@]}"
    case_run C3c-g2 'G2 minus label=disable' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${unmask_all[@]}" "${tun[@]}" "${secc[@]}"
    case_run C3d-g2 'G2 minus /dev/fuse' c2 "$ONE_RANGE" "$PULL" -- "${label[@]}" "${unmask_all[@]}" "${tun[@]}" "${secc[@]}"
    case_run C3e-g2 'G2 with no device or security option' c2 "$ONE_RANGE" "$PULL" -- "${secc[@]}"
  else
    log "C3 skipped: G2 did not pass"
  fi
  log "S-07d seccomp finished"
}

# cmd_final: controls and the narrower unmask, on the narrow-seccomp, no-SYS_ADMIN configuration.
cmd_final() {
  init_out
  build_image setcap
  build_image none
  extract_catatonit
  make_seccomp
  local fuse=(--device /dev/fuse) unmask_all=(--security-opt unmask=ALL) unmask_proc=(--security-opt 'unmask=/proc/*')
  local tun=(--device /dev/net/tun) secc=(--security-opt "seccomp=$OUT/seccomp-s07d.json")
  log "G3: control, FIX=none (setuid helpers), narrow seccomp, no label, unmask ALL, no SYS_ADMIN"
  IMAGE=localhost/ginn-s07d:none
  case_run G3-none 'control: FIX=none (setuid helpers), narrow seccomp, minus label, no SYS_ADMIN' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${unmask_all[@]}" "${tun[@]}" "${secc[@]}"
  IMAGE=localhost/ginn-s07d:setcap
  log "G4: setcap, narrow seccomp, no label, unmask=/proc/* (narrower than ALL), no SYS_ADMIN"
  case_run G4 'setcap, narrow seccomp, minus label, unmask=/proc/*, no SYS_ADMIN' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${unmask_proc[@]}" "${tun[@]}" "${secc[@]}"
  log "M: removals from the G4 set (setcap, narrow seccomp, no label, unmask=/proc/*, no SYS_ADMIN)"
  case_run M-tun 'G4 minus /dev/net/tun' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${unmask_proc[@]}" "${secc[@]}"
  case_run M-unmask 'G4 minus unmask' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${tun[@]}" "${secc[@]}"
  case_run M-fuse 'G4 minus /dev/fuse' c2 "$ONE_RANGE" "$PULL" -- "${unmask_proc[@]}" "${tun[@]}" "${secc[@]}"
  case_run M-seccomp 'G4 with the default seccomp profile' c2 "$ONE_RANGE" "$PULL" -- "${fuse[@]}" "${unmask_proc[@]}" "${tun[@]}"
  log "U1: realm sethostname inside the outer container; host hostname must not change"
  local host_before host_after
  host_before=$(cat /proc/sys/kernel/hostname)
  case_run U1 'setcap, narrow seccomp: realm sets the outer container hostname' c2 "$ONE_RANGE" \
    'hostname s07d-outer-test; echo outer-hostname=$(hostname)' -- "${fuse[@]}" "${unmask_all[@]}" "${tun[@]}" "${secc[@]}"
  host_after=$(cat /proc/sys/kernel/hostname)
  echo "host hostname before=$host_before after=$host_after" >> "$OUT/env.txt"
  log "host hostname before=$host_before after=$host_after"
  log "S-07d final finished"
}

# spike_path_ok PATH: teardown may only remove this spike's own storage. PATH must be absolute and
# not a symlink, must have a path component that starts with ginn-spike-s07d, must not contain . or
# .. components, and must not be $HOME, an ancestor of $HOME, or /.
spike_path_ok() {
  local path="$1" part hit=0
  local -a parts=()
  [[ $path == /* ]] || return 1
  if [[ -L $path ]]; then return 1; fi
  IFS=/ read -r -a parts <<< "$path"
  for part in "${parts[@]}"; do
    case "$part" in
      . | ..) return 1 ;;
      ginn-spike-s07d*) hit=1 ;;
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
    spike_path_ok "$p" || die "refusing teardown of $p: not an absolute, non-symlink ginn-spike-s07d path, and not \$HOME or above it"
  done
}

# teardown: containers, images and spike storage under the explicit --root. Logs in
# GINN_SPIKE_OUT stay for the record.
cmd_teardown() {
  guard_spike_paths "$ROOT_DIR" "$RUN_DIR"
  # Only the spike's own --root, --runroot and --tmpdir. Never the shared libpod tmpdir.
  "${PM[@]}" rm -af >/dev/null 2>&1 || true
  echo "teardown: containers left under spike roots: $("${PM[@]}" ps -aq 2>/dev/null | wc -l)"
  "${PM[@]}" rmi -af >/dev/null 2>&1 || true
  "${PM[@]}" unshare rm -rf "$ROOT_DIR" "$RUN_DIR" >/dev/null 2>&1 || true
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
  echo "teardown: removed $ROOT_DIR and $RUN_DIR"
}

case "${1:-}" in
  evidence) cmd_evidence ;;
  fix) cmd_fix "${2:-}" ;;
  diag) cmd_diag ;;
  diag2) cmd_diag2 ;;
  seccomp) cmd_seccomp ;;
  final) cmd_final ;;
  teardown) cmd_teardown ;;
  *) die "usage: $0 evidence|fix setuid|fix setcap|diag|diag2|seccomp|final|teardown" ;;
esac
