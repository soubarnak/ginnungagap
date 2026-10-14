# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501 - the gate measurement lines (T716) are long by design, one per value.
"""T7.16 host gates for ADR-014: the services-on exec relay and the give-up state (FR-081..083).

Run one gate at a time on the Void glibc host, with rootless Podman and the user's runit supervisor
live (`ginn doctor`). Select a gate by its exact test name, because -k matches substrings:

    GINN_INTEGRATION=1 PYTHONPATH=<checkout>/src .venv/bin/pytest \
        "tests/integration/test_exec_relay_real.py::test_g1_exec_root_goes_through_the_relay" -s

Every realm is named `t716-...`. The module removes each realm with its Realm Home, and the relay
service `_exec` with its lock file, socket and temporaries when `_exec` was absent from the sv
directory before the module ran (that listing is taken in the launcher fixture). A relay that was
there before is left as it was. It also removes the relay's runtime files of this module, and every
share and temporary directory it made. The CLI runs through the launcher of test_services_real.py, which imports ginn from this
checkout and points the helpers at native/build. Measurements are printed as lines that start with
`T716`, and the results file is written from them by hand (spikes/results/T7.16-relay-gates.md).

Relay-route execs go through PodmanCli.exec with an explicit ExecTarget, in a child process, so the
test can kill the client without killing itself. Session execs go through the product CLI.

G8 SIGKILLs this user's shared ginn-exec-relay listener, which every services-on realm of the user
uses. It is skipped while non-test services-on realms exist, so their relay is not disturbed.

Signals go only to processes of this user that the test started, or that it found through its own
realm cgroup, its own children, or the one ginn-exec-relay of this user.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import RealmName
from ginn.runtime._enter_plan import exec_user
from ginn.services._exec_lock import exec_lock_busy, exec_lock_path, exec_orphan_path
from ginn.services.exec_relay import exec_service_dir, exec_socket
from ginn.services.supervisor import UP_WAIT_S
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmStore
from tests.integration.conftest import REPO, run_tool, tool
from tests.integration.test_services_real import (
    PODMAN,
    SV,
    _retire,
    create,
    delete,
    pid_of,
    pss_kib,
    realm_helpers,
    rss_kib,
    sv_status,
    wait_run_with_init,
    write_launcher,
)
from tests.integration.test_services_real import ginn as ginn_run
from tests.integration.test_services_real import start_ok as start_realm

PREFIX = "t716-"
RUN_DISTRO = "arch"
UID = os.getuid()
GID = os.getgid()
ROOT = "0:0"
EXEC_BIN = "ginn-exec-relay"
"""The listener's comm. The kernel keeps 15 characters, and this name is exactly 15."""
RUN_COMM = "ginn-exec-run"
PODMAN_COMM = "podman"
SPAWN_S = 60.0
"""How long a background command may take to reach the step that a gate waits for."""
SESSION_DROP = (
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "PULSE_SERVER",
    "SSH_AUTH_SOCK",
)
"""Session variables that the stripped session leaves out (session grants, ADR-008)."""
_SPAWNED: list[subprocess.Popen[str]] = []
_TTYS: list[Tty] = []
_SHARES: list[Path] = []
_EMPTY_RUNTIME: list[Path] = []
_FACTS: dict[str, Any] = {}
T716_1 = (
    "T716-1: a services-on container's /run/user/<uid> is root-owned (systemd mounts /run over the "
    "ginn tmpfs), so a session grant attach fails with GINN-E-ATTACH-FAILED"
)
T716_2 = (
    "T716-2: the --root token kill cannot read the environ of a container-root process, so it finds "
    "no tokened process, releases the lock early and writes no orphan marker"
)


class KnownGapT716One(AssertionError):
    """The failure of T716-1 (a session grant attach in a services-on container). Only the G1 and G5
    places raise it, when their own signature shows. Any other failure stays a plain failure."""


class KnownGapT716Two(AssertionError):
    """The failure of T716-2 (the --root token kill). Only the --root item raises it."""


def T716(gate: str, line: str) -> None:
    """One measurement line, printed for the results file (run with -s)."""
    print(f"T716 {gate} {line}", flush=True)


def env_for(launcher: Path, *, stripped: bool = False) -> dict[str, str]:
    """The environment of a ginn command: the launcher's directory first on PATH, as ginn() does.

    `stripped` is the deviation of G3 and G6: the host XDG_RUNTIME_DIR is an empty directory of this
    test, and the session sockets are unset, so the enter attach has no session grant under
    /run/user/<uid> (the attach bug recorded under G1). ginn's own runtime root never reads it.
    """
    base = {k: v for k, v in os.environ.items() if not (stripped and k in SESSION_DROP)}
    if stripped:
        if not _EMPTY_RUNTIME:
            empty = Path(tempfile.mkdtemp(prefix="t716-xdg-", dir="/tmp"))  # noqa: S108 - mkdtemp, 0700
            _EMPTY_RUNTIME.append(empty)
        base["XDG_RUNTIME_DIR"] = str(_EMPTY_RUNTIME[0])
    return {**base, "PATH": f"{launcher.parent}{os.pathsep}{os.environ.get('PATH', '')}"}


def spawn(launcher: Path, *args: str) -> subprocess.Popen[str]:
    """Start one ginn command in the background, with --json, and keep it for the teardown."""
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(launcher), "--json", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env_for(launcher),
        cwd="/tmp",  # noqa: S108 - a working directory, not a temp file
    )
    _SPAWNED.append(proc)
    return proc


def finish(proc: subprocess.Popen[str], timeout: float = 900.0) -> tuple[int, dict[str, Any]]:
    """Wait for a spawned ginn command and return (exit status, its last JSON object)."""
    out, err = proc.communicate(timeout=timeout)
    lines = (out + "\n" + err).splitlines()
    objects = [json.loads(line) for line in lines if line.startswith("{")]
    body = next((o for o in reversed(objects) if "progress" not in o), {})
    return int(proc.returncode), body


def codes(body: dict[str, Any]) -> list[str]:
    """The warning and error codes of one JSON body."""
    found = [str(w.get("code")) for w in body.get("warnings", []) if isinstance(w, dict)]
    err = body.get("error")
    if isinstance(err, dict):
        found.append(str(err.get("code")))
    return found


def exec_cli(launcher: Path, name: str, *argv: str, root: bool = False) -> tuple[int, str, str]:
    """`ginn exec` in a realm, through the product CLI. Its command writes to the stdio it gets."""
    args = [str(launcher), "exec", *(["--root"] if root else []), name, "--", *argv]
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        args,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env_for(launcher),
        cwd="/tmp",  # noqa: S108 - a working directory, not a temp file
        timeout=300,
        check=False,
    )
    return done.returncode, done.stdout, done.stderr


def wait_for(predicate: Callable[[], bool], timeout: float, step: float = 0.1) -> bool:
    """Poll `predicate` until it is true or `timeout` passes. Returns the last answer."""
    began = time.monotonic()
    while True:
        if predicate():
            return True
        if time.monotonic() - began >= timeout:
            return False
        time.sleep(step)


def _mine(pid: int) -> bool:
    """True when /proc/<pid> belongs to this user. Only such processes are looked at or signalled."""
    try:
        return Path(f"/proc/{pid}").stat().st_uid == UID
    except OSError:
        return False


def procs() -> list[tuple[int, str, int, bytes]]:
    """This user's processes as (pid, comm, ppid, cmdline). A process that vanishes is skipped."""
    found: list[tuple[int, str, int, bytes]] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or not _mine(int(proc.name)):
            continue
        try:
            comm = (proc / "comm").read_text(encoding="utf-8").strip()
            stat = (proc / "stat").read_text(encoding="utf-8")
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            cmd = (proc / "cmdline").read_bytes()
        except (OSError, ValueError, IndexError):
            continue
        found.append((int(proc.name), comm, ppid, cmd))
    return found


def pids_named(comm: str) -> list[int]:
    return [pid for pid, name, _, _ in procs() if name == comm]


def children_named(parent: int, comm: str) -> list[int]:
    return [pid for pid, name, ppid, _ in procs() if ppid == parent and name == comm]


def comm_of(pid: int | None) -> str | None:
    if pid is None:
        return None
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def ppid_of(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    return int(stat.rsplit(")", 1)[1].split()[1])


def listener_or_none() -> int | None:
    found = pids_named(EXEC_BIN)
    return found[0] if len(found) == 1 else None


def listener() -> int:
    """The one ginn-exec-relay of this user. Two would mean a second listener, which is a failure."""
    found = pids_named(EXEC_BIN)
    assert len(found) == 1, f"expected one {EXEC_BIN}, found {found}"
    return found[0]


def run_under_listener() -> int:
    """The ginn-exec-run that is a child of the listener (the worker after its exec), or 0."""
    relay_pid = listener_or_none()
    if relay_pid is None:
        return 0
    found = children_named(relay_pid, RUN_COMM)
    return found[0] if found else 0


def realm_pids_with(name: str, needle: bytes) -> list[int]:
    """Processes of the realm's cgroup tree whose cmdline holds `needle` (found by its cgroup)."""
    found: list[int] = []
    for pid in realm_helpers(name)["cgroup"]:
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmd:
            found.append(pid)
    return found


def environ_of(pid: int) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if b"=" in item:
            key, _, value = item.partition(b"=")
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def relay_service_state(paths: OwnedPaths) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [SV, "status", str(exec_service_dir(paths))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return (done.stdout.strip().splitlines() or [""])[0]


def podman_argv(paths: OwnedPaths) -> list[str]:
    from ginn.podman.cli import PodmanCli  # lazy: the import is only for the global flags

    return PodmanCli.from_paths(paths, binary=PODMAN, oci_runtime="crun").global_argv()


def podman_run(paths: OwnedPaths, args: list[str], env: dict[str, str] | None = None) -> Any:
    """One podman call through ginn's own global flags. check=False, so the caller reads the result."""
    if env is None:
        env = {"XDG_RUNTIME_DIR": str(paths.runtime), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*podman_argv(paths), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


def full_container_id(paths: OwnedPaths, name: str) -> str:
    """The 64-character id of the realm's container. The relay accepts only the full id (ADR-014)."""
    done = podman_run(
        paths,
        [
            "ps",
            "-a",
            "--no-trunc",
            "--filter",
            f"label=io.ginn.realm={name}",
            "--format",
            "{{.ID}}",
        ],
    )
    assert done.returncode == 0, done.stderr[-300:]
    found = done.stdout.split()
    assert len(found) == 1 and len(found[0]) == 64, found
    return found[0]


def podman_clients(paths: OwnedPaths, name: str) -> list[int]:
    """The podman processes that run `exec` in the realm's container, found by the container id."""
    cid = full_container_id(paths, name).encode()
    found: list[int] = []
    for pid, comm, _, cmd in procs():
        argv = cmd.split(b"\0")
        if comm == PODMAN_COMM and b"exec" in argv and any(cid in arg for arg in argv):
            found.append(pid)
    return found


EXEC_SCRIPT = """
import json, sys
from ginn.podman.adapter import ExecTarget
from ginn.podman.cli import PodmanCli
from ginn.store.paths import OwnedPaths
realm, cid, user, mutating, podman = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1", sys.argv[5]
argv = json.loads(sys.argv[6])
cli = PodmanCli.from_paths(OwnedPaths.from_env(), binary=podman, oci_runtime="crun")
target = ExecTarget(realm=realm, services=True, mutating=mutating)
result = cli.exec(cid, argv, target=target, user=user, capture=True)
print(json.dumps({"exit": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}), flush=True)
"""
"""One exec through the adapter, with the services-on target (ADR-014, Decision 1). It runs in a
child process, so a test can kill its client, and the relay sees EOF before EXIT."""


def relay_popen(
    launcher: Path,
    paths: OwnedPaths,
    name: str,
    argv: list[str],
    *,
    user: str = ROOT,
    mutating: bool = False,
) -> subprocess.Popen[str]:
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-c",
            EXEC_SCRIPT,
            name,
            full_container_id(paths, name),
            user,
            "1" if mutating else "0",
            PODMAN,
            json.dumps(argv),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**env_for(launcher), "PYTHONPATH": str(REPO / "src")},
        cwd="/tmp",  # noqa: S108 - a working directory, not a temp file
    )
    _SPAWNED.append(proc)
    return proc


def relay_exec(
    launcher: Path,
    paths: OwnedPaths,
    name: str,
    *argv: str,
    user: str = ROOT,
    mutating: bool = False,
) -> tuple[int, str, str]:
    """One relay-route exec, run to its end. Returns (exit status, stdout, stderr). A ginn-side
    failure comes back as the child's status with its stderr, which carries the GINN-E code."""
    proc = relay_popen(launcher, paths, name, list(argv), user=user, mutating=mutating)
    out, err = proc.communicate(timeout=900)
    if proc.returncode != 0:
        return int(proc.returncode), "", err
    body = json.loads(out.strip().splitlines()[-1])
    return int(body["exit"]), str(body["stdout"]), str(body["stderr"])


ARCH_REFRESH = ("pacman", "--sync", "--refresh", "--sysupgrade", "--noconfirm")
"""The arch refresh argv (distros/data/arch.toml). Realm creation runs it (CL-9), but a services-on
create does not (CL-18), so a fresh services-on realm has no sync database until a refresh. The
gates that install into one run this argv first, through the relay, as a precondition."""


def refresh_through_relay(launcher: Path, paths: OwnedPaths, name: str) -> None:
    rc, _out, err = relay_exec(launcher, paths, name, *ARCH_REFRESH, mutating=True)
    T716(
        "precondition",
        f"refresh_through_relay realm={name} rc={rc} stderr_tail={err.strip()[-160:]!r}",
    )
    assert rc == 0, err[-300:]


def lock_free(paths: OwnedPaths, name: str) -> bool:
    return not exec_lock_busy(paths, RealmName(name))


def lock_holder(paths: OwnedPaths, name: str) -> str:
    try:
        return exec_lock_path(paths, RealmName(name)).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def audit_entries(paths: OwnedPaths, name: str) -> list[dict[str, Any]]:
    """The Audit Log lines of one realm, oldest first (DR-035)."""
    try:
        lines = paths.audit_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    found: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(ValueError):
            entry = json.loads(line)
            if entry.get("realm") == name:
                found.append(entry)
    return found


_STEP_RE = re.compile(r'"step"\s*:\s*"([a-z-]+)"')


class StepWatch:
    """Polls the realm's journal file and records each step name it sees (FR-122, ADR-014)."""

    def __init__(self, paths: OwnedPaths, name: str) -> None:
        self._file = paths.journal / f"{name}.json"
        self.seen: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> StepWatch:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                text = self._file.read_text(encoding="utf-8")
            except OSError:
                text = ""
            for step in _STEP_RE.findall(text):
                if step not in self.seen:
                    self.seen.append(step)
            time.sleep(0.02)


def _set_ctty() -> None:
    """Runs in the child after setsid and the descriptor copies: the slave becomes the ctty."""
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class Tty:
    """A ginn command with a controlling terminal, driven from the master side (FR-048, CL-27)."""

    def __init__(self, launcher: Path, *args: str, stripped: bool = False) -> None:
        self.master, self.slave = os.openpty()
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [str(launcher), *args],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
            preexec_fn=_set_ctty,  # noqa: PLW1509 - the child only makes the tty its controlling tty
            env=env_for(launcher, stripped=stripped),
            cwd="/tmp",  # noqa: S108 - a working directory, not a temp file
            close_fds=True,
        )
        self.buf = ""
        _TTYS.append(self)

    def send(self, text: str) -> None:
        os.write(self.master, text.encode("utf-8"))

    def read_until(self, needle: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and needle not in self.buf:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.buf += chunk.decode("utf-8", "replace")
        return self.buf

    def resize(self, rows: int, cols: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def termios_flags(self) -> tuple[bool, bool]:
        """(ICANON, ECHO) of the terminal, read from the slave side that the command shares."""
        lflag = termios.tcgetattr(self.slave)[3]
        return bool(lflag & termios.ICANON), bool(lflag & termios.ECHO)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=30)
        for fd in (self.master, self.slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def services_realm(launcher: Path, name: str, distro: str = RUN_DISTRO) -> None:
    """Create a services-on realm and start it. The caller removes it with drop_realm."""
    assert create(launcher, distro, name, "on").rc == 0, name
    start_realm(launcher, name)


def drop_realm(launcher: Path, name: str) -> None:
    ginn_run(launcher, "stop", name)
    delete(launcher, name)


@contextlib.contextmanager
def running_realm(launcher: Path, name: str) -> Iterator[str]:
    services_realm(launcher, name)
    try:
        yield name
    finally:
        drop_realm(launcher, name)


def sweep_prefix(launcher: Path) -> list[str]:
    """Delete every realm whose name starts with PREFIX, and return the names left."""
    for realm in ginn_run(launcher, "list").body.get("realms", []):
        name = str(realm.get("name", ""))
        if name.startswith(PREFIX):
            ginn_run(launcher, "delete", name, "--purge-home", "--yes")
    return [
        str(r.get("name"))
        for r in ginn_run(launcher, "list").body.get("realms", [])
        if str(r.get("name", "")).startswith(PREFIX)
    ]


def sv_listing(paths: OwnedPaths) -> list[str]:
    return sorted(p.name for p in paths.sv.iterdir()) if paths.sv.exists() else []


def runtime_listing(paths: OwnedPaths) -> list[str]:
    return sorted(p.name for p in paths.runtime.iterdir()) if paths.runtime.exists() else []


@pytest.fixture(scope="module")
def paths() -> OwnedPaths:
    return OwnedPaths.from_env()


@pytest.fixture(scope="module")
def launcher(paths: OwnedPaths) -> Iterator[Path]:
    """The ginn command of this module (test_services_real.write_launcher), with the same seams."""
    run_tool([tool("make"), "-C", str(REPO / "native")])
    base = Path(tempfile.mkdtemp(prefix="ginn-t716-", dir="/tmp"))  # noqa: S108 - mkdtemp, 0700
    bin_dir = base / "bin"
    bin_dir.mkdir(mode=0o700)
    script = write_launcher(bin_dir)
    _FACTS["sv_before"] = sv_listing(paths)
    _FACTS["runtime_before"] = runtime_listing(paths)
    _FACTS["realms_before"] = [
        str(r.get("name")) for r in ginn_run(script, "list").body.get("realms", [])
    ]
    _FACTS["cgroup_of_pytest"] = Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
    try:
        yield script
    finally:
        teardown(script, paths)
        shutil.rmtree(base, ignore_errors=True)


def teardown(launcher: Path, paths: OwnedPaths) -> None:
    """Remove every process, realm, share and service this module made, and record what is left."""
    for tty in _TTYS:
        tty.close()
    for proc in _SPAWNED:
        if proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=30)
    _FACTS["realms_left"] = sweep_prefix(launcher)
    for share in _SHARES:
        shutil.rmtree(share, ignore_errors=True)
    for empty in _EMPTY_RUNTIME:
        shutil.rmtree(empty, ignore_errors=True)
    _FACTS["other_realms_seen"] = [
        str(r.get("name"))
        for r in ginn_run(launcher, "list").body.get("realms", [])
        if not str(r.get("name", "")).startswith(PREFIX)
    ]
    # The decision is the sv listing taken before the module ran, not the realms left (ADR-014).
    if "_exec" not in _FACTS.get("sv_before", []):
        _retire(exec_service_dir(paths))
        for stale in sorted(paths.sv.glob("._exec.*")):
            shutil.rmtree(stale, ignore_errors=True)
        with contextlib.suppress(OSError):
            (paths.sv / ".exec-relay.lock").unlink()
        if listener_or_none() is None:
            with contextlib.suppress(OSError):
                exec_socket(paths).unlink()
            lock_dir = paths.runtime / "exec"
            if lock_dir.is_dir():
                for entry in lock_dir.iterdir():
                    if entry.name.startswith(PREFIX):
                        with contextlib.suppress(OSError):
                            entry.unlink()
                with contextlib.suppress(OSError):
                    lock_dir.rmdir()
    _FACTS["sv_after"] = sv_listing(paths)
    _FACTS["runtime_after"] = runtime_listing(paths)
    print(f"T716 teardown {json.dumps(_FACTS, default=str, sort_keys=True)}", flush=True)


# ----------------------------------------------------------------------------------------------
# G1: FR-083 AC-1, `ginn exec --root` through the relay (the F2 gate)
# ----------------------------------------------------------------------------------------------


@pytest.mark.fr("FR-083")
@pytest.mark.fr("FR-049")
@pytest.mark.xfail(strict=True, reason=T716_1, raises=KnownGapT716One)
def test_g1_exec_root_goes_through_the_relay(launcher: Path, paths: OwnedPaths) -> None:
    """`ginn exec --root arch -- cat /proc/1/comm` on a services-on realm, with the session's own
    environment. The relay route is entered; the session grant attach that `ginn exec` runs first
    fails on the services-on container (the G1 finding in the results file)."""
    name = PREFIX + "g1"
    with running_realm(launcher, name):
        state = relay_service_state(paths)
        T716("G1", f"_exec_service_status={state!r}")
        assert state.startswith("run:"), state
        began = time.monotonic()
        rc, out, err = exec_cli(launcher, name, "cat", "/proc/1/comm", root=True)
        T716(
            "G1",
            f"ginn_exec_root_rc={rc} stdout={out.strip()!r} took={time.monotonic() - began:.2f}s stderr={err.strip()[-300:]!r}",
        )
        if rc != 0 and "GINN-E-ATTACH-FAILED" in err:
            raise KnownGapT716One(f"{T716_1}: {err.strip()[-200:]}")
        assert rc == 0, err.strip()[-300:]
        assert out.strip() == "systemd", out


@pytest.mark.fr("FR-083")
@pytest.mark.fr("FR-049")
def test_g1b_relay_route_chain_and_pid1_without_session_grants(
    launcher: Path, paths: OwnedPaths
) -> None:
    """Supplementary to G1, through the adapter with the services-on target: the relay route runs
    `cat /proc/1/comm` in the realm, and a held exec's podman client is a child of the run helper,
    which is a child of the listener. It needs no session attach, so it isolates the relay itself."""
    name = PREFIX + "g1b"
    with running_realm(launcher, name):
        rc, out, err = relay_exec(launcher, paths, name, "cat", "/proc/1/comm")
        T716("G1b", f"relay_exec_root_rc={rc} stdout={out.strip()!r} stderr={err.strip()[-200:]!r}")
        assert rc == 0 and out.strip() == "systemd", (rc, out, err[-300:])
        held = relay_popen(launcher, paths, name, ["sleep", "20"])
        assert wait_for(lambda: bool(podman_clients(paths, name)), timeout=SPAWN_S), (
            "no exec client"
        )
        client = podman_clients(paths, name)[0]
        run_pid = ppid_of(client) or 0
        relay_pid = ppid_of(run_pid) or 0
        chain = [comm_of(client), comm_of(run_pid), comm_of(relay_pid)]
        T716("G1b", f"route_chain_podman_run_listener={chain}")
        assert chain == [PODMAN_COMM, RUN_COMM, EXEC_BIN], chain
        _out2, err2 = held.communicate(timeout=120)
        assert held.returncode == 0, err2[-300:]


# ----------------------------------------------------------------------------------------------
# G2: FR-081 AC-3, five start crashes in 60 s leave the realm failed (the F10 gate)
# ----------------------------------------------------------------------------------------------


@pytest.mark.fr("FR-081")
@pytest.mark.fr("NFR-033")
def test_g2_five_crashes_in_60_s_leave_the_realm_failed(launcher: Path, paths: OwnedPaths) -> None:
    """Five kills of a fresh container init within 60 s. runsv stops the service (no more
    restarts), and the record reads `failed`, not `stopped` (ADR-014, Decision 2, CL-24)."""
    name = PREFIX + "g2"
    with running_realm(launcher, name):
        began = time.monotonic()
        last = 0
        kills: list[dict[str, Any]] = []
        for index in range(5):
            last = wait_run_with_init(paths, name, not_pid=last)
            before = str(ginn_run(launcher, "status", name).body.get("state"))
            os.kill(last, signal.SIGKILL)
            kills.append(
                {"n": index + 1, "t": round(time.monotonic() - began, 2), "state_before": before}
            )
        down = wait_for(
            lambda: sv_status(paths, name).startswith("down"),
            timeout=60 - (time.monotonic() - began) + 15,
        )
        elapsed = time.monotonic() - began
        time.sleep(3)
        state = str(ginn_run(launcher, "status", name).body.get("state"))
        gave_up = [e for e in audit_entries(paths, name) if "gave-up" in json.dumps(e)]
        T716("G2", f"kills={json.dumps(kills)} down={down} elapsed={elapsed:.2f}s state={state}")
        T716("G2", f"give_up_audit={json.dumps(gave_up, default=str)[:600]}")
        assert down, "runsv did not stop the service after five crashes"
        assert state == "failed", f"five crashes left the realm {state}, not failed"


# ----------------------------------------------------------------------------------------------
# G3: enter smoke, through a pty (FR-048, CL-27). Stripped session (see env_for).
# ----------------------------------------------------------------------------------------------


@pytest.mark.fr("FR-048")
@pytest.mark.fr("FR-049")
def test_g3_enter_smoke_through_a_pty(launcher: Path, paths: OwnedPaths) -> None:
    """A services-on realm entered through the relay from a pty: the realm cgroup of the container
    init, raw Ctrl-C reaching the container, SIGWINCH resizing it, the exit status, and termios."""
    name = PREFIX + "g3"
    with running_realm(launcher, name):
        tty = Tty(launcher, "enter", name, stripped=True)
        try:
            before = tty.termios_flags()
            tty.send("echo MARK-$((6*7))\n")
            assert "MARK-42" in tty.read_until("MARK-42", 120), tty.buf[-400:]
            init = pid_of(paths, name)
            host_cgroup = Path(f"/proc/{init}/cgroup").read_text(encoding="utf-8").strip()
            T716(
                "G3", f"init_host_cgroup={host_cgroup!r} realm_in_path={f'/{name}/' in host_cgroup}"
            )
            assert f"/{name}/" in host_cgroup, host_cgroup
            tty.send("cat /proc/self/cgroup; echo CG-$((1+1))\n")
            cg = tty.read_until("CG-2", 60)
            T716("G3", f"inside_proc_self_cgroup={cg.split('CG-2')[0][-200:]!r}")
            tty.resize(40, 100)
            tty.send("stty size; echo SZ-$((1+2))\n")
            size = tty.read_until("SZ-3", 60)
            T716("G3", f"stty_size_after_winch_has_40_100={'40 100' in size}")
            assert "40 100" in size, size[-300:]
            during = tty.termios_flags()
            T716("G3", f"termios_during icanon,echo={during} before={before}")
            tty.send("sleep 100\n")
            time.sleep(2)
            tty.send("\x03")
            tty.send("echo RC-$?\n")
            ctrl_c = tty.read_until("RC-130", 60)
            T716("G3", f"ctrl_c_reached_container_sleep_status_130={'RC-130' in ctrl_c}")
            assert "RC-130" in ctrl_c, ctrl_c[-300:]
            tty.send("exit 7\n")
            code = tty.proc.wait(timeout=120)
            after = tty.termios_flags()
            T716("G3", f"exit_code={code} termios_after_exit={after} restored={after == before}")
            assert code == 7, code
            assert after == before, (before, after)
        finally:
            tty.close()


@pytest.mark.fr("FR-048")
def test_g3_termios_after_a_killed_podman_client(launcher: Path, paths: OwnedPaths) -> None:
    """Kill -9 of the podman client of an enter session (not of the ginn process). The CLI sees
    EXIT and restores the terminal on that exit path, so the termios is compared before and after."""
    name = PREFIX + "g3k"
    with running_realm(launcher, name):
        tty = Tty(launcher, "enter", name, stripped=True)
        try:
            before = tty.termios_flags()
            tty.send("echo MARK-$((6*7))\n")
            assert "MARK-42" in tty.read_until("MARK-42", 120), tty.buf[-300:]
            assert wait_for(lambda: bool(podman_clients(paths, name)), timeout=30), "no client"
            victims = podman_clients(paths, name)
            for pid in victims:
                os.kill(pid, signal.SIGKILL)
            code = tty.proc.wait(timeout=120)
            after = tty.termios_flags()
            T716(
                "G3",
                f"killed_podman_client victims={victims} ginn_exit={code} before={before} after={after}",
            )
            assert before == after, (before, after)
        finally:
            tty.close()


# ----------------------------------------------------------------------------------------------
# G4: install and remove through the relay, with exec-acked in the journal (FR-040, FR-041, FR-124)
# ----------------------------------------------------------------------------------------------


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-041")
@pytest.mark.fr("FR-124")
def test_g4_install_and_remove_through_the_relay(launcher: Path, paths: OwnedPaths) -> None:
    name = PREFIX + "g4"
    with running_realm(launcher, name):
        refresh_through_relay(launcher, paths, name)
        with StepWatch(paths, name) as watch:
            began = time.monotonic()
            status, body = finish(spawn(launcher, "install", name, "htop"))
            took = time.monotonic() - began
        T716(
            "G4",
            f"install rc={status} codes={codes(body)} took={took:.2f}s steps_seen={watch.seen}",
        )
        assert status == 0, body
        assert "exec-acked" in watch.seen, watch.seen
        probe = relay_exec(launcher, paths, name, "pacman", "-Q", "htop")
        T716("G4", f"pacman_Q_htop_rc={probe[0]}")
        assert probe[0] == 0, probe[2][-300:]
        with StepWatch(paths, name) as watch2:
            status, body = finish(spawn(launcher, "remove", name, "htop"))
        T716("G4", f"remove rc={status} codes={codes(body)} steps_seen={watch2.seen}")
        assert status == 0, body
        assert "exec-acked" in watch2.seen, watch2.seen
        gone = relay_exec(launcher, paths, name, "pacman", "-Q", "htop")[0]
        T716("G4", f"after_remove_pacman_rc={gone}")
        assert gone != 0
        ops = [(e.get("op"), e.get("outcome")) for e in audit_entries(paths, name)]
        T716("G4", f"audit_ops={json.dumps(ops, default=str)[:700]}")
        assert any(str(op).startswith("install") for op, _ in ops), ops
        assert any(str(op).startswith("remove") for op, _ in ops), ops


# ----------------------------------------------------------------------------------------------
# G5: rebuild replay through the relay, services off to on (FR-021, FR-083)
# ----------------------------------------------------------------------------------------------


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-083")
@pytest.mark.xfail(strict=True, reason=T716_1, raises=KnownGapT716One)
def test_g5_rebuild_replay_goes_through_the_relay(launcher: Path, paths: OwnedPaths) -> None:
    """A services-off realm with htop turns services on. Its next start rebuilds it, and the replay
    runs through the relay, routed by the plan's spec.services. Generation 2, PID 1 systemd, and
    htop is still installed. The checks run through the relay, not the session attach."""
    name = PREFIX + "g5"
    assert create(launcher, RUN_DISTRO, name, "off").rc == 0
    try:
        assert ginn_run(launcher, "start", name).rc == 0
        assert ginn_run(launcher, "install", name, "htop").rc == 0
        assert ginn_run(launcher, "stop", name).rc == 0
        assert ginn_run(launcher, "services", name, "on").rc == 0
        with StepWatch(paths, name) as watch:
            ran = ginn_run(launcher, "start", name)
        T716("G5", f"start rc={ran.rc} codes={ran.codes()} steps_seen={watch.seen}")
        if "GINN-W-REBUILD-DEFERRED" in ran.codes():
            raise KnownGapT716One(f"{T716_1}: the lazy rebuild was deferred, codes {ran.codes()}")
        assert ran.rc == 0, ran.error_text()
        generation = ginn_run(launcher, "status", name).body.get("generation")
        T716("G5", f"generation={generation}")
        assert generation == 2, generation
        rc1, pid1, err1 = relay_exec(launcher, paths, name, "cat", "/proc/1/comm")
        rc2, _, err2 = relay_exec(launcher, paths, name, "pacman", "-Q", "htop")
        T716("G5", f"pid1={pid1.strip()!r} rc={rc1} htop_rc={rc2}")
        assert rc1 == 0 and pid1.strip() == "systemd", err1[-300:]
        assert rc2 == 0, err2[-300:]
    finally:
        ginn_run(launcher, "stop", name)
        delete(launcher, name)


# ----------------------------------------------------------------------------------------------
# G6: export sync listing (FR-044), and grant re-attach at enter (FR-061, ADR-008)
# ----------------------------------------------------------------------------------------------


@pytest.mark.fr("FR-044")
def test_g6_export_sync_listing_through_the_relay(launcher: Path, paths: OwnedPaths) -> None:
    """The desktop entries of a services-on realm follow its install and remove (FR-044)."""
    name = PREFIX + "g6"
    with running_realm(launcher, name):
        refresh_through_relay(launcher, paths, name)
        status, body = finish(spawn(launcher, "install", name, "htop"))
        assert status == 0, body
        listed = sorted(p.name for p in paths.applications_dir.glob("ginn-*htop*.desktop"))
        T716("G6", f"export_listing_after_install={listed}")
        assert listed, "no htop desktop entry after the install"
        status, body = finish(spawn(launcher, "remove", name, "htop"))
        assert status == 0, body
        after = sorted(p.name for p in paths.applications_dir.glob("ginn-*htop*.desktop"))
        T716("G6", f"export_listing_after_remove={after}")
        assert not after


@pytest.mark.fr("FR-061")
def test_g6_grant_reattach_at_enter_through_the_relay(launcher: Path, paths: OwnedPaths) -> None:
    """A Custom realm shares a directory. The directory is replaced while the realm runs, so the
    grant's identity changes. The next enter re-attaches it (ADR-008), and the new file is seen.
    Stripped session (see env_for)."""
    name = PREFIX + "g6c"
    share = Path(tempfile.mkdtemp(prefix="t716-share-", dir=str(Path.home())))
    _SHARES.append(share)
    created = ginn_run(
        launcher, "create", RUN_DISTRO, "--name", name, "--services", "on", "--tier", "custom"
    )
    assert created.rc == 0, created.error_text()
    try:
        (share / "old-marker").write_text("old\n", encoding="utf-8")
        perms = ginn_run(launcher, "perms", name, "--share", str(share))
        T716("G6", f"perms_share_rc={perms.rc} codes={perms.codes()}")
        assert perms.rc == 0, perms.error_text()
        start_realm(launcher, name)
        shutil.rmtree(share)
        share.mkdir(mode=0o700)
        (share / "new-marker").write_text("new\n", encoding="utf-8")
        tty = Tty(launcher, "enter", name, stripped=True)
        try:
            tty.send(
                f"test -e {share}/new-marker && echo NEW-SEEN-$((1+1)) || echo NEW-GONE-$((1+1))\n"
            )
            seen = tty.read_until("\r\nNEW-", 120)
            tty.send("exit 0\n")
            tty.proc.wait(timeout=120)
        finally:
            tty.close()
        verdict = "NEW-SEEN-2" in seen
        T716("G6", f"reattach_new_marker_seen={verdict} output_tail={seen[-120:]!r}")
        assert verdict, seen[-300:]
    finally:
        drop_realm(launcher, name)


# ----------------------------------------------------------------------------------------------
# G7: EOF before EXIT kills the tokened processes, for --user and --root (FR-123, FR-124, O8)
# ----------------------------------------------------------------------------------------------


def measure_eof_kill(
    launcher: Path,
    paths: OwnedPaths,
    name: str,
    label: str,
    user: str,
    argv: list[str],
    needle: bytes,
) -> dict[str, Any]:
    """Kill the client of one mutating exec, and time the lock and the target process."""
    child = relay_popen(launcher, paths, name, argv, user=user, mutating=True)
    assert wait_for(lambda: bool(lock_holder(paths, name)), timeout=60), (
        "the exec never took the lock"
    )
    assert wait_for(lambda: bool(realm_pids_with(name, needle)), timeout=60), (
        f"{label} did not start"
    )
    holder = lock_holder(paths, name)
    probe = []
    for pid in realm_pids_with(name, needle):
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
            probe.append(
                {
                    "pid": pid,
                    "environ_readable": True,
                    "token_key_present": b"GINN_EXEC_TOKEN=" in raw,
                }
            )
        except OSError as exc:
            probe.append({"pid": pid, "environ_readable": False, "error": exc.strerror})
    t0 = time.monotonic()
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=30)
    lock_released = wait_for(lambda: lock_free(paths, name), timeout=60, step=0.05)
    lock_s = time.monotonic() - t0
    gone = wait_for(lambda: not realm_pids_with(name, needle), timeout=60, step=0.05)
    gone_s = time.monotonic() - t0
    survivors = realm_pids_with(name, needle)
    orphan = exec_orphan_path(paths, RealmName(name)).exists()
    for pid in survivors:
        # Found in this realm's cgroup, and it is this test's own command. Nothing else is signalled.
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    wait_for(lambda: not realm_pids_with(name, needle), timeout=20, step=0.2)
    with contextlib.suppress(OSError):
        exec_orphan_path(paths, RealmName(name)).unlink()
    result = {
        "label": label,
        "holder": holder,
        "lock_released": lock_released,
        "lock_released_s": round(lock_s, 2),
        "target_gone": gone,
        "target_gone_s": round(gone_s, 2),
        "survivors": survivors,
        "orphan_marker": orphan,
        "environ_probe_before_kill": probe,
    }
    T716("G7", json.dumps(result, sort_keys=True))
    return result


@pytest.mark.fr("FR-123")
@pytest.mark.fr("FR-124")
def test_g7_user_eof_kills_the_tokened_exec(launcher: Path, paths: OwnedPaths) -> None:
    """EOF before EXIT for `--user`: the worker sends SIGTERM to podman, and the kill helper sweeps the
    tokened processes. native/ginn-exec-run.c waits 2 s for podman, and native/ginn-exec-kill.c sweeps
    for at most 10 s (the ADR says 10 s for the whole step). The --root case is its own test below."""
    name = PREFIX + "g7u"
    user_mapped = exec_user(UID, GID, root=False)
    with running_realm(launcher, name):
        user = measure_eof_kill(
            launcher, paths, name, "user", user_mapped, ["sleep", "301"], b"sleep\x00301"
        )
        assert user["target_gone"] and user["lock_released"], user


@pytest.mark.fr("FR-123")
@pytest.mark.fr("FR-124")
def test_g7_env_i_escapes_the_token_kill(launcher: Path, paths: OwnedPaths) -> None:
    """A process that clears its environment carries no token, so the token kill does not find it. That
    is the documented limit (ADR-014, Known limit of the token kill). The lock is still released."""
    name = PREFIX + "g7e"
    with running_realm(launcher, name):
        cleared = measure_eof_kill(
            launcher,
            paths,
            name,
            "root env -i",
            ROOT,
            ["env", "-i", "sleep", "302"],
            b"sleep\x00302",
        )
        assert cleared["lock_released"], cleared
        assert cleared["survivors"], cleared


@pytest.mark.fr("FR-123")
@pytest.mark.fr("FR-124")
@pytest.mark.xfail(strict=True, reason=T716_2, raises=KnownGapT716Two)
def test_g7_root_eof_kill_is_t716_2(launcher: Path, paths: OwnedPaths) -> None:
    """EOF before EXIT for `--root`: the kill must reach the container-root sleep, release the lock once
    it is gone, and leave no orphan marker. Strict xfail on T716-2: the environ of a container-root
    process is unreadable, so the sweep finds no tokened process and returns at once."""
    name = PREFIX + "g7r"
    with running_realm(launcher, name):
        root = measure_eof_kill(
            launcher, paths, name, "root", ROOT, ["sleep", "300"], b"sleep\x00300"
        )
        if not (root["target_gone"] and root["lock_released"]):
            raise KnownGapT716Two(
                f"{T716_2}: the sleep survived ({root['target_gone_s']} s, target gone {root['target_gone']}), "
                f"the lock was released after {root['lock_released_s']} s, environ probe "
                f"{root['environ_probe_before_kill']}"
            )
        assert root["target_gone"] and root["lock_released"], root


@pytest.mark.fr("FR-123")
@pytest.mark.fr("FR-124")
def test_g7_enter_session_kill_is_recorded(launcher: Path, paths: OwnedPaths) -> None:
    """Kill -9 of the podman client of an enter session. The enter session has no token, so the walk
    cannot find its processes (ADR-014, Worker lifetime). The outcome is recorded, not asserted."""
    name = PREFIX + "g7n"
    with running_realm(launcher, name):
        enter_alive = _enter_kill_survives(launcher, paths, name)
        T716("G7", f"enter_session_killed_client_sleep_survives={enter_alive}")


def _enter_kill_survives(launcher: Path, paths: OwnedPaths, name: str) -> bool:
    """Kill -9 of the podman client of an enter session (no token). Does its sleep survive?"""
    tty = Tty(launcher, "enter", name, stripped=True)
    try:
        tty.send("echo MARK-$((6*7))\n")
        assert "MARK-42" in tty.read_until("MARK-42", 120), tty.buf[-300:]
        tty.send("sleep 303\n")
        wait_for(lambda: bool(realm_pids_with(name, b"sleep\x00303")), timeout=30)
        for pid in podman_clients(paths, name):
            os.kill(pid, signal.SIGKILL)
        time.sleep(12)
        alive = realm_pids_with(name, b"sleep\x00303")
        for pid in alive:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        return bool(alive)
    finally:
        tty.close()


# ----------------------------------------------------------------------------------------------
# G8: kill -9 of the listener, then of a worker, during install (ADR-014, Worker lifetime)
# ----------------------------------------------------------------------------------------------


def non_test_services_realms(paths: OwnedPaths) -> list[str]:
    """The services-on realms of this user that this module did not make. A record that does not load
    counts as services-on, as the relay sync rule does (ADR-014)."""
    store = RealmStore(paths, time.time)
    found: list[str] = []
    for realm in store.list_names():
        if realm.value.startswith(PREFIX):
            continue
        try:
            record = store.load(realm)
        except Exception:
            found.append(realm.value)
            continue
        if record.services_at_create or record.settings.services:
            found.append(realm.value)
    return found


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-123")
@pytest.mark.fr("FR-124")
def test_g8_listener_and_worker_death_during_install(launcher: Path, paths: OwnedPaths) -> None:
    """Listener and worker death during an install. The listener is SIGKILLed, so the user's shared
    relay is disturbed: the test is skipped while non-test services-on realms exist. Observed contract:
    A, the worker survives and the install succeeds; B, a request right after a listener kill succeeds
    within the revive window (the roadmap expected UNAVAILABLE, recorded in ADR-014); C, a killed worker
    reports LOST; D, a second mutating operation is not REALM-BUSY once the lock is released."""
    others = non_test_services_realms(paths)
    if others:
        pytest.skip(
            f"G8 SIGKILLs the user's shared relay listener, and these services-on realms exist: {others}"
        )
    name = PREFIX + "g8"
    with running_realm(launcher, name):
        refresh_through_relay(launcher, paths, name)
        # A. The listener is killed while a worker runs. The worker must survive, and the install must finish.
        held = spawn(launcher, "install", name, "htop")
        assert wait_for(lambda: run_under_listener() > 0, timeout=SPAWN_S, step=0.02), "no worker"
        worker_a = run_under_listener()
        old = listener()
        os.kill(old, signal.SIGKILL)
        status_a, body_a = finish(held, timeout=900)
        T716(
            "G8",
            f"A listener_killed={old} worker={worker_a} install_rc={status_a} codes={codes(body_a)}",
        )
        assert status_a == 0, body_a
        restarted = wait_for(lambda: listener_or_none() not in (None, old), timeout=60, step=0.05)
        T716("G8", f"A listener_restarted_by_runsv={restarted}")
        assert restarted, "runsv did not restart the listener"
        # B. A request right after another listener kill, with no worker running.
        second = listener()
        os.kill(second, signal.SIGKILL)
        t0 = time.monotonic()
        rc_b, _out_b, err_b = relay_exec(launcher, paths, name, "true")
        elapsed_b = time.monotonic() - t0
        assert rc_b == 0, err_b[-200:]
        assert elapsed_b <= UP_WAIT_S, elapsed_b
        T716(
            "G8",
            f"B request_after_listener_kill rc={rc_b} took={time.monotonic() - t0:.2f}s stderr_tail={err_b[-200:]!r}",
        )
        wait_for(lambda: listener_or_none() not in (None, second), timeout=60, step=0.05)
        # C. A worker is killed during an install. The client must report LOST.
        held_c = spawn(launcher, "install", name, "tree")
        assert wait_for(lambda: run_under_listener() > 0, timeout=SPAWN_S, step=0.02), (
            "no worker for C"
        )
        worker_c = run_under_listener()
        t0 = time.monotonic()
        os.kill(worker_c, signal.SIGKILL)
        status_c, body_c = finish(held_c, timeout=900)
        T716(
            "G8",
            f"C worker_killed={worker_c} install_rc={status_c} codes={codes(body_c)} client_saw_s={time.monotonic() - t0:.2f}",
        )
        assert "GINN-E-RELAY-LOST" in codes(body_c), codes(body_c)
        orphan = bool(realm_pids_with(name, b"pacman"))
        T716("G8", f"C orphan_pacman_alive_after_lost={orphan}")
        # D. The second mutating operation, while the orphan may still run. The kernel released the lock.
        status_d, body_d = finish(spawn(launcher, "install", name, "figlet"), timeout=900)
        busy = "GINN-E-REALM-BUSY" in codes(body_d)
        assert not busy, codes(body_d)
        T716("G8", f"D second_install_rc={status_d} codes={codes(body_d)} realm_busy={busy}")
        wait_for(lambda: not realm_pids_with(name, b"pacman"), timeout=900, step=0.5)
        T716("G8", f"D orphan_pacman_gone={not realm_pids_with(name, b'pacman')}")


# ----------------------------------------------------------------------------------------------
# G9: environment parity (O5, ADR-014 Environment)
# ----------------------------------------------------------------------------------------------


@pytest.mark.fr("FR-049")
@pytest.mark.fr("NFR-SEC-005")
def test_g9_environment_parity_and_pause_lookup(launcher: Path, paths: OwnedPaths) -> None:
    name = PREFIX + "g9"
    with running_realm(launcher, name):
        held = relay_popen(launcher, paths, name, ["sleep", "20"])
        assert wait_for(lambda: bool(podman_clients(paths, name)), timeout=SPAWN_S), "no client"
        client_env = environ_of(podman_clients(paths, name)[0])
        relay_env = environ_of(listener())
        bad = ("HOME", "TERM")

        def leaked(env: dict[str, str]) -> list[str]:
            return sorted(k for k in env if k in bad or k.startswith(("LANG", "LC_")))

        T716("G9", f"podman_client_env_keys={sorted(client_env)} leaked={leaked(client_env)}")
        T716("G9", f"relay_env_keys={sorted(relay_env)} leaked={leaked(relay_env)}")
        T716(
            "G9",
            "the listener keeps the environment its runsv gave it; podman is the process checked (ADR-014, O5)",
        )
        relay_view = {
            k: v
            for k, v in client_env.items()
            if k
            in ("XDG_RUNTIME_DIR", "CONTAINERS_CONF", "CONTAINERS_STORAGE_CONF", "TMPDIR", "PATH")
        }
        assert client_env.get("XDG_RUNTIME_DIR") == str(paths.runtime), client_env.get(
            "XDG_RUNTIME_DIR"
        )
        via_relay = podman_run(
            paths, ["ps", "-a", "--format", "{{.ID}} {{.Names}}"], env=relay_view
        )
        via_session = podman_run(paths, ["ps", "-a", "--format", "{{.ID}} {{.Names}}"])
        T716(
            "G9",
            f"podman_ps rc relay={via_relay.returncode} session={via_session.returncode} agree={via_relay.stdout == via_session.stdout}",
        )
        pause = paths.runtime / "libpod" / "tmp" / "pause.pid"
        pause_pid = int(pause.read_text(encoding="utf-8").strip()) if pause.exists() else 0
        T716("G9", f"pause_pid_file={pause.exists()} pause_comm={comm_of(pause_pid)}")
        assert via_relay.returncode == 0 and via_session.returncode == 0, via_relay.stderr[-300:]
        assert via_relay.stdout == via_session.stdout
        assert not leaked(client_env), leaked(client_env)
        assert comm_of(pause_pid) == "catatonit"
        _out, err = held.communicate(timeout=120)
        assert held.returncode == 0, err[-300:]


# ----------------------------------------------------------------------------------------------
# G10: the session-side errno of a services-on exec, by strace (O1)
# ----------------------------------------------------------------------------------------------


@pytest.mark.fr("FR-083")
def test_g10_session_side_podman_exec_errno(launcher: Path, paths: OwnedPaths) -> None:
    """Strace of a session-side `podman exec` into a services-on realm: the errno of the failing
    write to the realm's cgroup.procs. Skipped when strace is not installed."""
    strace = shutil.which("strace")
    if strace is None:
        pytest.skip("strace is not installed on this host, so O1 stays open")
    name = PREFIX + "g10"
    with running_realm(launcher, name):
        cid = full_container_id(paths, name)[:12]
        out = Path(tempfile.mkdtemp(prefix="t716-strace-", dir="/tmp"))  # noqa: S108 - mkdtemp, 0700
        log = out / "strace.log"
        env = {"XDG_RUNTIME_DIR": str(paths.runtime), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    strace,
                    "-f",
                    "-e",
                    "trace=openat,write",
                    "-s",
                    "1024",
                    "-o",
                    str(log),
                    *podman_argv(paths),
                    "exec",
                    cid,
                    "true",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=180,
                check=False,
            )
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        finally:
            shutil.rmtree(out, ignore_errors=True)
        keep = os.environ.get("GINN_T716_STRACE_LOG", "")
        if keep and text:
            Path(keep).write_text(text, encoding="utf-8")
        failing = [
            line
            for line in text.splitlines()
            if "= -1 E" in line and ("cgroup" in line or "write(" in line or "openat(" in line)
        ]
        errnos = sorted({m for line in failing for m in re.findall(r"= -1 (E[A-Z]+)", line)})
        T716(
            "G10",
            f"podman_exec_rc={done.returncode} failing_cgroup_procs_lines={failing[:3]} errnos={errnos}",
        )
        T716("G10", f"podman_stderr_tail={done.stderr[-240:]!r}")
        assert done.returncode != 0, "a session-side exec into a services-on realm should fail (F2)"
        assert errnos, "the strace shows no failing cgroup write or open"


# ----------------------------------------------------------------------------------------------
# G11: RSS and PSS with the relay present (NFR-002, NFR-003, NFR-021, ADR-007)
# ----------------------------------------------------------------------------------------------


def realm_sample(paths: OwnedPaths, name: str) -> dict[str, int]:
    """The ADR-007 attribution of one realm: its cgroup tree, runsv, ginn-wait, conmon and pause."""
    helpers = realm_helpers(name)
    pids = set(helpers["cgroup"]) | set(helpers["runsv"]) | set(helpers["ginn-wait"])
    conmon = paths.runtime / "services" / f"{name}.conmon.pid"
    if conmon.exists():
        pids.add(int(conmon.read_text(encoding="utf-8").strip()))
    pause = paths.runtime / "libpod" / "tmp" / "pause.pid"
    if pause.exists():
        pids.add(int(pause.read_text(encoding="utf-8").strip()))
    vm = pss = 0
    for pid in sorted(pids):
        try:
            vm += rss_kib(pid)["VmRSS"]
        except (OSError, ValueError):
            continue
        pss += pss_kib(pid) or 0
    return {"VmRSS": vm, "Pss": pss, "processes": len(pids)}


def relay_sample() -> dict[str, int]:
    pid = listener()
    return {"pid": pid, "VmRSS": rss_kib(pid)["VmRSS"], "Pss": pss_kib(pid) or 0}


@pytest.mark.fr("NFR-002")
@pytest.mark.fr("NFR-003")
@pytest.mark.fr("NFR-021")
def test_g11_memory_with_the_relay_present(launcher: Path, paths: OwnedPaths) -> None:
    """Five services-on arch realms, a 60 s settle, and the relay counted once for the user. Values
    are recorded. The NFR-003 (40 MiB) and NFR-021 (200 MiB) budgets are read from VmRSS and are
    not asserted here: their verdict is the F11 record (ADR-007)."""
    names = [PREFIX + f"g11-{i}" for i in range(5)]
    made: list[str] = []
    try:
        for name in names:
            services_realm(launcher, name)
            made.append(name)
        time.sleep(60)
        realms = {name: realm_sample(paths, name) for name in names}
        relay = relay_sample()
        per_realm_mib = sorted(round(r["VmRSS"] / 1024, 2) for r in realms.values())
        total_vm = sum(r["VmRSS"] for r in realms.values()) + relay["VmRSS"]
        total_pss = sum(r["Pss"] for r in realms.values()) + relay["Pss"]
        T716(
            "G11",
            f"per_realm_vmrss_mib_sorted={per_realm_mib} median_mib={per_realm_mib[2]} budget_nfr003_mib=40",
        )
        T716("G11", f"relay_listener={json.dumps(relay, sort_keys=True)}")
        T716(
            "G11",
            f"nfr021_five_realms_plus_relay_vmrss_mib={total_vm / 1024:.2f} pss_mib={total_pss / 1024:.2f} budget_mib=200",
        )
        helpers = [
            (pid, comm)
            for pid, comm, _, _ in procs()
            if comm in ("ginn-exec-worker", "ginn-exec-run", "ginn-exec-kill")
        ]
        T716("G11", f"per_request_helpers_resident_at_idle={helpers}")
        assert total_vm > 0
    finally:
        for name in made:
            drop_realm(launcher, name)


# ----------------------------------------------------------------------------------------------
# G12: runsv write order and the stop rule (ADR-014, T7.12 amendment), with real runsv
# ----------------------------------------------------------------------------------------------


def sv_line(svc: Path) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [SV, "status", str(svc)], capture_output=True, text=True, timeout=30, check=False
    )
    return (done.stdout.strip().splitlines() or [""])[0]


@pytest.mark.fr("FR-081")
def test_g12_runsv_writes_want_down_before_finish(paths: OwnedPaths) -> None:
    """A transient runit service whose finish script records `sv status`. The record must show the
    `want down` mark when finish runs (ADR-014, T7.12 amendment, item 2)."""
    tag = PREFIX + "order-" + os.urandom(3).hex()
    out = Path(tempfile.mkdtemp(prefix="t716-order-", dir="/tmp"))  # noqa: S108 - mkdtemp, 0700
    svc = paths.sv / tag
    tmp = paths.sv / f".{tag}.tmp"
    tmp.mkdir(mode=0o700)
    (tmp / "run").write_text("#!/bin/sh\nexec sleep 3600\n", encoding="utf-8")
    (tmp / "finish").write_text(
        f"#!/bin/sh\n{shlex.quote(SV)} status {shlex.quote(str(svc))} > "
        f"{shlex.quote(str(out / 'status'))} 2>&1\necho done >> {shlex.quote(str(out / 'status'))}\n",
        encoding="utf-8",
    )
    for script in ("run", "finish"):
        (tmp / script).chmod(0o755)
    os.rename(tmp, svc)
    try:
        assert wait_for(lambda: sv_line(svc).startswith("run:"), timeout=30)
        time.sleep(1)
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [SV, "down", str(svc)], capture_output=True, text=True, timeout=60, check=False
        )
        wait_for(
            lambda: (
                (out / "status").exists() and "done" in (out / "status").read_text(encoding="utf-8")
            ),
            timeout=60,
        )
        text = (out / "status").read_text(encoding="utf-8") if (out / "status").exists() else ""
        T716("G12", f"sv_status_in_finish={text!r} want_down_seen={'want down' in text}")
        assert "want down" in text, text
    finally:
        _retire(svc)
        shutil.rmtree(out, ignore_errors=True)


@pytest.mark.fr("FR-081")
@pytest.mark.fr("NFR-033")
def test_g12_a_stop_during_a_run_is_not_counted(launcher: Path, paths: OwnedPaths) -> None:
    """Four kills within the window, then `ginn stop` (a user stop, want down). The realm reads
    stopped, no give-up is audited, and after a start one more kill restarts the realm, not gives up."""
    name = PREFIX + "g12"
    with running_realm(launcher, name):
        last = 0
        for _ in range(4):
            last = wait_run_with_init(paths, name, not_pid=last)
            os.kill(last, signal.SIGKILL)
            time.sleep(1)
        settled = wait_for(
            lambda: str(ginn_run(launcher, "status", name).body.get("state")) == "running",
            timeout=90,
            step=1.0,
        )
        T716("G12", f"settled_running_before_stop={settled}")
        stop = ginn_run(launcher, "stop", name)
        T716("G12", f"stop_error={stop.error_text() if stop.rc else 'none'}")
        state = str(ginn_run(launcher, "status", name).body.get("state"))
        gave_up = [e for e in audit_entries(paths, name) if "gave-up" in json.dumps(e)]
        T716("G12", f"stop_rc={stop.rc} state_after_stop={state} gave_up_entries={len(gave_up)}")
        assert state == "stopped", state
        assert not gave_up
        start_realm(launcher, name)
        last = wait_run_with_init(paths, name, not_pid=0)
        os.kill(last, signal.SIGKILL)
        restarted = wait_for(
            lambda: str(ginn_run(launcher, "status", name).body.get("state")) == "running",
            timeout=60,
        )
        T716("G12", f"one_more_kill_after_start_restarted_running={restarted}")
        assert restarted


# ----------------------------------------------------------------------------------------------
# G13: SIGINT to a non-tty podman exec (O2). Recorded, not asserted.
# ----------------------------------------------------------------------------------------------


def container_sleep_pids(full_cid: str, duration: str) -> list[int]:
    """This user's processes that run exactly `sleep <duration>` inside the container `full_cid`. The
    container's processes sit in its libpod-<id> cgroup scope, so the scope is checked as well as the
    argv. The duration is unique to this gate, so no other process matches."""
    scope = f"libpod-{full_cid}".encode()
    found: list[int] = []
    for pid, _comm, _ppid, cmd in procs():
        if cmd.split(b"\0")[:2] != [b"sleep", duration.encode()]:
            continue
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_bytes()
        except OSError:
            continue
        if scope in cgroup:
            found.append(pid)
    return found


@pytest.mark.fr("FR-049")
def test_g13_non_tty_sigint_to_the_podman_client(launcher: Path, paths: OwnedPaths) -> None:
    """A services-off realm, so no relay and no session attach. A direct `podman exec` of a command
    that traps INT, with SIGINT sent to the podman client. The outcome answers O2 for podman itself.
    The relay forwards SIGINT to the podman client (CL-27), so the answer carries to the relay route.
    Recorded, not asserted."""
    name = PREFIX + "g13"
    assert create(launcher, RUN_DISTRO, name, "off").rc == 0
    try:
        assert ginn_run(launcher, "start", name).rc == 0
        full = full_container_id(paths, name)
        cid = full[:12]
        script = 'trap "exit 42" INT; sleep 1713 & wait; echo after-wait'
        env = {"XDG_RUNTIME_DIR": str(paths.runtime), "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [*podman_argv(paths), "exec", cid, "sh", "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert wait_for(
            lambda: bool(container_sleep_pids(full, "1713")), timeout=SPAWN_S, step=0.1
        ), "no sleep"
        os.kill(proc.pid, signal.SIGINT)
        try:
            out, err = proc.communicate(timeout=120)
            status = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err, status = "", "", -9
        T716(
            "G13",
            f"podman_client_exit={status} stdout={out.strip()[-120:]!r} stderr_tail={err[-160:]!r}",
        )
        alive = container_sleep_pids(full, "1713")
        T716("G13", f"sleep_100_alive_after_podman_sigint={bool(alive)}")
        for pid in alive:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    finally:
        ginn_run(launcher, "stop", name)
        delete(launcher, name)
