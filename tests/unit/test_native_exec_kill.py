# SPDX-License-Identifier: Apache-2.0
"""native/ginn-exec-kill.c: the namespace join, the unknown records and the root refusal (FR-124).

A --root exec's processes belong to container root. Only the rootless namespace above them can
read their environ, so the kill helper joins the outermost ancestor of a foreign namespace that
is its own child. An unreadable process after a join is unknown, unless it predates
ginn-exec-run. Without a join the record says only `unknown join`.

The tests run the helper directly on a fake cgroup tree of the test's own sleepers. Some
sleepers start in a user namespace (preexec_fn unshare), so the helper's setns is real. A shim
(LD_PRELOAD) logs each setns and the namespace it named, can make setns fail, can make every
environ open fail with EACCES (as a process of another owner does), can make the environ open of a
zombie fail with EACCES, can kill a process as its environ is opened or read, can make every environ
read fail with EIO, and can make geteuid return 0. Nothing here runs podman or touches a realm. Gate
G7 on the Void host is the real check."""

from __future__ import annotations

import errno
import os
import re
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
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
/* Test-only. GINN_TEST_SETNS_LOG collects "<nstype> <result> <namespace>" per setns.
 * GINN_TEST_SETNS_FAIL makes setns fail with EPERM. GINN_TEST_ENVIRON_EACCES makes each open of a
 * .../environ path fail with EACCES. GINN_TEST_ZOMBIE_ENVIRON_ERRNO=<errno> makes the open of the
 * environ of a zombie fail with that errno. GINN_TEST_ENVIRON_EXIT=<pid> makes the open of that
 * pid's environ kill it, wait until it is a zombie, and fail with EACCES.
 * GINN_TEST_ENVIRON_EXIT_READ=<pid> does the same inside that pid's environ read and fails the read
 * with EIO. GINN_TEST_ESRCH_PID=<pid> with GINN_TEST_ESRCH_SWEEPS=<n> fails that pid's environ
 * open with ESRCH n times while it lives, then kills it. GINN_TEST_ENVIRON_EIO makes each read of
 * a .../environ fd fail with EIO.
 * GINN_TEST_EUID_ROOT makes geteuid return 0. */
static int environ_pid(const char *path) {
	const char *p;
	int pid;
	if (strncmp(path, "/proc/", 6)) return -1;
	p = path + 6;
	if (*p < '0' || *p > '9') return -1;
	pid = atoi(p);
	while (*p >= '0' && *p <= '9') p++;
	return !strcmp(p, "/environ") ? pid : -1;
}

/* The state letter of pid from /proc/<pid>/stat, or 0 when it cannot be read. */
static char state_of(int pid) {
	char path[64], buf[256], *p, c = 0;
	FILE *f;
	snprintf(path, sizeof path, "/proc/%d/stat", pid);
	f = fopen(path, "r");
	if (!f) return 0;
	if (fgets(buf, sizeof buf, f) && (p = strrchr(buf, ')')) && p[1] == ' ') c = p[2];
	fclose(f);
	return c;
}

/* Test-only. Kills pid, and returns once its state is Z or after 5 s. */
static void kill_and_wait(int pid) {
	struct timespec tick = {0, 10000000};
	int i;
	kill(pid, SIGKILL);
	for (i = 0; i < 500 && state_of(pid) != 'Z'; i++) nanosleep(&tick, NULL);
}
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
	int mode = 0, pid = environ_pid(path);
	va_list ap;
	if (!real) real = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open");
	if (getenv("GINN_TEST_ENVIRON_EACCES") && n >= 8 && !strcmp(path + n - 8, "/environ")) {
		errno = EACCES;
		return -1;
	}
	if (getenv("GINN_TEST_ZOMBIE_ENVIRON_ERRNO") && pid >= 0 && state_of(pid) == 'Z') {
		errno = atoi(getenv("GINN_TEST_ZOMBIE_ENVIRON_ERRNO"));
		return -1;
	}
	if (getenv("GINN_TEST_ESRCH_PID") && pid > 0 && pid == atoi(getenv("GINN_TEST_ESRCH_PID"))) {
		static int seen;
		if (seen++ >= atoi(getenv("GINN_TEST_ESRCH_SWEEPS"))) kill_and_wait(pid);
		errno = ESRCH;
		return -1;
	}
	const char *exit_pid = getenv("GINN_TEST_ENVIRON_EXIT");
	if (exit_pid && pid > 0 && pid == atoi(exit_pid)) {
		kill_and_wait(pid);
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

ssize_t read(int fd, void *buf, size_t count) {
	static ssize_t (*real)(int, void *, size_t);
	char link[256] = "", path[64];
	ssize_t k;
	int pid;
	if (!real) real = (ssize_t (*)(int, void *, size_t))dlsym(RTLD_NEXT, "read");
	if (getenv("GINN_TEST_ENVIRON_EIO")) {
		snprintf(path, sizeof path, "/proc/self/fd/%d", fd);
		k = readlink(path, link, sizeof link - 1);
		if (k > 8 && !strcmp(link + k - 8, "/environ")) {
			errno = EIO;
			return -1;
		}
	}
	if (getenv("GINN_TEST_ENVIRON_EXIT_READ")) {
		snprintf(path, sizeof path, "/proc/self/fd/%d", fd);
		k = readlink(path, link, sizeof link - 1);
		link[k < 0 ? 0 : k] = '\0';
		pid = environ_pid(link);
		if (pid > 0 && pid == atoi(getenv("GINN_TEST_ENVIRON_EXIT_READ"))) {
			kill_and_wait(pid);
			errno = EIO;
			return -1;
		}
	}
	return real(fd, buf, count);
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


def _err(code: int) -> str:
    """The text the helper prints for errno `code`: its strerror, then the number (FR-124)."""
    return f"{os.strerror(code)} (errno {code})"


@pytest.mark.fr("FR-124")
def test_processes_in_the_helpers_own_namespace_are_killed_and_no_join_is_attempted(
    kill_bin: Path, tmp_path: Path
) -> None:
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
    # Each unread process is named with its step and errno, and the failed join with its errno.
    unread = [candidate.pid, tokened.pid]
    for pid in unread:
        assert f"  pid {pid}: open environ: {_err(errno.EACCES)}\n" in run.stderr, run.stderr
    join = f"  join: setns above pid {candidate.pid}: {_err(errno.EPERM)}\n"
    assert join in run.stderr, run.stderr
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
    # Only the young process is unread; the join succeeded, so no join line is printed.
    assert f"  pid {young.pid}: open environ: {_err(errno.EACCES)}\n" in err, err
    assert "  join:" not in err and f"  pid {candidate.pid}:" not in err, err
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


@pytest.mark.fr("FR-124")
def test_the_warning_names_each_unread_process_with_its_step_and_errno(
    kill_bin: Path, tmp_path: Path
) -> None:
    """Both processes share the helper's namespace, so no join is attempted. Each environ open
    fails with EACCES. The warning names each pid with its step and errno, then says why there
    was no join."""
    lib = _build_shim(tmp_path)
    first = _sleeper({"OTHER": "1"}, tmp_path / "ready-a", namespaced=False)
    second = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready-b", namespaced=False)
    cgroup = _cgroup(tmp_path, first.pid, second.pid)
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_ENVIRON_EACCES": "1"}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 3, run.stderr
    warning = "GINN-W-RELAY-ORPHAN: the realm's processes could not be read for their token;"
    assert run.stderr.splitlines() == [
        f"{warning} left running",
        f"  pid {first.pid}: open environ: {_err(errno.EACCES)}",
        f"  pid {second.pid}: open environ: {_err(errno.EACCES)}",
        "  join: not attempted, no realm process is in a joinable user namespace",
    ], run.stderr
    assert first.poll() is None and second.poll() is None, "an unreadable process was killed"
    text = marker.read_text(encoding="utf-8")
    assert re.fullmatch(rf"token {TOKEN}\ntime \d+\nunknown join\n", text), text


@pytest.mark.fr("FR-124")
def test_a_failed_environ_read_is_named_with_the_read_step_and_its_errno(
    kill_bin: Path, tmp_path: Path
) -> None:
    """The open succeeds and the read fails with EIO, so the warning names the read step."""
    lib = _build_shim(tmp_path)
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready", namespaced=False)
    cgroup = _cgroup(tmp_path, tokened.pid)
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_ENVIRON_EIO": "1"}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 3, run.stderr
    assert f"  pid {tokened.pid}: read environ: {_err(errno.EIO)}\n" in run.stderr, run.stderr
    assert "  join: not attempted" in run.stderr, run.stderr
    assert tokened.poll() is None, "an unreadable process was killed"


@pytest.mark.fr("FR-124")
def test_the_unread_detail_is_bounded_to_eight_pids_and_then_a_count(
    kill_bin: Path, tmp_path: Path
) -> None:
    """Ten unreadable processes: the warning names the first eight with their step and errno,
    then counts the other two. None is killed."""
    lib = _build_shim(tmp_path)
    sleepers = [
        _sleeper({"OTHER": "1"}, tmp_path / f"ready-{index}", namespaced=False)
        for index in range(10)
    ]
    cgroup = _cgroup(tmp_path, *(sleeper.pid for sleeper in sleepers))
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_ENVIRON_EACCES": "1"}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 3, run.stderr
    lines = run.stderr.splitlines()
    named = [line for line in lines if line.startswith("  pid ")]
    expected = [f"  pid {s.pid}: open environ: {_err(errno.EACCES)}" for s in sleepers[:8]]
    assert named == expected, run.stderr
    assert lines[-2:] == [
        "  and 2 more unread processes",
        "  join: not attempted, no realm process is in a joinable user namespace",
    ], run.stderr
    assert all(sleeper.poll() is None for sleeper in sleepers), "an unreadable process was killed"


@pytest.mark.fr("FR-124")
@pytest.mark.parametrize("refused", [errno.EACCES, errno.ESRCH], ids=["eacces", "esrch"])
def test_a_zombie_whose_environ_open_is_refused_is_gone_and_not_recorded(
    kill_bin: Path, tmp_path: Path, refused: int
) -> None:
    """The first sweep SIGTERMs the tokened sleeper. The test does not reap it, so the next sweep
    finds a zombie whose environ open the shim refuses with the errno of the kernel (EACCES on the
    GitHub runner, ESRCH on the Void host). The helper checks the pidfd first, and a single-thread
    zombie's pidfd is readable, so the zombie is gone: no warning, no marker, exit 0."""
    lib = _build_shim(tmp_path)
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready", namespaced=False)
    cgroup = _cgroup(tmp_path, tokened.pid)
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_ZOMBIE_ENVIRON_ERRNO": str(refused)}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 0, run.stderr
    assert run.stderr == "", run.stderr
    assert not marker.exists()
    assert _dead(tokened.pid), "the helper did not leave the killed process unreaped"
    assert tokened.wait(timeout=WAIT_S) == -signal.SIGTERM


@pytest.mark.fr("FR-124")
def test_a_process_that_exits_while_its_environ_is_read_is_gone_and_not_recorded(
    kill_bin: Path, tmp_path: Path
) -> None:
    """The shim kills the tokened sleeper as the helper opens its environ, waits until it is a
    zombie and refuses the open with EACCES. The helper checks the pidfd again after the failure, so
    the process is gone: no warning, no marker, exit 0, and the sleeper died of its SIGKILL."""
    lib = _build_shim(tmp_path)
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready", namespaced=False)
    cgroup = _cgroup(tmp_path, tokened.pid)
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_ENVIRON_EXIT": str(tokened.pid)}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 0, run.stderr
    assert run.stderr == "", run.stderr
    assert not marker.exists()
    assert tokened.wait(timeout=WAIT_S) == -signal.SIGKILL


@pytest.mark.fr("FR-124")
def test_a_process_that_exits_inside_its_environ_read_is_gone_and_not_recorded(
    kill_bin: Path, tmp_path: Path
) -> None:
    """The shim kills the tokened sleeper inside the helper's environ read, waits until it is a
    zombie and fails the read with EIO. The helper checks the pidfd after the failure, so the
    process is gone: no warning, no marker, exit 0, and the sleeper died of its SIGKILL."""
    lib = _build_shim(tmp_path)
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready", namespaced=False)
    cgroup = _cgroup(tmp_path, tokened.pid)
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_ENVIRON_EXIT_READ": str(tokened.pid)}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 0, run.stderr
    assert run.stderr == "", run.stderr
    assert not marker.exists()
    assert tokened.wait(timeout=WAIT_S) == -signal.SIGKILL


_THREADED = """
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>
static void *hold(void *ready) {
    FILE *f = fopen((const char *)ready, "w");
    if (f) fclose(f);
    sleep(300);
    return NULL;
}
int main(int argc, char **argv) {
    pthread_t t;
    if (argc != 2 || pthread_create(&t, NULL, hold, argv[1]) != 0) return 1;
    pthread_exit(NULL);
}
"""
"""Starts a worker that touches its ready file and sleeps, then exits its main thread, so the
process lives on as its worker while its leader is a zombie."""


def _build_threaded(tmp: Path) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    src = tmp / "threaded.c"
    src.write_text(_THREADED, encoding="utf-8")
    exe = tmp / "threaded"
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [cc, "-pthread", "-o", str(exe), str(src)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return exe


def _state(pid: int) -> str:
    """The state letter of pid, from /proc/<pid>/stat."""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return stat.rsplit(")", 1)[1].split()[0]


def _threaded_leader(tmp: Path) -> subprocess.Popen[bytes]:
    """A tokened process whose main thread exited while its worker sleeps: the leader is a zombie
    (stat state Z), the process lives, and its pidfd is not readable."""
    exe = _build_threaded(tmp)
    ready = tmp / "ready"
    leader = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(exe), str(ready)], env={"GINN_EXEC_TOKEN": TOKEN}
    )
    _RUNNING.append(leader)
    deadline = time.monotonic() + WAIT_S
    while not (ready.exists() and _state(leader.pid) == "Z"):
        assert time.monotonic() < deadline, "the leader never became a zombie beside its worker"
        time.sleep(0.02)
    pidfd = os.pidfd_open(leader.pid)
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        assert poller.poll(0) == [], "the pidfd reads as exited while a thread still runs"
    finally:
        os.close(pidfd)
    return leader


@pytest.mark.fr("FR-124")
def test_a_main_thread_that_exits_while_a_worker_runs_is_killed_through_the_worker(
    kill_bin: Path, tmp_path: Path
) -> None:
    """The leader's environ open gives ESRCH, as on the Void host. The helper reads the worker's
    environ, which holds the token, and SIGTERMs the process through its pidfd. The worker dies, the
    process is gone on the next sweep: exit 0, no warning, no marker."""
    lib = _build_shim(tmp_path)
    leader = _threaded_leader(tmp_path)
    cgroup = _cgroup(tmp_path, leader.pid)
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_ZOMBIE_ENVIRON_ERRNO": str(errno.ESRCH)}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 0, run.stderr
    assert run.stderr == "", run.stderr
    assert not marker.exists()
    assert leader.wait(timeout=WAIT_S) == -signal.SIGTERM


@pytest.mark.fr("FR-124")
def test_a_main_thread_that_exits_while_a_worker_runs_is_unread_when_its_environ_is_refused(
    kill_bin: Path, tmp_path: Path
) -> None:
    """An EACCES on the zombie leader's environ does not start the worker fallback, so the
    process is unread: the run exits 3 with the warning and an unknown-join record, and nothing is
    killed."""
    lib = _build_shim(tmp_path)
    leader = _threaded_leader(tmp_path)
    cgroup = _cgroup(tmp_path, leader.pid)
    marker = tmp_path / "orphan"
    env = {"LD_PRELOAD": str(lib), "GINN_TEST_ZOMBIE_ENVIRON_ERRNO": str(errno.EACCES)}
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 3, run.stderr
    assert f"  pid {leader.pid}: open environ: {_err(errno.EACCES)}\n" in run.stderr, run.stderr
    assert "could not be read" in run.stderr, run.stderr
    text = marker.read_text(encoding="utf-8")
    assert re.fullmatch(rf"token {TOKEN}\ntime \d+\nunknown join\n", text), text
    assert leader.poll() is None, "the process with a live worker was reaped or ended"


@pytest.mark.fr("FR-124")
def test_a_process_still_exiting_at_the_next_sweep_is_pending_then_gone(
    kill_bin: Path, tmp_path: Path
) -> None:
    """The environ of the tokened sleeper gives ESRCH for three sweeps while its pidfd is not
    readable, as a process does while its memory is released. Then it exits. The helper keeps
    sweeping while the process is pending, finds its pidfd readable, and exits 0 with no warning and
    no marker."""
    lib = _build_shim(tmp_path)
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready", namespaced=False)
    cgroup = _cgroup(tmp_path, tokened.pid)
    marker = tmp_path / "orphan"
    env = {
        "LD_PRELOAD": str(lib),
        "GINN_TEST_ESRCH_PID": str(tokened.pid),
        "GINN_TEST_ESRCH_SWEEPS": "3",
    }
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 0, run.stderr
    assert run.stderr == "", run.stderr
    assert not marker.exists()
    assert tokened.wait(timeout=WAIT_S) == -signal.SIGKILL


@pytest.mark.fr("FR-124")
def test_a_process_that_stays_pending_past_the_bound_is_recorded_unknown(
    kill_bin: Path, tmp_path: Path
) -> None:
    """The environ of the sleeper gives ESRCH on every sweep and the process never exits. The
    helper sweeps to its 10 s bound, then records the pending process as unread: exit 3 with the
    warning and an unknown-join record. Nothing is killed."""
    lib = _build_shim(tmp_path)
    tokened = _sleeper({"GINN_EXEC_TOKEN": TOKEN}, tmp_path / "ready", namespaced=False)
    cgroup = _cgroup(tmp_path, tokened.pid)
    marker = tmp_path / "orphan"
    env = {
        "LD_PRELOAD": str(lib),
        "GINN_TEST_ESRCH_PID": str(tokened.pid),
        "GINN_TEST_ESRCH_SWEEPS": "1000000",
    }
    run = _run_kill(kill_bin, cgroup, marker, env)
    assert run.returncode == 3, run.stderr
    assert "could not be read" in run.stderr, run.stderr
    assert f"  pid {tokened.pid}: open environ: {_err(errno.ESRCH)}\n" in run.stderr, run.stderr
    text = marker.read_text(encoding="utf-8")
    assert re.fullmatch(rf"token {TOKEN}\ntime \d+\nunknown join\n", text), text
    assert tokened.poll() is None, "a pending process was killed"


@pytest.mark.fr("FR-124")
def test_a_lone_process_with_an_empty_environ_holds_no_token_and_is_spared(
    kill_bin: Path, tmp_path: Path
) -> None:
    """A live process that cleared its environment reads zero bytes, and it has no other thread,
    so it holds no token: the helper spares it with no warning and no marker."""
    lone = _sleeper({}, tmp_path / "ready", namespaced=False)
    cgroup = _cgroup(tmp_path, lone.pid)
    marker = tmp_path / "orphan"
    run = _run_kill(kill_bin, cgroup, marker, {})
    assert run.returncode == 0, run.stderr
    assert run.stderr == "", run.stderr
    assert not marker.exists()
    assert lone.poll() is None, "a process with no token was killed"
