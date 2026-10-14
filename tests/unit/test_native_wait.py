# SPDX-License-Identifier: Apache-2.0
"""native/ginn-wait.c: it compiles clean, stays within 200 lines, validates its argv, and supervises
one conmon pid. A conmon that dies is a crash (exit 1). A SIGTERM runs `podman stop` once and exits
0 (FR-081, NFR-033).

Every case uses a fake conmon (a Python child whose pid goes in a pidfile) and a fake podman (a
shell script that logs its argv and, on a call, kills the conmon named by the pidfile). Nothing
here runs real podman, and no realm is involved. Where runit starts the helper is M7 (T7.4).
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "native"
SOURCE = NATIVE / "ginn-wait.c"
WARNINGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2"]
LINE_LIMIT = 200
WAIT_S = 10.0

pytestmark = [pytest.mark.fr("FR-081"), pytest.mark.fr("NFR-SEC-005")]

_RUNNING: list[subprocess.Popen[bytes]] = []
"""Every conmon and helper that a test started. A test that fails part way leaves none running."""


@pytest.fixture(scope="module")
def helper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    out = tmp_path_factory.mktemp("wait") / "ginn-wait"
    built = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [cc, *WARNINGS, "-o", str(out), str(SOURCE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    return out


@pytest.fixture(autouse=True)
def _nothing_left_running() -> Iterator[None]:
    """Kill the conmon stand-ins and helpers that the test started and did not finish."""
    yield
    while _RUNNING:
        proc = _RUNNING.pop()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=WAIT_S)


def _conmon() -> subprocess.Popen[bytes]:
    """A stand-in conmon: a sleeping child whose pid goes in the pidfile."""
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time\ntime.sleep(300)"]
    )
    _RUNNING.append(proc)
    return proc


def _pidfile(tmp_path: Path, pid: int) -> Path:
    path = tmp_path / "conmon.pid"
    path.write_text(f"{pid}\n", encoding="utf-8")
    return path


def _podman(
    tmp_path: Path, pidfile: Path, *, kills_conmon: bool = True, status: int = 0
) -> tuple[Path, Path]:
    """A podman stand-in: it appends its argv to a log, one argument per line, and exits `status`.
    When `kills_conmon` is set, a call ends the conmon in the pidfile, as `podman stop` does."""
    log = tmp_path / "podman.log"
    script = tmp_path / "podman"
    kill = f"kill \"$(cat '{pidfile}')\"\n" if kills_conmon else ""
    script.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{log}'\n{kill}exit {status}\n", encoding="utf-8"
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


def _start(
    helper: Path, pidfile: Path, podman: Path, globals_: list[str], name: str = "ginn-r-g1"
) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(helper), str(pidfile), str(podman), *globals_, "--", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _RUNNING.append(proc)
    return proc


def _waiting(proc: subprocess.Popen[bytes]) -> str:
    """Wait for the "waiting <pid> <start>" line, so no signal goes out before the watch is up."""
    ready, _, _ = select.select([proc.stdout], [], [], WAIT_S)  # type: ignore[list-item]
    assert ready, "the helper printed no 'waiting' line"
    line = proc.stdout.readline().decode("utf-8")  # type: ignore[union-attr]
    assert line.startswith("waiting "), line
    return line


@pytest.mark.fr("NFR-033")
def test_the_start_time_is_field_22_of_proc_stat(helper: Path, tmp_path: Path) -> None:
    """Field 22 (starttime) is what tells a reused pid from conmon. Pin the index against the
    stat file that Python reads for the same process."""
    conmon = _conmon()
    pidfile = _pidfile(tmp_path, conmon.pid)
    podman, _log = _podman(tmp_path, pidfile)
    proc = _start(helper, pidfile, podman, _globals(tmp_path))
    line = _waiting(proc)
    rest = Path(f"/proc/{conmon.pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    start = int(rest[19])  # rest[0] is field 3 (state), so rest[19] is field 22
    assert start > 0
    assert int(line.split()[2]) == start, line


def _finish(proc: subprocess.Popen[bytes]) -> tuple[int, str]:
    """Wait for a helper that exits by itself, and return its status and stderr. It is killed only
    when it is still running after WAIT_S, which is a hang."""
    try:
        _out, err = proc.communicate(timeout=WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        _out, err = proc.communicate(timeout=WAIT_S)
        pytest.fail("the helper did not exit on its own")
    return proc.returncode, err.decode("utf-8")


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
    assert "ginn-wait.c" in run.stdout


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["/p"],
        ["/p", "/usr/bin/podman"],
        ["/p", "/usr/bin/podman", "ginn-r-g1"],
        ["/p", "/usr/bin/podman", "--"],
        ["/p", "/usr/bin/podman", "--", "a", "b"],
        ["/p", "/usr/bin/podman", "--", "-x"],
        ["/p", "/usr/bin/podman", "--", "x;touch y"],
        ["/p", "/usr/bin/podman", "--", "x/y"],
        ["/p", "/usr/bin/podman", "--", ""],
        ["p", "/usr/bin/podman", "--", "x"],
        ["/p", "podman", "--", "x"],
    ],
)
def test_bad_argv_exits_2_with_usage(helper: Path, args: list[str]) -> None:
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), *args], capture_output=True, text=True, check=False, timeout=WAIT_S
    )
    assert run.returncode == 2, run.stderr
    assert "usage: ginn-wait" in run.stderr


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "open"),
        ("12x\n", "does not hold a pid"),
        ("dead", "is not running"),
    ],
)
def test_a_pidfile_that_names_no_running_conmon_exits_3(
    helper: Path, tmp_path: Path, content: str | None, reason: str
) -> None:
    pidfile = tmp_path / "conmon.pid"
    if content == "dead":
        exited = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 - fixed argv
        exited.wait(timeout=WAIT_S)
        content = f"{exited.pid}\n"
    if content is not None:
        pidfile.write_text(content, encoding="utf-8")
    podman, log = _podman(tmp_path, pidfile)
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), str(pidfile), str(podman), "--", "ginn-r-g1"],
        capture_output=True,
        text=True,
        check=False,
        timeout=WAIT_S,
    )
    assert run.returncode == 3, run.stderr
    assert reason in run.stderr
    assert not log.exists()  # podman is never called for a pid that is not conmon


@pytest.mark.fr("NFR-033")
def test_a_conmon_that_dies_on_its_own_makes_the_helper_exit_1_and_never_calls_podman(
    helper: Path, tmp_path: Path
) -> None:
    conmon = _conmon()
    pidfile = _pidfile(tmp_path, conmon.pid)
    podman, log = _podman(tmp_path, pidfile)
    proc = _start(helper, pidfile, podman, _globals(tmp_path))
    _waiting(proc)
    conmon.kill()  # conmon dies with no stop request, as in a crash
    conmon.wait(timeout=WAIT_S)
    code, err = _finish(proc)
    assert code == 1
    assert "conmon" in err
    assert not log.exists()


@pytest.mark.fr("NFR-033")
def test_sigterm_runs_podman_stop_once_with_the_global_args_and_exits_0(
    helper: Path, tmp_path: Path
) -> None:
    conmon = _conmon()
    pidfile = _pidfile(tmp_path, conmon.pid)
    podman, log = _podman(tmp_path, pidfile)
    globals_ = _globals(tmp_path)
    proc = _start(helper, pidfile, podman, globals_, name="ginn-realm-g1")
    _waiting(proc)
    proc.send_signal(signal.SIGTERM)
    code, err = _finish(proc)
    assert code == 0, err
    # One call only: the log holds the globals, then `stop` and the name, and nothing else.
    assert log.read_text(encoding="utf-8").splitlines() == [*globals_, "stop", "ginn-realm-g1"]
    conmon.wait(timeout=WAIT_S)  # the stop ended conmon; a live conmon would time out here


@pytest.mark.fr("NFR-033")
@pytest.mark.parametrize(
    ("conmon_ends", "code"),
    [(False, 4), (True, 0)],
    ids=["conmon-alive", "conmon-gone"],
)
def test_a_failed_podman_stop_exits_4_unless_conmon_is_already_gone(
    helper: Path, tmp_path: Path, conmon_ends: bool, code: int
) -> None:
    conmon = _conmon()
    pidfile = _pidfile(tmp_path, conmon.pid)
    podman, log = _podman(tmp_path, pidfile, kills_conmon=conmon_ends, status=7)
    proc = _start(helper, pidfile, podman, _globals(tmp_path))
    _waiting(proc)
    proc.send_signal(signal.SIGTERM)
    got, err = _finish(proc)
    assert got == code, err
    assert log.read_text(encoding="utf-8").splitlines()[-2:] == ["stop", "ginn-r-g1"]
    if conmon_ends:
        conmon.wait(timeout=WAIT_S)
    else:
        assert conmon.poll() is None  # the helper left conmon alone and reports it
        assert "podman stop failed" in err


@pytest.mark.fr("NFR-003")
def test_the_helper_stays_under_2_mib_resident_while_it_waits(helper: Path, tmp_path: Path) -> None:
    """A guard against growth, not the NFR-003 budget. The dynamic glibc build measured about
    1.5 MiB on the reference host (libc pages count). T7.7 measures the helper in the table."""
    conmon = _conmon()
    pidfile = _pidfile(tmp_path, conmon.pid)
    podman, _log = _podman(tmp_path, pidfile)
    proc = _start(helper, pidfile, podman, _globals(tmp_path))
    _waiting(proc)
    status = Path(f"/proc/{proc.pid}/status").read_text(encoding="utf-8")
    rss_kib = next(int(ln.split()[1]) for ln in status.splitlines() if ln.startswith("VmRSS:"))
    assert rss_kib < 2048, f"ginn-wait holds {rss_kib} KiB resident"
