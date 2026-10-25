#!/home/soubarna/Documents/bongbetic-wip/ginnungagap/.venv/bin/python
# SPDX-License-Identifier: Apache-2.0
"""S-11 host spike for ADR-015: the init set at services-on create, through a provisioning stage.

Run from the repo root with the venv python, one distribution at a time (the host is shared):
  s11.py probe                  podman version and info, store paths, and the user's storage mtime
  s11.py distro KEY [--nokeep]  pull, provision, commit, boot x3, services-off check, rmi check
  s11.py cleanup                remove spike containers, images, storage, leaves, sv entries and pause
KEY is one of debian, fedora, kali, arch. The result JSON goes to spikes/s11/out/KEY/. Storage,
logs and job files outside out/ stay in the session scratchpad (WORK below) and are removed by
cleanup.

Every podman call goes through podman_argv(), which passes --remote=false, --root, --runroot,
--tmpdir and --runtime crun, and podman_env(), which sets XDG_RUNTIME_DIR under /run/user/<uid>/s11
and redirects HOME and the XDG data, config and cache dirs into WORK (so the containers/image
blob cache never reaches the user's ~/.local/share/containers). The services-on steps (create, cp,
start and every exec into a services-on container) run as runit jobs in the supervisor leaf (F2),
through the user's sv directory, as spikes/f6f1 did.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parents[1]
OUT = SPIKE / "out"
RUNNER = SPIKE / "s11_runner.py"
NATIVE_WAIT = str(REPO / "native" / "build" / "ginn-wait")
TRUST = REPO / "src" / "ginn" / "trust" / "data" / "trust.toml"
PODMAN = "/usr/bin/podman"
DU = "/usr/bin/du"
SV_TOOL = "/usr/sbin/sv"
PY = str(REPO / ".venv" / "bin" / "python")
UID = os.getuid()
GID = os.getgid()
USER_HOME = Path.home()
WORK = Path(
    "/home/soubarna/.cache/tmp/claude-1000/-home-soubarna-Documents-bongbetic-wip-ginnungagap"
    "/341badce-de09-4807-b1e0-7a42b356460c/scratchpad/s11"
)
"""Session scratchpad: podman storage, tmpdir, HOME and XDG dirs of the spike, logs."""
ROOT = WORK / "root"
TMPDIR = WORK / "tmp"
HOME = WORK / "home"
XDG_CFG = WORK / "xdg" / "config"
XDG_DATA = WORK / "xdg" / "data"
XDG_CACHE = WORK / "xdg" / "cache"
LOGS = WORK / "logs"
RUN = Path(f"/run/user/{UID}/s11")
"""The spike's runtime dir. Short, so unix socket paths stay under the 108-byte limit."""
RUNROOT = RUN / "rr"
XRD = RUN / "xdg"
PAUSE = XRD / "libpod" / "tmp" / "pause.pid"
CGROUP_ROOT = Path("/sys/fs/cgroup/ginn") / str(UID)
USER_STORE = USER_HOME / ".local" / "share" / "containers"
"""Read only as metadata (mtime): the spike must never change it."""

sys.path.insert(0, str(REPO / "src"))
from ginn.distros import loader  # noqa: E402 - read-only use of the product's distro data
from ginn.runtime.spec import (  # noqa: E402 - the product's own constants, read only
    JOURNALD_DROPIN_TEXT,
    SERVICES_MASKED_UNITS,
    realm_passwd_entry,
)
from ginn.store.paths import OwnedPaths  # noqa: E402 - sv directory of the user's supervisor

JOURNALD_TARGET = "/etc/systemd/journald.conf.d/ginn.conf"
PASSWD = realm_passwd_entry(USER_HOME.name, UID, GID, USER_HOME, "/bin/bash")
IDLE_S = 60
BOOT_RUNS = 3

INIT_APT = ("systemd", "systemd-sysv", "dbus")
CLEAN_APT = (
    ("apt-get", "clean"),
    ("rm", "-rf", "/var/lib/apt/lists"),
    ("mkdir", "-p", "/var/lib/apt/lists/partial"),
)
DISTROS: dict[str, dict[str, Any]] = {
    "debian": {"init": INIT_APT, "clean": CLEAN_APT, "family": "apt"},
    "kali": {"init": INIT_APT, "clean": CLEAN_APT, "family": "apt"},
    "fedora": {
        "init": ("systemd", "dbus-broker"),
        "clean": (("dnf", "clean", "all"),),
        "family": "rpm",
    },
    "arch": {
        "init": (),
        "clean": (("pacman", "--sync", "--clean", "--clean", "--noconfirm"),),
        "family": "pacman",
    },
}
"""The init set and the clean steps of ADR-015 Decision 3, as the ADR states them. They are not in
the distro data yet (T7.17), so this spike carries them."""

LOG_TAIL = 40
"""Lines of each step's output kept in the result JSON. The full output goes to LOGS."""


def say(tag: str, data: Any) -> None:
    print(tag + " " + json.dumps(data, sort_keys=True, default=str), flush=True)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def pinned(key: str) -> tuple[str, str]:
    """The pinned reference `repo@digest` and the digest of a distribution, from the Trust Manifest."""
    raw = tomllib.loads(TRUST.read_text(encoding="utf-8"))["distro"][key]
    image = str(raw["image"])
    last = image.rsplit("/", 1)[-1]
    repo = image.rsplit(":", 1)[0] if ":" in last else image
    return f"{repo}@{raw['digest']}", str(raw["digest"])


# ---------------------------------------------------------------- podman: the one call path


def ensure_dirs() -> None:
    for d in (ROOT, TMPDIR, HOME, XDG_CFG, XDG_DATA, XDG_CACHE, LOGS, RUNROOT, XRD, OUT):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(RUN, 0o700)


def podman_env() -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin",
        "HOME": str(HOME),
        "XDG_RUNTIME_DIR": str(XRD),
        "XDG_CONFIG_HOME": str(XDG_CFG),
        "XDG_DATA_HOME": str(XDG_DATA),
        "XDG_CACHE_HOME": str(XDG_CACHE),
        "LANG": "C.UTF-8",
    }


def podman_argv(args: list[str]) -> list[str]:
    """The podman argv with the global flags of the spike (AGENTS.md). With no `args` it is the
    global part alone, which ginn-wait takes as `<podman> [global-arg...] -- <container>`."""
    if not str(XRD).startswith(f"/run/user/{UID}/s11/") or not str(ROOT).startswith(str(WORK)):
        raise SystemExit("s11: refusing a podman call outside the spike's own dirs")
    return [
        PODMAN,
        "--remote=false",
        "--root",
        str(ROOT),
        "--runroot",
        str(RUNROOT),
        "--tmpdir",
        str(TMPDIR),
        "--runtime",
        "crun",
        *args,
    ]


def podman(args: list[str], timeout: float = 1800.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        podman_argv(args),
        env=podman_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def podman_json(args: list[str]) -> Any:
    r = podman(args)
    if r.returncode != 0:
        raise RuntimeError(f"podman {' '.join(args[:2])} rc {r.returncode}: {r.stderr.strip()}")
    return json.loads(r.stdout)


def du(path: Path) -> int:
    r = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [DU, "-sxb", str(path)], capture_output=True, text=True, timeout=900, check=False
    )
    try:
        return int(r.stdout.split()[0])
    except (ValueError, IndexError):
        return -1


def user_store_mtime() -> dict[str, Any]:
    """Metadata only: the mtime of the user's own store, before and after the spike."""
    try:
        st = os.stat(USER_STORE)
        return {"path": str(USER_STORE), "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns}
    except OSError as err:
        return {"path": str(USER_STORE), "error": err.strerror}


# ---------------------------------------------------------------- the supervisor leaf (F2 route)

RUN_SCRIPT = """#!{py} -I
import sys
sys.path.insert(0, {spike!r})
from s11_runner import run
run({job!r})
"""


def sv_dir() -> Path:
    return OwnedPaths.from_env().sv


def submit(name: str, out: Path, steps: list[dict[str, Any]], tail: list[str] | None) -> None:
    """Make a runit service `name` that runs the job, in the user's sv directory (runsvdir in the
    supervisor leaf). Service names are the container names, so `sample` finds the runsv."""
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(out / "job.json", {"out": str(out), "env": podman_env(), "steps": steps, "tail": tail})
    sv = sv_dir()
    tmp = sv / f".{name}.tmp"
    tmp.mkdir(mode=0o700)
    run_path = tmp / "run"
    run_path.write_text(
        RUN_SCRIPT.format(py=PY, spike=str(SPIKE), job=str(out / "job.json")), encoding="utf-8"
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
        name = str(step["name"])
        rc_file = out / f"{name}.rc"
        rc = int(rc_file.read_text(encoding="utf-8").strip()) if rc_file.exists() else None
        out_file = out / f"{name}.out"
        text = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
        results[name] = {"rc": rc, "out": text}
    return results


def retire(name: str) -> None:
    """Take a service down, wait for its runsv to exit, then remove its directory (f6f1 `retire`)."""
    svc = sv_dir() / name
    if not svc.is_dir():
        return
    for verb in ("down", "exit"):
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [SV_TOOL, verb, str(svc)], capture_output=True, text=True, timeout=120, check=False
        )
    began = time.monotonic()
    while time.monotonic() - began < 120:
        gone = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [SV_TOOL, "status", str(svc)], capture_output=True, text=True, timeout=30, check=False
        )
        text = gone.stdout + gone.stderr
        if "not running" in text or "unable to" in text:
            break
        time.sleep(0.5)
    shutil.rmtree(svc, ignore_errors=True)


def leaf_job(prefix: str, out: Path, steps: list[dict[str, Any]], timeout: float) -> dict[str, dict[str, Any]]:
    """Run argv steps as an exec job in the leaf, wait for them, and retire the service."""
    name = f"{prefix}-{secrets.token_hex(3)}"
    submit(name, out, steps, None)
    try:
        if wait_done(out, timeout) is None:
            raise TimeoutError(f"leaf job {name} did not finish in {timeout:.0f} s")
        return step_results(out, steps)
    finally:
        retire(name)


def leaf_exec(
    tag: str, container: str, cmds: list[tuple[str, list[str]]], out: Path, timeout: float = 900.0
) -> dict[str, dict[str, Any]]:
    """Exec each (name, argv) into a services-on container, in one leaf job (F2 route)."""
    steps = [
        {"name": n, "argv": podman_argv(["exec", container, *argv]), "timeout": timeout}
        for n, argv in cmds
    ]
    return leaf_job(f"s11x-{tag}", out, steps, timeout + 300)


# ---------------------------------------------------------------- measurement (the T7.7 gate's method)


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
    pids: list[int] = []
    if d.is_dir():
        for root, _dirs, _files in os.walk(d):
            procs = Path(root) / "cgroup.procs"
            if procs.exists():
                pids += [int(x) for x in procs.read_text(encoding="utf-8").split()]
    return [f"{p}:{_comm(p)}" for p in sorted(set(pids))]


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def host_memory() -> dict[str, Any]:
    avail = next(
        int(line.split()[1])
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        if line.startswith("MemAvailable:")
    )
    return {"MemAvailable_kib": avail}


def sample(cgroup_dir: Path, svc: str, conmon_pidfile: Path, pause_pidfile: Path) -> dict[str, Any]:
    """The NFR-003 attribution of one realm, the pid set of the gate's realm_rss (f6f1 `sample`).
    VmRSS is the budget figure, PSS is recorded beside it."""
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
        "pss_mib": round(totals["Pss"] / 1024, 2),
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


class Sampler:
    """Samples the root and tmpdir sizes of the spike's storage (ADR-015 Decision 11) and keeps peaks."""

    def __init__(self, every: float = 5.0) -> None:
        self.every = every
        self.peak: dict[str, int] = {"root": 0, "tmp": 0}
        self.series: list[dict[str, Any]] = []
        self.phase = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        began = time.monotonic()
        while not self._stop.is_set():
            root, tmp = du(ROOT), du(TMPDIR)
            self.peak["root"] = max(self.peak["root"], root)
            self.peak["tmp"] = max(self.peak["tmp"], tmp)
            self.series.append(
                {"t": round(time.monotonic() - began, 1), "phase": self.phase, "root": root, "tmp": tmp}
            )
            self._stop.wait(self.every)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=900)


# ---------------------------------------------------------------- phases


def logfile(key: str, tag: str) -> Path:
    path = LOGS / key / f"{tag}.log"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def cexec(name: str, argv: list[str], key: str, tag: str, timeout: float = 3600.0, user: str | None = None) -> dict[str, Any]:
    """Exec argv in a services-off container from the session (no leaf: no services cgroup)."""
    args = ["exec"]
    if user is not None:
        args += ["--user", user]
    args += [name, *argv]
    began = time.monotonic()
    r = podman(args, timeout=timeout)
    seconds = round(time.monotonic() - began, 1)
    text = r.stdout + r.stderr
    with logfile(key, tag).open("a", encoding="utf-8") as sink:
        sink.write(f"$ {' '.join(argv)}\n{text}\n[rc {r.returncode}, {seconds} s]\n")
    lines = text.strip().splitlines()
    return {"argv": argv, "rc": r.returncode, "seconds": seconds, "tail": lines[-LOG_TAIL:], "out": r.stdout.strip()}


def cdu(name: str, key: str, tag: str) -> int:
    res = cexec(name, ["du", "-sxb", "/"], key, tag, timeout=900)
    try:
        return int(res["out"].split()[0]) if res["rc"] == 0 else -1
    except (ValueError, IndexError):
        return -1


def pkg_snapshot(key: str, name: str, tag: str) -> dict[str, int]:
    """Installed package names and sizes in bytes, from the distribution's own database."""
    family = DISTROS[key]["family"]
    if family == "apt":
        res = cexec(name, ["dpkg-query", "-W", "-f", "${Package} ${Installed-Size}\\n"], key, tag)
        sizes: dict[str, int] = {}
        for line in res["out"].splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                sizes[parts[0]] = int(parts[1]) * 1024
        return sizes
    if family == "rpm":
        res = cexec(name, ["rpm", "-qa", "--qf", "%{NAME} %{SIZE}\\n"], key, tag)
        sizes = {}
        for line in res["out"].splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                sizes[parts[0]] = int(parts[1])
        return sizes
    res = cexec(name, ["pacman", "-Qi"], key, tag)
    sizes = {}
    pending: str | None = None
    factors = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    for line in res["out"].splitlines():
        if ":" not in line:
            continue
        field, value = (s.strip() for s in line.split(":", 1))
        if field == "Name":
            pending = value
        elif field == "Installed Size" and pending is not None:
            m = re.fullmatch(r"([0-9.]+) (B|KiB|MiB|GiB)", value)
            if m:
                sizes[pending] = int(float(m.group(1)) * factors[m.group(2)])
            pending = None
    return sizes


def delta_bytes(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    """Installed bytes of the packages new in `after` (the closure of the install), and the size
    change of the packages in both (upgrades), kept apart."""
    new = {n: s for n, s in after.items() if n not in before}
    changed = {n: after[n] - before[n] for n in after if n in before and after[n] != before[n]}
    return {
        "new_packages": len(new),
        "new_installed_bytes": sum(new.values()),
        "changed_packages": len(changed),
        "changed_bytes_net": sum(changed.values()),
        "new_names": sorted(new)[:400],
    }


def diff_summary(name: str) -> dict[str, Any]:
    r = podman(["diff", name])
    kinds: dict[str, int] = {"A": 0, "C": 0, "D": 0}
    top: dict[str, int] = {}
    stubs = 0
    stub_roots = ("/dev", "/proc", "/sys", "/run/.containerenv", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf")
    for line in r.stdout.splitlines():
        if " " not in line:
            continue
        kind, path = line.split(" ", 1)
        kinds[kind] = kinds.get(kind, 0) + 1
        parts = path.split("/")
        key = "/" + parts[1] if len(parts) > 1 and parts[1] else "/"
        top[key] = top.get(key, 0) + 1
        if path.startswith(stub_roots):
            stubs += 1
    return {"rc": r.returncode, "kinds": kinds, "top_level": dict(sorted(top.items())), "stub_like": stubs, "lines": len(r.stdout.splitlines())}


def image_facts(ref: str) -> dict[str, Any]:
    data = podman_json(["image", "inspect", ref])[0]
    cfg = data.get("Config") or {}
    return {
        "id": data.get("Id"),
        "digest": data.get("Digest"),
        "repo_digests": data.get("RepoDigests"),
        "size": data.get("Size"),
        "entrypoint": cfg.get("Entrypoint"),
        "cmd": cfg.get("Cmd"),
        "user": cfg.get("User"),
        "env": cfg.get("Env") or [],
        "labels": cfg.get("Labels") or {},
    }


def compare_images(base: dict[str, Any], derived: dict[str, Any]) -> dict[str, Any]:
    base_env, der_env = set(base["env"]), set(derived["env"])
    b_labels, d_labels = base["labels"], derived["labels"]
    return {
        "entrypoint": derived["entrypoint"],
        "cmd": derived["cmd"],
        "user": derived["user"],
        "env_extra_in_derived": sorted(der_env - base_env),
        "env_missing_in_derived": sorted(base_env - der_env),
        "labels_extra": {k: d_labels[k] for k in sorted(set(d_labels) - set(b_labels))},
        "labels_missing": sorted(set(b_labels) - set(d_labels)),
        "labels_changed": {k: [b_labels[k], d_labels[k]] for k in sorted(set(b_labels) & set(d_labels)) if b_labels[k] != d_labels[k]},
        "size_base": base["size"],
        "size_derived": derived["size"],
        "size_delta": (derived["size"] or 0) - (base["size"] or 0),
    }


def save(key: str, name: str, data: dict[str, Any]) -> None:
    write_json(OUT / key / f"{name}.json", data)


def provision(key: str, keep_id: bool, ref: str, digest: str, rec: dict[str, Any], sampler: Sampler) -> dict[str, Any]:
    """ADR-015 Decision 2, as a spike: create the provisioning container, run the steps of the
    distribution data, check /sbin/init, clean, stop, commit with the Decision 2f configuration."""
    suffix = "" if keep_id else "-nokeep"
    tagp = f"prov{suffix}"
    cname = f"ginn_provision_s11-{key}{suffix}_g1"
    iname = f"localhost/ginn-init-s11-{key}{suffix}:g1"
    label = f"s11-{key}{suffix}:g1"
    cfg = DISTROS[key]
    dist = loader.load(key)
    ips = dist.integration_set(os.environ.get("LANG"))
    refresh = dist.argv(loader.Verb.REFRESH)
    steps: list[tuple[str, list[str], float, bool]] = [("refresh", refresh, 3600.0, False)]
    if cfg["init"]:
        steps.append(("init-install", dist.argv(loader.Verb.INSTALL, list(cfg["init"])), 3600.0, True))
    if ips.packages:
        steps.append(("ips-install", dist.argv(loader.Verb.INSTALL, list(ips.packages)), 3600.0, True))
    for i, step in enumerate(ips.steps):
        steps.append((f"ips-step-{i}", list(step), 900.0, False))
    init_hash = hashlib.sha256(
        json.dumps(
            {"init": list(cfg["init"]), "ips": list(ips.packages), "steps": [list(s) for s in ips.steps], "clean": [list(c) for c in cfg["clean"]]},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    rec[f"init_set_hash{suffix}"] = init_hash
    rec[f"argv{suffix}"] = {"refresh": refresh, "init": list(cfg["init"]), "ips_packages": list(ips.packages), "ips_steps": [list(s) for s in ips.steps], "clean": [list(c) for c in cfg["clean"]], "locale_note": ips.fallback_reason, "lang": ips.lang}
    ph: list[dict[str, Any]] = []
    rec[f"phases{suffix}"] = ph

    def note(entry: dict[str, Any]) -> None:
        ph.append(entry)
        say("PHASE", {"distro": key, "keep_id": keep_id, **{k: v for k, v in entry.items() if k != "tail"}})
        save(key, "summary", rec)

    # Decision 2b: services-off base spec with keep-id and root, and no passwd entry.
    sampler.phase = tagp
    argv = ["create", "--name", cname, "--user", "0:0"]
    if keep_id:
        argv += ["--userns", "keep-id"]
    argv += ["--network", "host", "--http-proxy=false", "--init", "--entrypoint", json.dumps(["sleep"]),
             "--label", f"io.ginn.provision={label}", ref, "infinity"]
    began = time.monotonic()
    r = podman(argv)
    note({"phase": "create", "rc": r.returncode, "seconds": round(time.monotonic() - began, 1), "argv": argv, "root_bytes": du(ROOT), "stderr": r.stderr.strip()[-600:]})
    if r.returncode != 0:
        raise RuntimeError(f"provisioning create failed: {r.stderr.strip()}")
    began = time.monotonic()
    r = podman(["start", cname])
    note({"phase": "start", "rc": r.returncode, "seconds": round(time.monotonic() - began, 1), "root_bytes": du(ROOT), "stderr": r.stderr.strip()[-600:]})
    if r.returncode != 0:
        raise RuntimeError(f"provisioning start failed: {r.stderr.strip()}")
    try:
        before = pkg_snapshot(key, cname, f"{tagp}-pkgs-0")
        note({"phase": "base-packages", "count": len(before), "container_bytes": cdu(cname, key, f"{tagp}-du")})
        for name, argv_step, timeout, install in steps:
            snap_before = pkg_snapshot(key, cname, f"{tagp}-{name}-pre") if install else None
            began = time.monotonic()
            res = cexec(cname, argv_step, key, f"{tagp}-{name}", timeout=timeout)
            entry: dict[str, Any] = {"phase": name, "rc": res["rc"], "seconds": res["seconds"], "argv": argv_step, "tail": res["tail"], "root_bytes": du(ROOT)}
            if install:
                snap_after = pkg_snapshot(key, cname, f"{tagp}-{name}-post")
                entry["delta"] = delta_bytes(snap_before or {}, snap_after)
            note(entry)
            if res["rc"] != 0:
                raise RuntimeError(f"provisioning step {name} rc {res['rc']}")
            if name == "refresh":
                entry["container_bytes_after_refresh"] = cdu(cname, key, f"{tagp}-du-refresh")
        init_check = cexec(cname, ["test", "-x", "/sbin/init"], key, f"{tagp}-check")
        link = cexec(cname, ["readlink", "-f", "/sbin/init"], key, f"{tagp}-check")
        note({"phase": "check-init", "rc": init_check["rc"], "readlink": link["out"], "seconds": init_check["seconds"]})
        if init_check["rc"] != 0:
            raise RuntimeError("GINN-E-NO-INIT: /sbin/init is missing in the provisioning container")
        cache_before = cexec(cname, ["sh", "-c", "du -sxb /var/cache /var/lib/apt/lists /var/lib/dnf /var/lib/pacman/sync /var/cache/pacman/pkg 2>/dev/null; true"], key, f"{tagp}-cache")
        root_before_clean = cdu(cname, key, f"{tagp}-du-preclean")
        clean_entries = []
        for i, step in enumerate(cfg["clean"]):
            res = cexec(cname, list(step), key, f"{tagp}-clean-{i}")
            clean_entries.append({"argv": list(step), "rc": res["rc"], "seconds": res["seconds"], "tail": res["tail"]})
        root_after_clean = cdu(cname, key, f"{tagp}-du-postclean")
        cache_after = cexec(cname, ["sh", "-c", "du -sxb /var/cache /var/lib/apt/lists /var/lib/dnf /var/lib/pacman/sync /var/cache/pacman/pkg 2>/dev/null; true"], key, f"{tagp}-cache2")
        pkg_cache_left = cexec(cname, ["sh", "-c", "ls -A /var/cache/pacman/pkg 2>/dev/null | wc -l"], key, f"{tagp}-pkgcache")
        note({
            "phase": "clean",
            "entries": clean_entries,
            "root_bytes_before_clean": root_before_clean,
            "root_bytes_after_clean": root_after_clean,
            "cache_du_before": cache_before["out"],
            "cache_du_after": cache_after["out"],
            "pacman_pkg_cache_entries_after": pkg_cache_left["out"],
        })
        if any(c["rc"] != 0 for c in clean_entries):
            raise RuntimeError("a clean step failed")
        note({"phase": "diff", **diff_summary(cname)})
    except Exception:
        podman(["rm", "-f", cname])
        raise
    stop = podman(["stop", "-t", "10", cname])
    note({"phase": "stop", "rc": stop.returncode})
    # Decision 2f, as verified in the spike: `--change ENTRYPOINT []` and `--change CMD []` are
    # accepted but keep the create-time entrypoint and command (sleep, infinity). The reset is
    # done at create: `create --entrypoint []` with no command, from the stage image, gives an
    # image with null Entrypoint and Cmd. So: stage commit with the user, env and labels, then the
    # reset container, then the final commit. The final image is `iname`, the stage is `stage`.
    base = rec["base"]
    user = base["user"] or "0"
    change = ["--change", f"USER {user}"]
    for entry_env in base["env"]:
        change += ["--change", f"ENV {entry_env}"]
    change += [
        "--change", "LABEL io.ginn.provision=",
        "--change", f"LABEL io.ginn.init-set={init_hash}",
        "--change", f"LABEL io.ginn.base-digest={digest}",
    ]
    stage = f"localhost/ginn-init-s11-{key}{suffix}-stage:g1"
    rname = f"ginn_reset_s11-{key}{suffix}_g1"
    sampler.phase = f"commit{suffix}"
    began = time.monotonic()
    r = podman(["commit", *change, cname, stage], timeout=3600)
    note({"phase": "commit-stage", "rc": r.returncode, "seconds": round(time.monotonic() - began, 1), "image_id": r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "", "stderr": r.stderr.strip()[-800:], "change_argv": change, "root_bytes": du(ROOT), "tmp_bytes": du(TMPDIR)})
    if r.returncode != 0:
        podman(["rm", "-f", cname])
        raise RuntimeError(f"stage commit failed: {r.stderr.strip()}")
    podman(["rm", "-f", rname])
    rc_create = podman(["create", "--name", rname, "--entrypoint", "[]", stage])
    note({"phase": "reset-create", "rc": rc_create.returncode, "argv": ["create", "--name", rname, "--entrypoint", "[]", stage], "stderr": rc_create.stderr.strip()[-400:]})
    if rc_create.returncode != 0:
        podman(["rm", "-f", cname])
        raise RuntimeError(f"reset create failed: {rc_create.stderr.strip()}")
    began = time.monotonic()
    r = podman(["commit", rname, iname], timeout=3600)
    note({"phase": "commit", "rc": r.returncode, "seconds": round(time.monotonic() - began, 1), "image_id": r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "", "stderr": r.stderr.strip()[-800:], "root_bytes": du(ROOT), "tmp_bytes": du(TMPDIR)})
    podman(["rm", "-f", rname])
    if r.returncode != 0:
        podman(["rm", "-f", cname])
        raise RuntimeError(f"commit failed: {r.stderr.strip()}")
    derived = image_facts(iname)
    rec[f"derived{suffix}"] = derived
    rec[f"compare{suffix}"] = compare_images(base, derived)
    note({"phase": "compare", **rec[f"compare{suffix}"]})
    # The provisioning container goes now (Decision 2g). Its writable layer is the largest storage.
    root_before_rm = du(ROOT)
    rm = podman(["rm", "-f", cname])
    note({"phase": "rm-provision", "rc": rm.returncode, "root_bytes_before": root_before_rm, "root_bytes_after": du(ROOT)})
    rec[f"image_id{suffix}"] = derived["id"]
    rec[f"image_name{suffix}"] = iname
    rec[f"stage_name{suffix}"] = stage
    save(key, "summary", rec)
    return derived


def boot_once(key: str, image_id: str, index: int, diag: bool, rec: dict[str, Any], sampler: Sampler) -> dict[str, Any]:
    """ADR-003 services-on argv from the derived image, with ADR-007 mitigations a, b and d, the
    explicit `/sbin/init` entrypoint and no command, and a machine-id copied in before the first start."""
    tag = f"{key}-r{index}-{secrets.token_hex(2)}"
    name = f"s11-boot-{tag}"
    jobdir = OUT / key / "jobs" / name
    jobdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    pidfile = jobdir / "conmon.pid"
    mid_file = jobdir / "machine-id"
    mid_file.write_text(secrets.token_hex(16) + "\n", encoding="utf-8")
    mid_file.chmod(0o644)
    dropin = jobdir / "journald-ginn.conf"
    dropin.write_text(JOURNALD_DROPIN_TEXT, encoding="utf-8")
    mounts = ["--mount", f"type=bind,source={dropin},target={JOURNALD_TARGET},ro"]
    for unit in SERVICES_MASKED_UNITS:
        mounts += ["--mount", f"type=bind,source=/dev/null,target=/etc/systemd/system/{unit},ro"]
    create = [
        "create", "--name", name,
        "--systemd=always", "--cgroupns", "private", "--cgroup-parent", f"/ginn/{UID}/{name}",
        "--user", "0:0", "--userns", "keep-id", "--passwd-entry", PASSWD,
        "--network", "host", "--http-proxy=false",
        "--stop-signal", "SIGRTMIN+3", "--stop-timeout", "30",
        "--env", "SYSTEMD_LOG_LEVEL=warning",
        "--conmon-pidfile", str(pidfile),
        "--label", "io.ginn.spike=s11",
        "--tmpfs", f"/run/user/{UID}:mode=0700,U",
        *mounts,
        "--entrypoint", json.dumps(["/sbin/init"]),
        image_id,
    ]
    steps = [
        {"name": "create", "argv": podman_argv(create), "required": True, "timeout": 300},
        {"name": "cp", "argv": podman_argv(["cp", str(mid_file), f"{name}:/etc/machine-id"]), "required": True, "timeout": 300},
        {"name": "start", "argv": podman_argv(["start", name]), "required": True, "timeout": 300},
    ]
    tail = [NATIVE_WAIT, str(pidfile), *podman_argv([]), "--", name]
    result: dict[str, Any] = {"index": index, "name": name, "machine_id": mid_file.read_text(encoding="utf-8").strip()}
    sampler.phase = f"boot-{index}"
    began = time.monotonic()
    submit(name, jobdir, steps, tail)
    state = wait_done(jobdir, 900)
    boot_steps = step_results(jobdir, steps)
    result["boot_job_state"] = state
    result["boot_rc"] = {n: v["rc"] for n, v in boot_steps.items()}
    if state != "ok":
        result["error"] = "a create, cp or start step failed"
        result["boot_out"] = {n: v["out"][-800:] for n, v in boot_steps.items()}
        retire(name)
        podman(["rm", "-f", name])
        rm_empty_leaf(name)
        return result
    cmds: list[tuple[str, list[str]]] = [
        ("wait", ["systemctl", "is-system-running", "--wait"]),
        ("state", ["systemctl", "is-system-running"]),
        ("failed", ["systemctl", "--failed", "--no-legend", "--plain"]),
        ("machine-id", ["cat", "/etc/machine-id"]),
        ("journald-dropin", ["cat", JOURNALD_TARGET]),
        ("busctl", ["busctl", "--no-pager", "list"]),
        ("pid1", ["cat", "/proc/1/comm"]),
    ]
    if diag:
        cmds += [
            ("failed-full", ["systemctl", "--failed", "--no-pager"]),
            ("version", ["systemctl", "--version"]),
            ("procs", ["sh", "-c", "cat /proc/[0-9]*/comm 2>/dev/null | sort | uniq -c"]),
            ("journal-warnings", ["journalctl", "-b", "-p", "warning", "--no-pager"]),
            ("machine-id-ls", ["ls", "-l", "/etc/machine-id"]),
        ]
    for unit in SERVICES_MASKED_UNITS:
        cmds.append((f"mask {unit}", ["systemctl", "show", "-p", "LoadState", "-p", "ActiveState", unit]))
    settle = leaf_exec(tag, name, cmds, jobdir / "settle", timeout=900)
    result["settle_rc"] = {n: v["rc"] for n, v in settle.items() if not n.startswith("mask ") and n not in ("journal-warnings",)}
    result["is_system_running"] = settle["state"]["out"].strip()
    result["failed_units"] = [line.split()[0] for line in settle["failed"]["out"].splitlines() if line.strip()]
    result["failed_units_plain"] = settle["failed"]["out"].strip()
    result["machine_id_in_container"] = settle["machine-id"]["out"].strip()
    result["journald_dropin_matches"] = settle["journald-dropin"]["out"] == JOURNALD_DROPIN_TEXT
    result["busctl_rc"] = settle["busctl"]["rc"]
    result["pid1"] = settle["pid1"]["out"].strip()
    result["masks"] = {u: settle[f"mask {u}"]["out"].strip().replace("\n", " ") for u in SERVICES_MASKED_UNITS}
    result["masks_all_masked_inactive"] = all(
        "LoadState=masked" in v and "ActiveState=inactive" in v for v in result["masks"].values()
    )
    result["boot_seconds"] = round(time.monotonic() - began, 1)
    ccfg = (podman_json(["container", "inspect", name])[0].get("Config") or {})
    result["container_entrypoint"] = ccfg.get("Entrypoint")
    result["container_cmd"] = ccfg.get("Cmd")
    if diag:
        result["diag"] = {n: v["out"][-6000:] for n, v in settle.items() if n in ("failed-full", "version", "procs", "journal-warnings", "machine-id-ls")}
    result["pass_zero_failed_running"] = result["is_system_running"] == "running" and not result["failed_units"]
    if index >= 1:
        time.sleep(IDLE_S)
    host = host_memory()
    s = sample(CGROUP_ROOT / name, name, pidfile, PAUSE)
    result.update({"idle_s": IDLE_S, "host": host, "vmrss_mib": s["vmrss_mib"], "pss_mib": s["pss_mib"], "pss_unreadable": s["pss_unreadable"], "container_procs": s["container_procs"], "container_proc_names": s["container_proc_names"], "helpers": s["helpers"]})
    size = podman_json(["container", "inspect", "--size", name])[0]
    result["writable_layer_bytes"] = size.get("SizeRw")
    result["rootfs_bytes"] = size.get("SizeRootFs")
    retire(name)
    rm = podman(["rm", "-f", name])
    result["rm_rc"] = rm.returncode
    rm_empty_leaf(name)
    say("BOOT", {"distro": key, "run": index, "state": result["is_system_running"], "failed": result["failed_units"], "vmrss_mib": result["vmrss_mib"], "pss_mib": result["pss_mib"]})
    return result


def off_suite(key: str, image_id: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Services-off container from the derived image: keep-id, passwd entry, sleep infinity (FR-082)."""
    name = f"s11-off-{key}"
    dist = loader.load(key)
    out: dict[str, Any] = {"name": name}
    argv = ["create", "--name", name, "--user", f"{UID}:{GID}", "--userns", "keep-id", "--passwd-entry", PASSWD,
            "--network", "host", "--http-proxy=false", "--init", "--entrypoint", json.dumps(["sleep"]),
            "--label", "io.ginn.spike=s11", image_id, "infinity"]
    r = podman(argv)
    out["create_rc"] = r.returncode
    out["create_stderr"] = r.stderr.strip()[-400:]
    if r.returncode != 0:
        return out
    try:
        r = podman(["start", name])
        out["start_rc"] = r.returncode
        out["start_stderr"] = r.stderr.strip()[-400:]
        out["id_u"] = cexec(name, ["id", "-u"], key, "off-id")["out"]
        procs = cexec(name, ["sh", "-c", "cat /proc/[0-9]*/comm 2>/dev/null"], key, "off-procs")["out"].split()
        out["pid1"] = cexec(name, ["cat", "/proc/1/comm"], key, "off-pid1")["out"]
        out["systemd_process_present"] = "systemd" in procs
        refresh = dist.argv(loader.Verb.REFRESH)
        install = dist.argv(loader.Verb.INSTALL, ["htop"])
        out["install_refresh_rc"] = cexec(name, refresh, key, "off-refresh", user="0:0")["rc"]
        res = cexec(name, install, key, "off-install", user="0:0")
        out["install_htop_rc"] = res["rc"]
        out["htop_before_restart"] = cexec(name, ["sh", "-c", "command -v htop"], key, "off-htop-1")["rc"]
        size = podman_json(["container", "inspect", "--size", name])[0]
        out["writable_layer_bytes_after_install"] = size.get("SizeRw")
        podman(["stop", "-t", "10", name])
        r = podman(["start", name])
        out["restart_rc"] = r.returncode
        out["htop_after_restart"] = cexec(name, ["sh", "-c", "command -v htop"], key, "off-htop-2")["rc"]
        out["pass_services_off"] = (
            out["start_rc"] == 0 and out["id_u"] == str(UID) and not out["systemd_process_present"]
            and out["install_htop_rc"] == 0 and out["htop_after_restart"] == 0
        )
    finally:
        podman(["rm", "-f", name])
    rec["off"] = out
    save(key, "summary", rec)
    return out


def rmi_check(key: str, base_ref: str, derived_ids: list[str], rec: dict[str, Any]) -> dict[str, Any]:
    """Decision 13 and Q8: the base is refused while a derived child exists (containers first, none
    left on the base). The derived images are then removed, and the base after them."""
    out: dict[str, Any] = {}
    base_id = rec["base"]["id"]
    out["containers_on_base"] = podman(["ps", "-a", "--format", "{{.Names}}"]).stdout.split()
    out["children_present"] = [image for _label, image in derived_ids]
    r = podman(["rmi", base_id])
    out["base_rmi_by_id_while_children"] = {"rc": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}
    out["base_inspect_after_id_rmi"] = podman(["image", "inspect", "--format", "{{.Id}}", base_id]).stderr.strip()[-200:]
    final_image = derived_ids[0][1]
    run = podman(["run", "--rm", "--network", "none", final_image, "true"])
    out["child_run_after_base_rmi"] = {"rc": run.returncode, "stderr": run.stderr.strip()[-300:]}
    out["derived_rmi"] = {}
    for label, image in derived_ids:
        r = podman(["rmi", image])
        out["derived_rmi"][label] = {"rc": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()[-400:]}
    r = podman(["rmi", base_id])
    out["base_rmi_after_children"] = {"rc": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()[-400:]}
    rec["rmi"] = out
    save(key, "summary", rec)
    return out


def syntax_check(key: str) -> dict[str, Any]:
    """Throwaway commits from the pinned base, to settle the --change reset syntax (ADR-015 open
    question 4) and the child-image rmi rule by image ID. Its images are removed at the end."""
    ref, digest = pinned(key)
    ensure_dirs()
    out: dict[str, Any] = {"distro": key, "base_ref": ref}
    r = podman(["pull", "--platform", "linux/amd64", ref], timeout=3600)
    out["pull_rc"] = r.returncode
    base = image_facts(ref)
    out["base"] = {"id": base["id"], "entrypoint": base["entrypoint"], "cmd": base["cmd"], "user": base["user"]}
    variants: dict[str, list[str]] = {
        "empty-array": ["--change", "ENTRYPOINT []", "--change", "CMD []"],
        "empty-string": ["--change", 'ENTRYPOINT ""', "--change", 'CMD ""'],
        "null-word": ["--change", "ENTRYPOINT null", "--change", "CMD null"],
        "empty-array-no-cmd": ["--change", "ENTRYPOINT []"],
    }
    out["variants"] = {}
    first_image = None
    for i, (label, change) in enumerate(variants.items()):
        cname = f"s11-syn-{key}-{i}"
        iname = f"localhost/s11-syntax-{key}-{i}:1"
        podman(["rm", "-f", cname])
        c = podman(["create", "--name", cname, "--entrypoint", json.dumps(["sleep"]), ref, "infinity"])
        if c.returncode != 0:
            out["variants"][label] = {"create_rc": c.returncode, "stderr": c.stderr.strip()[-300:]}
            continue
        m = podman(["commit", *change, cname, iname])
        entry: dict[str, Any] = {"commit_rc": m.returncode, "commit_stderr": m.stderr.strip()[-400:], "change": change}
        if m.returncode == 0:
            facts = image_facts(iname)
            entry["entrypoint"] = facts["entrypoint"]
            entry["cmd"] = facts["cmd"]
            entry["user"] = facts["user"]
            if first_image is None:
                first_image = iname
        out["variants"][label] = entry
        podman(["rm", "-f", cname])
    if first_image is not None:
        by_id = podman(["rmi", base["id"]])
        out["rmi_base_by_id_with_child"] = {"rc": by_id.returncode, "stdout": by_id.stdout.strip(), "stderr": by_id.stderr.strip()}
        by_name = podman(["rmi", ref])
        out["rmi_base_by_name_with_child"] = {"rc": by_name.returncode, "stdout": by_name.stdout.strip(), "stderr": by_name.stderr.strip()}
        imgs = podman(["images", "-a", "--format", "{{.ID}} {{.Repository}}:{{.Tag}}"]).stdout.strip().splitlines()
        out["images_after_name_rmi"] = imgs
        child_rm = podman(["rmi", first_image])
        out["rmi_child"] = {"rc": child_rm.returncode, "stderr": child_rm.stderr.strip()[-300:]}
    for i in range(len(variants)):
        podman(["rmi", "-f", f"localhost/s11-syntax-{key}-{i}:1"])
    podman(["rmi", "-f", base["id"]])
    write_json(OUT / key / "syntax.json", out)
    say("SYNTAX", {k: v for k, v in out.items() if k in ("variants", "rmi_base_by_id_with_child", "rmi_base_by_name_with_child")})
    return out


def run_distro(key: str, nokeep: bool) -> None:
    ensure_dirs()
    ref, digest = pinned(key)
    rec: dict[str, Any] = {"distro": key, "pinned_ref": ref, "pinned_digest": digest, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "user_store_before": user_store_mtime()}
    sampler = Sampler()
    sampler.start()
    sampler.phase = "pull"
    began = time.monotonic()
    r = podman(["pull", "--platform", "linux/amd64", ref], timeout=3600)
    rec["pull"] = {"rc": r.returncode, "seconds": round(time.monotonic() - began, 1), "stderr": r.stderr.strip()[-400:]}
    if r.returncode != 0:
        raise RuntimeError(f"pull failed: {r.stderr.strip()}")
    rec["base"] = image_facts(ref)
    rec["root_bytes_after_pull"] = du(ROOT)
    save(key, "summary", rec)
    say("BASE", {"distro": key, "digest": rec["base"]["digest"], "entrypoint": rec["base"]["entrypoint"], "cmd": rec["base"]["cmd"], "user": rec["base"]["user"], "size": rec["base"]["size"]})
    try:
        derived = provision(key, True, ref, digest, rec, sampler)
        boot = []
        for i in range(1, BOOT_RUNS + 1):
            boot.append(boot_once(key, derived["id"], i, i == 1, rec, sampler))
            rec["boot"] = boot
            save(key, "summary", rec)
        sampler.phase = "off"
        off_suite(key, derived["id"], rec)
        if nokeep:
            provision(key, False, ref, digest, rec, sampler)
    finally:
        sampler.stop()
        rec["sampler_peak"] = sampler.peak
        write_json(OUT / key / "sampler.json", sampler.series)
        save(key, "summary", rec)
    derived_ids = [("keep-id", rec["image_name"]), ("keep-id-stage", rec["stage_name"])]
    if nokeep:
        derived_ids += [("nokeep", f"localhost/ginn-init-s11-{key}-nokeep:g1"), ("nokeep-stage", f"localhost/ginn-init-s11-{key}-nokeep-stage:g1")]
    rec["root_bytes_before_rmi"] = du(ROOT)
    rmi_check(key, ref, derived_ids, rec)
    rec["root_bytes_after_rmi"] = du(ROOT)
    rec["user_store_after"] = user_store_mtime()
    rec["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save(key, "summary", rec)
    say("DONE", {"distro": key, "pass_boot": [b["pass_zero_failed_running"] for b in rec["boot"]], "off": rec["off"].get("pass_services_off")})


def probe() -> None:
    ensure_dirs()
    ver = podman(["version"]).stdout
    info = podman_json(["info", "--format", "json"])
    data = {
        "podman_version": ver.strip(),
        "store": info.get("store"),
        "host_rootless": ((info.get("host") or {}).get("security") or {}).get("rootless"),
        "kernel": (info.get("host") or {}).get("kernel"),
        "root": str(ROOT),
        "runroot": str(RUNROOT),
        "tmpdir": str(TMPDIR),
        "xdg_runtime_dir": str(XRD),
        "user_store_before": user_store_mtime(),
    }
    write_json(OUT / "probe.json", data)
    say("PROBE", {"driver": (info.get("store") or {}).get("graphDriverName"), "graphRoot": (info.get("store") or {}).get("graphRoot"), "runRoot": (info.get("store") or {}).get("runRoot")})


def pause_pid_ours() -> list[int]:
    """The rootless pause started by the spike's own podman, found by its pid file. Only a process
    whose environment names the spike's XDG_RUNTIME_DIR counts."""
    found = []
    for pid in _pid_file(PAUSE):
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            if f"XDG_RUNTIME_DIR={XRD}".encode() in env and os.stat(f"/proc/{pid}").st_uid == UID:
                found.append(pid)
        except OSError:
            continue
    return found


def cleanup() -> None:
    ensure_dirs()
    names = podman(["ps", "-a", "--format", "{{.Names}}"]).stdout.split()
    spike = [n for n in names if n.startswith("s11") or n.startswith("ginn_provision_s11")]
    for n in spike:
        retire(n)
        podman(["rm", "-f", n])
        rm_empty_leaf(n)
    for svc in sorted(sv_dir().glob("s11*")):
        retire(svc.name)
    for tmp in sorted(sv_dir().glob(".s11*.tmp")):
        shutil.rmtree(tmp, ignore_errors=True)
    images = podman(["rmi", "-af", "--force"], timeout=1800)
    for pid in pause_pid_ours():
        os.kill(pid, signal.SIGTERM)
    for d in sorted(CGROUP_ROOT.glob("s11*")):
        rm_empty_leaf(d.name)
    removed_storage = podman(["unshare", "rm", "-rf", str(ROOT), str(TMPDIR), str(XDG_CACHE), str(XDG_DATA), str(XDG_CFG), str(HOME)], timeout=1800)
    shutil.rmtree(XRD, ignore_errors=True)
    shutil.rmtree(RUNROOT, ignore_errors=True)
    shutil.rmtree(RUN, ignore_errors=True)
    say("CLEANUP", {"containers": spike, "rmi_rc": images.returncode, "storage_rm_rc": removed_storage.returncode, "user_store": user_store_mtime()})


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="s11")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    p = sub.add_parser("distro")
    p.add_argument("key", choices=sorted(DISTROS))
    p.add_argument("--nokeep", action="store_true", help="also provision without --userns keep-id")
    p = sub.add_parser("syntax")
    p.add_argument("key", choices=sorted(DISTROS))
    sub.add_parser("cleanup")
    a = ap.parse_args(argv)
    if a.cmd == "probe":
        probe()
    elif a.cmd == "distro":
        run_distro(a.key, a.nokeep)
    elif a.cmd == "syntax":
        syntax_check(a.key)
    elif a.cmd == "cleanup":
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
