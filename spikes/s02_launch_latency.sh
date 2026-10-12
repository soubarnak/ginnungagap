#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Three-flag rule: every podman call passes --root, --runroot and --tmpdir, all inside this spike's
# directories. Without --tmpdir podman shares the user's libpod tmpdir.
# S-02 (risk R-006): launch latency for NFR-004 (p95 <= 300 ms), the NFR-005 cold start
# (FR-017) and Python startup in the FR-046 launcher path.
# Builds spikes/out/s02_run, runs every measurement against one throwaway rootless container
# in dedicated podman storage, and prints Markdown to stdout. Requires rootless podman and
# network access for the first image pull. Never resets podman storage.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
OUT="$HERE/out"
ROOT="${S02_ROOT:-$HOME/.local/share/ginn-spike-s02/podman}"
RUNROOT="${S02_RUNROOT:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ginn-spike-s02}"
TMPROOT="$RUNROOT/libpod-tmp"
NAME=s02
IMAGE=docker.io/library/alpine:3
N=${S02_N:-100}
WARM=${S02_WARM:-5}
COLD_N=${S02_COLD_N:-20}
COLD_WARM=2
HOST_UID=$(id -u)
P=(podman --root "$ROOT" --runroot "$RUNROOT" --tmpdir "$TMPROOT")
HELPER="$OUT/s02_run"
STATE="$OUT/state.json"
ROWS="$OUT/rows.tsv"

# Only the spike container is removed here. The spike storage is left in place.
cleanup() { "${P[@]}" rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

die() { echo "S-02 FAIL: $*" >&2; exit 1; }

# Timing driver. Runs under python3 -I. Wall time per call is taken with perf_counter_ns around
# subprocess.run (stdin DEVNULL). Every return code is checked; any nonzero aborts the spike.
IFS= read -r -d '' DRIVER <<'PY' || true
import json, math, subprocess, sys, time

def pct(vals, p):
    # nearest-rank percentile over sorted values
    return vals[max(1, math.ceil(p / 100 * len(vals))) - 1]

def emit(label, ns):
    ms = sorted(x / 1e6 for x in ns)
    print("\t".join([label, str(len(ms)), f"{pct(ms, 50):.3f}", f"{pct(ms, 95):.3f}", f"{ms[-1]:.3f}"]))

def run(argv):
    t0 = time.perf_counter_ns()
    r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dt = time.perf_counter_ns() - t0
    if r.returncode != 0:
        sys.exit(f"driver: rc {r.returncode} from {argv}: {r.stderr.decode(errors='replace')[-300:]}")
    return dt, r.stdout.decode(errors="replace")

def groups(args):
    out, cur = [], []
    for a in args:
        if a == "--":
            out.append(cur)
            cur = []
        else:
            cur.append(a)
    out.append(cur)
    return out

mode = sys.argv[1]
if mode == "readstate":
    label, n, path = sys.argv[2], int(sys.argv[3]), sys.argv[4]
    samples = []
    for _ in range(n):
        t0 = time.perf_counter_ns()
        with open(path) as f:
            json.loads(f.read())
        samples.append(time.perf_counter_ns() - t0)
    emit(label, samples)
else:
    g = groups(sys.argv[2:])
    label, n, warm = g[0][0], int(g[0][1]), int(g[0][2])
    if mode == "bench":
        argv = g[1]
        for _ in range(warm):
            run(argv)
        emit(label, [run(argv)[0] for _ in range(n)])
    elif mode == "cold":
        stop, start, verify = g[1], g[2], g[3]
        samples = []
        for i in range(warm + n):
            run(stop)
            dt, _ = run(start)
            _, state = run(verify)
            if state.strip() != "running":
                sys.exit(f"driver: not running after start: {state.strip()!r}")
            if i >= warm:
                samples.append(dt)
        emit(label, samples)
    else:
        sys.exit(f"driver: unknown mode {mode}")
PY

# Launchers emulate the FR-046 path: read the state file, check the start time (PID reuse guard),
# then exec. The state file holds the container name, host PID 1 and its start time (stat field 22).
IFS= read -r -d '' LAUNCH_EXEC <<'PY' || true
import json, pathlib, subprocess, sys
st = json.loads(pathlib.Path(sys.argv[1]).read_text())
sys.exit(subprocess.run(["podman", "--root", sys.argv[2], "--runroot", sys.argv[3],
                         "--tmpdir", sys.argv[4], "exec", st["name"], *sys.argv[5:]]).returncode)
PY
IFS= read -r -d '' LAUNCH_HELPER <<'PY' || true
import json, pathlib, subprocess, sys
st = json.loads(pathlib.Path(sys.argv[1]).read_text())
pid = int(st["pid"])
start = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
if start != st["starttime"]:
    sys.exit("stale state file: start time mismatch")
sys.exit(subprocess.run([sys.argv[2], str(pid), *sys.argv[3:]]).returncode)
PY

# Same user-visible facts from inside each runner: ids, seccomp, bounding set, cgroup, namespaces.
IFS= read -r -d '' PARITY <<'SH' || true
for f in Uid Groups CapEff CapBnd NoNewPrivs Seccomp; do grep "^$f:" /proc/$$/status; done
echo "cgroup: $(cat /proc/$$/cgroup | tr '\n' ' ')"
for n in user mnt pid uts ipc cgroup net; do echo "ns/$n $(readlink /proc/$$/ns/$n)"; done
SH

row() { # label, argv...: one timed row appended to $ROWS
  local label=$1; shift
  echo "running: $label" >&2
  python3 -I -c "$DRIVER" bench "$label" "$N" "$WARM" -- "$@" >> "$ROWS"
}

snapshot() {
  echo "- memory: $(free -m | awk '/^Mem:/ {print "total " $2 " MiB, available " $7 " MiB"}')"
  echo "- loadavg: $(cut -d' ' -f1-3 /proc/loadavg)"
  echo "- pressure cpu: $(head -n1 /proc/pressure/cpu | sed 's/ total=.*//')"
  echo "- pressure memory: $(head -n1 /proc/pressure/memory | sed 's/ total=.*//')"
  echo "- pressure io: $(head -n1 /proc/pressure/io | sed 's/ total=.*//')"
}

gates() { # label, runner prefix argv...: proves the namespaces were entered before any timing
  local label=$1; shift
  local -a pre=("$@")
  local uid pid_in rc=0
  "${pre[@]}" grep -q Alpine /etc/os-release || die "$label: not inside the Alpine rootfs"
  uid=$("${pre[@]}" id -u)
  [[ "$uid" == "$HOST_UID" ]] || die "$label: uid $uid, expected $HOST_UID"
  pid_in=$("${pre[@]}" sh -c 'echo $$')
  (( pid_in < 1000 )) || die "$label: pid $pid_in is not in the container pid namespace"
  "${pre[@]}" sh -c 'exit 7' || rc=$?
  [[ $rc == 7 ]] || die "$label: exit 7 came back as $rc (IR-011, FR-049)"
  echo "- $label: Alpine rootfs, uid $uid, in-namespace pid $pid_in, exit status 7 passed through"
}

cgroup_probe() { # helper child vs container init, host view; podman top lists container cgroup members
  local hp hc=""
  "$HELPER" "$PID" sleep 3 & hp=$!
  sleep 1
  hc=$(pgrep -P "$hp" | head -n1 || true)
  echo "- container init, host PID $PID: $(tr '\n' ' ' < /proc/$PID/cgroup)"
  if [[ -n "$hc" ]]; then echo "- helper child, host PID $hc: $(tr '\n' ' ' < /proc/$hc/cgroup)"; fi
  echo '```'
  "${P[@]}" top "$NAME" hpid,args
  echo '```'
  wait "$hp" || true
}

table() { # optional grep pattern on the row label
  local filter=${1:-.}
  echo "| Row | n | p50 ms | p95 ms | max ms |"
  echo "|---|---:|---:|---:|---:|"
  awk -F'\t' -v re="$filter" '$1 ~ re {printf "| %s | %s | %s | %s | %s |\n", $1, $2, $3, $4, $5}' "$ROWS"
}

parity_block() { # label, runner prefix argv...
  local label=$1; shift
  echo "### $label"
  echo '```'
  "$@" sh -c "$PARITY"
  echo '```'
  echo
}

# ---- build -------------------------------------------------------------------------------
mkdir -p "$OUT" "$TMPROOT"
cc -std=c11 -O2 -Wall -Wextra -Werror -D_FORTIFY_SOURCE=2 -o "$HELPER" "$HERE/s02_run.c"

# ---- environment -------------------------------------------------------------------------
echo "## Environment"
echo
echo "- kernel: $(uname -r)"
echo "- cpu: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')"
echo "- podman: $("${P[@]}" --version), runc: $(/usr/bin/runc --version | head -n1)"
echo "- python3: $(python3 -V 2>&1) at $(command -v python3)"
echo "- storage: --root $ROOT --runroot $RUNROOT --tmpdir $TMPROOT"
if [[ -x /usr/bin/catatonit ]]; then
  INIT=(--init --init-path /usr/bin/catatonit)
  echo "- catatonit: /usr/bin/catatonit present, container runs with --init"
else
  INIT=()
  echo "- catatonit: missing from /usr/bin and /usr/libexec/podman; container runs without --init"
fi
snapshot
echo

# ---- container and state -----------------------------------------------------------------
"${P[@]}" rm -f "$NAME" >/dev/null 2>&1 || true
if ! "${P[@]}" image exists "$IMAGE"; then "${P[@]}" pull -q "$IMAGE" >/dev/null; fi
"${P[@]}" run -d --name "$NAME" --userns keep-id --network host "${INIT[@]}" "$IMAGE" sleep infinity >/dev/null
PID=$("${P[@]}" inspect --format '{{.State.Pid}}' "$NAME")
ST=$(sed 's/.*) //' "/proc/$PID/stat" | awk '{print $20}')
printf '{"name": "%s", "pid": %s, "starttime": "%s"}\n' "$NAME" "$PID" "$ST" > "$STATE"
: > "$ROWS"

EXEC_PREFIX=("${P[@]}" exec "$NAME")
HELPER_PREFIX=("$HELPER" "$PID")
NSENTER_PREFIX=(nsenter --preserve-credentials -U -m -p -u -i -t "$PID")

# ---- correctness gates (untimed) ---------------------------------------------------------
echo "## Gates (untimed)"
echo
gates "podman exec" "${EXEC_PREFIX[@]}"
gates "s02_run" "${HELPER_PREFIX[@]}"
gates "nsenter" "${NSENTER_PREFIX[@]}"
neg=0
out=$("$HELPER" 1 true 2>&1) || neg=$?
[[ $neg == 125 ]] || die "negative control: helper on host PID 1 returned $neg"
echo "- negative control: s02_run on host PID 1 -> exit $neg: $out"
echo

# ---- parity (untimed) --------------------------------------------------------------------
echo "## Parity: what the process inherits"
echo
parity_block "podman exec" "${EXEC_PREFIX[@]}"
parity_block "s02_run" "${HELPER_PREFIX[@]}"
parity_block "nsenter" "${NSENTER_PREFIX[@]}"
echo "### Host view of cgroup membership"
echo
cgroup_probe
echo

# ---- latency (timed) ---------------------------------------------------------------------
echo "## Latency"
echo
echo "Method: wall time per call from python3 -I with perf_counter_ns around subprocess.run;" \
     "$WARM warm-up calls discarded; $N measured; nearest-rank percentiles; any nonzero exit aborts."
echo
row "podman inspect --format {{.State.Pid}} (PID lookup)" "${P[@]}" inspect --format '{{.State.Pid}}' "$NAME"
row "A: podman exec s02 true" "${P[@]}" exec "$NAME" true
row "B: podman exec --user $HOST_UID s02 true" "${P[@]}" exec --user "$HOST_UID" "$NAME" true
row "C: s02_run PID true" "$HELPER" "$PID" true
row "D: nsenter --preserve-credentials -U -m -p -u -i -t PID true" "${NSENTER_PREFIX[@]}" true
row "P1: python3 -c pass" python3 -c pass
row "P2: python3 -I -S -c pass" python3 -I -S -c pass
row "P3: python3 -c 'import argparse, json, subprocess, pathlib'" python3 -c "import argparse, json, subprocess, pathlib"
(cd "$REPO" && row "P4: PYTHONPATH=src python3 -m ginn --version" env PYTHONPATH=src python3 -m ginn --version)
echo "running: state file read" >&2
python3 -I -c "$DRIVER" readstate "S: state file read and JSON parse, in process (1000 reads)" 1000 "$STATE" >> "$ROWS"
row "L1: python3 launcher + state + podman exec" python3 -c "$LAUNCH_EXEC" "$STATE" "$ROOT" "$RUNROOT" "$TMPROOT" true
row "L2: python3 launcher + state + s02_run" python3 -c "$LAUNCH_HELPER" "$STATE" "$HELPER" true
table
echo

# ---- cold path (FR-017, NFR-005), last because it changes the PID --------------------------
echo "## Cold start (FR-017, NFR-005)"
echo
echo "Method: podman stop -t 0 and verify are untimed; podman start is timed; $COLD_WARM warm-up cycles discarded; $COLD_N measured; the container must report running after each start."
echo
echo "running: cold start" >&2
python3 -I -c "$DRIVER" cold "K: podman start after stop -t 0" "$COLD_N" "$COLD_WARM" -- \
  "${P[@]}" stop -t 0 "$NAME" -- "${P[@]}" start "$NAME" -- \
  "${P[@]}" inspect --format '{{.State.Status}}' "$NAME" >> "$ROWS"
table '^K:'
echo
echo "## Snapshot after run"
echo
snapshot
