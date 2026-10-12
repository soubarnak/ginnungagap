#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Three-flag rule: every podman call (teardown included) passes --root, --runroot and --tmpdir,
# all inside this spike's directories. Without --tmpdir podman shares the user's libpod tmpdir.
# S-09 (FR-026, FR-027, NFR-SEC-002, OOS-9, R-002): can a Kali realm run raw sockets
# (NET_RAW) inside an isolated pasta netns, and can it change host interfaces?
#
#   spikes/s09_kali_netns.sh run        pull and build the image, run every variant
#   spikes/s09_kali_netns.sh teardown   remove spike containers, image and storage
#
# Every podman call uses the spike storage (--root, --runroot, --tmpdir). Raw logs go to S09_OUT
# (default ~/.cache/ginn-spike/s09), never into the repository. A denied probe (EPERM) is
# an expected result: each probe records its exit status and the run continues.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT="${S09_ROOT:-$HOME/.local/share/ginn-spike-s09/podman}"
RUNROOT="${S09_RUNROOT:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ginn-spike-s09}"
TMPROOT="$RUNROOT/libpod-tmp"
OUT="${S09_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s09}"
SRC=docker.io/kalilinux/kali-rolling
IMAGE=localhost/ginn-s09:kali-rolling
CTRS=(s09a0 s09a s09b s09c s09p s09q s09s)
P=(podman --root "$ROOT" --runroot "$RUNROOT" --tmpdir "$TMPROOT")
ROWS="$OUT/rows.tsv"
RSS="$OUT/rss.tsv"
CAPS="$OUT/caps.txt"
INIT=()

log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# row RUN LABEL RC LOGFILE: one TSV row. Decisive line = last output line, rc marker skipped.
row() {
  local last=""
  last=$(grep -v -e '^[[:space:]]*$' -e '^\[rc=' "$4" | tail -n 1 | cut -c1-200) || true
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$last" >> "$ROWS"
}

# probe RUN LABEL CTR ARGV...: exec ARGV in CTR. Output kept, exit status recorded, never fatal.
probe() {
  local run=$1 label=$2 ctr=$3 rc=0 f
  shift 3
  f="$OUT/$run-$label.log"
  printf '$ %s\n' "$*" > "$f"
  timeout 240 "${P[@]}" exec "$ctr" "$@" </dev/null >> "$f" 2>&1 || rc=$?
  printf '[rc=%s]\n' "$rc" >> "$f"
  row "$run" "$label" "$rc" "$f"
}

# hprobe RUN LABEL ARGV...: run ARGV on the host (no container). Same recording as probe.
hprobe() {
  local run=$1 label=$2 rc=0 f
  shift 2
  f="$OUT/$run-$label.log"
  printf '$ %s\n' "$*" > "$f"
  timeout 60 "$@" </dev/null >> "$f" 2>&1 || rc=$?
  printf '[rc=%s]\n' "$rc" >> "$f"
  row "$run" "$label" "$rc" "$f"
}

# decode_caps RUN LABEL: decode CapEff and CapBnd of a caps probe log with host capsh.
decode_caps() {
  local f=$1 name hex
  while read -r name hex; do
    printf '%s %s %s\n' "$(basename "$f")" "$name" "$(capsh --decode="$hex" 2>&1)" >> "$CAPS"
  done < <(grep -E '^Cap(Eff|Bnd):' "$f" || true)
}

# rss RUN CTR: RSS of every pasta or slirp4netns process that serves CTR (matched on netns path or pid).
rss() {
  local run=$1 ctr=$2 key pid
  key=$("${P[@]}" inspect --format '{{.NetworkSettings.SandboxKey}}' "$ctr")
  pid=$("${P[@]}" inspect --format '{{.State.Pid}}' "$ctr")
  ps -eo pid=,ppid=,rss=,args= -ww | grep -E 'pasta|slirp4netns' | grep -v grep > "$OUT/ps-$run-$ctr.txt" || true
  awk -v run="$run" -v ctr="$ctr" -v k="$key" -v p="$pid" \
    '(k != "" && index($0, k)) || $0 ~ ("[ ,=]" p "([ ]|$)") { printf "%s\t%s\t%s KiB\t%s\n", run, ctr, $3, $4 }' \
    "$OUT/ps-$run-$ctr.txt" >> "$RSS" || true
}

# mk NAME FLAG...: start a detached spike container that runs sleep infinity.
mk() {
  local name=$1
  shift
  "${P[@]}" rm -f "$name" >/dev/null 2>&1 || true
  "${P[@]}" run -d --name "$name" --userns keep-id --user 0:0 ${INIT[@]+"${INIT[@]}"} "$@" \
    "$IMAGE" sleep infinity > "$OUT/$name-run.log" 2>&1
  log "started $name: $*"
}

# ns_iface CTR: first non-loopback interface name inside CTR.
ns_iface() {
  "${P[@]}" exec "$1" ip -o -br link show | awk '$1 != "lo" { print $1; exit }'
}

# nmap_copy RUN LABEL CTR NMAP_ARGS...: run nmap from a copy without file capabilities.
# cp does not carry security.capability xattrs, so the copy execs without NET_ADMIN in the bounding set.
nmap_copy() {
  local run=$1 label=$2 ctr=$3
  shift 3
  probe "$run" "$label" "$ctr" sh -c \
    'cp /usr/lib/nmap/nmap /tmp/nmap-nocaps && chmod 755 /tmp/nmap-nocaps && timeout 180 /tmp/nmap-nocaps "$@"' \
    nmap "$@"
}

# serve CTR PORT: HTTP responder on PORT in CTR, perl core sockets (image has no python3 or ncat).
serve() {
  local c=$1 port=$2 code
  # Read the request headers before answering: closing with unread bytes sends RST, not FIN.
  code='socket(S,PF_INET,SOCK_STREAM,getprotobyname("tcp")) or die "socket: $!"; setsockopt(S,SOL_SOCKET,SO_REUSEADDR,pack("l",1)); bind(S,sockaddr_in($ARGV[0],INADDR_ANY)) or die "bind: $!"; listen(S,5) or die "listen: $!"; while (accept(C,S)) { while (<C>) { last if /^\r?$/ } print C "HTTP/1.0 200 OK\r\nContent-Length: 8\r\n\r\nkali-ok\n"; close C }'
  log "serve $c:$port with perl core sockets"
  "${P[@]}" exec -d "$c" perl -MSocket -e "$code" "$port" || log "serve $c:$port failed to start"
  sleep 2
}

pull() {
  log "pull $SRC"
  "${P[@]}" pull "$SRC" > "$OUT/pull.log" 2>&1
  "${P[@]}" image inspect --format '{{.Id}} {{index .RepoDigests 0}}' "$SRC" > "$OUT/digest.txt"
  log "digest $(cat "$OUT/digest.txt")"
}

build() {
  local ctx
  ctx=$(mktemp -d "$OUT/ctx.XXXXXX")
  log "build $IMAGE from $HERE/Dockerfile.s09"
  "${P[@]}" build --pull=never --network host -f "$HERE/Dockerfile.s09" -t "$IMAGE" "$ctx" \
    > "$OUT/build.log" 2>&1
  rm -rf "$ctx"
  "${P[@]}" image inspect --format '{{.Id}}' "$IMAGE" > "$OUT/image-id.txt"
}

# Run A: proposed Kali flags, isolated pasta netns, NET_RAW only.
run_a() {
  local r=A c=s09a nsif wifi
  mk "$c" --network pasta --cap-add NET_RAW
  probe "$r" ip-addr "$c" ip addr
  probe "$r" ip-d-link "$c" ip -d link
  nsif=$(ns_iface "$c" || true)
  probe "$r" caps-pid1 "$c" grep -E '^Cap' /proc/1/status
  probe "$r" caps-exec "$c" grep -E '^Cap' /proc/self/status
  probe "$r" ping-1.1.1.1 "$c" timeout 15 ping -c1 -W5 1.1.1.1
  probe "$r" tcp-outbound-443 "$c" timeout 15 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443 && echo TCP_CONNECT_OK'
  probe "$r" nmap-syn-loopback "$c" timeout 120 nmap -Pn -sS -p 22,80 127.0.0.1
  probe "$r" nmap-syn-example "$c" timeout 180 nmap -Pn -sS -p 80 --reason example.com
  probe "$r" nmap-ack-example "$c" timeout 180 nmap -Pn -sA -p 80 --reason example.com
  nmap_copy "$r" nmap-copy-syn-loopback "$c" -Pn -sS -p 22,80 127.0.0.1
  nmap_copy "$r" nmap-copy-syn-example "$c" -Pn -sS -p 80 --reason example.com
  nmap_copy "$r" nmap-copy-ack-example "$c" -Pn -sA -p 80 --reason example.com
  # Control: an unassigned LAN address. A real SYN gets no answer, so a RST here would come from the relay.
  nmap_copy "$r" nmap-copy-syn-lan-unassigned "$c" -Pn -sS -p 80 --reason 192.168.0.250
  hprobe "$r" host-connect-lan-unassigned bash -c 'exec 3<>/dev/tcp/192.168.0.250/80 && echo CONNECTED'
  probe "$r" tcpdump "$c" timeout 30 bash -c '(sleep 3; exec 3<>/dev/tcp/127.0.0.1/9) 2>/dev/null & timeout 20 tcpdump -c 3 -i any -n'
  probe "$r" ip-add-dummy "$c" ip link add dummy0 type dummy
  probe "$r" ip-lo-down "$c" ip link set lo down
  if [[ -n $nsif ]]; then
    probe "$r" ip-ns-iface-down "$c" ip link set "$nsif" down
  fi
  wifi=$(ip -o link show | awk -F': ' '$2 ~ /^wl/ { print $2; exit }') || true
  probe "$r" ip-host-iface-monitor "$c" ip link set "${wifi:-wlan0}" type monitor
  probe "$r" iw-dev "$c" iw dev
  hprobe "$r" host-ip-br-link ip -br link
  decode_caps "$OUT/$r-caps-pid1.log"
  decode_caps "$OUT/$r-caps-exec.log"
  rss "$r" "$c"
  "${P[@]}" rm -f "$c" >/dev/null 2>&1 || true
}

# Run B (control): shared host netns, NET_RAW only.
run_b() {
  local r=B c=s09b
  mk "$c" --network host --cap-add NET_RAW
  probe "$r" caps-exec "$c" grep -E '^Cap' /proc/self/status
  probe "$r" ip-br-link "$c" ip -br link
  probe "$r" nmap-syn-loopback "$c" timeout 120 nmap -Pn -sS -p 22 127.0.0.1
  nmap_copy "$r" nmap-copy-syn-loopback "$c" -Pn -sS -p 22 127.0.0.1
  probe "$r" ip-lo-down "$c" ip link set lo down
  hprobe "$r" host-ip-br-link ip -br link
  decode_caps "$OUT/$r-caps-exec.log"
  "${P[@]}" rm -f "$c" >/dev/null 2>&1 || true
}

# Run C: pasta with NET_RAW and NET_ADMIN. Changes must stay inside the realm netns.
run_c() {
  local r=C c=s09c nsif
  mk "$c" --network pasta --cap-add NET_RAW --cap-add NET_ADMIN
  probe "$r" caps-exec "$c" grep -E '^Cap' /proc/self/status
  probe "$r" nmap-syn-loopback "$c" timeout 120 nmap -Pn -sS -p 22,80 127.0.0.1
  probe "$r" ip-add-dummy "$c" ip link add dummy0 type dummy
  probe "$r" ip-br-link-after-dummy "$c" ip -br link
  hprobe "$r" host-after-dummy ip -br link
  nsif=$(ns_iface "$c" || true)
  if [[ -n $nsif ]]; then
    probe "$r" ip-ns-iface-down "$c" ip link set "$nsif" down
    probe "$r" ip-br-link-after-down "$c" ip -br link
    hprobe "$r" host-after-down ip -br link
  fi
  decode_caps "$OUT/$r-caps-exec.log"
  rss "$r" "$c"
  "${P[@]}" rm -f "$c" >/dev/null 2>&1 || true
}

# Run P1: pasta inbound forward on all host addresses (-t 8080), then a host browser-style fetch.
run_p1() {
  local r=P1 c=s09p hostip
  mk "$c" --network pasta:-t,8080
  serve "$c" 8080
  hostip=$(ip -4 -o addr show scope global | awk '{ split($4, a, "/"); print a[1]; exit }') || true
  hprobe "$r" host-curl-loopback curl -sS -m 5 http://127.0.0.1:8080/
  if [[ -n ${hostip:-} ]]; then
    hprobe "$r" host-curl-hostip curl -sS -m 5 "http://$hostip:8080/"
  fi
  hprobe "$r" host-ss-8080 ss -ltn "( sport = :8080 )"
  rss "$r" "$c"
  "${P[@]}" rm -f "$c" >/dev/null 2>&1 || true
}

# Run P2: pasta with podman loopback-only publish (-p 127.0.0.1:8081:8081), same checks.
run_p2() {
  local r=P2 c=s09q hostip
  mk "$c" --network pasta -p 127.0.0.1:8081:8081
  serve "$c" 8081
  hostip=$(ip -4 -o addr show scope global | awk '{ split($4, a, "/"); print a[1]; exit }') || true
  hprobe "$r" host-curl-loopback curl -sS -m 5 http://127.0.0.1:8081/
  if [[ -n ${hostip:-} ]]; then
    hprobe "$r" host-curl-hostip curl -sS -m 5 "http://$hostip:8081/"
  fi
  hprobe "$r" host-ss-8081 ss -ltn "( sport = :8081 )"
  rss "$r" "$c"
  "${P[@]}" rm -f "$c" >/dev/null 2>&1 || true
}

# Run S: slirp4netns comparison, same NET_RAW flags as run A.
run_s() {
  local r=S c=s09s
  mk "$c" --network slirp4netns --cap-add NET_RAW
  probe "$r" caps-exec "$c" grep -E '^Cap' /proc/self/status
  probe "$r" ip-addr "$c" ip addr
  probe "$r" tcp-outbound-443 "$c" timeout 15 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443 && echo TCP_CONNECT_OK'
  probe "$r" nmap-syn-loopback "$c" timeout 120 nmap -Pn -sS -p 22,80 127.0.0.1
  probe "$r" nmap-syn-example "$c" timeout 180 nmap -Pn -sS -p 80 --reason example.com
  nmap_copy "$r" nmap-copy-syn-example "$c" -Pn -sS -p 80 --reason example.com
  nmap_copy "$r" nmap-copy-ack-example "$c" -Pn -sA -p 80 --reason example.com
  decode_caps "$OUT/$r-caps-exec.log"
  rss "$r" "$c"
  "${P[@]}" rm -f "$c" >/dev/null 2>&1 || true
}

cleanup() {
  local c
  for c in "${CTRS[@]}"; do "${P[@]}" rm -f "$c" >/dev/null 2>&1 || true; done
}

cmd_run() {
  mkdir -p "$OUT" "$(dirname "$ROOT")" "$RUNROOT" "$TMPROOT"
  : > "$ROWS"
  : > "$RSS"
  : > "$CAPS"
  trap cleanup EXIT
  if command -v catatonit >/dev/null 2>&1 || [[ -x /usr/libexec/podman/catatonit ]]; then
    INIT=(--init)
  else
    log "init: catatonit absent on host, --init dropped for every run"
  fi
  pull
  build
  run_a0
  run_a
  run_b
  run_c
  run_p1
  run_p2
  run_s
  log "done; rows $ROWS, rss $RSS, caps $CAPS"
}

# Run A0: no capability added. Control for NET_RAW.
run_a0() {
  local r=A0 c=s09a0
  mk "$c" --network pasta
  probe "$r" caps-exec "$c" grep -E '^Cap' /proc/self/status
  probe "$r" nmap-syn-loopback "$c" timeout 120 nmap -Pn -sS -p 22,80 127.0.0.1
  probe "$r" tcp-outbound-443 "$c" timeout 15 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443 && echo TCP_CONNECT_OK'
  decode_caps "$OUT/$r-caps-exec.log"
  "${P[@]}" rm -f "$c" >/dev/null 2>&1 || true
}

cmd_teardown() {
  local c
  for c in "${CTRS[@]}"; do "${P[@]}" rm -f "$c" >/dev/null 2>&1 || true; done
  "${P[@]}" rmi -f "$IMAGE" "$SRC" >/dev/null 2>&1 || true
  # Podman calls above may recreate the storage skeleton, so remove the roots last and do not call podman after.
  rm -rf "$ROOT" "$RUNROOT" 2>/dev/null || true
  if [[ -e $ROOT || -e $RUNROOT ]]; then
    # Subuid-owned layers: retry inside the user namespace.
    "${P[@]}" unshare rm -rf "$ROOT" >/dev/null 2>&1 || true
    rm -rf "$RUNROOT" 2>/dev/null || true
  fi
  if [[ -e $ROOT || -e $RUNROOT ]]; then
    die "storage not fully removed: $ROOT $RUNROOT"
  fi
  rmdir "$(dirname "$ROOT")" 2>/dev/null || true
  printf 'teardown ok: %s %s removed\n' "$ROOT" "$RUNROOT"
}

case ${1:-} in
  run) cmd_run ;;
  teardown) cmd_teardown ;;
  *) die "usage: $0 run|teardown" ;;
esac
