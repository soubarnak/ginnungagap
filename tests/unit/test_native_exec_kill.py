# SPDX-License-Identifier: Apache-2.0
"""native/ginn-exec-kill.c: the namespace join, the unknown records and the root refusal (FR-124).

A --root exec's processes belong to container root. Only the rootless namespace above them can
read their environ, so the kill helper joins the outermost ancestor of a foreign namespace that
is its own child. An unreadable process after a join is unknown, unless it predates
ginn-exec-run. Without a join the record says only `unknown join`.

The tests run the helper directly on a fake cgroup tree of the test's own sleepers. Some
sleepers start in a user namespace (preexec_fn unshare), so the helper's setns is real. A shim
(LD_PRELOAD) logs each setns and the namespace it named, can make setns fail, can make every
environ open fail with EACCES (as a process of another owner does), and can make geteuid return
0. Nothing here runs podman or touches a realm. Gate G7 on the Void host is the real check."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.environ_support import require_environ_readable

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "native"
WARNINGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2"]
WAIT_S = 15.0
TOKEN = "a" * 32
CLONE = os.CLONE_NEWUSER
_RUNNING: list[subprocess.Popen[bytes]] = []
"""Every process a test started, so a failing test leaves none running."""

_SHIM = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>
/* Test-only. GINN_TEST_SETNS_LOG collects "<nstype> <result> <namespace>" per setns.
 * GINN_TEST_SETNS_FAIL makes setns fail with EPERM. GINN_TEST_ENVIRON_EACCES makes each open of a
 * .../environ path fail with EACCES. GINN_TEST_EUID_ROOT makes geteuid return 0. */
int setns(int fd, int nstype) {
	static int (*real)(int, int);
	const char *log = getenv("GINN_TEST_SETNS_LOG");
	char link[128] = "", path[64];
	ssize_t k;
	int rc;
	if (!real) real = (int (*)(int, int))dlsym(RTLD_NEXT, "setns");
	snprintf(path, sizeof path, "/proc/self/fd/%d", fd);
	k = readlink(path, link, sizeof link - 1);
	link[k < 0 ? 0 : k] = '\0';
	if (getenv("GINN_TEST_SETNS_FAIL")) {
		errno = EPERM;
		rc = -1;
	} else {
		rc = real(fd, nstype);
	}
	if (log) {
		FILE *f = fopen(log, "a");
		if (f) fprintf(f, "%d %d %s\n", nstype, rc, link), fclose(f);
	}
	return rc;
}

uid_t geteuid(void) {
	static uid_t (*real)(void);
	if (getenv("GINN_TEST_EUID_ROOT")) return 0;
	if (!real) real = (uid_t (*)(void))dlsym(RTLD_NEXT, "geteuid");
	return real();
}

int open(const char *path, int flags, ...) {
	static int (*real)(const char *, int, ...);
	size_t n = strlen(path);
	int mode = 0;
	va_list ap;
	if (!real) real = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open");
	if (getenv("GINN_TEST_ENVIRON_EACCES") && n >= 8 && !strcmp(path + n - 8, "/environ")) {
		errno = EACCES;
		return -1;
	}
	if (flags & O_CREAT) {
		va_start(ap, flags);
		mode = va_arg(ap, int);
		va_end(ap);
		return real(path, flags, mode);
	}
	return real(path, flags);
}
"""

_SLEEPER = "import pathlib, sys, time\npathlib.Path(sys.argv[1]).touch()\ntime.sleep(300)\n"
"""A sleeper that touches its ready file, then sleeps. Its environment carries the token, if any."""

_OUTER = (
    "import os, pathlib, subprocess, sys, time\n"
    "os.unshare(os.CLONE_NEWUSER)\n"
    "pathlib.Path('/proc/self/setgroups').write_text('deny', encoding='utf-8')\n"
    "pathlib.Path('/proc/self/uid_map').write_text(f'0 {sys.argv[3]} 1', encoding='utf-8')\n"
    "pathlib.Path('/proc/self/gid_map').write_text(f'0 {sys.argv[4]} 1', encoding='utf-8')\n"
    "inner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],\n"
    "    env={'GINN_EXEC_TOKEN': sys.argv[1]}, preexec_fn=lambda: os.unshare(os.CLONE_NEWUSER))\n"
    "pathlib.Path(sys.argv[2]).write_text(str(inner.pid), encoding='utf-8')\n"
    "time.sleep(300)\n"
)
"""A process in a user namespace that starts a child in another one, for the nested case."""

_GATED = 'while [ ! -e "$0" ]; do sleep 0.02; done; "$1" "$2" "$3" "$4"; exit $?'
"""Waits for the gate file, then runs the helper as its child, so the helper's parent starts with
the shell. The helper's argv arrives as shell arguments and is never built into the script."""
SH = shutil.which("sh") or "/bin/sh"


@pytest.fixture(scope="module")
def kill_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    out = tmp_path_factory.mktemp("exec-kill-bin") / "ginn-exec-kill"
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [cc, *WARNINGS, "-o", str(out), str(NATIVE / "ginn-exec-kill.c")],
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


def _build_shim(tmp: Path) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    src = tmp / "shim.c"
    src.write_text(_SHIM, encoding="utf-8")
    lib = tmp / "libkillshim.so"
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [cc, "-shared", "-fPIC", "-o", str(lib), str(src), "-ldl"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return lib


def _require_userns() -> None:
    """Skip when this sandbox forbids the unprivileged user namespaces the join tests need."""
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import os; os.unshare(os.CLONE_NEWUSER)"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("unprivileged user namespaces are not allowed in this sandbox")


def _unshare() -> None:
    os.unshare(os.CLONE_NEWUSER)


def _sleeper(env: dict[str, str], ready: Path, *, namespaced: bool) -> subprocess.Popen[bytes]:
    """A sleeper that touches `ready` once it runs. A namespaced one starts in a user namespace."""
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _SLEEPER, str(ready)],
        env=env,
        preexec_fn=_unshare if namespaced else None,  # noqa: PLW1509 - single-threaded test
    )
    _RUNNING.append(proc)
    deadline = time.monotonic() + WAIT_S
    while not ready.exists():
        assert time.monotonic() < deadline, "the sleeper never started"
        time.sleep(0.02)
    return proc


def _cgroup(tmp: Path, *pids: int) -> Path:
    cgroup = tmp / "cg"
    cgroup.mkdir(exist_ok=True)
    (cgroup / "cgroup.procs").write_text("".join(f"{pid}\n" for pid in pids), encoding="utf-8")
    return cgroup


def _ns(pid: int) -> str:
    """The user namespace of pid, as /proc names it: user:[inode]."""
    return os.readlink(f"/proc/{pid}/ns/user")


def _dead(pid: int) -> bool:
    """True when pid has exited or is a zombie, which its parent, the test, does not reap."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def _run_kill(
    kill: Path, cgroup: Path, marker: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(kill), str(cgroup), TOKEN, str(marker)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _gated(
    kill: Path, gate: Path, cgroup: Path, marker: Path, env: dict[str, str]
) -> subprocess.Popen[bytes]:
    env = {**env, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [SH, "-c", _GATED, str(gate), str(kill), str(cgroup), TOKEN, str(marker)],
        env=env,
        stderr=subprocess.PIPE,
    )
    _RUNNING.append(proc)
    return proc


def _finish(proc: subprocess.Popen[bytes]) -> tuple[int, str]:
    _out, err = proc.communicate(timeout=WAIT_S)
    return proc.returncode, err.decode("utf-8")


@pytest.mark.fr("FR-124")
def test_processes_in_the_helpers_own_namespace_are_killed_and_no_join_is_attempted(
    kill_bin: Path, tmp_path: Path
) -> None:
    require_environ_readable()
    lib = _build_shim(tmp_path)
    log = tmp_path / "setns.log"
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready-a", namespaced=False)
    spared = _sleeper({"OTHER": "1"}, tmp_path / "ready-b", namespaced=False)
    cgroup = _cgroup(tmp_path, tokened.pid, spared.pid)
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_SETNS_LOG": str(log)}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 0, run.stderr
    assert tokened.wait(timeout=WAIT_S) == -signal.SIGTERM
    assert spared.poll() is None
    assert not log.exists(), "the helper tried to join a namespace it already shares"
    assert not marker.exists()


@pytest.mark.fr("FR-124")
def test_a_token_process_in_another_namespace_is_joined_then_killed_through_its_pidfd(
    kill_bin: Path, tmp_path: Path
) -> None:
    require_environ_readable()
    _require_userns()
    lib = _build_shim(tmp_path)
    log = tmp_path / "setns.log"
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready", namespaced=True)
    ns = _ns(tokened.pid)
    cgroup = _cgroup(tmp_path, tokened.pid)
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_SETNS_LOG": str(log)}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 0, run.stderr
    assert tokened.wait(timeout=WAIT_S) == -signal.SIGTERM
    assert log.read_text(encoding="utf-8").splitlines() == [f"{CLONE} 0 {ns}"]
    assert not marker.exists()


@pytest.mark.fr("FR-124")
def test_the_join_goes_to_the_outermost_ancestor_not_to_the_nested_namespace_of_the_token(
    kill_bin: Path, tmp_path: Path
) -> None:
    """The token process sits in a namespace nested under the outer one. The helper must join the
    outer one, which holds the subuid mapping, and kill the token process through it."""
    require_environ_readable()
    _require_userns()
    lib = _build_shim(tmp_path)
    log = tmp_path / "setns.log"
    pidfile = tmp_path / "inner.pid"
    outer = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _OUTER, TOKEN, str(pidfile), str(os.getuid()), str(os.getgid())]
    )
    _RUNNING.append(outer)
    deadline = time.monotonic() + WAIT_S
    while not (pidfile.exists() and pidfile.read_text(encoding="utf-8").strip().isdigit()):
        assert time.monotonic() < deadline, "the outer process never started its child"
        time.sleep(0.02)
    inner = int(pidfile.read_text(encoding="utf-8"))
    try:
        outer_ns, inner_ns = _ns(outer.pid), _ns(inner)
        cgroup = _cgroup(tmp_path, inner)
        marker = tmp_path / "orphan"
        run = _run_kill(
            kill_bin, cgroup, marker, {"LD_PRELOAD": str(lib), "GINN_TEST_SETNS_LOG": str(log)}
        )
        assert run.returncode == 0, run.stderr
        assert outer_ns != inner_ns
        assert log.read_text(encoding="utf-8").splitlines() == [f"{CLONE} 0 {outer_ns}"]
        deadline = time.monotonic() + WAIT_S
        while not _dead(inner):
            assert time.monotonic() < deadline, "the nested token process survived"
            time.sleep(0.02)
    finally:
        try:
            os.kill(inner, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.fr("FR-124")
def test_a_refused_join_records_only_unknown_join_and_spares_the_processes(
    kill_bin: Path, tmp_path: Path
) -> None:
    """A refused join: the record says only that unreadable processes exist, and none is killed."""
    _require_userns()
    lib = _build_shim(tmp_path)
    log = tmp_path / "setns.log"
    candidate = _sleeper({"OTHER": "1"}, tmp_path / "ready-a", namespaced=True)
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready-b", namespaced=False)
    cgroup = _cgroup(tmp_path, candidate.pid, tokened.pid)
    marker = tmp_path / "orphan"
    env = {
        "LD_PRELOAD": str(lib),
        "GINN_TEST_SETNS_LOG": str(log),
        "GINN_TEST_SETNS_FAIL": "1",
        "GINN_TEST_ENVIRON_EACCES": "1",
    }
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 3, run.stderr
    assert "GINN-W-RELAY-ORPHAN" in run.stderr and "could not be read" in run.stderr, run.stderr
    text = marker.read_text(encoding="utf-8")
    assert re.fullmatch(rf"token {TOKEN}\ntime \d+\nunknown join\n", text), text
    assert tokened.poll() is None and candidate.poll() is None, "an unreadable process was killed"
    assert log.read_text(encoding="utf-8").splitlines() == [f"{CLONE} -1 {_ns(candidate.pid)}"]


@pytest.mark.fr("FR-124")
def test_without_a_candidate_no_join_is_tried_and_the_record_says_unknown_join(
    kill_bin: Path, tmp_path: Path
) -> None:
    """Every process is in the helper's own namespace, so there is nothing to join."""
    lib = _build_shim(tmp_path)
    log = tmp_path / "setns.log"
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready", namespaced=False)
    cgroup = _cgroup(tmp_path, tokened.pid)
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_SETNS_LOG": str(log), "GINN_TEST_ENVIRON_EACCES": "1"}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 3, run.stderr
    assert re.fullmatch(
        rf"token {TOKEN}\ntime \d+\nunknown join\n", marker.read_text(encoding="utf-8")
    )
    assert tokened.poll() is None
    assert not log.exists(), "a join was attempted with no process in another namespace"


@pytest.mark.fr("FR-124")
def test_after_a_join_an_unreadable_process_that_predates_the_parent_is_not_recorded(
    kill_bin: Path, tmp_path: Path
) -> None:
    """Both processes started before the parent shell, so neither can be the exec's. The join
    succeeds and nothing is recorded."""
    _require_userns()
    lib = _build_shim(tmp_path)
    log = tmp_path / "setns.log"
    candidate = _sleeper({"OTHER": "1"}, tmp_path / "ready-a", namespaced=True)
    old = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready-b", namespaced=False)
    cgroup = _cgroup(tmp_path, candidate.pid, old.pid)
    time.sleep(0.1)  # clock ticks are 10 ms, so the parent must start clearly later
    gate = tmp_path / "gate"
    gate.touch()
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_SETNS_LOG": str(log), "GINN_TEST_ENVIRON_EACCES": "1"}
    code, err = _finish(_gated(kill_bin, gate, cgroup, marker, env))
    assert code == 0, err
    assert not marker.exists()
    assert old.poll() is None
    assert log.read_text(encoding="utf-8").splitlines() == [f"{CLONE} 0 {_ns(candidate.pid)}"]


@pytest.mark.fr("FR-124")
def test_after_a_join_an_unreadable_process_that_started_after_the_parent_is_unknown(
    kill_bin: Path, tmp_path: Path
) -> None:
    """The process started after the parent shell, so it can be the exec's. It is listed by pid,
    and it is not killed, because its environ cannot be read."""
    _require_userns()
    lib = _build_shim(tmp_path)
    log = tmp_path / "setns.log"
    candidate = _sleeper({"OTHER": "1"}, tmp_path / "ready-a", namespaced=True)
    cgroup = _cgroup(tmp_path)
    time.sleep(0.1)
    gate = tmp_path / "gate"
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_SETNS_LOG": str(log), "GINN_TEST_ENVIRON_EACCES": "1"}
    shell = _gated(kill_bin, gate, cgroup, marker, env)
    time.sleep(0.1)
    young = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready-b", namespaced=False)
    _cgroup(tmp_path, candidate.pid, young.pid)
    gate.touch()
    code, err = _finish(shell)
    assert code == 3, err
    assert "whose token could not be read" in err, err
    text = marker.read_text(encoding="utf-8")
    assert re.fullmatch(rf"token {TOKEN}\ntime \d+\nunknown {young.pid}\n", text), text
    assert young.poll() is None
    assert log.read_text(encoding="utf-8").splitlines() == [f"{CLONE} 0 {_ns(candidate.pid)}"]


@pytest.mark.fr("CR-003")
def test_the_helper_refuses_to_run_as_euid_zero_before_it_touches_anything(
    kill_bin: Path, tmp_path: Path
) -> None:
    lib = _build_shim(tmp_path)
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready", namespaced=False)
    cgroup = _cgroup(tmp_path, tokened.pid)
    marker = tmp_path / "orphan"
    run = _run_kill(kill_bin, cgroup, marker, {"LD_PRELOAD": str(lib), "GINN_TEST_EUID_ROOT": "1"})
    assert run.returncode == 2, run.stderr
    assert "refuses to run as root" in run.stderr, run.stderr
    assert tokened.poll() is None
    assert not marker.exists()
