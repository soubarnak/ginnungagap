#!/home/soubarna/Documents/bongbetic-wip/ginnungagap/.venv/bin/python
# SPDX-License-Identifier: Apache-2.0
"""F6 root cause on arch, and the F1 image probe (spikes/results/F6-F1-probe.md, 2026-10-14).

Run from the repo root with the venv python. Every podman call passes --root, --runroot and
--tmpdir, and XDG_RUNTIME_DIR is a directory this user owns: the product's own dirs for the
product realm, and the spike dirs below for everything else. Nothing touches
~/.local/share/containers or /run/user/<uid>/libpod.

Services-on boots and in-realm commands run from runit services in the supervisor leaf (the
gate's leaf_exec route, F2). The services live in the user's sv directory with prefixes `f6v-`
(boots, image boots included) and `f6x-` (exec jobs), so they never touch the product's own
entries. The runner is f6f1_runner.py.

Subcommands:
  product-diag            in-realm diagnostics of the product realm f6-arch (Part A)
  product-measure N       N idle runs of f6-arch, stop and start between runs (Part A control)
  variant KEY [N]         hand-built services-on boot, key v0..v4 (Part A); N=0 is diag only
  image-check REF         pull REF into the spike store and check /sbin/init (Part B)
  image-boot REF TAG      services-on boot of REF with the v0 argv, then diagnostics (Part B)
  cleanup                 remove spike containers, images, storage, sv entries and leaves
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parents[1]
OUT = SPIKE / "out"
NATIVE_WAIT = str(REPO / "native" / "build" / "ginn-wait")
LAUNCH = str(SPIKE / "ginn-launch")
PY = str(REPO / ".venv" / "bin" / "python")
SV_TOOL = "/usr/sbin/sv"
SKOPEO = "/usr/sbin/skopeo"
PODMAN = "/usr/bin/podman"
UID = os.getuid()
CGROUP_ROOT = Path("/sys/fs/cgroup/ginn") / str(UID)
HOME = Path.home()

sys.path.insert(0, str(REPO / "src"))
from ginn.podman.cli import PodmanCli  # noqa: E402 - the product adapter, read only
from ginn.runtime.spec import (  # noqa: E402 - the product's own constants, read only
    JOURNALD_DROPIN_TEXT,
    SERVICES_MASKED_UNITS,
)
from ginn.store.paths import OwnedPaths  # noqa: E402

SPIKE_ROOT = HOME / ".cache" / "ginn-f6f1" / "root"
SPIKE_RUNROOT = Path(f"/run/user/{UID}/f6f1/rr")
SPIKE_XRD = Path(f"/run/user/{UID}/f6f1/xdg")
SPIKE_TMP = SPIKE_XRD / "podman-tmp"
SPIKE_PAUSE = SPIKE_XRD / "libpod" / "tmp" / "pause.pid"
PASSWD = "soubarna:*:1000:1000:soubarna:/home/soubarna:/bin/bash"
EXTRA_MASKS: tuple[str, ...] = (
    "systemd-firstboot.service",
    "systemd-homed.service",
    "systemd-resolved.service",
    "systemd-resolved-monitor.socket",
    "systemd-resolved-varlink.socket",
    "systemd-nsresourced.service",
    "systemd-nsresourced.socket",
    "systemd-userdbd.service",
    "systemd-userdbd.socket",
)
LOGIND_MASK: tuple[str, ...] = ("systemd-logind.service",)
COMMIT_MASK: tuple[str, ...] = ("systemd-machine-id-commit.service",)
VARIANTS: dict[str, dict[str, Any]] = {
    # mid: none, bind-ro, bind-rw, or cp (podman cp of the realm's machine-id before the first
    # start). extra: the firstboot, homed, resolved, nsresourced and userdbd masks. logind and
    # commit: one unit each. ADR-007 a, b and d are in every key.
    "v0": {"mid": "none", "extra": False, "logind": False, "commit": False},
    "v1": {"mid": "bind-ro", "extra": False, "logind": False, "commit": False},
    "v1rw": {"mid": "bind-rw", "extra": False, "logind": False, "commit": False},
    "v1cp": {"mid": "cp", "extra": False, "logind": False, "commit": False},
    "v1mask": {"mid": "bind-ro", "extra": False, "logind": False, "commit": True},
    "v2": {"mid": "none", "extra": True, "logind": False, "commit": False},
    "v3": {"mid": "bind-ro", "extra": True, "logind": False, "commit": False},
    "v3cp": {"mid": "cp", "extra": True, "logind": False, "commit": False},
    "v4cp": {"mid": "cp", "extra": True, "logind": True, "commit": False},
}
DIAG: list[tuple[str, str]] = [
    ("failed", "systemctl --failed --no-pager"),
    ("is-system-running", "systemctl is-system-running; true"),
    (
        "machine-id",
        "cat /etc/machine-id; echo; ls -l /etc/machine-id; grep machine-id /proc/1/mountinfo",
    ),
    (
        "journal",
        "journalctl -b --no-pager -u dbus-broker -u dbus.socket -u systemd-firstboot"
        " -u systemd-resolved -u systemd-homed -u systemd-resolved-monitor.socket"
        " -u systemd-resolved-varlink.socket",
    ),
    (
        "status",
        "systemctl status dbus-broker.service systemd-firstboot.service systemd-homed.service"
        " systemd-resolved.service --no-pager -l",
    ),
    ("running-services", "systemctl list-units --type=service --state=running --no-pager"),
    ("ps", "ps -eo pid,rss,comm"),
    ("busctl", "busctl --no-pager list; echo busctl-rc=$?"),
    ("packages", "pacman -Q systemd dbus-broker 2>&1; true"),
    (
        "commit",
        "systemctl status systemd-machine-id-commit.service --no-pager -l; "
        "journalctl -b --no-pager -u systemd-machine-id-commit.service",
    ),
    (
        "probe-audit",
        "timeout 8 dbus-broker-launch --scope system --audit 2>&1 | tail -4; echo rc=$?",
    ),
    ("probe-noaudit", "timeout 8 dbus-broker-launch --scope system 2>&1 | tail -4; echo rc=$?"),
]
SETTLE = "systemctl is-system-running --wait; systemctl is-system-running; true"
IDLE_S = 60


@dataclass(frozen=True)
class Store:
    """A podman call prefix: the global flags (binary first) and the XDG_RUNTIME_DIR it runs with."""

    global_argv: list[str]
    xrd: Path

    def env(self) -> dict[str, str]:
        return {
            "PATH": "/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin",
            "HOME": str(HOME),
            "XDG_RUNTIME_DIR": str(self.xrd),
            "LANG": "C.UTF-8",
        }

    def run(self, args: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*self.global_argv, *args],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


def spike_store() -> Store:
    for d in (SPIKE_ROOT, SPIKE_RUNROOT, SPIKE_XRD, SPIKE_TMP):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    argv = [
        PODMAN,
        "--remote=false",
        "--root",
        str(SPIKE_ROOT),
        "--runroot",
        str(SPIKE_RUNROOT),
        "--tmpdir",
        str(SPIKE_TMP),
        "--runtime",
        "crun",
    ]
    return Store(argv, SPIKE_XRD)


def product_paths() -> OwnedPaths:
    return OwnedPaths.from_env()


def product_store(paths: OwnedPaths) -> Store:
    return Store(
        PodmanCli.from_paths(paths, binary=PODMAN, oci_runtime="crun").global_argv(), paths.runtime
    )


def sv_dir(paths: OwnedPaths | None = None) -> Path:
    return (paths or product_paths()).sv


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def say(tag: str, data: Any) -> None:
    print(tag + " " + json.dumps(data, sort_keys=True), flush=True)


# ---------------------------------------------------------------- leaf services (F2 route)

RUN_SCRIPT = """#!{py} -I
import sys
sys.path.insert(0, {spike!r})
from f6f1_runner import run
run({job!r})
"""


def submit(
    paths: OwnedPaths,
    name: str,
    out: Path,
    env: dict[str, str],
    steps: list[dict[str, Any]],
    tail: list[str] | None,
) -> None:
    """Make a runit service `name` whose run script runs the job, in the user's sv directory."""
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    job_path = out / "job.json"
    write_json(job_path, {"out": str(out), "env": env, "steps": steps, "tail": tail})
    sv = sv_dir(paths)
    tmp = sv / f".{name}.tmp"
    tmp.mkdir(mode=0o700)
    run_path = tmp / "run"
    run_path.write_text(
        RUN_SCRIPT.format(py=PY, spike=str(SPIKE), job=str(job_path)), encoding="utf-8"
    )
    run_path.chmod(0o755)
    os.rename(tmp, sv / name)


def wait_done(out: Path, timeout: float) -> str | None:
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        done = out / "done"
        if done.exists():
            return done.read_text(encoding="utf-8").strip()
        time.sleep(0.5)
    return None


def step_results(out: Path, steps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for step in steps:
        name = step["name"]
        rc_file = out / f"{name}.rc"
        rc = int(rc_file.read_text(encoding="utf-8").strip()) if rc_file.exists() else None
        out_file = out / f"{name}.out"
        text = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
        results[name] = {"rc": rc, "out": text}
    return results


def retire(paths: OwnedPaths, name: str) -> None:
    """Take a service down, wait for its runsv to exit, then remove its directory (gate's _retire)."""
    svc = sv_dir(paths) / name
    for verb in ("down", "exit"):
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [SV_TOOL, verb, str(svc)], capture_output=True, text=True, timeout=120, check=False
        )
    began = time.monotonic()
    while time.monotonic() - began < 90:
        gone = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [SV_TOOL, "status", str(svc)], capture_output=True, text=True, timeout=30, check=False
        )
        text = gone.stdout + gone.stderr
        if "not running" in text or "unable to" in text:
            break
        time.sleep(0.5)
    shutil.rmtree(svc, ignore_errors=True)


def leaf_job(
    paths: OwnedPaths,
    prefix: str,
    out_root: Path,
    env: dict[str, str],
    steps: list[dict[str, Any]],
    timeout: float = 900.0,
) -> dict[str, dict[str, Any]]:
    """Run steps as an exec job in the leaf and wait for them. The service is retired after."""
    name = f"{prefix}-{secrets.token_hex(3)}"
    out = out_root / name
    submit(paths, name, out, env, steps, None)
    try:
        state = wait_done(out, timeout)
        if state is None:
            raise TimeoutError(f"leaf job {name} did not finish in {timeout:.0f} s")
        return step_results(out, steps)
    finally:
        retire(paths, name)


def exec_steps(
    store: Store, cid: str, cmds: list[tuple[str, str]], timeout: float = 600.0
) -> list[dict[str, Any]]:
    return [
        {"name": n, "argv": [*store.global_argv, "exec", cid, "sh", "-c", c], "timeout": timeout}
        for n, c in cmds
    ]


def exec_in_realm(
    paths: OwnedPaths,
    store: Store,
    cid: str,
    cmds: list[tuple[str, str]],
    out: Path,
    timeout: float = 900.0,
) -> dict[str, dict[str, Any]]:
    return leaf_job(paths, "f6x", out, store.env(), exec_steps(store, cid, cmds, 600.0), timeout)


# ---------------------------------------------------------------- measurement (gate's method)


def _proc_table() -> dict[int, dict[str, Any]]:
    table: dict[int, dict[str, Any]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            ppid = int((entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[1])
            argv = [a for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
        except (OSError, ValueError, IndexError):
            continue
        table[int(entry.name)] = {"comm": comm, "ppid": ppid, "argv": argv}
    return table


def _rss(pid: int) -> dict[str, int]:
    values = {"VmRSS": 0, "RssAnon": 0, "RssFile": 0}
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        key = line.split(":", 1)[0]
        if key in values:
            values[key] = int(line.split()[1])
    return values


def _pss(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text(encoding="utf-8").splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def _pid_file(path: Path) -> list[int]:
    try:
        return [int(path.read_text(encoding="utf-8").strip())]
    except (OSError, ValueError):
        return []


def _cgroup_pids(d: Path) -> list[str]:
    """Every pid in the cgroup tree, with its comm name (for the process count)."""
    pids: list[int] = []
    if d.is_dir():
        for root, _dirs, _files in os.walk(d):
            procs = Path(root) / "cgroup.procs"
            if procs.exists():
                pids += [int(x) for x in procs.read_text(encoding="utf-8").split()]
    return [f"{p}:{_comm(p)}" for p in sorted(set(pids))]


def _comm(pid: int) -> str:
    try:
        return (Path(f"/proc/{pid}/comm")).read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def host_memory() -> dict[str, Any]:
    avail = next(
        int(line.split()[1])
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        if line.startswith("MemAvailable:")
    )
    psi = Path("/proc/pressure/memory").read_text(encoding="utf-8").splitlines()[0]
    return {"MemAvailable_kib": avail, "psi": psi}


def sample(cgroup_dir: Path, svc: str, conmon_pidfile: Path, pause_pidfile: Path) -> dict[str, Any]:
    """The NFR-003 attribution of one realm: cgroup tree, conmon, pause (once), runsv and ginn-wait.

    Same pid set as the T7.7 gate's realm_rss. VmRSS is the budget figure, Pss is recorded beside.
    """
    table = _proc_table()
    runsv = [
        p
        for p, e in table.items()
        if e["comm"] == "runsv" and len(e["argv"]) > 1 and Path(e["argv"][1].decode()).name == svc
    ]
    waits = [p for p, e in table.items() if e["comm"] == "ginn-wait" and e["ppid"] in runsv]
    groups = {
        "cgroup": [int(x.split(":")[0]) for x in _cgroup_pids(cgroup_dir)],
        "conmon": _pid_file(conmon_pidfile),
        "pause": _pid_file(pause_pidfile),
        "runsv": runsv,
        "ginn-wait": waits,
    }
    pids = sorted({p for group in groups.values() for p in group})
    totals = {"VmRSS": 0, "RssAnon": 0, "RssFile": 0, "Pss": 0}
    pss_unreadable = 0
    for pid in pids:
        try:
            st = _rss(pid)
        except (OSError, ValueError):
            continue
        for key in ("VmRSS", "RssAnon", "RssFile"):
            totals[key] += st[key]
        pss = _pss(pid)
        if pss is None:
            pss_unreadable += 1
        else:
            totals["Pss"] += pss
    return {
        "kib": totals,
        "vmrss_mib": round(totals["VmRSS"] / 1024, 2),
        "pss_mib_readable": round(totals["Pss"] / 1024, 2),
        "pss_unreadable": pss_unreadable,
        "container_procs": len(groups["cgroup"]),
        "container_proc_names": _cgroup_pids(cgroup_dir),
        "helpers": {k: v for k, v in groups.items() if k != "cgroup"},
    }


def rm_empty_leaf(name: str) -> bool:
    leaf = CGROUP_ROOT / name
    if not leaf.is_dir():
        return True
    if (leaf / "cgroup.procs").read_text(encoding="utf-8").split():
        return False
    try:
        leaf.rmdir()
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- Part A: product realm


def product_diag(name: str = "f6-arch") -> None:
    paths = product_paths()
    store = product_store(paths)
    cid = store.run(
        ["ps", "-a", "--filter", f"label=io.ginn.realm={name}", "--format", "{{.ID}}"]
    ).stdout.strip()
    out = OUT / "product-diag"
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    insp = store.run(["inspect", cid])
    (out / "inspect.json").write_text(insp.stdout, encoding="utf-8")
    res = exec_in_realm(paths, store, cid, DIAG, out)
    for key, value in res.items():
        (out / f"{key}.txt").write_text(value["out"], encoding="utf-8")
        say("DIAG", {"step": key, "rc": value["rc"], "lines": value["out"].count("\n")})


def product_exec(cmds: list[str], name: str = "f6-arch") -> None:
    """Ad-hoc read-only commands in the product realm, each as `sh -c` in its own step (F6 probes)."""
    paths = product_paths()
    store = product_store(paths)
    cid = store.run(
        ["ps", "-a", "--filter", f"label=io.ginn.realm={name}", "--format", "{{.ID}}"]
    ).stdout.strip()
    out = OUT / "product-exec"
    res = exec_in_realm(paths, store, cid, [(f"c{i}", c) for i, c in enumerate(cmds)], out)
    for key, value in res.items():
        print(f"== {key} rc={value['rc']}")
        print(value["out"], end="")


def product_measure(n: int, name: str = "f6-arch") -> None:
    paths = product_paths()
    store = product_store(paths)
    out_root = OUT / "product-measure"
    rows: list[dict[str, Any]] = []
    for i in range(1, n + 1):
        cid = store.run(
            ["ps", "-a", "--filter", f"label=io.ginn.realm={name}", "--format", "{{.ID}}"]
        ).stdout.strip()
        settle = exec_in_realm(paths, store, cid, [("settle", SETTLE)], out_root / f"run{i}-settle")
        time.sleep(IDLE_S)
        host = host_memory()
        s = sample(
            CGROUP_ROOT / name,
            name,
            paths.runtime / "services" / f"{name}.conmon.pid",
            paths.runtime / "libpod" / "tmp" / "pause.pid",
        )
        row = {
            "run": i,
            "settle": settle["settle"]["out"].strip().splitlines()[:3],
            "host": host,
            **s,
        }
        rows.append(row)
        say(
            "MEASURE",
            {
                "variant": "product",
                "run": i,
                "vmrss_mib": s["vmrss_mib"],
                "pss_mib": s["pss_mib_readable"],
                "unread": s["pss_unreadable"],
                "procs": s["container_procs"],
            },
        )
        write_json(out_root / f"run{i}.json", row)
        if i < n:
            subprocess.run(
                [LAUNCH, "stop", name], capture_output=True, text=True, timeout=600, check=False
            )  # noqa: S603
            subprocess.run(
                [LAUNCH, "start", name], capture_output=True, text=True, timeout=600, check=False
            )  # noqa: S603
    write_json(out_root / "summary.json", rows)


# ---------------------------------------------------------------- Part A: hand variants


def boot_args(
    name: str, pidfile: Path, image: str, mounts: list[tuple[str, str, bool]]
) -> list[str]:
    args = [
        "create",
        "--name",
        name,
        "--systemd=always",
        "--cgroupns",
        "private",
        "--cgroup-parent",
        f"/ginn/{UID}/{name}",
        "--user",
        "0:0",
        "--userns",
        "keep-id",
        "--passwd-entry",
        PASSWD,
        "--network",
        "host",
        "--http-proxy=false",
        "--stop-signal",
        "SIGRTMIN+3",
        "--stop-timeout",
        "30",
        "--env",
        "SYSTEMD_LOG_LEVEL=warning",
        "--conmon-pidfile",
        str(pidfile),
        "--label",
        "io.ginn.spike=f6f1",
        "--tmpfs",
        f"/run/user/{UID}:mode=0700,U",
    ]
    for src, dst, ro in mounts:
        args += ["-v", f"{src}:{dst}" + (":ro" if ro else "")]
    return [*args, image, "/sbin/init"]


def variant_setup(
    key: str, out: Path
) -> tuple[list[tuple[str, str, bool]], tuple[str, str] | None]:
    """The create-time binds of a variant (src, dst, read-only), and the file that podman cp puts
    into the created container before its first start (src, dst), or None."""
    spec = VARIANTS[key]
    drop = out / "journald-ginn.conf"
    drop.write_text(JOURNALD_DROPIN_TEXT, encoding="utf-8")
    units = list(SERVICES_MASKED_UNITS)
    if spec["extra"]:
        units += list(EXTRA_MASKS)
    if spec["logind"]:
        units += list(LOGIND_MASK)
    if spec["commit"]:
        units += list(COMMIT_MASK)
    mounts: list[tuple[str, str, bool]] = [
        (str(drop), "/etc/systemd/journald.conf.d/ginn.conf", True)
    ]
    mounts += [("/dev/null", f"/etc/systemd/system/{u}", True) for u in units]
    copy_in: tuple[str, str] | None = None
    mid = str(spec["mid"])
    if mid != "none":
        mid_file = out / "machine-id"
        mid_file.write_text(secrets.token_hex(16) + "\n", encoding="utf-8")
        if mid == "cp":
            copy_in = (str(mid_file), "/etc/machine-id")
        else:
            mounts.append((str(mid_file), "/etc/machine-id", mid == "bind-ro"))
    return mounts, copy_in


def ginn_wait_tail(store: Store, pidfile: Path, name: str) -> list[str]:
    return [NATIVE_WAIT, str(pidfile), *store.global_argv, "--", name]


def boot(
    paths: OwnedPaths,
    store: Store,
    tag: str,
    image: str,
    mounts: list[tuple[str, str, bool]],
    out: Path,
    copy_in: tuple[str, str] | None = None,
) -> tuple[str, str | None]:
    """Create (and cp in, when set) and start one services-on container under its own leaf service.

    Returns (name, state): state is ok when every required step passed, failed, or None on timeout.
    """
    name = f"f6v-{tag}"
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    pidfile = out / "conmon.pid"
    steps: list[dict[str, Any]] = [
        {
            "name": "create",
            "argv": [*store.global_argv, *boot_args(name, pidfile, image, mounts)],
            "required": True,
            "timeout": 300,
        },
    ]
    if copy_in is not None:
        steps.append(
            {
                "name": "cp",
                "argv": [*store.global_argv, "cp", copy_in[0], f"{name}:{copy_in[1]}"],
                "required": True,
                "timeout": 300,
            }
        )
    steps.append(
        {
            "name": "start",
            "argv": [*store.global_argv, "start", name],
            "required": True,
            "timeout": 300,
        }
    )
    submit(paths, name, out, store.env(), steps, ginn_wait_tail(store, pidfile, name))
    state = wait_done(out, 600)
    return name, state


def cleanup_boot(paths: OwnedPaths, store: Store, name: str) -> None:
    retire(paths, name)
    store.run(["rm", "-f", name], timeout=300)
    rm_empty_leaf(name)


def variant_run(key: str, runs: int) -> None:
    paths = product_paths()
    store = spike_store()
    ensure_image(store, IMAGE_ARCH)
    base = OUT / (f"variant-{key}" if runs == 0 else f"measure-{key}")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for i in range(1, max(runs, 1) + 1):
        tag = f"{key}-r{i}-{secrets.token_hex(2)}"
        out = base / f"run{i}-{tag}"
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        mounts, copy_in = variant_setup(key, out)
        name, state = boot(paths, store, tag, IMAGE_ARCH, mounts, out, copy_in)
        row: dict[str, Any] = {"variant": key, "run": i, "name": name, "boot_state": state}
        try:
            if state != "ok":
                row["error"] = "a required step failed (see create, cp and start)"
                row["create_rc"] = step_rc(out, "create")
                row["cp_rc"] = step_rc(out, "cp")
                row["start_rc"] = step_rc(out, "start")
                rows.append(row)
                say("BOOT", row)
                continue
            settle = exec_in_realm(paths, store, name, [("settle", SETTLE)], out / "settle")
            row["settle"] = settle["settle"]["out"].strip().splitlines()[-4:]
            if i == 1:
                exec_in_realm(paths, store, name, DIAG, out / "diag")
                ins = store.run(["inspect", name])
                (out / "inspect.json").write_text(ins.stdout, encoding="utf-8")
            if runs > 0:
                time.sleep(IDLE_S)
                host = host_memory()
                s = sample(CGROUP_ROOT / name, name, out / "conmon.pid", SPIKE_PAUSE)
                row.update({"host": host, **s})
                say(
                    "MEASURE",
                    {
                        "variant": key,
                        "run": i,
                        "vmrss_mib": s["vmrss_mib"],
                        "pss_mib": s["pss_mib_readable"],
                        "unread": s["pss_unreadable"],
                        "procs": s["container_procs"],
                    },
                )
            rows.append(row)
            write_json(out / "row.json", row)
        finally:
            cleanup_boot(paths, store, name)
    write_json(base / f"summary-{runs}.json", rows)


def step_rc(out: Path, name: str) -> int | None:
    rc = out / f"{name}.rc"
    return int(rc.read_text(encoding="utf-8").strip()) if rc.exists() else None


# ---------------------------------------------------------------- Part B: images


IMAGE_ARCH = "quay.io/archlinux/archlinux@sha256:c556b483a8e653aa6389bb66263e1d96cd6e17c67ca5968c87495d313c4a2e93"


def strip_tag(ref: str) -> str:
    last = ref.rsplit("/", 1)[-1]
    if "@" in ref:
        return ref.split("@", 1)[0]
    if ":" in last:
        return ref.rsplit(":", 1)[0]
    return ref


def _raw_manifest(ref: str) -> tuple[bytes, dict[str, Any]]:
    """The raw registry document (skopeo --raw) and its parsed form. Bytes, so its sha256 is exact."""
    raw = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [SKOPEO, "inspect", "--raw", f"docker://{ref}"],
        capture_output=True,
        check=False,
        timeout=300,
    )
    return raw.stdout, json.loads(raw.stdout)


def registry_sizes(ref: str) -> dict[str, Any]:
    """linux/amd64 platform manifest digest and its compressed size, read from the registry."""
    raw, doc = _raw_manifest(ref)
    result: dict[str, Any] = {"document_sha256": hashlib.sha256(raw).hexdigest(), "index": False}
    if "manifests" in doc:
        result["index"] = True
        picks = [
            m
            for m in doc["manifests"]
            if m.get("platform", {}).get("os") == "linux"
            and m.get("platform", {}).get("architecture") == "amd64"
        ]
        if len(picks) != 1:
            return {**result, "error": f"expected one linux/amd64 entry, found {len(picks)}"}
        platform = picks[0]["digest"]
        man_raw = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [SKOPEO, "inspect", "--raw", f"docker://{strip_tag(ref)}@{platform}"],
            capture_output=True,
            check=False,
            timeout=300,
        ).stdout
        doc = json.loads(man_raw)
        result["platform_digest"] = platform
        result["platform_digest_matches_document"] = (
            hashlib.sha256(man_raw).hexdigest() == platform.split(":", 1)[1]
        )
    else:
        result["platform_digest"] = "sha256:" + result["document_sha256"]
    layers = doc.get("layers", [])
    result["layers"] = len(layers)
    result["compressed_bytes"] = sum(int(layer["size"]) for layer in layers)
    return result


def ensure_image(store: Store, ref: str) -> None:
    """Pull `ref` into the spike store for linux/amd64 unless it is already there."""
    if store.run(["image", "exists", ref]).returncode != 0:
        store.run(["pull", "--platform", "linux/amd64", ref], timeout=1800)


def image_check(ref: str) -> dict[str, Any]:
    store = spike_store()
    pull = store.run(["pull", "--platform", "linux/amd64", ref], timeout=1800)
    dig = store.run(["image", "inspect", "--format", "{{.Digest}}", ref]).stdout.strip()
    size = store.run(["image", "inspect", "--format", "{{.Size}}", ref]).stdout.strip()
    probe = store.run(
        [
            "run",
            "--rm",
            "--cgroups=disabled",
            "--network",
            "none",
            "--platform",
            "linux/amd64",
            ref,
            "sh",
            "-c",
            "for p in /sbin/init /usr/lib/systemd/systemd; do"
            ' if [ -e $p ] || [ -L $p ]; then echo "$p: present -> $(readlink -f $p)";'
            ' else echo "$p: absent"; fi; done; ls -l /sbin/init 2>&1;'
            ' . /etc/os-release; echo "$PRETTY_NAME"',
        ],
        timeout=600,
    )
    reg = registry_sizes(ref)
    row = {
        "ref": ref,
        "pull_rc": pull.returncode,
        "podman_digest": dig,
        "uncompressed_bytes_podman": int(size) if size.isdigit() else size,
        "registry": reg,
        "init_probe": probe.stdout.strip(),
        "init_probe_rc": probe.returncode,
        "init_probe_err": probe.stderr.strip()[-300:],
    }
    row["has_sbin_init"] = "/sbin/init: present" in probe.stdout
    return row


def image_boot(ref: str, tag: str) -> dict[str, Any]:
    paths = product_paths()
    store = spike_store()
    ensure_image(store, ref)
    out = OUT / "image-boot" / tag
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    mounts, copy_in = variant_setup("v1cp", out)
    name, state = boot(paths, store, f"img-{tag}-{secrets.token_hex(2)}", ref, mounts, out, copy_in)
    row: dict[str, Any] = {"ref": ref, "boot_state": state, "name": name}
    try:
        if state != "ok":
            row["create_rc"] = step_rc(out, "create")
            row["start_rc"] = step_rc(out, "start")
            row["start_out_tail"] = (
                (out / "start.out").read_text(encoding="utf-8", errors="replace")[-400:]
                if (out / "start.out").exists()
                else ""
            )
            row["create_out_tail"] = (
                (out / "create.out").read_text(encoding="utf-8", errors="replace")[-400:]
                if (out / "create.out").exists()
                else ""
            )
            return row
        settle = exec_in_realm(paths, store, name, [("settle", SETTLE)], out / "settle")
        row["settle"] = settle["settle"]["out"].strip().splitlines()[-4:]
        exec_in_realm(paths, store, name, DIAG, out / "diag")
        row["failed_units"] = (
            (out / "diag" / "failed.out").read_text(encoding="utf-8", errors="replace")
            if (out / "diag" / "failed.out").exists()
            else ""
        )
    finally:
        cleanup_boot(paths, store, name)
    return row


# ---------------------------------------------------------------- cleanup


def cleanup() -> None:
    """Remove spike containers, images, storage, sv entries, leaves and the spike pause."""
    paths = product_paths()
    store = spike_store()
    names = store.run(
        ["ps", "-a", "--filter", "label=io.ginn.spike=f6f1", "--format", "{{.Names}}"]
    ).stdout.split()
    for n in names:
        retire(paths, n)
        store.run(["rm", "-f", n], timeout=300)
        rm_empty_leaf(n)
    for svc in sorted(sv_dir(paths).glob("f6v-*")) + sorted(sv_dir(paths).glob("f6x-*")):
        retire(paths, svc.name)
    for tmp in sorted(sv_dir(paths).glob(".f6*.tmp")):
        shutil.rmtree(tmp, ignore_errors=True)
    store.run(["rmi", "-af", "--force"], timeout=600)
    store.run(
        ["unshare", "rm", "-rf", str(SPIKE_ROOT), str(SPIKE_RUNROOT), str(SPIKE_TMP)], timeout=600
    )
    pause = _pid_file(SPIKE_PAUSE)
    for pid in pause:
        if _comm(pid) == "catatonit" and os.stat(f"/proc/{pid}").st_uid == UID:
            os.kill(pid, signal.SIGTERM)
    for d in sorted(CGROUP_ROOT.glob("f6v-*")):
        rm_empty_leaf(d.name)
    shutil.rmtree(SPIKE_XRD.parent, ignore_errors=True)
    shutil.rmtree(HOME / ".cache" / "ginn-f6f1", ignore_errors=True)
    say("CLEANUP", {"removed_containers": names})


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="f6f1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("product-diag")
    p = sub.add_parser("product-exec")
    p.add_argument("cmds", nargs="+")
    p = sub.add_parser("product-measure")
    p.add_argument("n", type=int)
    p = sub.add_parser("variant")
    p.add_argument("key", choices=sorted(VARIANTS))
    p.add_argument("runs", type=int, nargs="?", default=0)
    p = sub.add_parser("image-check")
    p.add_argument("ref")
    p = sub.add_parser("image-boot")
    p.add_argument("ref")
    p.add_argument("tag")
    sub.add_parser("cleanup")
    a = ap.parse_args(argv)
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if a.cmd == "product-diag":
        product_diag()
    elif a.cmd == "product-exec":
        product_exec(a.cmds)
    elif a.cmd == "product-measure":
        product_measure(a.n)
    elif a.cmd == "variant":
        variant_run(a.key, a.runs)
    elif a.cmd == "image-check":
        say("IMAGE", image_check(a.ref))
    elif a.cmd == "image-boot":
        say("BOOT_IMAGE", image_boot(a.ref, a.tag))
    elif a.cmd == "cleanup":
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
