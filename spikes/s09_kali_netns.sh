#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Four-part pin rule: every host podman call (teardown included) passes --root, --runroot and
# --tmpdir inside this spike's storage, and runs with XDG_RUNTIME_DIR pinned to the spike-owned
# directory XRD. pin_guard refuses to run podman otherwise. Without the XDG pin, rootless podman
# finds the user's pause process under /run/user/<uid>/libpod, and the three flags do not stop
# that. Earlier runs of this script did not pin XDG_RUNTIME_DIR (see spikes/results/S-09.md).
# S-09 (FR-026, FR-027, NFR-SEC-002, OOS-9, R-002): can a Kali realm run raw sockets
# (NET_RAW) inside an isolated pasta netns, and can it change host interfaces?
#
#   spikes/s09_kali_netns.sh run        every variant (A0, A, B, C, P1, P2, S); builds the image
#   spikes/s09_kali_netns.sh rerun-c    Run C rerun only: pasta + NET_RAW, the Dockerfile.s09 image
#   spikes/s09_kali_netns.sh teardown   remove spike containers, images, storage and the XRD dir
#
# Raw logs go to S09_OUT (default ~/.cache/ginn-spike/s09), never into the repository. A denied
# probe (EPERM) is an expected result: each probe records its exit status and the run continues.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
XRD_EXPECT="/run/user/$(id -u)/ginn-spike-s09c-xrd"
XRD="${S09_XDG:-$XRD_EXPECT}"
ROOT="${S09_ROOT:-$HOME/.local/share/ginn-spike-s09/podman}"
RUNROOT="$XRD/runroot"
TMPROOT="$XRD/libpod-tmp"
OUT="${S09_OUT:-${XDG_CACHE_HOME:-$HOME/.cache}/ginn-spike/s09}"
SRC=docker.io/kalilinux/kali-rolling
IMAGE=localhost/ginn-s09:kali-rolling
CTRS=(s09a0 s09a s09b s09c s09p s09q s09s s09cr)
P=(env "XDG_RUNTIME_DIR=$XRD" podman --root "$ROOT" --runroot "$RUNROOT" --tmpdir "$TMPROOT")
ROWS="$OUT/rows.tsv"
RSS="$OUT/rss.tsv"
CAPS="$OUT/caps.txt"
INIT=()

# In-realm raw-socket probes (perl core, Socket). Each prints a marker and exits 0 on success.
# Protocol 1 is IPPROTO_ICMP, given as a number: getprotobyname("icmp") returns nothing in the image
# (first run: EPROTONOSUPPORT, which is not a capability result).
PERL_RAW_SOCKET='use Socket; socket(my $s, PF_INET, SOCK_RAW, 1) or die "raw socket: $!\n"; print "RAW_ICMP_SOCKET_OK\n";'
PERL_RAW_ECHO='use Socket;
socket(my $s, PF_INET, SOCK_RAW, 1) or die "raw socket: $!\n";
my $id = $$ & 0xffff;
my $body = "ginn-s09";
sub csum { my $d = shift; $d .= "\0" if length($d) % 2; my $t = 0; $t += $_ for unpack("n*", $d); $t = ($t >> 16) + ($t & 0xffff); $t += $t >> 16; return (~$t) & 0xffff; }
my $pkt = pack("CCnnn", 8, 0, 0, $id, 1) . $body;
substr($pkt, 2, 2) = pack("n", csum($pkt));
send($s, $pkt, 0, sockaddr_in(0, inet_aton("127.0.0.1"))) or die "send: $!\n";
my $deadline = time() + 5;
while (time() < $deadline) {
  my $rin = ""; vec($rin, fileno($s), 1) = 1;
  next unless select(my $rout = $rin, undef, undef, 1) > 0;
  my $buf = "";
  recv($s, $buf, 1500, 0);
  next if length($buf) < 28;
  my $ihl = (unpack("C", substr($buf, 0, 1)) & 0xf) * 4;
  my ($type, $code, $ck, $rid, $seq) = unpack("CCnnn", substr($buf, $ihl, 8));
  if ($type == 0 && $rid == $id) { print "RAW_ICMP_ECHO_REPLY_OK id=$id seq=$seq\n"; exit 0; }
}
die "no raw ICMP echo reply\n";'
PERL_AF_PACKET='use Socket;
socket(my $p, 17, 3, unpack("n", pack("v", 3))) or die "packet socket: $!\n";
print "AF_PACKET_SOCKET_OK\n";
socket(my $u, PF_INET, SOCK_DGRAM, getprotobyname("udp")) or die "udp socket: $!\n";
send($u, "ginn-s09", 0, sockaddr_in(9, inet_aton("127.0.0.1"))) or die "send: $!\n";
my $n = 0;
my $deadline = time() + 3;
while (time() < $deadline) {
  my $rin = ""; vec($rin, fileno($p), 1) = 1;
  next unless select(my $rout = $rin, undef, undef, 1) > 0;
  my $buf = "";
  recv($p, $buf, 2048, 0);
  $n++ if length($buf) > 0;
}
print "AF_PACKET_FRAMES_RECEIVED=$n\n";
exit($n > 0 ? 0 : 1);'

log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$OUT/run.log"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# pin_guard: refuse to run podman unless XDG_RUNTIME_DIR is pinned to the spike-owned XRD.
# P sets XDG_RUNTIME_DIR to XRD on every podman call. Each entry point runs this guard first.
pin_guard() {
  local owner mode
  [[ $XRD == "$XRD_EXPECT" ]] || die "refusing podman: XDG pin $XRD is not $XRD_EXPECT"
  case $ROOT in *'/.local/share/containers'*|*'/libpod'*) die "refusing podman: ROOT $ROOT is user storage" ;; esac
  if [[ -L $XRD ]]; then die "refusing podman: $XRD is a symlink"; fi
  if [[ ! -e $XRD ]]; then mkdir -m 0700 "$XRD" || die "cannot create $XRD"; fi
  [[ -d $XRD ]] || die "refusing podman: $XRD is not a directory"
  owner=$(stat -c %u "$XRD")
  mode=$(stat -c %a "$XRD")
  [[ $owner == "$(id -u)" && $mode == 700 ]] || die "refusing podman: $XRD owner=$owner mode=$mode, need $(id -u) and 700"
}

# row RUN LABEL RC LOGFILE: one TSV row. Decisive line = last output line, headers and rc marker skipped.
row() {
  local last=""
  last=$(grep -v -e '^[[:space:]]*$' -e '^\[rc=' -e '^--- ' "$4" | tail -n 1 | cut -c1-200) || true
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$last" >> "$ROWS"
}

# probe RUN LABEL CTR ARGV...: exec ARGV in CTR. stdout and stderr are kept apart and both appear
# in the log file, then the exit status. Never fatal.
probe() {
  local run=$1 label=$2 ctr=$3 rc=0 f
  shift 3
  f="$OUT/$run-$label.log"
  pin_guard
  printf '$ %s\n' "$*" > "$f"
  timeout 240 "${P[@]}" exec "$ctr" "$@" </dev/null >"$f.stdout" 2>"$f.stderr" || rc=$?
  { printf -- '--- stdout\n'; cat "$f.stdout"; printf -- '--- stderr\n'; cat "$f.stderr"; printf '[rc=%s]\n' "$rc"; } >> "$f"
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

# host_snap LABEL: write the host interface state to $OUT/host-LABEL.txt. Read-only.
host_snap() {
  {
    echo '-- ip -br link'; ip -br link
    echo '-- ip -br addr'; ip -br addr
    echo '-- lsmod dummy'; lsmod | grep -E '^dummy ' || echo '(dummy module not loaded)'
  } > "$OUT/host-$1.txt" 2>&1
  log "host snapshot $1 at $(date -Is)"
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
  pin_guard
  key=$("${P[@]}" inspect --format '{{.NetworkSettings.SandboxKey}}' "$ctr")
  pid=$("${P[@]}" inspect --format '{{.State.Pid}}' "$ctr")
  ps -eo pid=,ppid=,rss=,args= -ww | grep -E 'pasta|slirp4netns' | grep -v grep > "$OUT/ps-$run-$ctr.txt" || true
  awk -v run="$run" -v ctr="$ctr" -v k="$key" -v p="$pid" \
    '(k != "" && index($0, k)) || $0 ~ ("[ ,=]" p "([ ]|$)") { printf "%s\t%s\t%s KiB\t%s\n", run, ctr, $3, $4 }' \
    "$OUT/ps-$run-$ctr.txt" >> "$RSS" || true
}

# mk NAME FLAG...: start a detached spike container that runs sleep infinity. The exact podman
# argv is written to $OUT/NAME-argv.txt (printf %q, so it can be pasted back into a shell).
mk() {
  local name=$1
  shift
  pin_guard
  "${P[@]}" rm -f "$name" >/dev/null 2>&1 || true
  local argv=("${P[@]}" run -d --name "$name" --userns keep-id --user 0:0 ${INIT[@]+"${INIT[@]}"} "$@" "$IMAGE" sleep infinity)
  printf '%q ' "${argv[@]}" > "$OUT/$name-argv.txt"
  echo >> "$OUT/$name-argv.txt"
  "${argv[@]}" > "$OUT/$name-run.log" 2>&1
  log "started $name: $*"
}

# rm_ctr NAME: remove one spike container through the pinned wrapper.
rm_ctr() {
  pin_guard
  "${P[@]}" rm -f "$1" >/dev/null 2>&1 || true
}

# ns_iface CTR: first non-loopback interface name inside CTR.
ns_iface() {
  pin_guard
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
  pin_guard
  "${P[@]}" exec -d "$c" perl -MSocket -e "$code" "$port" || log "serve $c:$port failed to start"
  sleep 2
}

pull() {
  pin_guard
  log "pull $SRC"
  "${P[@]}" pull "$SRC" > "$OUT/pull.log" 2>&1
  "${P[@]}" image inspect --format '{{.Id}} {{index .RepoDigests 0}}' "$SRC" > "$OUT/digest.txt"
  log "digest $(cat "$OUT/digest.txt")"
}

build() {
  local ctx
  pin_guard
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
  rm_ctr "$c"
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
  rm_ctr "$c"
}

# Run C rerun (FR-027 AC-1, AC-2): pasta with NET_RAW only, the product flags. Each interface change
# is attempted from inside the realm. Host state is snapped at start (cmd_rerun_c), after the realm
# add, after the realm down, and at the end (cmd_rerun_c). The image is Dockerfile.s09 on Kali.
run_c() {
  local r=Cr c=s09cr nsif
  mk "$c" --network pasta --cap-add NET_RAW
  "${P[@]}" inspect --format '{{.State.Running}}' "$c" | grep -qx true || die "container $c not running, see $OUT/$c-run.log"
  pin_guard
  "${P[@]}" inspect "$c" > "$OUT/$c-inspect.json"
  probe "$r" tool-check "$c" sh -c 'command -v ip; command -v perl'
  grep -q '/ip$' "$OUT/$r-tool-check.log.stdout" || die "no ip(8) in $SRC; stop, nothing is installed by this spike"
  grep -q '/perl$' "$OUT/$r-tool-check.log.stdout" || die "no perl in $SRC"
  probe "$r" caps-exec "$c" grep -E '^Cap' /proc/self/status
  probe "$r" realm-ip-br-link-before "$c" ip -br link
  probe "$r" realm-ip-br-addr-before "$c" ip -br addr
  nsif=$(ns_iface "$c" || true)
  [[ -n $nsif ]] || die "no non-loopback interface in $c"
  probe "$r" ip-link-add-dummy "$c" ip link add dummys09c type dummy
  host_snap after-add
  probe "$r" ip-link-set-iface-down "$c" ip link set "$nsif" down
  host_snap after-down
  probe "$r" ip-link-set-lo-down "$c" ip link set lo down
  probe "$r" ip-link-set-host-wlp4s0-down "$c" ip link set wlp4s0 down
  probe "$r" ip-link-set-host-dummy0-down "$c" ip link set dummy0 down
  probe "$r" ip-addr-add-iface "$c" ip addr add 10.99.0.1/32 dev "$nsif"
  probe "$r" proto-lookup "$c" perl -MSocket -e 'print "getprotobyname(icmp)=", (getprotobyname("icmp") // "undef"), "\n"'
  probe "$r" raw-icmp-socket "$c" perl -e "$PERL_RAW_SOCKET"
  probe "$r" raw-icmp-echo-loopback "$c" perl -e "$PERL_RAW_ECHO"
  probe "$r" af-packet-capture "$c" perl -e "$PERL_AF_PACKET"
  probe "$r" realm-ip-br-link-after "$c" ip -br link
  probe "$r" realm-ip-br-addr-after "$c" ip -br addr
  decode_caps "$OUT/$r-caps-exec.log"
  rm_ctr "$c"
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
  rm_ctr "$c"
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
  rm_ctr "$c"
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
  rm_ctr "$c"
}

cleanup() {
  local c
  pin_guard
  for c in "${CTRS[@]}"; do "${P[@]}" rm -f "$c" >/dev/null 2>&1 || true; done
}

# spike_procs: print every live process that belongs to this spike. A process belongs to the spike
# when its environ pins XDG_RUNTIME_DIR to XRD, or (not for shells, whose command text quotes the
# paths) its cmdline names XRD or the spike storage root. Unreadable /proc entries are skipped quietly.
spike_procs() {
  local p pid cmd env_all exe hit
  for p in /proc/[0-9]*; do
    pid=${p#/proc/}
    [[ $pid == "$$" ]] && continue
    cmd=$({ tr '\0' ' ' < "$p/cmdline"; } 2>/dev/null) || continue
    env_all=$({ tr '\0' '\n' < "$p/environ"; } 2>/dev/null) || env_all=""
    exe=$(readlink "$p/exe" 2>/dev/null) || exe=""
    hit=0
    case ${exe##*/} in
      bash|sh|dash|zsh|"") ;;
      *) [[ $cmd == *"$XRD"* || $cmd == *"$ROOT"* ]] && hit=1 ;;
    esac
    [[ $'\n'$env_all$'\n' == *$'\n'"XDG_RUNTIME_DIR=$XRD"$'\n'* ]] && hit=1
    if (( hit )); then
      printf 'pid=%s cmd=%s\n' "$pid" "$cmd"
    fi
  done
}

cmd_run() {
  pin_guard
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

# rerun-c: Run C rerun only. Writes into a new timestamped directory under OUT, so no earlier log is
# overwritten. The Kali image is pulled and Dockerfile.s09 is built on it inside the spike storage
# (the same image as the earlier S-09 runs). The host installs nothing.
cmd_rerun_c() {
  OUT="$OUT/rerun-c-$(date +%Y%m%dT%H%M%S)"
  mkdir -p "$OUT"
  ROWS="$OUT/rows.tsv"
  RSS="$OUT/rss.tsv"
  CAPS="$OUT/caps.txt"
  : > "$ROWS"
  : > "$RSS"
  : > "$CAPS"
  host_snap start
  pin_guard
  mkdir -p "$(dirname "$ROOT")" "$RUNROOT" "$TMPROOT"
  trap cleanup EXIT
  pull
  build
  run_c
  host_snap end
  diff -u "$OUT/host-start.txt" "$OUT/host-end.txt" > "$OUT/host-diff.txt" || true
  log "done; rows $ROWS, caps $CAPS, host diff $OUT/host-diff.txt"
}

# stop_spike_pause: stop the rootless pause process that podman starts under XRD. Only a process whose
# environ pins XRD and whose cmdline is the pause command gets SIGTERM, by its exact pid. Runs after
# the last podman call, because each podman call can start a new pause process.
stop_spike_pause() {
  local line pid cmd n
  while IFS= read -r line; do
    pid=${line#pid=}
    pid=${pid%% *}
    cmd=${line#*cmd=}
    case $cmd in 'catatonit -P '|'podman pause'*) ;; *) continue ;; esac
    n=$({ tr '\0' '\n' < "/proc/$pid/environ"; } 2>/dev/null | grep -cx "XDG_RUNTIME_DIR=$XRD" || true)
    [[ $n == 1 ]] || continue
    log "stopping spike pause process pid=$pid cmd=$cmd"
    kill -TERM "$pid" 2>/dev/null || true
  done < <(spike_procs)
  sleep 2
}

cmd_teardown() {
  pin_guard
  local c leftover
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
  stop_spike_pause
  leftover=$(spike_procs)
  [[ -z $leftover ]] || die "spike processes remain, XDG dir kept: $leftover"
  if grep -qF "$XRD" /proc/self/mountinfo; then die "mounts remain under $XRD, XDG dir kept"; fi
  rm -rf "$XRD"
  [[ ! -e $XRD ]] || die "XDG dir not removed: $XRD"
  printf 'teardown ok: %s %s %s removed; no spike processes\n' "$ROOT" "$RUNROOT" "$XRD"
}

case ${1:-} in
  run) cmd_run ;;
  rerun-c) cmd_rerun_c ;;
  teardown) cmd_teardown ;;
  *) die "usage: $0 run|rerun-c|teardown" ;;
esac
