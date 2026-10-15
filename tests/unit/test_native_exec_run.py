# SPDX-License-Identifier: Apache-2.0
"""native/ginn-exec-run.c and native/ginn-exec-kill.c: one checked request, run directly (ADR-014).

ginn-exec-run takes the client socket on fd 3 and podman's stdio and pipe descriptors by number, as
ginn-exec-worker passes them after it has checked the request. These tests start it directly with
those numbers, so no worker runs. Podman is a shell stand-in: it can wait for a gate file, ignore
TERM, or write to its pipes. Nothing here runs real podman, and nothing touches /run/user/<uid> or
~/.local/share/containers. The token kill runs against a fake cgroup tree that holds the test's own
processes. The real cgroup walk is host-gate material (ADR-014, gates G7 and G8).
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.environ_support import require_environ_readable

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "native"
HEADER = NATIVE / "ginn-exec-frame.h"
WARNINGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2"]
LINE_LIMIT = 200
WAIT_S = 15.0
REALM = "tst-run"
CID = "d" * 64
TOKEN = "e" * 32
TTY, LOCK, CAPTURED = 1, 4, 8
ACK, REJECT, SIG, EXIT = 2, 3, 4, 5
_RUNNING: list[subprocess.Popen[bytes]] = []
"""Every ginn-exec-run and stand-in a test started, so a failing test leaves none running."""


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    out = tmp_path_factory.mktemp("exec-run-bin")
    for name in ("ginn-exec-run", "ginn-exec-kill"):
        run = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [cc, *WARNINGS, "-o", str(out / name), str(NATIVE / f"{name}.c")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, run.stderr
    return out


@pytest.fixture(autouse=True)
def _nothing_left_running() -> Iterator[None]:
    yield
    while _RUNNING:
        proc = _RUNNING.pop()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=WAIT_S)


def _globals(root: Path) -> list[str]:
    return [
        "--remote=false",
        "--root",
        str(root / "root"),
        "--runroot",
        str(root / "rr"),
        "--tmpdir",
        str(root / "t"),
    ]


def _env(root: Path) -> dict[str, str]:
    """Podman's environment, which ginn-exec-run keeps as its own."""
    return {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(root), "TMPDIR": str(root / "t")}


def _high(fd: int) -> int:
    """A copy of fd numbered from 20 and not CLOEXEC, as the worker numbers what it passes on."""
    return fcntl.fcntl(fd, fcntl.F_DUPFD, 20)


def _stdio_numbers() -> list[int]:
    """Copies of the test's stdin, stdout and stderr, numbered from 20."""
    return [_high(0), _high(1), _high(2)]


def _stand_in(tmp: Path, *, body: str = "", wait_for: Path | None = None, status: int = 0) -> Path:
    """A shell stand-in for podman. It writes its pid and argv, runs `body`, optionally waits for
    `wait_for` to exist, and exits with `status`."""
    script = tmp / "podman-sh"
    lines = [
        "#!/bin/sh",
        f"echo $$ > '{tmp / 'pid'}'",
        f"printf '%s\\n' \"$@\" > '{tmp / 'argv'}'",
        body,
    ]
    if wait_for is not None:
        lines.append(f"while [ ! -e '{wait_for}' ]; do sleep 0.05; done")
    lines.append(f"exit {status}")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _spawn(
    bin_dir: Path,
    root: Path,
    flags: int,
    numbers: list[int],
    sock: socket.socket,
    podman: Path,
    env: dict[str, str],
) -> subprocess.Popen[bytes]:
    """Start ginn-exec-run with the client socket on fd 3 and the descriptor numbers in its argv."""
    fd = sock.fileno()
    argv = [
        str(bin_dir / "ginn-exec-run"),
        str(root),
        REALM,
        str(flags),
        TOKEN if flags & LOCK else "",
        *map(str, numbers),
        "--",
        str(podman),
        *_globals(root),
        "exec",
        "--",
        CID,
        "true",
    ]
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        env=env,
        close_fds=False,
        preexec_fn=lambda: os.dup2(fd, 3, inheritable=True),  # noqa: PLW1509 - single-threaded test
        stderr=subprocess.PIPE,
    )
    _RUNNING.append(proc)
    for copy in numbers:
        os.close(copy)  # the child holds its own copies; a copy left here would hold a pipe open
    return proc


def _pair() -> tuple[socket.socket, socket.socket]:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.settimeout(WAIT_S)
    return parent, child


def _finish(proc: subprocess.Popen[bytes]) -> tuple[int, str]:
    try:
        _out, err = proc.communicate(timeout=WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=WAIT_S)
        pytest.fail("ginn-exec-run did not exit on its own")
    return proc.returncode, err.decode("utf-8")


def _recv(sock: socket.socket) -> bytes:
    data = sock.recv(65536)
    assert data, "ginn-exec-run closed the client before it sent a frame"
    return data


def _wait_for(path: Path) -> None:
    """Wait until the stand-in has reached a point it marks with a file, such as after its trap."""
    deadline = time.monotonic() + WAIT_S
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.02)


def _pid_of(tmp: Path) -> int:
    """The pid the stand-in wrote. ACK goes out before the shell starts, so wait for the file."""
    pidfile = tmp / "pid"
    deadline = time.monotonic() + WAIT_S
    while not (pidfile.exists() and pidfile.read_text(encoding="utf-8").endswith("\n")):
        assert time.monotonic() < deadline, "the stand-in never wrote its pid"
        time.sleep(0.02)
    return int(pidfile.read_text(encoding="utf-8"))


def _lock_busy(path: Path) -> bool:
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _wait_free(path: Path) -> None:
    deadline = time.monotonic() + WAIT_S
    while _lock_busy(path):
        assert time.monotonic() < deadline, "the exec lock was still held after EXIT"
        time.sleep(0.02)


def _read_to_eof(fd: int) -> bytes:
    out = b""
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                return out
            out += chunk
    pytest.fail("the captured pipe did not reach EOF")


@pytest.mark.fr("FR-049")
def test_the_ack_pid_and_the_exit_status_come_back(built: Path, tmp_path: Path) -> None:
    podman = _stand_in(tmp_path, status=3)
    numbers = _stdio_numbers()
    parent, child = _pair()
    proc = _spawn(built, tmp_path, 0, numbers, child, podman, _env(tmp_path))
    child.close()
    ack = _recv(parent)
    assert ack[0] == ACK
    assert int.from_bytes(ack[1:5], "big") == _pid_of(tmp_path)
    assert _recv(parent) == bytes([EXIT, 0, 3])
    assert _finish(proc)[0] == 0


@pytest.mark.fr("FR-049")
def test_a_signal_frame_reaches_podman_and_a_signal_death_is_reported(
    built: Path, tmp_path: Path
) -> None:
    gate = tmp_path / "go"
    podman = _stand_in(tmp_path, wait_for=gate)
    numbers = _stdio_numbers()
    parent, child = _pair()
    proc = _spawn(built, tmp_path, 0, numbers, child, podman, _env(tmp_path))
    child.close()
    assert _recv(parent)[0] == ACK
    _pid_of(tmp_path)
    parent.send(bytes([SIG, signal.SIGTERM]))
    assert _recv(parent) == bytes([EXIT, 1, signal.SIGTERM])
    assert _finish(proc)[0] == 0


@pytest.mark.fr("FR-049")
def test_captured_output_reaches_the_client_pipe_and_the_status_follows(
    built: Path, tmp_path: Path
) -> None:
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    numbers = [_high(0), _high(out_w), _high(err_w), _high(out_r), _high(err_r)]
    podman = _stand_in(tmp_path, body="echo hi")
    parent, child = _pair()
    proc = _spawn(built, tmp_path, CAPTURED, numbers, child, podman, _env(tmp_path))
    child.close()
    os.close(out_w)
    os.close(err_w)
    assert _recv(parent)[0] == ACK
    assert _read_to_eof(out_r) == b"hi\n"
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert _finish(proc)[0] == 0
    for fd in (out_r, err_r):
        os.close(fd)


@pytest.mark.fr("FR-049")
def test_podman_keeps_only_its_stdio_and_never_the_client_socket(
    built: Path, tmp_path: Path
) -> None:
    """The client socket must not reach podman, or the client would never see EOF (ADR-014)."""
    listing = tmp_path / "fds"
    podman = _stand_in(tmp_path, body=f"ls /proc/$$/fd > '{listing}'")
    numbers = _stdio_numbers()
    parent, child = _pair()
    proc = _spawn(built, tmp_path, 0, numbers, child, podman, _env(tmp_path))
    child.close()
    assert _recv(parent)[0] == ACK
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert _finish(proc)[0] == 0
    open_fds = {int(line) for line in listing.read_text(encoding="utf-8").split()}
    assert 3 not in open_fds, "the client socket reached podman"
    assert not open_fds & set(numbers), "a descriptor number reached podman beyond its stdio"


@pytest.mark.fr("FR-123")
def test_a_held_exec_lock_refuses_the_request_and_a_free_one_runs(
    built: Path, tmp_path: Path
) -> None:
    lock = tmp_path / "exec" / f"{REALM}.lock"
    lock.parent.mkdir(mode=0o700)
    holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        parent, child = _pair()
        proc = _spawn(
            built, tmp_path, LOCK, _stdio_numbers(), child, _stand_in(tmp_path), _env(tmp_path)
        )
        child.close()
        frame = _recv(parent)
        assert frame[:2] == bytes([REJECT, 2]), frame
        assert frame[4:].startswith(b"GINN-E-REALM-BUSY"), frame
        assert _finish(proc)[0] == 0
    finally:
        os.close(holder)
    assert not _lock_busy(lock)
    parent, child = _pair()
    proc = _spawn(
        built, tmp_path, LOCK, _stdio_numbers(), child, _stand_in(tmp_path), _env(tmp_path)
    )
    child.close()
    assert _recv(parent)[0] == ACK
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert _finish(proc)[0] == 0


@pytest.mark.fr("FR-123")
def test_the_lock_is_held_while_podman_runs_and_free_after_it_exits(
    built: Path, tmp_path: Path
) -> None:
    gate = tmp_path / "go"
    lock = tmp_path / "exec" / f"{REALM}.lock"
    numbers = _stdio_numbers()
    parent, child = _pair()
    proc = _spawn(
        built, tmp_path, LOCK, numbers, child, _stand_in(tmp_path, wait_for=gate), _env(tmp_path)
    )
    child.close()
    assert _recv(parent)[0] == ACK
    assert _lock_busy(lock), "the exec lock is free while podman runs"
    podman_pid = _pid_of(tmp_path)
    second_parent, second_child = _pair()
    second = _spawn(
        built, tmp_path, LOCK, _stdio_numbers(), second_child, _stand_in(tmp_path), _env(tmp_path)
    )
    second_child.close()
    refused = _recv(second_parent)
    assert refused[:2] == bytes([REJECT, 2]), refused
    assert b"is locked by exec run" in refused and str(podman_pid).encode() in refused, refused
    assert _finish(second)[0] == 0
    gate.touch()
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert not _lock_busy(lock), "the lock was still held when EXIT arrived"
    assert _finish(proc)[0] == 0


@pytest.mark.fr("FR-123")
def test_an_early_client_gets_its_child_stopped_and_the_lock_is_released(
    built: Path, tmp_path: Path
) -> None:
    lock = tmp_path / "exec" / f"{REALM}.lock"
    numbers = _stdio_numbers()
    parent, child = _pair()
    proc = _spawn(
        built,
        tmp_path,
        LOCK,
        numbers,
        child,
        _stand_in(tmp_path, wait_for=tmp_path / "never"),
        _env(tmp_path),
    )
    child.close()
    assert _recv(parent)[0] == ACK
    podman_pid = _pid_of(tmp_path)
    assert _lock_busy(lock), "the exec lock is free while podman runs"
    parent.close()
    _wait_free(lock)
    assert _finish(proc)[0] == 0
    with pytest.raises(ProcessLookupError):
        os.kill(podman_pid, 0)


@pytest.mark.fr("FR-049")
def test_a_child_that_ignores_term_is_killed_two_seconds_after_the_client_left(
    built: Path, tmp_path: Path
) -> None:
    ready = tmp_path / "ready"
    podman = _stand_in(tmp_path, body=f"trap '' TERM\n: > '{ready}'", wait_for=tmp_path / "never")
    numbers = _stdio_numbers()
    parent, child = _pair()
    proc = _spawn(built, tmp_path, 0, numbers, child, podman, _env(tmp_path))
    child.close()
    assert _recv(parent)[0] == ACK
    _wait_for(ready)
    podman_pid = _pid_of(tmp_path)
    started = time.monotonic()
    parent.close()
    assert _finish(proc)[0] == 0
    assert time.monotonic() - started >= 1.9, "SIGKILL came before the 2 s grace"
    with pytest.raises(ProcessLookupError):
        os.kill(podman_pid, 0)


@pytest.mark.fr("FR-049")
def test_the_drain_lets_podman_finish_writing_after_the_client_left(
    built: Path, tmp_path: Path
) -> None:
    """Podman writes 300 KiB to a pipe that nobody reads until the client leaves. The drain must let
    it finish, so run exits 0 with podman's done file present, and not by SIGKILL."""
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    numbers = [_high(0), _high(out_w), _high(err_w), _high(out_r), _high(err_r)]
    done = tmp_path / "done"
    ready = tmp_path / "ready"
    podman = _stand_in(
        tmp_path, body=f"trap '' TERM\n: > '{ready}'\nhead -c 300000 /dev/zero\necho ok > '{done}'"
    )
    parent, child = _pair()
    proc = _spawn(built, tmp_path, CAPTURED, numbers, child, podman, _env(tmp_path))
    child.close()
    os.close(out_w)
    os.close(err_w)
    assert _recv(parent)[0] == ACK
    _wait_for(ready)
    parent.close()
    code, err = _finish(proc)
    assert code == 0, err
    assert done.read_text(encoding="utf-8") == "ok\n"
    for fd in (out_r, err_r):
        os.close(fd)


@pytest.mark.fr("FR-123")
def test_the_token_kill_ends_the_tokened_processes_in_the_realm_tree_and_spares_the_rest(
    built: Path, tmp_path: Path
) -> None:
    require_environ_readable()
    token = "c" * 32
    cgroup = tmp_path / "cg"
    (cgroup / "libpod").mkdir(parents=True)
    sleeper = [sys.executable, "-c", "import time; time.sleep(300)"]
    tokened = subprocess.Popen(sleeper, env={"GINN_EXEC_TOKEN": token})  # noqa: S603 - fixed argv
    spared = subprocess.Popen(sleeper, env={"OTHER": "1"})  # noqa: S603 - fixed argv
    _RUNNING.extend([tokened, spared])
    (cgroup / "libpod" / "cgroup.procs").write_text(f"{tokened.pid}\n", encoding="utf-8")
    (cgroup / "cgroup.procs").write_text(f"{spared.pid}\n", encoding="utf-8")
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(built / "ginn-exec-kill"), str(cgroup), token, str(tmp_path / "orphan")],
        capture_output=True,
        text=True,
        check=False,
        timeout=WAIT_S,
    )
    assert run.returncode == 0, run.stderr
    assert tokened.wait(timeout=WAIT_S) == -signal.SIGTERM
    assert spared.poll() is None
    assert not (tmp_path / "orphan").exists()


@pytest.mark.fr("FR-049")
def test_the_run_helper_with_the_header_stays_within_200_lines() -> None:
    """The lines target counts the shared header toward each relay helper (ADR-014)."""
    header = len(HEADER.read_text(encoding="utf-8").splitlines())
    own = len((NATIVE / "ginn-exec-run.c").read_text(encoding="utf-8").splitlines())
    total = own + header
    assert total <= LINE_LIMIT, f"ginn-exec-run.c with the header has {total} lines"


_SURVIVE_SOURCE = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdarg.h>
#include <sys/syscall.h>
/* Test-only: every pidfd_send_signal fails, so a tokened process outlives the helper's bound. */
long syscall(long number, ...) {
	static long (*real)(long, ...);
	va_list ap;
	long a[4];
	int i;
	if (!real) real = (long (*)(long, ...))dlsym(RTLD_NEXT, "syscall");
	va_start(ap, number);
	for (i = 0; i < 4; i++) a[i] = va_arg(ap, long);
	va_end(ap);
	if (number == SYS_pidfd_send_signal) {
		errno = EPERM;
		return -1;
	}
	return real(number, a[0], a[1], a[2], a[3]);
}
"""


@pytest.mark.fr("FR-124")
def test_a_token_process_that_survives_the_bound_leaves_an_orphan_marker(
    built: Path, tmp_path: Path
) -> None:
    """The kill helper sweeps for 10 s. A process whose signals all fail is still there at the
    bound, so the helper writes the orphan marker, prints GINN-W-RELAY-ORPHAN, and exits 3."""
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    source = tmp_path / "survive.c"
    source.write_text(_SURVIVE_SOURCE, encoding="utf-8")
    lib = tmp_path / "libsurvive.so"
    built_lib = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [cc, "-shared", "-fPIC", "-o", str(lib), str(source), "-ldl"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built_lib.returncode == 0, built_lib.stderr
    token = "f" * 32
    cgroup = tmp_path / "cg"
    cgroup.mkdir()
    sleeper = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time; time.sleep(300)"], env={"GINN_EXEC_TOKEN": token}
    )
    _RUNNING.append(sleeper)
    (cgroup / "cgroup.procs").write_text(f"{sleeper.pid}\n", encoding="utf-8")
    marker = tmp_path / "orphan"
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(built / "ginn-exec-kill"), str(cgroup), token, str(marker)],
        env={"LD_PRELOAD": str(lib)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert run.returncode == 3, run.stderr
    assert "GINN-W-RELAY-ORPHAN" in run.stderr, run.stderr
    text = marker.read_text(encoding="utf-8")
    assert f"token {token}" in text and f"pid {sleeper.pid}" in text, text
    assert sleeper.poll() is None, "the shim should have kept the process alive"
    sleeper.kill()
    sleeper.wait(timeout=WAIT_S)


_FORK_SOURCE = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <string.h>
#include <sys/types.h>
/* Test-only: in ginn-exec-run, the second fork (the token kill's) fails with EAGAIN. */
pid_t fork(void) {
	static pid_t (*real)(void);
	static int calls;
	if (!real) real = (pid_t (*)(void))dlsym(RTLD_NEXT, "fork");
	if (strcmp(program_invocation_short_name, "ginn-exec-run") == 0 && ++calls == 2) {
		errno = EAGAIN;
		return -1;
	}
	return real();
}
"""


def _build_shim(tmp: Path, source: str, name: str) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    src = tmp / f"{name}.c"
    src.write_text(source, encoding="utf-8")
    lib = tmp / name
    built_lib = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [cc, "-shared", "-fPIC", "-o", str(lib), str(src), "-ldl"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built_lib.returncode == 0, built_lib.stderr
    return lib


@pytest.mark.fr("FR-124")
def test_a_failed_kill_fork_writes_kill_not_run_and_still_releases_the_lock_and_sends_exit(
    built: Path, tmp_path: Path
) -> None:
    """The token kill cannot start. The marker says so, the lock is free before EXIT,
    and EXIT goes out."""
    lib = _build_shim(tmp_path, _FORK_SOURCE, "libforkfail.so")
    lock = tmp_path / "exec" / f"{REALM}.lock"
    parent, child = _pair()
    env = {**_env(tmp_path), "LD_PRELOAD": str(lib)}
    proc = _spawn(built, tmp_path, LOCK, _stdio_numbers(), child, _stand_in(tmp_path), env)
    child.close()
    assert _recv(parent)[0] == ACK
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert not _lock_busy(lock), "the lock was still held when EXIT arrived"
    assert _finish(proc)[0] == 0
    marker = (tmp_path / "exec" / f"{REALM}.orphan").read_text(encoding="utf-8")
    assert "kill not run" in marker and f"token {TOKEN}" in marker, marker


@pytest.mark.fr("FR-124")
def test_a_survivor_record_is_appended_after_an_earlier_record_and_does_not_replace_it(
    built: Path, tmp_path: Path
) -> None:
    """The kill helper opens the marker for append (ADR-014): an unsettled record stays."""
    lib = _build_shim(tmp_path, _SURVIVE_SOURCE, "libsurvive-append.so")
    token = "f" * 32
    earlier = f"token {'9' * 32}\ntime 1700000000\npid 5\n"
    cgroup = tmp_path / "cg"
    cgroup.mkdir()
    sleepers = [
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", "import time; time.sleep(300)"],
            env={"GINN_EXEC_TOKEN": token},
        )
        for _ in range(2)
    ]
    _RUNNING.extend(sleepers)
    (cgroup / "cgroup.procs").write_text("".join(f"{p.pid}\n" for p in sleepers), encoding="utf-8")
    marker = tmp_path / "orphan"
    marker.write_text(earlier, encoding="utf-8")
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(built / "ginn-exec-kill"), str(cgroup), token, str(marker)],
        env={"LD_PRELOAD": str(lib)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert run.returncode == 3, run.stderr
    text = marker.read_text(encoding="utf-8")
    assert text.startswith(earlier), text
    # One record, written in one write: the token line, the time line, then a pid line per survivor.
    record = text[len(earlier) :].splitlines()
    assert text.endswith("\n") and len(record) == 4, text
    assert record[0] == f"token {token}", text
    assert re.fullmatch(r"time \d+", record[1]), text
    assert sorted(record[2:]) == sorted(f"pid {p.pid}" for p in sleepers), text
    for proc in sleepers:
        proc.kill()
        proc.wait(timeout=WAIT_S)


@pytest.mark.fr("FR-124")
def test_a_kill_not_run_record_is_appended_after_an_earlier_record_and_does_not_replace_it(
    built: Path, tmp_path: Path
) -> None:
    """ginn-exec-run opens the marker for append too, so the same rule holds for its record."""
    lib = _build_shim(tmp_path, _FORK_SOURCE, "libforkfail-append.so")
    earlier = f"token {'9' * 32}\ntime 1700000000\npid 5\n"
    exec_dir = tmp_path / "exec"
    exec_dir.mkdir()
    marker = exec_dir / f"{REALM}.orphan"
    marker.write_text(earlier, encoding="utf-8")
    parent, child = _pair()
    env = {**_env(tmp_path), "LD_PRELOAD": str(lib)}
    proc = _spawn(built, tmp_path, LOCK, _stdio_numbers(), child, _stand_in(tmp_path), env)
    child.close()
    assert _recv(parent)[0] == ACK
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert _finish(proc)[0] == 0
    text = marker.read_text(encoding="utf-8")
    assert text.startswith(earlier), text
    # The record is whole: its token and time lines and its kill-not-run line, written at once.
    assert re.fullmatch(re.escape(earlier) + rf"token {TOKEN}\ntime \d+\nkill not run\n", text), (
        text
    )


@pytest.mark.fr("FR-123")
def test_a_finished_exec_leaves_no_holder_line_for_a_later_refusal_to_name(
    built: Path, tmp_path: Path
) -> None:
    lock = tmp_path / "exec" / f"{REALM}.lock"
    parent, child = _pair()
    proc = _spawn(
        built, tmp_path, LOCK, _stdio_numbers(), child, _stand_in(tmp_path), _env(tmp_path)
    )
    child.close()
    assert _recv(parent)[0] == ACK
    finished_pid = _pid_of(tmp_path)
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert not _lock_busy(lock)
    assert lock.stat().st_size == 0, "the holder line survived the release"
    assert _finish(proc)[0] == 0
    holder = os.open(lock, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        refused_parent, refused_child = _pair()
        second = _spawn(
            built,
            tmp_path,
            LOCK,
            _stdio_numbers(),
            refused_child,
            _stand_in(tmp_path),
            _env(tmp_path),
        )
        refused_child.close()
        frame = _recv(refused_parent)
        assert frame[:2] == bytes([REJECT, 2]) and b"not yet recorded" in frame, frame
        assert str(finished_pid).encode() not in frame, frame
        assert _finish(second)[0] == 0
    finally:
        os.close(holder)


@pytest.mark.fr("FR-123")
def test_a_holder_line_naming_a_dead_pid_is_never_named(built: Path, tmp_path: Path) -> None:
    lock = tmp_path / "exec" / f"{REALM}.lock"
    lock.parent.mkdir(mode=0o700)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 - fixed argv, no shell
    dead.wait(timeout=WAIT_S)
    holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    os.write(holder, f"run {dead.pid} podman {dead.pid}\n".encode())
    try:
        parent, child = _pair()
        second = _spawn(
            built, tmp_path, LOCK, _stdio_numbers(), child, _stand_in(tmp_path), _env(tmp_path)
        )
        child.close()
        frame = _recv(parent)
        assert frame[:2] == bytes([REJECT, 2]) and b"not yet recorded" in frame, frame
        assert str(dead.pid).encode() not in frame, frame
        assert _finish(second)[0] == 0
    finally:
        os.close(holder)


@pytest.mark.fr("FR-123")
def test_a_symlink_where_the_lock_belongs_is_refused_and_never_followed(
    built: Path, tmp_path: Path
) -> None:
    (tmp_path / "exec").mkdir(mode=0o700)
    target = tmp_path / "target"
    (tmp_path / "exec" / f"{REALM}.lock").symlink_to(target)
    parent, child = _pair()
    proc = _spawn(
        built, tmp_path, LOCK, _stdio_numbers(), child, _stand_in(tmp_path), _env(tmp_path)
    )
    child.close()
    code, err = _finish(proc)
    assert code == 1 and "open" in err, err
    assert not target.exists(), "the symlink was followed"


_TRUNCATE_SOURCE = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>
/* Test-only: in ginn-exec-run, ftruncate fails with EIO. Other programs keep the real call. */
int ftruncate(int fd, off_t length) {
	static int (*real)(int, off_t);
	if (!real) real = (int (*)(int, off_t))dlsym(RTLD_NEXT, "ftruncate");
	if (strcmp(program_invocation_short_name, "ginn-exec-run") == 0) {
		errno = EIO;
		return -1;
	}
	return real(fd, length);
}
"""


@pytest.mark.fr("FR-123")
def test_a_failed_holder_clear_is_logged_and_the_exec_still_completes(
    built: Path, tmp_path: Path
) -> None:
    """A holder line that cannot be cleared is logged, not fatal. The exec runs, the lock is
    released before EXIT, and the client gets a definite result, not a lost connection."""
    lib = _build_shim(tmp_path, _TRUNCATE_SOURCE, "libtruncfail.so")
    lock = tmp_path / "exec" / f"{REALM}.lock"
    parent, child = _pair()
    env = {**_env(tmp_path), "LD_PRELOAD": str(lib)}
    proc = _spawn(built, tmp_path, LOCK, _stdio_numbers(), child, _stand_in(tmp_path), env)
    child.close()
    assert _recv(parent)[0] == ACK
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert not _lock_busy(lock), "the lock was still held when EXIT arrived"
    code, err = _finish(proc)
    assert code == 0 and "cannot clear the holder line" in err, err
