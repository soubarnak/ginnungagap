# SPDX-License-Identifier: Apache-2.0
"""Start and stop the helper processes of the security modules: ginn-agent-filter and
xdg-dbus-proxy (FR-067, FR-068).

`Launcher` is the one shape the lifecycle code takes. The lifecycle asks for a child by argv, and
gets back a `Child` that is ready to accept connections, or a GinnError with the code the caller
names. Unit tests pass a fake launcher, so no process starts. `start_ready` is the real launcher.
It runs the argv as a list (NFR-SEC-005), with no shell, and it polls the socket path with a probe
connection until the helper listens.

A helper leads its own process group (`start_new_session`), and the clients it forks share that
group. Stopping a helper therefore signals the group, so no client that is still connected keeps
forwarding after the stop. `Proc` is the record a ginn command keeps for a helper: its pid and its
start time, so that a later command can tell the helper from a pid the kernel has reused.
"""

from __future__ import annotations

import os
import signal
import socket
import stat
import struct
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, TypeGuard

from ginn.errors import GinnError
from ginn.store.journal import is_pid_alive, process_start_ticks

START_TIMEOUT_S: Final = 5.0
"""How long a helper may take to listen. A helper that is not up by then has failed."""
STOP_TIMEOUT_S: Final = 5.0
"""How long a helper may take to exit after SIGTERM before its group is killed."""
PROBE_S: Final = 0.2
POLL_S: Final = 0.02
LOG_TAIL_BYTES: Final = 4096


def _is_int(value: object) -> TypeGuard[int]:
    """True for an int that is not a bool. JSON `true` must never read as a pid or a time."""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Proc:
    """A helper process as a ginn record keeps it: its pid and its start time in clock ticks.

    `alive` is the one test that every caller uses. It is `store.journal.is_pid_alive`, so a zombie
    counts as dead, and a pid whose start time differs counts as a reused pid.
    """

    pid: int
    start_ticks: int

    @classmethod
    def of(cls, pid: int) -> Proc | None:
        """The record of the running process `pid`, or None when /proc has no entry for it."""
        ticks = process_start_ticks(pid)
        return None if ticks is None else cls(pid, ticks)

    def alive(self) -> bool:
        return is_pid_alive(self.pid, self.start_ticks)

    def to_dict(self) -> dict[str, int]:
        return {"pid": self.pid, "start_ticks": self.start_ticks}

    @classmethod
    def from_dict(cls, data: object) -> Proc:
        """Read the record back. A missing key, a bool or a negative value raises ValueError."""
        if not isinstance(data, dict):
            raise ValueError("a process record is an object with pid and start_ticks")
        pid, ticks = data.get("pid"), data.get("start_ticks")
        if not _is_int(pid) or not _is_int(ticks) or pid <= 0 or ticks < 0:
            raise ValueError("a process record needs a positive pid and a start time")
        return cls(pid, ticks)


class Child(Protocol):
    """A helper process ginn started. `proc` is its record, and `running` says it still runs."""

    proc: Proc

    def running(self) -> bool: ...

    def stop(self) -> None: ...


Launcher = Callable[[Sequence[str]], Child]
"""Start the helper for an argv and return once it accepts connections (or raise GinnError)."""


def signal_group(pid: int, sig: int) -> None:
    """Send `sig` to the process group that `pid` leads. A group that is already gone is no error.

    Only a helper that `start_new_session` started leads its group, so `pid` is the group id.
    """
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return


def _reap(pid: int) -> None:
    """Collect the exit of `pid` when it is our child. A process that is not ours is left alone."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return


def _group_alive(pid: int) -> bool:
    """True while process group `pid` has a member. A permission error counts as alive."""
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_group(record: Proc) -> None:
    """Stop the helper that `record` names, and every client it still serves.

    The record is checked first (`Proc.alive`), so a pid that the kernel has reused is never
    signalled. SIGTERM goes to the group. The group is killed with SIGKILL when the leader is still
    running after STOP_TIMEOUT_S. When the leader is gone but its group has members, the group is
    signalled too, but only while no process holds the pid at all: a pid that is taken again is a
    different process, and its group is never signalled. The kernel keeps a pid unused while a
    group still uses it, so an unheld pid means the group is the helper's own.
    """
    if not record.alive():
        _reap(record.pid)
        if Proc.of(record.pid) is None and _group_alive(record.pid):
            _stop_orphan_group(record.pid)
        return
    signal_group(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while record.alive() and time.monotonic() < deadline:
        time.sleep(POLL_S)
    if record.alive():
        signal_group(record.pid, signal.SIGKILL)
    _reap(record.pid)


def _stop_orphan_group(pid: int) -> None:
    """SIGTERM a group whose leader is gone, then SIGKILL it when members still run after a wait."""
    signal_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while _group_alive(pid) and time.monotonic() < deadline:
        time.sleep(POLL_S)
    if _group_alive(pid):
        signal_group(pid, signal.SIGKILL)


def peer_pid(path: str) -> int | None:
    """The pid of the process that listens at the unix socket `path`, or None.

    The pid comes from SO_PEERCRED on a probe connection, so it names the process that accepted the
    connection, which is the listener. The probe connects and closes at once, and sends nothing.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(PROBE_S)
        probe.connect(path)
        raw = probe.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError:
        return None
    finally:
        probe.close()
    pid, _uid, _gid = struct.unpack("3i", raw)
    return pid if pid > 0 else None


def _stop_popen(popen: subprocess.Popen[bytes]) -> None:
    """Stop a helper this process started: SIGTERM to its group, then SIGKILL after the timeout."""
    signal_group(popen.pid, signal.SIGTERM)
    try:
        popen.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        signal_group(popen.pid, signal.SIGKILL)
        popen.wait()


class PopenChild:
    """A `Child` over a subprocess.Popen that this process started."""

    def __init__(self, popen: subprocess.Popen[bytes], proc: Proc) -> None:
        self._popen = popen
        self.proc = proc

    def running(self) -> bool:
        return self._popen.poll() is None

    def stop(self) -> None:
        _stop_popen(self._popen)


def host_file_type(path: str) -> int | None:
    """The file type of the target that `path` names, as `stat.S_IFMT`, or None when it is missing.

    A link is followed, as podman follows it. A dangling link, a permission error and a path with a
    NUL count as missing.
    """
    try:
        return stat.S_IFMT(os.stat(path).st_mode)
    except (OSError, ValueError):
        return None


def listening(path: str) -> bool:
    """True when a connection to the unix socket at `path` succeeds, so something listens there."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(PROBE_S)
        probe.connect(path)
    except OSError:
        return False
    finally:
        probe.close()
    return True


def start_ready(
    argv: Sequence[str], socket_path: str, log: str, failure: Callable[[str], GinnError]
) -> Child:
    """Start `argv` in its own session and wait until `socket_path` accepts a connection.

    The helper is detached, so it outlives the ginn process that starts it (FR-067, FR-068). It
    runs in `/`, so no working directory of ginn's is held. Its stderr goes to `log`, an
    append-only file in the Owned state directory, and not to a pipe: a pipe whose reader has gone
    raises SIGPIPE in a GLib helper such as xdg-dbus-proxy, which would kill the proxy on its first
    warning. `failure` builds the catalogue error from a detail: the exit or timeout cause, and the
    last line that the helper wrote to `log`. The argv is never in the detail.
    """
    if not argv or not os.path.isabs(argv[0]) or not os.path.isabs(log):
        raise ValueError("a helper and its log are absolute paths (NFR-SEC-005)")
    try:
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise failure(f"cannot open the helper log: {exc.strerror or exc}") from exc
    try:
        popen = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=fd,
            cwd="/",
            start_new_session=True,
        )
    except OSError as exc:
        os.close(fd)
        raise failure(f"cannot run the helper: {exc.strerror or exc}") from exc
    os.close(fd)
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline and popen.poll() is None:
        if listening(socket_path):
            proc = Proc.of(popen.pid)
            if proc is None:
                _stop_popen(popen)
                raise failure("it exited before it could be recorded")
            return PopenChild(popen, proc)
        time.sleep(POLL_S)
    detail = (
        "it exited before it listened" if popen.poll() is not None else "it did not listen in time"
    )
    _stop_popen(popen)
    last = _last_line(log)
    if last:
        detail += f": {last}"
    raise failure(detail)


def _last_line(log: str) -> str:
    """The last non-empty line of the helper's log, cut to a short length. Empty when unreadable."""
    try:
        with open(log, "rb") as handle:
            handle.seek(max(0, os.fstat(handle.fileno()).st_size - LOG_TAIL_BYTES))
            lines = [line for line in handle.read().decode("utf-8", "replace").splitlines() if line]
    except OSError:
        return ""
    return lines[-1][:200] if lines else ""
