# SPDX-License-Identifier: Apache-2.0
"""S-05r accounting helper: RSS and PSS of one realm's host-side processes.

Roles:
  conmon     the realm's conmon process
  container  every process of the realm: descendants of conmon (the container's PID 1 is a
             child of conmon, which is a subreaper), plus members of the realm's libpod
             cgroup scope when the init sits in one
  pause      ginn's rootless pause, the process named by <tmpdir>/pause.pid. It is accepted
             only when its comm is catatonit and its environ carries the spike
             XDG_RUNTIME_DIR, so the user's own pause is never counted or signalled.

RSS is VmRSS from /proc/PID/status. PSS is smaps_rollup Pss. Processes owned by a
subordinate uid cannot be read by the calling user, so their PSS is read inside
`podman unshare`. That is the only podman call outside the shell wrapper. It runs with the
full pin: XDG_RUNTIME_DIR in its environ, and --runtime, --root, --runroot and --tmpdir.

  measure.py --conmon PID --init PID --pause-file FILE --spike-xdg DIR [--json-out FILE]
             --podman-root DIR --podman-runroot DIR --podman-tmpdir DIR [--podman-runtime crun]
  measure.py --pss-only PID...                   (internal, run under unshare)
  measure.py --summarize RUNS.tsv --sweep ID     (median, min, max per config, one sweep id)
"""
import argparse
import json
import os
import statistics
import subprocess
import sys

SPIKE_XRD_TAIL = "/ginn-spike-s05r-xrd"
PAUSE_COMM = "catatonit"
# Summary columns, in MiB in the output. Names match runs.tsv.
SUMMARY_COLUMNS = ("realm_rss_kib", "pause_rss_kib", "total_rss_kib", "realm_pss_kib",
                   "pause_pss_kib", "total_pss_kib", "swap_kib", "nproc_container")
# Measurement output order. The shell writes these into runs.tsv in this order.
MEASURE_FIELDS = ("conmon_rss_kib", "realm_rss_kib", "pause_rss_kib", "total_rss_kib",
                  "realm_pss_kib", "pause_pss_kib", "total_pss_kib", "nproc_container",
                  "swap_kib", "pss_missing", "pause_pid")


def _read(path):
    with open(path, "rb") as fh:
        return fh.read().decode(errors="replace")


def _kib(value):
    return int(value.split()[0]) if value else 0


def _children_map():
    kids = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = _read(f"/proc/{entry}/stat")
        except OSError:
            continue
        ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        kids.setdefault(ppid, []).append(int(entry))
    return kids


def _descendants(kids, root):
    seen, todo = set(), [root]
    while todo:
        for child in kids.get(todo.pop(), []):
            if child not in seen:
                seen.add(child)
                todo.append(child)
    return seen


def _status(pid):
    fields = {}
    for line in _read(f"/proc/{pid}/status").splitlines():
        key, _, value = line.partition(":")
        fields[key] = value.strip()
    return fields


def _cgroup_path(pid):
    for line in _read(f"/proc/{pid}/cgroup").splitlines():
        if line.startswith("0::"):
            return line[3:]
    return ""


def _pss_direct(pid):
    try:
        for line in _read(f"/proc/{pid}/smaps_rollup").splitlines():
            if line.startswith("Pss:"):
                return _kib(line.split(":", 1)[1])
    except OSError:
        return None
    return None


def _spike_pause(pause_file, spike_xdg):
    """Return the pid of the spike's own catatonit pause, or exit with the reason."""
    try:
        pid = int(_read(pause_file).strip())
        comm = _read(f"/proc/{pid}/comm").strip()
    except (OSError, ValueError) as exc:
        raise SystemExit(f"no live spike pause in {pause_file}: {exc}")
    if comm != PAUSE_COMM:
        raise SystemExit(f"spike pause {pid} has comm {comm!r}, expected {PAUSE_COMM!r}")
    if f"XDG_RUNTIME_DIR={spike_xdg}" not in _read(f"/proc/{pid}/environ").split("\0"):
        raise SystemExit(f"pause {pid} does not carry XDG_RUNTIME_DIR={spike_xdg}")
    return pid


def _pss_via_unshare(pids, args):
    if not args.spike_xdg.endswith(SPIKE_XRD_TAIL):
        raise SystemExit(f"refusing: {args.spike_xdg} is not the S-05r spike dir")
    # Full pinned form: XDG_RUNTIME_DIR in the environ, and the three storage flags plus runtime.
    cmd = ["podman", "--runtime", args.podman_runtime, "--root", args.podman_root,
           "--runroot", args.podman_runroot, "--tmpdir", args.podman_tmpdir,
           "unshare", sys.executable, "-I", os.path.abspath(__file__), "--pss-only",
           *map(str, pids)]
    env = dict(os.environ, XDG_RUNTIME_DIR=args.spike_xdg)
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        return {}
    return {int(k): v for k, v in json.loads(res.stdout).items()}


def _row(pid, role):
    try:
        st = _status(pid)
    except OSError:
        return None
    if "VmRSS" not in st:
        return None
    return {"pid": pid, "role": role, "name": st.get("Name", "?"),
            "rss_kib": _kib(st.get("VmRSS")), "swap_kib": _kib(st.get("VmSwap")),
            "anon_kib": _kib(st.get("RssAnon")), "file_kib": _kib(st.get("RssFile")),
            "shmem_kib": _kib(st.get("RssShmem"))}


def _sum(rows, role):
    return sum(r["rss_kib"] for r in rows if r["role"] == role)


def _pss_sum(rows, roles):
    return sum(r["pss_kib"] or 0 for r in rows if r["role"] in roles)


def realm(args):
    pause = _spike_pause(args.pause_file, args.spike_xdg)
    kids = _children_map()
    conmon = args.conmon
    members = _descendants(kids, conmon) | {args.init}
    path = _cgroup_path(args.init)
    if "libpod-" in path:
        procs = f"/sys/fs/cgroup{path}/cgroup.procs"
        try:
            members |= {int(p) for p in _read(procs).split()}
        except OSError:
            pass
    members.discard(conmon)
    members.discard(pause)
    rows = []
    for pid, role in [(conmon, "conmon")] + [(p, "container") for p in sorted(members)] + \
            [(pause, "pause")]:
        row = _row(pid, role)
        if row:
            rows.append(row)
    denied = []
    for row in rows:
        row["pss_kib"] = _pss_direct(row["pid"])
        if row["pss_kib"] is None:
            denied.append(row["pid"])
    if denied and args.podman_root:
        via = _pss_via_unshare(denied, args)
        for row in rows:
            if row["pid"] in via:
                row["pss_kib"] = via[row["pid"]]
    missing = [r["pid"] for r in rows if r["pss_kib"] is None]
    realm_roles = ("conmon", "container")
    out = {
        "conmon_rss_kib": _sum(rows, "conmon"),
        "realm_rss_kib": sum(r["rss_kib"] for r in rows if r["role"] in realm_roles),
        "pause_rss_kib": _sum(rows, "pause"),
        "total_rss_kib": sum(r["rss_kib"] for r in rows),
        "realm_pss_kib": _pss_sum(rows, realm_roles),
        "pause_pss_kib": _pss_sum(rows, ("pause",)),
        "total_pss_kib": _pss_sum(rows, ("conmon", "container", "pause")),
        "nproc_container": sum(1 for r in rows if r["role"] == "container"),
        "swap_kib": sum(r["swap_kib"] for r in rows),
        "pss_missing": len(missing),
        "pause_pid": pause,
        "procs": rows,
    }
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=1)
    print("\t".join(str(out[f]) for f in MEASURE_FIELDS))


def _mib(kib):
    return f"{kib / 1024:.2f}"


def summarize(args):
    with open(args.summarize) as fh:
        table = [line.rstrip("\n").split("\t") for line in fh]
    header, data = table[0], table[1:]
    idx = {name: i for i, name in enumerate(header)}
    sel = [r for r in data if r[idx["sweep_id"]] == args.sweep]
    if not sel:
        raise SystemExit(f"no rows for sweep {args.sweep}")
    configs = []
    for r in sel:
        if r[idx["cfg"]] not in configs:
            configs.append(r[idx["cfg"]])
    print("sweep\tcfg\truns_ok\tsettle_ok_or_na\tpause_pids\tcolumn\tmedian_MiB\tmin_MiB\tmax_MiB")
    for cfg in configs:
        ok = [r for r in sel if r[idx["cfg"]] == cfg and r[idx["status"]] == "ok"]
        settled = sum(1 for r in ok if r[idx["settle"]] in ("ok", "na"))
        pids = ",".join(sorted({r[idx["pause_pid"]] for r in ok}))
        for col in SUMMARY_COLUMNS:
            vals = [int(r[idx[col]]) for r in ok if r[idx[col]].lstrip("-").isdigit()]
            if not vals:
                continue
            # nproc_container is a process count, the rest are KiB shown as MiB.
            fmt = (lambda v: f"{v:g}") if col == "nproc_container" else _mib
            print(f"{args.sweep}\t{cfg}\t{len(ok)}\t{settled}\t{pids}\t{col}\t"
                  f"{fmt(statistics.median(vals))}\t{fmt(min(vals))}\t{fmt(max(vals))}")


def split(paths):
    """RssAnon, RssFile and RssShmem per realm and pause, read from saved raw JSON (one TSV row each).

    RssFile can fall under memory pressure without any swap (reclaimed file pages leave RSS), so the
    split shows which rows lost file-backed RSS. RssAnon is the part that does not move with reclaim.
    """
    print("raw\trealm_rss_kib\trealm_anon_kib\trealm_file_kib\trealm_shmem_kib\tpause_rss_kib"
          "\tpause_anon_kib\tpause_file_kib")
    for path in paths:
        with open(path) as fh:
            procs = json.load(fh)["procs"]
        realm = [p for p in procs if p["role"] != "pause"]
        pause = [p for p in procs if p["role"] == "pause"]
        fields = [sum(p["rss_kib"] for p in realm), sum(p["anon_kib"] for p in realm),
                  sum(p["file_kib"] for p in realm), sum(p["shmem_kib"] for p in realm),
                  sum(p["rss_kib"] for p in pause), sum(p["anon_kib"] for p in pause),
                  sum(p["file_kib"] for p in pause)]
        print("\t".join([os.path.basename(path)] + [str(v) for v in fields]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--split-raw", nargs="*", default=None)
    ap.add_argument("--conmon", type=int)
    ap.add_argument("--init", type=int)
    ap.add_argument("--pause-file", default="")
    ap.add_argument("--spike-xdg", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--podman-root", default="")
    ap.add_argument("--podman-runroot", default="")
    ap.add_argument("--podman-tmpdir", default="")
    ap.add_argument("--podman-runtime", default="crun")
    ap.add_argument("--pss-only", nargs="*", type=int)
    ap.add_argument("--summarize", default="")
    ap.add_argument("--sweep", default="")
    args = ap.parse_args()
    if args.split_raw is not None:
        split(args.split_raw)
    elif args.pss_only is not None:
        print(json.dumps({str(p): _pss_direct(p) for p in args.pss_only}))
    elif args.summarize:
        if not args.sweep:
            raise SystemExit("--summarize needs --sweep ID")
        summarize(args)
    else:
        realm(args)


if __name__ == "__main__":
    main()
