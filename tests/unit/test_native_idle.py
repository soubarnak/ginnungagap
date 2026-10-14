# SPDX-License-Identifier: Apache-2.0
"""native/ginn-idle.c: it compiles clean, stays within 200 lines, validates its argv, and stops an
idle services-off realm once its last user process has been gone for the grace (FR-018).

A realm is stood in for by an init in a new pid namespace (`unshare --user --map-root-user --pid
--fork`). The init writes its host pid to a control directory, and spawns or kills a user process
inside its namespace when a command file appears, so the test needs no pid of a user process. The
podman stand-in is a shell script that logs its argv. Nothing here runs real podman or a realm, and
the namespace is private to each test. Where the launcher starts ginn-idle is M7 (T7.6, wiring).
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "native"
SOURCE = NATIVE / "ginn-idle.c"
WARNINGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2"]
LINE_LIMIT = 200
WAIT_S = 10.0
GRACE = 3  # seconds; production passes 60 on argv, the tests use a short grace

pytestmark = [pytest.mark.fr("FR-018"), pytest.mark.fr("NFR-SEC-005")]

INIT = """\
import os, subprocess, sys, time
ctl = sys.argv[1]
def flag(name):
    return os.path.join(ctl, name)
tmp = flag("init.pid.tmp")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(os.readlink("/proc/self") + "\\n")
os.replace(tmp, flag("init.pid"))
child = None
while True:
    if child is None and os.path.exists(flag("spawn")):
        os.unlink(flag("spawn"))
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time\\ntime.sleep(600)"])
        open(flag("up"), "w").close()
    if child is not None and os.path.exists(flag("kill")):
        os.unlink(flag("kill"))
        child.kill()
    if child is not None and child.poll() is not None:
        child = None
    time.sleep(0.05)
"""
"""The stand-in init. It is pid 1 of its namespace, so it reaps the user process it spawns."""

_RUNNING: list[subprocess.Popen[bytes]] = []
"""Every realm, helper and stand-in that a test started. Each one leads its own process group."""


@pytest.fixture(scope="module")
def helper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    out = tmp_path_factory.mktemp("idle") / "ginn-idle"
    built = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [cc, *WARNINGS, "-o", str(out), str(SOURCE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    return out


@pytest.fixture(scope="module")
def unshare() -> str:
    """A new unprivileged pid namespace is needed for the realm stand-in. Skip without one."""
    path = shutil.which("unshare")
    if path is None:
        pytest.skip("unshare is not installed")
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [path, "--user", "--map-root-user", "--pid", "--fork", "true"],
        capture_output=True,
        check=False,
        timeout=WAIT_S,
    )
    if probe.returncode != 0:
        pytest.skip("unprivileged pid namespaces are not available")
    return path


@pytest.fixture(autouse=True)
def _nothing_left_running() -> Iterator[None]:
    """Kill the process groups that the test started and did not finish."""
    yield
    while _RUNNING:
        proc = _RUNNING.pop()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=WAIT_S)


def _realm(unshare_path: str, tmp_path: Path) -> tuple[int, Path]:
    """Start a stand-in realm. Return the init's host pid and its control directory."""
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    script = tmp_path / "init.py"
    script.write_text(INIT, encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            unshare_path,
            "--user",
            "--map-root-user",
            "--pid",
            "--fork",
            sys.executable,
            "-I",
            str(script),
            str(ctl),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _RUNNING.append(proc)
    _appears(ctl / "init.pid")
    return int((ctl / "init.pid").read_text(encoding="utf-8")), ctl


def _appears(path: Path, timeout: float = WAIT_S) -> float:
    """Wait until path exists. Return the monotonic time it was first seen."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"{path.name} did not appear")
        time.sleep(0.005)
    return time.monotonic()


def _spawn_user(ctl: Path) -> None:
    """Ask the stand-in init for one user process in its namespace, and wait until it runs."""
    (ctl / "spawn").touch()
    _appears(ctl / "up")


def _podman(tmp_path: Path, status: int = 0) -> tuple[Path, Path]:
    """A podman stand-in: it logs its argv, one argument per line, and exits `status`."""
    log = tmp_path / "podman.log"
    script = tmp_path / "podman"
    script.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{log}'\nexit {status}\n", encoding="utf-8"
    )
    script.chmod(0o755)
    return script, log


def _globals(tmp_path: Path) -> list[str]:
    return [
        "--root",
        str(tmp_path / "root"),
        "--runroot",
        str(tmp_path / "runroot"),
        "--tmpdir",
        str(tmp_path / "tmp"),
    ]


class _Out:
    """The helper's stdout, one line at a time. Every wait has a deadline."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._fd = proc.stdout.fileno()  # type: ignore[union-attr]
        self._buf = b""
        self._lines: list[str] = []

    def _read(self, timeout: float) -> bool:
        ready, _, _ = select.select([self._fd], [], [], max(timeout, 0.0))
        if not ready:
            return False
        chunk = os.read(self._fd, 4096)
        if not chunk:
            pytest.fail("the helper closed its stdout")
        *done, self._buf = (self._buf + chunk).split(b"\n")
        self._lines += [line.decode("utf-8") for line in done]
        return True

    def line(self, timeout: float = WAIT_S) -> str:
        """The next line the helper printed. Fail the test when none comes in time."""
        deadline = time.monotonic() + timeout
        while not self._lines:
            if not self._read(deadline - time.monotonic()):
                pytest.fail("the helper printed no line in time")
        return self._lines.pop(0)

    def quiet(self, seconds: float) -> None:
        """The helper prints nothing for `seconds`."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._read(deadline - time.monotonic())
            assert not self._lines, f"the helper printed {self._lines[0]!r} when it should be quiet"


def _start(
    helper: Path, init: int, podman: Path, globals_: list[str], grace: int = GRACE
) -> tuple[subprocess.Popen[bytes], _Out]:
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(helper), str(init), str(grace), str(podman), *globals_, "--", "ginn-r-g1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    _RUNNING.append(proc)
    out = _Out(proc)
    assert out.line().startswith("watching "), "the helper did not print its watch line"
    return proc, out


def _finish(proc: subprocess.Popen[bytes]) -> tuple[int, str]:
    """Wait for a helper that exits by itself. It is killed only when it hangs past WAIT_S."""
    try:
        _out, err = proc.communicate(timeout=WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=WAIT_S)
        pytest.fail("the helper did not exit on its own")
    return proc.returncode, err.decode("utf-8")


@pytest.mark.fr("FR-018")
def test_the_start_time_is_field_22_of_proc_stat(
    helper: Path, unshare: str, tmp_path: Path
) -> None:
    """The start time tells a reused pid from the init. Pin the index against the stat file that
    Python reads for the same process, since the helper only compares a value with itself."""
    init, _ctl = _realm(unshare, tmp_path)
    podman, _log = _podman(tmp_path)
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(helper), str(init), str(GRACE), str(podman), *_globals(tmp_path), "--", "ginn-r-g1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    _RUNNING.append(proc)
    line = _Out(proc).line()
    rest = Path(f"/proc/{init}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    start = int(rest[19])  # rest[0] is field 3 (state), so rest[19] is field 22
    assert start > 0
    assert line == f"watching {init} {start}", line


@pytest.mark.fr("FR-018")
def test_no_user_process_stops_the_realm_once_after_the_grace_and_exits_0(
    helper: Path, unshare: str, tmp_path: Path
) -> None:
    init, _ctl = _realm(unshare, tmp_path)
    podman, log = _podman(tmp_path)
    globals_ = _globals(tmp_path)
    proc, out = _start(helper, init, podman, globals_)
    assert out.line() == "armed"  # no user process yet: the grace starts at once
    armed = time.monotonic()
    stopped = _appears(log)
    code, err = _finish(proc)
    assert code == 0, err
    # One call only: the log holds the globals, then `stop` and the name, and nothing else.
    assert log.read_text(encoding="utf-8").splitlines() == [*globals_, "stop", "ginn-r-g1"]
    assert GRACE - 0.5 <= stopped - armed <= GRACE + 2.0


@pytest.mark.fr("FR-018")
def test_a_user_process_that_exits_starts_the_grace_at_its_exit(
    helper: Path, unshare: str, tmp_path: Path
) -> None:
    init, ctl = _realm(unshare, tmp_path)
    podman, log = _podman(tmp_path)
    _spawn_user(ctl)
    proc, out = _start(helper, init, podman, _globals(tmp_path))
    out.quiet(GRACE + 0.5)  # a user process runs: no grace, and no stop
    assert not log.exists()
    (ctl / "kill").touch()
    assert out.line() == "armed"
    armed = time.monotonic()
    stopped = _appears(log)
    code, err = _finish(proc)
    assert code == 0, err
    assert GRACE - 0.5 <= stopped - armed <= GRACE + 2.5


@pytest.mark.fr("FR-018")
def test_a_new_process_inside_the_grace_cancels_the_stop(
    helper: Path, unshare: str, tmp_path: Path
) -> None:
    init, ctl = _realm(unshare, tmp_path)
    podman, log = _podman(tmp_path)
    proc, out = _start(helper, init, podman, _globals(tmp_path))
    assert out.line() == "armed"
    _spawn_user(ctl)  # a process starts inside the grace
    assert out.line(timeout=GRACE) == "cancelled"
    out.quiet(GRACE + 1.5)  # past the grace that began at "armed": still no stop
    assert not log.exists()
    (ctl / "kill").touch()
    assert out.line() == "armed"
    armed = time.monotonic()
    stopped = _appears(log)
    code, err = _finish(proc)
    assert code == 0, err
    assert GRACE - 0.5 <= stopped - armed <= GRACE + 2.5


@pytest.mark.fr("FR-018")
def test_the_init_exiting_makes_the_helper_exit_0_and_never_calls_podman(
    helper: Path, unshare: str, tmp_path: Path
) -> None:
    init, _ctl = _realm(unshare, tmp_path)
    podman, log = _podman(tmp_path)
    proc, _out = _start(helper, init, podman, _globals(tmp_path))
    os.kill(init, signal.SIGKILL)  # the realm ends on its own, before the grace would
    code, err = _finish(proc)
    assert code == 0, err
    assert not log.exists()


@pytest.mark.fr("FR-018")
def test_sigterm_exits_0_and_leaves_the_realm_running(
    helper: Path, unshare: str, tmp_path: Path
) -> None:
    init, _ctl = _realm(unshare, tmp_path)
    podman, log = _podman(tmp_path)
    proc, _out = _start(helper, init, podman, _globals(tmp_path))
    proc.send_signal(signal.SIGTERM)
    code, err = _finish(proc)
    assert code == 0, err
    assert not log.exists()
    os.kill(init, 0)  # the stand-in init still runs: the helper never stopped the realm


@pytest.mark.fr("FR-018")
@pytest.mark.parametrize(
    "args",
    [
        [],
        ["1"],
        ["1", "3"],
        ["1", "3", "/p"],
        ["1", "3", "/p", "--"],
        ["1", "3", "/p", "n", "--"],
        ["1", "3", "/p", "--", "a", "b"],
        ["1", "3", "p", "--", "x"],
        ["1", "3", "/p", "--", "-x"],
        ["1", "3", "/p", "--", "x;touch y"],
        ["1", "3", "/p", "--", "x/y"],
        ["1", "3", "/p", "--", ""],
        ["x", "3", "/p", "--", "n"],
        ["0", "3", "/p", "--", "n"],
        ["1", "0", "/p", "--", "n"],
        ["1", "3601", "/p", "--", "n"],
        ["1", "three", "/p", "--", "n"],
    ],
)
def test_bad_argv_exits_2_with_usage(helper: Path, args: list[str]) -> None:
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), *args], capture_output=True, text=True, check=False, timeout=WAIT_S
    )
    assert run.returncode == 2, run.stderr
    assert "usage: ginn-idle" in run.stderr


@pytest.mark.fr("FR-018")
def test_an_init_that_is_not_running_exits_3_and_never_calls_podman(
    helper: Path, tmp_path: Path
) -> None:
    exited = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 - fixed argv
    exited.wait(timeout=WAIT_S)
    podman, log = _podman(tmp_path)
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), str(exited.pid), str(GRACE), str(podman), "--", "ginn-r-g1"],
        capture_output=True,
        text=True,
        check=False,
        timeout=WAIT_S,
    )
    assert run.returncode == 3, run.stderr
    assert "is not running" in run.stderr
    assert not log.exists()


@pytest.mark.fr("FR-018")
def test_a_failed_podman_stop_exits_4_while_the_init_runs(
    helper: Path, unshare: str, tmp_path: Path
) -> None:
    init, _ctl = _realm(unshare, tmp_path)
    podman, log = _podman(tmp_path, status=7)
    proc, _out = _start(helper, init, podman, _globals(tmp_path))
    code, err = _finish(proc)
    assert code == 4, err
    assert "podman stop failed" in err
    assert log.read_text(encoding="utf-8").splitlines()[-2:] == ["stop", "ginn-r-g1"]


@pytest.mark.fr("FR-018")
@pytest.mark.fr("NFR-002")
def test_the_helper_stays_under_2_mib_resident_while_it_waits(
    helper: Path, unshare: str, tmp_path: Path
) -> None:
    """A guard against growth, not the NFR-002 budget (ADR-007 counts the helper in T7.7)."""
    init, _ctl = _realm(unshare, tmp_path)
    podman, _log = _podman(tmp_path)
    proc, out = _start(helper, init, podman, _globals(tmp_path))
    assert out.line() == "armed"
    status = Path(f"/proc/{proc.pid}/status").read_text(encoding="utf-8")
    rss_kib = next(int(ln.split()[1]) for ln in status.splitlines() if ln.startswith("VmRSS:"))
    assert rss_kib < 2048, f"ginn-idle holds {rss_kib} KiB resident"


def test_source_stays_within_the_line_limit() -> None:
    lines = SOURCE.read_text(encoding="utf-8").count("\n")
    assert lines <= LINE_LIMIT, f"{SOURCE.name} has {lines} lines; the limit is {LINE_LIMIT}"


def test_the_makefile_check_target_builds_it_with_gcc_and_clang() -> None:
    """`make -C native check` is the gate: both compilers, warnings as errors, every helper."""
    make = shutil.which("make")
    if make is None or shutil.which("clang") is None or shutil.which("gcc") is None:
        pytest.skip("make, gcc and clang are needed for the Makefile gate")
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [make, "-C", str(NATIVE), "check"], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "ginn-idle.c" in run.stdout
