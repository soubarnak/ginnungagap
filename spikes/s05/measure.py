# SPDX-License-Identifier: Apache-2.0
"""S-05 accounting helper: RSS and PSS of one realm's host-side processes.

Roles:
  conmon     the realm's conmon process
  container  every process of the realm: descendants of conmon (the container's
             PID 1 is a child of conmon, which is a subreaper), plus members of
             the realm's libpod cgroup scope when the init sits in one
  pause      the rootless "podman pause" process named by <tmpdir>/pause.pid
             (the spike's own podman tmpdir), reported apart from the realm

RSS is VmRSS from /proc/PID/status. PSS is smaps_rollup Pss. Processes owned by
a subordinate uid (services-on realms run as keep-id root) cannot be read by the
calling user, so their PSS is read inside `podman unshare`, which runs in the
rootless user namespace.

  measure.py --conmon PID --init PID [--pause-pid PID] [--json-out FILE]
             [--podman-root DIR --podman-runroot DIR --podman-tmpdir DIR]
  measure.py --pss-only PID...                 (internal, run under unshare)
  measure.py --summarize RUNS.tsv              (median, min, max per config)
"""
import argparse
import json
import os
import statistics
import subprocess
import sys


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


def _pss_via_unshare(pids, root, runroot, tmpdir):
    cmd = ["podman", "--root", root, "--runroot", runroot, "--tmpdir", tmpdir, "unshare",
           sys.executable, "-I", os.path.abspath(__file__), "--pss-only", *map(str, pids)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        return {}
    return {int(k): v for k, v in json.loads(res.stdout).items()}


def _pause_pid(hint):
    # The rootless pause process sets its comm to "podman pause"; its cmdline is "podman".
    try:
        pid = int(hint)
        if "pause" in _read(f"/proc/{pid}/comm"):
            return pid
    except (OSError, ValueError):
        pass
    return None


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
    pause = _pause_pid(args.pause_pid)
    members.discard(pause)
    rows = []
    for pid, role in [(conmon, "conmon")] + [(p, "container") for p in sorted(members)] + \
            ([(pause, "pause")] if pause else []):
        row = _row(pid, role)
        if row:
            rows.append(row)
    denied = []
    for row in rows:
        row["pss_kib"] = _pss_direct(row["pid"])
        if row["pss_kib"] is None:
            denied.append(row["pid"])
    if denied and args.podman_root:
        via = _pss_via_unshare(denied, args.podman_root, args.podman_runroot, args.podman_tmpdir)
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
        "pause_pid": pause or 0,
        "procs": rows,
    }
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=1)
    fields = ["conmon_rss_kib", "realm_rss_kib", "pause_rss_kib", "total_rss_kib",
              "realm_pss_kib", "pause_pss_kib", "total_pss_kib", "nproc_container",
              "swap_kib", "pss_missing"]
    print("\t".join(str(out[f]) for f in fields))


def summarize(args):
    rows = [line.rstrip("\n").split("\t") for line in open(args.summarize)]
    header, data = rows[0], rows[1:]
    idx = {name: i for i, name in enumerate(header)}
    configs = []
    for r in data:
        if r[idx["cfg"]] not in configs:
            configs.append(r[idx["cfg"]])
    print("cfg\truns\tcol\tmedian\tmin\tmax")
    for cfg in configs:
        sel = [r for r in data if r[idx["cfg"]] == cfg and r[idx["status"]] == "ok"]
        for col in ("realm_rss_kib", "total_rss_kib", "total_pss_kib", "realm_pss_kib",
                    "pause_rss_kib", "swap_kib", "nproc_container"):
            vals = [int(r[idx[col]]) for r in sel if r[idx[col]].lstrip("-").isdigit()]
            if not vals:
                continue
            print(f"{cfg}\t{len(vals)}\t{col}\t{statistics.median(vals):.1f}\t{min(vals)}\t{max(vals)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--conmon", type=int)
    ap.add_argument("--init", type=int)
    ap.add_argument("--pause-pid", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--podman-root", default="")
    ap.add_argument("--podman-runroot", default="")
    ap.add_argument("--podman-tmpdir", default="")
    ap.add_argument("--pss-only", nargs="*", type=int)
    ap.add_argument("--summarize", default="")
    args = ap.parse_args()
    if args.pss_only is not None:
        print(json.dumps({str(p): _pss_direct(p) for p in args.pss_only}))
    elif args.summarize:
        summarize(args)
    else:
        realm(args)


if __name__ == "__main__":
    main()
