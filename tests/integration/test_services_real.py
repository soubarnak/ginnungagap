# SPDX-License-Identifier: Apache-2.0
"""Host gate for services mode, runit supervision and autostart (T7.7), on real rootless Podman.

Run with `GINN_INTEGRATION=1 .venv/bin/pytest tests/integration/test_services_real.py -v -s` on a
Void glibc host. The verdicts and numbers are in spikes/results/T7.7-services-real.md.

This gate drives the product CLI against the user's real runit supervisor
(`$XDG_STATE_HOME/ginn/sv`, run by `runsvdir` under the root service `ginn-soubarna`), not against
the temp base of conftest.py.
The CLI runs through a launcher that this module writes (`launcher`). The launcher points two native
helper constants, ginn-wait and ginn-rebind, at `native/build`, because `/usr/libexec/ginn` is not
installed on this host and installing it needs root. The launcher changes nothing else of the
product.
Every realm this module makes is named `t77-...`. The module removes them at the start and the end.

Which distributions boot as services-on realms on this host: arch does (its base image ships
systemd). debian and fedora do not (their pinned images have no /sbin/init). A services-on create
on either is refused at create with GINN-E-NO-INIT, before any start (F1, F3). That refusal is the
known gap F1: the items that expect it are strict xfails with `raises=KnownGapF1`, and
`create_or_gap` raises KnownGapF1 for those two distributions only. A start error is never F1, so
a failed start is a plain failure. The items that need a running realm use arch (RUN_DISTRO).

Verdict policy: a known product gap is never turned into an xfail while the test runs. A failing
assertion stays a failure unless the item carries a strict `xfail` mark for a tracked gap (F<n>).
Then an unexpected pass fails the suite. An environment-only gap is a skip.

In-realm commands run through `leaf_exec`. A session process cannot enter a services-on cgroup, so
the command runs as a throwaway runit service, which is inside the supervisor leaf.

Musl: no musl host exists, so the musl leg is marked host_musl and conftest.py skips it.
FR-085: deferred (CL-2), so its test is skipped with that reason.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import RealmName
from ginn.podman.cli import PodmanCli
from ginn.runtime.spec import SERVICES_MASKED_UNITS
from ginn.services._exec_lock import (
    exec_lock_busy,
    exec_lock_dir,
    exec_lock_path,
    exec_orphan_path,
)
from ginn.services.exec_relay import exec_service_dir, exec_socket
from ginn.services.runit_user import service_dir
from ginn.services.supervisor import parse_status
from ginn.store.paths import OwnedPaths
from tests.integration.conftest import REPO, run_tool, tool


class KnownGapF1(AssertionError):
    """The failure of gap F1: an image without /sbin/init, refused at create (GINN-E-NO-INIT).

    The raise is gated on F1_DISTROS in `create_or_gap`, so a NO-INIT elsewhere stays a plain
    failure. The mark is `raises=KnownGapF1`, so no other failure of an item is absorbed."""


F1_DISTROS = ("debian", "fedora")
"""The distributions whose pinned images have no /sbin/init (F1). Only their NO-INIT is the gap."""
PREFIX = "t77-"
DISTROS = ("debian", "arch", "fedora")
"""The three distributions whose images are pinned in trust.toml (FR-080 parametrisation)."""
RUN_DISTRO = "arch"
"""The distribution of the items that need a running services-on realm (module docstring)."""
UID = os.getuid()
NATIVE_BUILD = REPO / "native" / "build"
CGROUP_ROOT = Path("/sys/fs/cgroup/ginn") / str(UID)
"""The user's cgroup root, /sys/fs/cgroup/ginn/<uid> (ADR-003). The runtime root: paths.runtime."""
PODMAN = tool("podman")
SV = tool("sv")

F1_NO_INIT = (
    "F1: the pinned debian and fedora images have no /sbin/init, so a services-on create is "
    "refused with GINN-E-NO-INIT; see spikes/results/T7.7-services-real.md"
)
FR080_CASES = [
    pytest.param(
        "debian", marks=pytest.mark.xfail(strict=True, reason=F1_NO_INIT, raises=KnownGapF1)
    ),
    pytest.param("arch"),
    pytest.param(
        "fedora", marks=pytest.mark.xfail(strict=True, reason=F1_NO_INIT, raises=KnownGapF1)
    ),
]
NFR003_CASES = [
    pytest.param(
        "debian", marks=pytest.mark.xfail(strict=True, reason=F1_NO_INIT, raises=KnownGapF1)
    ),
    pytest.param("arch"),
    pytest.param(
        "fedora", marks=pytest.mark.xfail(strict=True, reason=F1_NO_INIT, raises=KnownGapF1)
    ),
]


@dataclass
class Ran:
    """One finished ginn command: its exit status and its last JSON object (progress skipped)."""

    rc: int
    body: dict[str, Any]

    def error_text(self) -> str:
        err = self.body.get("error")
        if isinstance(err, dict):
            return f"{err.get('code')}: {err.get('message')}"
        return f"exit {self.rc}"

    def codes(self) -> list[str]:
        return [str(w.get("code")) for w in self.body.get("warnings", []) if isinstance(w, dict)]


def ginn(launcher: Path, *args: str, timeout: float = 900.0) -> Ran:
    """Run the product CLI through the launcher with --json, and read its last JSON object."""
    env = {**os.environ, "PATH": f"{launcher.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(launcher), "--json", *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd="/tmp",  # noqa: S108 - a working directory, not a temp file
        timeout=timeout,
        check=False,
    )
    lines = (done.stdout + "\n" + done.stderr).splitlines()
    objects = [json.loads(line) for line in lines if line.startswith("{")]
    body = next((o for o in reversed(objects) if "progress" not in o), {})
    return Ran(done.returncode, body)


def start_ok(launcher: Path, name: str) -> None:
    """Start the realm and assert that the start succeeded. A failed start is never a skip, and it
    is never F1: an image without /sbin/init is refused at create (GINN-E-NO-INIT), before any
    start. So every failed start is a plain failure, whatever its code."""
    ran = ginn(launcher, "start", name)
    assert ran.rc == 0, ran.error_text()


def podman_cli(paths: OwnedPaths) -> PodmanCli:
    """The product's podman adapter over ginn's own storage (--root, --runroot, --tmpdir)."""
    return PodmanCli.from_paths(paths, binary=PODMAN, oci_runtime="crun")


def podman(paths: OwnedPaths, args: list[str]) -> str:
    """Run one podman call through the adapter's global flags, with ginn's own runtime directory."""
    env = {"XDG_RUNTIME_DIR": str(paths.runtime), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*podman_cli(paths).global_argv(), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=True,
    )
    return done.stdout


def container_id(paths: OwnedPaths, name: str) -> str:
    """The id of the realm's container. Its labels name the realm (io.ginn.realm)."""
    out = podman(
        paths, ["ps", "-a", "--filter", f"label=io.ginn.realm={name}", "--format", "{{.ID}}"]
    )
    return out.split()[0]


def pid_of(paths: OwnedPaths, name: str) -> int:
    """The host pid of the realm's container init (PID 1 inside the container)."""
    return int(podman(paths, ["inspect", "--format", "{{.State.Pid}}", container_id(paths, name)]))


def sv_status(paths: OwnedPaths, name: str) -> str:
    """The first line of `sv status` for the realm's runit directory (FR-081 AC-1)."""
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [SV, "status", str(service_dir(paths, RealmName(name)))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return (done.stdout.strip().splitlines() or [""])[0]


def rss_kib(pid: int) -> dict[str, int]:
    """VmRSS, RssAnon and RssFile of one process, in KiB (proc(5))."""
    values = {"VmRSS": 0, "RssAnon": 0, "RssFile": 0}
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        key = line.split(":", 1)[0]
        if key in values:
            values[key] = int(line.split()[1])
    return values


def pss_kib(pid: int) -> int | None:
    """Pss of one process in KiB, from smaps_rollup (proc(5)). None when it cannot be read: a
    process that runs under a subuid is not readable by the session user. The item does not fail."""
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text(encoding="utf-8").splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def realm_helpers(name: str) -> dict[str, list[int]]:
    """The realm's pids by role: its cgroup tree, its runsv and its ginn-wait.

    runsvdir starts `runsv <entry>` with the directory name, so runsv is matched by the realm name.
    ginn-wait is matched by its parent, which must be that runsv (the run script execs it).
    """
    found: dict[str, list[int]] = {"cgroup": [], "runsv": [], "ginn-wait": []}
    if (CGROUP_ROOT / name).is_dir():
        for procs in (CGROUP_ROOT / name).rglob("cgroup.procs"):
            found["cgroup"] += [int(p) for p in procs.read_text(encoding="utf-8").split()]
    waits: list[tuple[int, int]] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            comm = (proc / "comm").read_text(encoding="utf-8").strip()
            ppid = int((proc / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if len(argv) >= 2 and argv[0].endswith(b"runsv"):
            if argv[1].endswith(name.encode()) and argv[1].rsplit(b"/", 1)[-1] == name.encode():
                found["runsv"].append(int(proc.name))
        elif comm == "ginn-wait":
            waits.append((int(proc.name), ppid))
    found["ginn-wait"] = [pid for pid, ppid in waits if ppid in found["runsv"]]
    return found


def realm_rss(paths: OwnedPaths, name: str) -> dict[str, Any]:
    """The NFR-003 attribution of one realm (ADR-007): its cgroup tree, conmon, runsv, ginn-wait and
    the pause. The pause is ginn's catatonit, found by its pid file, and it is counted once per
    realm. The budget is read from VmRSS (the spec's RSS); Pss is recorded beside it, and a process
    whose Pss cannot be read is counted in pss_unreadable.
    """
    helpers = realm_helpers(name)
    pids = set(helpers["cgroup"]) | set(helpers["runsv"]) | set(helpers["ginn-wait"])
    conmon = paths.runtime / "services" / f"{name}.conmon.pid"
    if conmon.exists():
        pids.add(int(conmon.read_text(encoding="utf-8").strip()))
    pause = paths.runtime / "libpod" / "tmp" / "pause.pid"
    if pause.exists():
        pids.add(int(pause.read_text(encoding="utf-8").strip()))
    totals = {"VmRSS": 0, "RssAnon": 0, "RssFile": 0, "Pss": 0}
    pss_unreadable = 0
    for pid in sorted(pids):
        try:
            sample = rss_kib(pid)
        except (OSError, ValueError):
            continue
        for key in ("VmRSS", "RssAnon", "RssFile"):
            totals[key] += sample[key]
        pss = pss_kib(pid)
        if pss is None:
            pss_unreadable += 1
        else:
            totals["Pss"] += pss
    return {
        "pids": sorted(pids),
        "kib": totals,
        "helpers": helpers,
        "pss_unreadable": pss_unreadable,
    }


def host_memory() -> dict[str, Any]:
    """MemAvailable and the memory pressure line, read before a sample (ADR-007 method notes)."""
    avail = next(
        int(line.split()[1])
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        if line.startswith("MemAvailable:")
    )
    psi = Path("/proc/pressure/memory").read_text(encoding="utf-8").splitlines()[0]
    return {"MemAvailable_kib": avail, "psi": psi}


def leaf_exec(
    paths: OwnedPaths, name: str, command: str, timeout: float = 600.0
) -> tuple[int, str]:
    """Run `command` as container root in the realm, from a process inside the supervisor leaf.

    A session process cannot move into a services-on realm's cgroup: `podman exec` from the session
    fails with EACCES (see the result file). The runsv of the supervisor is inside the leaf, so the
    gate runs the exec as a throwaway runit service (`t77-exec-...`). Its run script writes the
    output and the status of `podman exec` to a temp directory, then sleeps until the gate takes the
    service down and removes it. It returns (status, output).
    """
    tag = f"{PREFIX}exec-{os.urandom(4).hex()}"
    out = Path(tempfile.mkdtemp(prefix="ginn-t77-exec-", dir="/tmp"))  # noqa: S108 - mkdtemp, 0700
    cli = podman_cli(paths).global_argv()
    inner = " ".join(
        shlex.quote(a) for a in [*cli, "exec", container_id(paths, name), "sh", "-c", command]
    )
    script = (
        "#!/bin/sh\n"
        f"export XDG_RUNTIME_DIR={paths.runtime} PATH=/usr/sbin:/usr/bin:/sbin:/bin\n"
        f"{inner} > {shlex.quote(str(out / 'out'))} 2>&1\n"
        f"echo $? > {shlex.quote(str(out / 'rc'))}\n"
        "exec sleep 3600\n"
    )
    svc_tmp = paths.sv / f".{tag}.tmp"
    svc_dir = paths.sv / tag
    svc_tmp.mkdir(mode=0o700)
    (svc_tmp / "run").write_text(script, encoding="utf-8")
    (svc_tmp / "run").chmod(0o755)
    os.rename(svc_tmp, svc_dir)
    try:
        began = time.monotonic()
        while not (out / "rc").exists():
            if time.monotonic() - began > timeout:
                raise TimeoutError(f"leaf exec did not finish in {timeout:.0f} s")
            time.sleep(0.5)
        status = int((out / "rc").read_text(encoding="utf-8").strip())
        return status, (out / "out").read_text(encoding="utf-8", errors="replace")
    finally:
        _retire(svc_dir)
        shutil.rmtree(out, ignore_errors=True)


def _retire(svc_dir: Path) -> None:
    """Take a gate service down, wait for its runsv to exit, then remove its directory.

    Removing the directory while runsv still has `supervise/` open leaves that directory behind, and
    runsvdir restarts the service from it. So runsv exits first, and the directory goes after.
    """
    for verb in ("down", "exit"):
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [SV, verb, str(svc_dir)], capture_output=True, text=True, timeout=60, check=False
        )
    began = time.monotonic()
    while time.monotonic() - began < 30:
        gone = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [SV, "status", str(svc_dir)], capture_output=True, text=True, timeout=30, check=False
        )
        if "not running" in gone.stdout + gone.stderr or "unable to" in gone.stdout + gone.stderr:
            break
        time.sleep(0.5)
    shutil.rmtree(svc_dir, ignore_errors=True)


def wait_run_with_init(
    paths: OwnedPaths, name: str, not_pid: int = 0, timeout: float = 60.0
) -> int:
    """Wait until runsv runs the realm and its container init is up. Return that init's pid.

    `sv status` shows `run` before the container has started, and then the container's pid is 0. A
    kill is sent only to a positive pid that is not `not_pid` (the one already killed). Signalling
    pid 0 would reach the whole process group, so it is never sent.
    """
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        if sv_status(paths, name).startswith("run:"):
            try:
                pid = pid_of(paths, name)
            except (subprocess.CalledProcessError, IndexError, ValueError):
                pid = 0
            if pid > 0 and pid != not_pid:
                return pid
        time.sleep(0.25)
    raise TimeoutError(f"the realm {name} did not run with a container init within {timeout:.0f} s")


def sweep(launcher: Path, paths: OwnedPaths) -> list[str]:
    """Delete every realm whose name starts with PREFIX, with its Realm Home. Returns what is left.

    The `t77-exec-*` services that leaf_exec left in the user's sv directory are retired first
    (`_retire`), with their `.tmp` directories. Empty cgroup leaves of this module are removed too.
    """
    for realm in ginn(launcher, "list").body.get("realms", []):
        name = str(realm.get("name", ""))
        if name.startswith(PREFIX):
            ginn(launcher, "delete", name, "--purge-home", "--yes")
    for exec_dir in sorted(paths.sv.glob(f"{PREFIX}exec-*")):
        if exec_dir.is_dir():
            _retire(exec_dir)
    for stale in sorted(paths.sv.glob(f".{PREFIX}exec-*.tmp")):
        shutil.rmtree(stale, ignore_errors=True)
    for leaf in CGROUP_ROOT.glob(f"{PREFIX}*"):
        # podman and `ginn delete` leave the empty realm cgroup behind (finding F9). Only this
        # module's own empty directories are removed.
        if leaf.is_dir() and not (leaf / "cgroup.procs").read_text(encoding="utf-8").split():
            with contextlib.suppress(OSError):
                leaf.rmdir()
    return [
        str(r.get("name"))
        for r in ginn(launcher, "list").body.get("realms", [])
        if str(r.get("name", "")).startswith(PREFIX)
    ]


@pytest.fixture(scope="module")
def paths() -> OwnedPaths:
    return OwnedPaths.from_env()


def write_launcher(bin_dir: Path) -> Path:
    """Write the gate's `ginn` command into `bin_dir` and return its path (harness only).

    The script imports ginn from this checkout, the one that holds the tests, and fails loudly when
    the import resolves elsewhere. The helper paths point at native/build, and the relay listener
    is `ginn-exec-relay` there, so its worker, run and kill helpers are the built siblings. Realm
    run and finish scripts and the relay's script embed this path (ginn_program), so they run the
    same code. Nothing is written under /usr.
    """
    script = bin_dir / "ginn"
    src = str(REPO / "src")
    script.write_text(
        f"#!{sys.executable}\n"
        "# Host-gate launcher (harness only): ginn from this checkout, helpers from native/build.\n"
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "import ginn\n"
        f"assert ginn.__file__.startswith({src!r}), ginn.__file__\n"
        "from ginn.__main__ import main\n"
        "from ginn.cli import _context\n"
        "from ginn.runtime import attach\n"
        "from ginn.services import exec_relay\n"
        f"NATIVE = {str(NATIVE_BUILD)!r}\n"
        "_context.WAIT_HELPER = NATIVE + '/ginn-wait'\n"
        "attach.DEFAULT_HELPER = NATIVE + '/ginn-rebind'\n"
        "exec_relay.EXEC_RELAY_HELPER = NATIVE + '/ginn-exec-relay'\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


_CREATED: list[str] = []
"""The realms that this module asked `create` for, so its teardown can remove their exec files."""


def remove_exec_leftovers(paths: OwnedPaths, names: Sequence[str], *, exec_existed: bool) -> None:
    """Remove the exec lock and orphan marker of each realm in `names`, and `exec/` if it is empty.

    A lock that an exec still holds is left alone, so no second holder can take it (ADR-014). The
    `exec/` directory goes only when this module found it absent, and it is empty now.
    """
    for name in dict.fromkeys(names):
        realm = RealmName(name)
        if exec_lock_busy(paths, realm):
            continue
        for leftover in (exec_lock_path(paths, realm), exec_orphan_path(paths, realm)):
            with contextlib.suppress(FileNotFoundError):
                leftover.unlink()
    exec_dir = exec_lock_dir(paths)
    if not exec_existed and exec_dir.is_dir() and not any(exec_dir.iterdir()):
        exec_dir.rmdir()


@pytest.fixture(scope="module")
def launcher(paths: OwnedPaths) -> Iterator[Path]:
    """The gate's ginn command: the product CLI with the helper paths of native/build (see the
    docs)."""
    run_tool([tool("make"), "-C", str(REPO / "native")])
    base = Path(tempfile.mkdtemp(prefix="ginn-t77-", dir="/tmp"))  # noqa: S108 - mkdtemp, mode 0700
    bin_dir = base / "bin"
    bin_dir.mkdir(mode=0o700)
    script = write_launcher(bin_dir)
    exec_existed = exec_lock_dir(paths).is_dir()
    _CREATED.clear()
    try:
        yield script
    finally:
        sweep(script, paths)
        remove_exec_leftovers(paths, _CREATED, exec_existed=exec_existed)
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(scope="module", autouse=True)
def no_leftovers(launcher: Path, paths: OwnedPaths) -> Iterator[None]:
    """Stale t77 realms are removed before the module, and its own realms are removed after it.

    The sv directory is snapshotted before the module. The relay service `_exec` is made by this
    module's services-on realms and ginn's sync never removes it (ADR-014), so the teardown removes
    it, with its sync lock, its socket and its ._exec.* temporaries, only when it was absent before
    the module ran. A relay that was there before is left as it was.
    """
    sv_before = {entry.name for entry in paths.sv.iterdir()} if paths.sv.exists() else set()
    exec_present_before = "_exec" in sv_before
    assert sweep(launcher, paths) == [], "stale t77 realms could not be removed before the gate"
    yield
    assert sweep(launcher, paths) == [], "t77 realms could not be removed after the gate"
    if not exec_present_before:
        _retire(exec_service_dir(paths))
        for stale in sorted(paths.sv.glob("._exec.*")):
            shutil.rmtree(stale, ignore_errors=True)
        with contextlib.suppress(OSError):
            (paths.sv / ".exec-relay.lock").unlink()
        with contextlib.suppress(OSError):
            exec_socket(paths).unlink()


def create(launcher: Path, distro: str, name: str, services: str) -> Ran:
    _CREATED.append(name)
    return ginn(launcher, "create", distro, "--name", name, "--services", services)


def create_or_gap(launcher: Path, distro: str, name: str, services: str) -> Ran:
    """`create`. A services-on refusal with GINN-E-NO-INIT on debian or fedora is the known gap F1
    (F3, FR-080), and it raises KnownGapF1, which only an F1 mark absorbs. On any other distribution
    the refusal is returned, so the caller's assertion fails: a NO-INIT there is a plain failure.
    """
    ran = create(launcher, distro, name, services)
    err = ran.body.get("error")
    no_init = isinstance(err, dict) and err.get("code") == "GINN-E-NO-INIT"
    if ran.rc != 0 and no_init and distro in F1_DISTROS:
        raise KnownGapF1(
            f"F1: the image has no /sbin/init, so the create refused: {ran.error_text()}"
        )
    return ran


def delete(launcher: Path, name: str) -> Ran:
    return ginn(launcher, "delete", name, "--purge-home", "--yes")


@pytest.mark.fr("FR-080")
@pytest.mark.fr("NFR-003")
def test_fr080_create_time_forms_on_host(launcher: Path, paths: OwnedPaths) -> None:
    """FR-080 (ADR-003 argv) and NFR-003 (ADR-007 a+b+d): the create argv of a services-on realm.

    Read from the container that the real create made: the crun services flags, the mitigations as
    read-only binds (the journald drop-in and the /dev/null unit masks of SERVICES_MASKED_UNITS),
    and no `--init`. It
    needs
    no start, so it does not depend on the blocked supervised start. It runs on arch, because
    debian's image has no /sbin/init and its create is refused (F1, GINN-E-NO-INIT).
    """
    name = PREFIX + "arch-argv"
    ran = create(launcher, RUN_DISTRO, name, "on")
    assert ran.rc == 0, ran.error_text()
    cid = container_id(paths, name)
    argv = json.loads(podman(paths, ["inspect", "--format", "{{json .Config.CreateCommand}}", cid]))
    assert "--systemd=always" in argv
    assert "--init" not in argv
    assert argv[argv.index("--cgroup-parent") + 1] == f"/ginn/{UID}/{name}"
    assert argv[argv.index("--user") + 1] == "0:0"
    assert argv[argv.index("--stop-signal") + 1] == "SIGRTMIN+3"
    assert argv[argv.index("--stop-timeout") + 1] == "30"
    assert "SYSTEMD_LOG_LEVEL=warning" in argv
    assert argv[-3:-1] == ["--entrypoint", '["/sbin/init"]']
    # ADR-015 Decision 4: a services-on container runs from its derived image, by its bare ID.
    assert "@sha256:" not in argv[-1]
    assert len(argv[-1]) == 64
    binds = [argv[i + 1] for i, word in enumerate(argv) if word == "--mount"]
    assert any(b.endswith("target=/etc/systemd/journald.conf.d/ginn.conf,ro") for b in binds)
    assert len([b for b in binds if "source=/dev/null" in b and b.endswith(",ro")]) == len(
        SERVICES_MASKED_UNITS
    )
    fmt = "{{range .Mounts}}{{.Destination}} rw={{.RW}}\n{{end}}"
    assert "/etc/systemd/journald.conf.d/ginn.conf rw=false" in podman(
        paths, ["inspect", "--format", fmt, cid]
    )
    assert delete(launcher, name).rc == 0


@pytest.mark.fr("FR-084")
def test_fr084_autostart_toggles_the_down_file(launcher: Path, paths: OwnedPaths) -> None:
    """FR-084 AC-1, the file part: autostart on removes the `down` file and off creates it (DR-012).

    The part that starts the realm when runsv starts is the test further down.
    """
    name = PREFIX + "arch-auto"
    assert create(launcher, RUN_DISTRO, name, "on").rc == 0
    down = service_dir(paths, RealmName(name)) / "down"
    assert down.exists(), "a new services-on realm is not started by runit (ADR-003 decision 5)"
    on = ginn(launcher, "autostart", name, "on")
    assert on.rc == 0, on.error_text()
    assert not down.exists()
    off = ginn(launcher, "autostart", name, "off")
    assert off.rc == 0, off.error_text()
    assert down.exists()
    assert delete(launcher, name).rc == 0


@pytest.mark.fr("FR-083")
def test_fr083_services_change_rebuilds_to_systemd_pid1(launcher: Path, paths: OwnedPaths) -> None:
    """FR-083 AC-1: a services-off realm with htop, turned on, restarts as generation 2 with systemd
    as PID 1 and htop still installed. Run on arch, the distribution that boots here.

    The htop install runs on the services-off realm, which starts without systemd. The restart is
    the lazy rebuild, which starts the new generation through runit (ADR-003 amendment, T8.3).
    """
    name = PREFIX + "arch-tog"
    assert create(launcher, "arch", name, "off").rc == 0
    try:
        assert ginn(launcher, "start", name).rc == 0
        assert ginn(launcher, "install", name, "htop").rc == 0
        assert ginn(launcher, "stop", name).rc == 0
        assert ginn(launcher, "services", name, "on").rc == 0
        ran = ginn(launcher, "start", name)
        assert ran.rc == 0, ran.error_text()
        assert ginn(launcher, "status", name).body.get("generation") == 2
        status, pid1 = leaf_exec(paths, name, "cat /proc/1/comm")
        assert status == 0 and pid1.strip() == "systemd", pid1[-300:]
        status, htop = leaf_exec(paths, name, "command -v htop")
        assert status == 0 and htop.strip(), htop[-300:]
    finally:
        ginn(launcher, "stop", name)
        delete(launcher, name)


@pytest.mark.parametrize("distro", FR080_CASES)
@pytest.mark.fr("FR-080")
def test_fr080_systemd_unit_on_crun(launcher: Path, paths: OwnedPaths, distro: str) -> None:
    """FR-080 AC-1 and AC-2 under the crun argv (ADR-003): an enabled unit is active, and is active
    again after a restart. PID 1 is systemd. The init sits under the realm cgroup, and runsv sits in
    the root-owned supervisor leaf. The in-realm steps run from the leaf (see leaf_exec).
    """
    name = PREFIX + distro[:4] + "-svc"
    assert create_or_gap(launcher, distro, name, "on").rc == 0
    try:
        start_ok(launcher, name)
        install, unit = {
            "debian": ("apt-get install -y cron", "cron"),
            "arch": ("pacman -Sy --noconfirm cronie", "cronie"),
            "fedora": ("dnf install -y cronie", "crond"),
        }[distro]
        status, out = leaf_exec(paths, name, install)
        assert status == 0, out[-400:]
        status, out = leaf_exec(
            paths, name, f"systemctl enable --now {unit} && systemctl is-active {unit}"
        )
        assert status == 0 and out.strip().splitlines()[-1] == "active", out[-400:]
        status, facts = leaf_exec(
            paths,
            name,
            "cat /proc/1/comm; systemctl is-system-running; "
            "systemctl --failed --no-legend --plain | cut -d ' ' -f1 | tr '\\n' ' '; "
            "echo; cat /sys/fs/cgroup/cgroup.controllers",
        )
        assert status == 0, facts[-400:]
        print("FR080_FACTS " + json.dumps({"distro": distro, "output": facts.split()}))
        assert facts.split()[0] == "systemd"
        init_cgroup = Path(f"/proc/{pid_of(paths, name)}/cgroup").read_text(encoding="utf-8")
        assert f"/ginn/{UID}/{name}/" in init_cgroup
        runsv = realm_helpers(name)["runsv"]
        assert runsv, "no runsv is running for the realm"
        runsv_cgroup = Path(f"/proc/{runsv[0]}/cgroup").read_text(encoding="utf-8").strip()
        assert runsv_cgroup.endswith(f"/ginn/{UID}/supervisor"), runsv_cgroup
        stopped = ginn(launcher, "stop", name)
        assert stopped.rc == 0, stopped.error_text()
        start_ok(launcher, name)
        status, out = leaf_exec(paths, name, f"systemctl is-active {unit}")
        assert status == 0 and out.strip() == "active", out[-400:]
    finally:
        ginn(launcher, "stop", name)
        delete(launcher, name)


@pytest.fixture
def running_realm(launcher: Path, paths: OwnedPaths) -> Iterator[str]:
    """A services-on arch realm, started. The start must succeed (start_ok): a failed start is an
    error of this fixture, and no item of the arch realm is skipped for it."""
    name = PREFIX + "arch-run"
    assert create(launcher, RUN_DISTRO, name, "on").rc == 0
    try:
        start_ok(launcher, name)
        yield name
    finally:
        ginn(launcher, "stop", name)
        delete(launcher, name)


@pytest.mark.fr("FR-081")
def test_fr081_ac1_runit_shows_the_realm_up(running_realm: str, paths: OwnedPaths) -> None:
    """FR-081 AC-1: `sv status` on the realm's supervision entry shows it up."""
    assert sv_status(paths, running_realm).startswith("run:")


@pytest.mark.fr("FR-081")
@pytest.mark.fr("NFR-033")
def test_fr081_ac2_a_killed_init_restarts_within_10_s(
    launcher: Path, running_realm: str, paths: OwnedPaths
) -> None:
    """FR-081 AC-2 (NFR-033): kill the container's init. Within 10 s of the kill, runsv has
    restarted `run` with a new run pid, a new container init runs, and `ginn status` reads running.

    The clock starts at the kill. The restart is a new run pid in `sv status` (the ginn-wait that
    runsv supervises), then a new container init (wait_run_with_init, not the killed pid), then
    the realm record. The elapsed time is read once all three hold.
    """
    old_init = wait_run_with_init(paths, running_realm)
    old_run = parse_status(sv_status(paths, running_realm)).pid
    os.kill(old_init, signal.SIGKILL)
    began = time.monotonic()
    new_run = None
    while time.monotonic() - began < 30:
        status = parse_status(sv_status(paths, running_realm))
        if status.pid is not None and status.pid != old_run and status.state == "run":
            new_run = status.pid
            break
        time.sleep(0.25)
    assert new_run is not None, "runsv did not restart run within 30 s"
    new_init = wait_run_with_init(paths, running_realm, not_pid=old_init, timeout=30.0)
    # The record reads failed while the crash is recorded and the restart's start runs, and it
    # reads running once that start has attached (lifecycle.py, service.py). So the state is polled
    # inside the window, and the elapsed time is read when it reads running.
    state = ""
    while True:
        state = str(ginn(launcher, "status", running_realm).body.get("state"))
        elapsed = time.monotonic() - began
        if state == "running" or elapsed >= 10:
            break
        time.sleep(0.5)
    print(
        f"FR081_AC2_RESTART_S {elapsed:.2f} old_run={old_run} new_run={new_run} "
        f"new_init={new_init} state={state}"
    )
    assert state == "running", f"ginn status reads {state} within 10 s of the kill"
    assert elapsed <= 10, f"restart took {elapsed:.1f} s"


@pytest.mark.fr("FR-081")
def test_fr081_ac3_five_crashes_in_60_s_give_up(
    launcher: Path, paths: OwnedPaths, running_realm: str
) -> None:
    """FR-081 AC-3 (AS-028): five kills within 60 s leave the realm down, and `ginn status` says
    failed.

    Each kill waits for a fresh, running container init (wait_run_with_init). The window is
    60 s from the first kill.
    """
    began = time.monotonic()
    last = 0
    for _ in range(5):
        last = wait_run_with_init(paths, running_realm, not_pid=last)
        os.kill(last, signal.SIGKILL)
    while time.monotonic() - began < 60 and not sv_status(paths, running_realm).startswith("down"):
        time.sleep(0.25)
    print(f"FR081_AC3_GIVE_UP_S {time.monotonic() - began:.2f}")
    assert time.monotonic() - began <= 60, "five kills did not give up within 60 s"
    time.sleep(15)
    assert parse_status(sv_status(paths, running_realm)).state == "down"
    state = str(ginn(launcher, "status", running_realm).body.get("state"))
    if state != "failed":
        assert state == "failed", f"five crashes left the realm {state}, not failed"


@pytest.mark.fr("FR-084")
def test_fr084_autostart_starts_the_realm_when_runsv_starts(
    launcher: Path, paths: OwnedPaths
) -> None:
    """FR-084 AC-1 without a reboot: with autostart on, a new runsv (the user's supervisor starting)
    starts the realm with no user action. `sv exit` ends the runsv, and runsvdir starts a new one.
    The realm counts as started only when its record reads running, because `sv status` shows `run`
    for a moment before a failed start.
    """
    name = PREFIX + "arch-boot"
    assert create(launcher, RUN_DISTRO, name, "on").rc == 0
    try:
        assert ginn(launcher, "autostart", name, "on").rc == 0
        exited = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [SV, "exit", str(service_dir(paths, RealmName(name)))],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert exited.returncode in (0, 1), exited.stderr
        began = time.monotonic()
        state = ""
        while time.monotonic() - began < 40:
            state = str(ginn(launcher, "status", name).body.get("state"))
            if state == "running":
                break
            time.sleep(1)
        print(f"FR084_AC1_START_S {time.monotonic() - began:.2f} state={state}")
        assert state == "running", f"runsv did not start the realm (state {state})"
    finally:
        ginn(launcher, "stop", name)
        delete(launcher, name)


def relay_listener_rss() -> dict[str, int]:
    """The user's ginn-exec-relay listener, when it runs: VmRSS and Pss in KiB, by comm and uid.

    With no listener, or more than one, the sample is zero and the count says why (ADR-007).
    """
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (
                entry.stat().st_uid != UID
                or (entry / "comm").read_text(encoding="utf-8").strip() != "ginn-exec-relay"
            ):
                continue
        except OSError:
            continue
        found.append(int(entry.name))
    if len(found) != 1:
        return {"count": len(found), "pid": 0, "VmRSS": 0, "Pss": 0}
    try:
        vm = rss_kib(found[0])["VmRSS"]
    except (OSError, ValueError):
        return {"count": 1, "pid": found[0], "VmRSS": 0, "Pss": 0}
    return {"count": 1, "pid": found[0], "VmRSS": vm, "Pss": pss_kib(found[0]) or 0}


@pytest.mark.parametrize("distro", NFR003_CASES)
@pytest.mark.fr("NFR-003")
def test_nfr003_idle_rss_of_a_services_on_realm(
    launcher: Path, paths: OwnedPaths, distro: str
) -> None:
    """NFR-003: idle RSS of a services-on realm after 60 s with the ADR-003 argv, median of runs.

    The sample count is GINN_T77_SAMPLES (default 10, the roadmap count). Each run starts the realm,
    settles it for 60 s, and samples realm_rss, with MemAvailable and the memory pressure line read
    just before. The pause is counted once per realm (ADR-007). The median is VmRSS; each row also
    carries the Pss total beside it.
    """
    name = PREFIX + distro[:4] + "-rss"
    assert create_or_gap(launcher, distro, name, "on").rc == 0
    samples = int(os.environ.get("GINN_T77_SAMPLES", "10"))
    rows: list[dict[str, Any]] = []
    try:
        for index in range(samples):
            start_ok(launcher, name)
            time.sleep(60)
            host = host_memory()
            attribution = realm_rss(paths, name)
            rows.append(
                {
                    "run": index + 1,
                    "host": host,
                    **attribution["kib"],
                    "pss_unreadable": attribution["pss_unreadable"],
                    "pids": attribution["pids"],
                }
            )
            ginn(launcher, "stop", name)
    finally:
        delete(launcher, name)
    totals = sorted(int(row["VmRSS"]) for row in rows)
    median_mib = totals[len(totals) // 2] / 1024
    print("NFR003_SAMPLES " + json.dumps({"distro": distro, "rows": rows}, sort_keys=True))
    print(f"NFR003_MEDIAN_MIB {distro} {median_mib:.2f} MAX_MIB {totals[-1] / 1024:.2f}")
    assert median_mib <= 40, f"NFR-003: VmRSS median {median_mib:.2f} MiB is over the 40 MiB budget"


@pytest.mark.fr("NFR-021")
def test_nfr021_five_idle_services_on_realms(launcher: Path, paths: OwnedPaths) -> None:
    """NFR-021 (Should): five idle services-on realms running together, on arch. The pause is
    counted once per realm (ADR-007), so the total is the sum of the five attributions. The budget
    is read from VmRSS; the per-realm Pss is recorded beside it (F11)."""
    names = [PREFIX + f"arch-five{i}" for i in range(5)]
    per_realm: dict[str, dict[str, Any]] = {}
    try:
        for name in names:
            assert create(launcher, RUN_DISTRO, name, "on").rc == 0
        for name in names:
            start_ok(launcher, name)
        time.sleep(60)
        per_realm = {name: realm_rss(paths, name) for name in names}
    finally:
        for name in names:
            delete(launcher, name)
    relay = relay_listener_rss()
    vmrss_mib = (sum(r["kib"]["VmRSS"] for r in per_realm.values()) + relay["VmRSS"]) / 1024
    pss_mib = (sum(r["kib"]["Pss"] for r in per_realm.values()) + relay["Pss"]) / 1024
    print(
        "NFR021_TOTAL "
        + json.dumps(
            {
                "per_realm_vmrss_kib": {n: r["kib"]["VmRSS"] for n, r in per_realm.items()},
                "per_realm_pss_kib": {n: r["kib"]["Pss"] for n, r in per_realm.items()},
                "pss_unreadable": {n: r["pss_unreadable"] for n, r in per_realm.items()},
                "relay_listener": relay,
                "vmrss_total_mib": round(vmrss_mib, 2),
                "pss_total_mib": round(pss_mib, 2),
            }
        )
    )
    assert vmrss_mib <= 5 * 40, (
        f"NFR-021: five realms and the relay total {vmrss_mib:.2f} MiB (VmRSS) against 200"
    )


@pytest.mark.fr("FR-080")
@pytest.mark.fr("NFR-003")
def test_logind_mask_against_the_fr080_apps(launcher: Path, paths: OwnedPaths) -> None:
    """ADR-007 mitigation b masks systemd-logind.service by default (CL-28, the S-05 logind file).

    The mask is the create-time read-only bind of /dev/null over the unit path, not a
    `systemctl mask` symlink, and `systemctl mask` would refuse to replace a bind mount. So the
    test does not run it. It reads the mask back on the first start (is-enabled `masked`, is-active
    `inactive`), then restarts the realm, and checks that the FR-080 apps still run with logind
    masked."""
    name = PREFIX + "arch-logind"
    assert create(launcher, RUN_DISTRO, name, "on").rc == 0
    try:
        start_ok(launcher, name)
        status, out = leaf_exec(paths, name, "pacman -Sy --noconfirm cronie")
        assert status == 0, out[-400:]
        status, out = leaf_exec(
            paths,
            name,
            "echo ENABLED=$(systemctl is-enabled systemd-logind); "
            "echo ACTIVE=$(systemctl is-active systemd-logind)",
        )
        assert "ENABLED=masked" in out.split(), out[-300:]
        assert "ACTIVE=inactive" in out.split(), out[-300:]
        assert ginn(launcher, "stop", name).rc == 0
        start_ok(launcher, name)
        status, out = leaf_exec(
            paths,
            name,
            "systemctl enable --now cronie >/dev/null 2>&1; "
            "echo CRON=$(systemctl is-active cronie); "
            "echo LOGIND=$(systemctl is-active systemd-logind); "
            "echo FAILED=$(systemctl --failed --no-legend --plain | wc -l)",
        )
        print("LOGIND_MASK_FACTS " + json.dumps({"output": out.split()}))
        assert status == 0, out[-300:]
        assert "CRON=active" in out.split(), out[-300:]
    finally:
        ginn(launcher, "stop", name)
        delete(launcher, name)


@pytest.mark.fr("NFR-002")
@pytest.mark.fr("NFR-003")
def test_nfr002_helper_rss_standalone(paths: OwnedPaths) -> None:
    """NFR-002 and NFR-003 count the helpers. Each one runs alone, idle, for 60 s, and its VmRSS,
    RssAnon and RssFile are read. The helper's podman arguments name ginn's own storage.

    The runsv is taken down with `sv down` and `sv exit` (`_retire`), and every process is waited
    on before the test returns.
    """
    base = Path(tempfile.mkdtemp(prefix="ginn-t77-helpers-", dir="/tmp"))  # noqa: S108
    svc = base / "svc"
    procs: dict[str, subprocess.Popen[bytes]] = {}
    quiet: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    # The helpers take the podman path first, then its global flags (native/ginn-idle.c, ginn-
    # wait.c).
    global_args = podman_cli(paths).global_argv()
    init = subprocess.Popen(["sleep", "900"])  # noqa: S603, S607
    conmon = subprocess.Popen(["sleep", "900"])  # noqa: S603, S607
    try:
        svc.mkdir()
        (svc / "run").write_text("#!/bin/sh\nexec sleep 900\n", encoding="utf-8")
        (svc / "run").chmod(0o755)
        (base / "watch").mkdir()
        pidfile = base / "conmon.pid"
        pidfile.write_text(str(conmon.pid), encoding="utf-8")
        procs["runsv"] = subprocess.Popen([tool("runsv"), str(svc)], **quiet)  # noqa: S603
        procs["ginn-watch"] = subprocess.Popen(  # noqa: S603
            [str(NATIVE_BUILD / "ginn-watch"), str(base / "watch"), "1000", "5000", "/bin/true"],
            **quiet,
        )
        procs["ginn-idle"] = subprocess.Popen(  # noqa: S603
            [
                str(NATIVE_BUILD / "ginn-idle"),
                str(init.pid),
                "3600",
                *global_args,
                "--",
                "t77-nocontainer",
            ],
            **quiet,
        )
        procs["ginn-wait"] = subprocess.Popen(  # noqa: S603
            [str(NATIVE_BUILD / "ginn-wait"), str(pidfile), *global_args, "--", "t77-nocontainer"],
            **quiet,
        )
        time.sleep(60)
        report = {name: rss_kib(p.pid) for name, p in procs.items() if p.poll() is None}
        assert set(report) == set(procs), f"a helper exited: {sorted(set(procs) - set(report))}"
        print("HELPER_RSS_KIB " + json.dumps(report, sort_keys=True))
    finally:
        if svc.exists():
            _retire(svc)
        for proc in procs.values():
            proc.kill()
        init.kill()
        conmon.kill()
        for proc in (*procs.values(), init, conmon):
            proc.wait(timeout=30)
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skip(
    reason="FR-085 is deferred (CL-2): the Docker-in-realm Part B re-run has no FR to prove"
)
@pytest.mark.fr("FR-085")
def test_fr085_dockerd_in_a_services_on_development_realm() -> None:
    raise AssertionError("not run: FR-085 is deferred")


@pytest.mark.host_musl
@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-081")
def test_fr080_on_musl() -> None:
    """The musl leg of FR-080 and FR-081 (S-03). conftest.py skips this test on glibc hosts."""
    raise AssertionError("the musl leg needs a musl host")
