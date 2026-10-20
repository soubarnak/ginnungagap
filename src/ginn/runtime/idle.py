# SPDX-License-Identifier: Apache-2.0
"""The idle watcher of a services-off realm: ginn-idle stops it 60 s after its last user process
exits (FR-018, NFR-002, native/ginn-idle.c).

`arm` starts one watcher for a realm's container init, right after the services-off start. The
helper counts the live processes in the init's pid namespace each second. When the count stays at
zero for the grace, it runs `<podman> [global-arg...] stop <container>` and exits 0. It exits 0 too
when the init is gone, so `ginn stop` and `ginn delete` need no signal for it.

The watcher runs in a session of its own, with stdin and stdout on /dev/null and stderr in a log
under the runtime root. ginn keeps one record per realm, `runtime/idle/<realm>.json`, with the
watcher's pid and start time and the init's pid and start time. A start whose init matches a
watcher that runs spawns nothing. A record whose watcher is gone, or whose pid was reused (the start
time differs), is replaced.

Arming is best effort. A missing helper, a relative podman path, an init that is not running, or a
spawn that fails is GINN-W-IDLE-STOP, and the start goes on (FR-018). A helper that the start cannot
run does not make the realm stop when idle, so the warning says so.

An idle stop is recorded from the helper's own log line (idle_stopped), because the helper does not
write the realm record. The helper prints "ginn-idle: no user process for <n> s; stopping <ctr>"
before its podman stop, and "ginn-idle: podman stop failed" when that stop fails. A realm whose
container does not run, and whose watcher logged the first line and not the second, stopped idle
(FR-018). The start, stop and status paths read that answer in lifecycle.py.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ginn.domain import RealmName
from ginn.errors import Warning as GinnWarning
from ginn.errors import warn
from ginn.store.journal import is_pid_alive, process_start_ticks
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths, write_bytes_atomic

IDLE_HELPER: Final = "/usr/libexec/ginn/ginn-idle"
"""The installed watcher (native/Makefile, LIBEXECDIR)."""
IDLE_GRACE_S: Final = 60
"""Seconds with no user process before the realm stops (FR-018 AC-1)."""
_DIR: Final = "idle"
_STOPPING: Final = re.compile(
    r"^ginn-idle: no user process for \d+ s; stopping (\S+)$", re.MULTILINE
)
"""The helper's line before its podman stop (native/ginn-idle.c). Group 1 is the container."""
_STOP_FAILED: Final = "ginn-idle: podman stop failed"
"""The helper's line when its podman stop fails. A realm with this line did not stop idle."""
_LOG_LIMIT: Final = 64 * 1024

Spawn = Callable[[Sequence[str], Path], int]
"""Starts argv detached, with its stderr in the log, and returns its pid. Tests replace it.
export/watch.py starts ginn-watch the same way."""


def spawn_detached(argv: Sequence[str], log: Path, *, pass_fds: Sequence[int] = ()) -> int:
    """Start argv in a session of its own, with no stdin or stdout, and its stderr in `log`.

    The log is a new file each time. An earlier watcher that still runs keeps writing to the file it
    had open, so the log describes only the watcher that this call starts. The descriptors in
    `pass_fds` stay open in the child, under the same numbers (the export watcher's directory).
    """
    log.unlink(missing_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell (NFR-SEC-005)
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=fd,
            start_new_session=True,
            close_fds=True,
            pass_fds=tuple(pass_fds),
        )
    finally:
        os.close(fd)
    return proc.pid


@dataclass(frozen=True)
class IdleHooks:
    """The seams of the watcher. The defaults are the real ones.

    `helper` is the absolute path of ginn-idle, `grace_s` the seconds it waits with no user
    process, `spawn` starts it, and `start_ticks` reads a process's start time (None when /proc has
    no entry).
    """

    helper: str = IDLE_HELPER
    grace_s: int = IDLE_GRACE_S
    spawn: Spawn = spawn_detached
    start_ticks: Callable[[int], int | None] = process_start_ticks


@dataclass(frozen=True)
class IdleRecord:
    """The watcher of one realm, the container it was armed for, and the container's init."""

    watcher_pid: int
    watcher_start: int
    container_id: str
    init_pid: int
    init_start: int


def _dir(paths: OwnedPaths) -> Path:
    return paths.runtime / _DIR


def record_path(paths: OwnedPaths, name: RealmName) -> Path:
    """The record of the realm's watcher (`runtime/idle/<realm>.json`)."""
    return _dir(paths) / f"{name.value}.json"


def log_path(paths: OwnedPaths, name: RealmName) -> Path:
    """The stderr log of the realm's watcher (`runtime/idle/<realm>.log`)."""
    return _dir(paths) / f"{name.value}.log"


def read_record(paths: OwnedPaths, name: RealmName) -> IdleRecord | None:
    """The realm's watcher record. A missing or unreadable record is None."""
    try:
        data = json.loads(record_path(paths, name).read_text(encoding="utf-8"))
        return IdleRecord(
            watcher_pid=int(data["watcher_pid"]),
            watcher_start=int(data["watcher_start"]),
            container_id=str(data["container_id"]),
            init_pid=int(data["init_pid"]),
            init_start=int(data["init_start"]),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write_record(paths: OwnedPaths, name: RealmName, record: IdleRecord) -> None:
    body = {
        "watcher_pid": record.watcher_pid,
        "watcher_start": record.watcher_start,
        "container_id": record.container_id,
        "init_pid": record.init_pid,
        "init_start": record.init_start,
    }
    write_bytes_atomic(record_path(paths, name), json.dumps(body).encode("utf-8"))


def _unarmed(name: RealmName, detail: str) -> GinnWarning:
    return warn("GINN-W-IDLE-STOP", realm=name.value, detail=detail)


def arm(
    paths: OwnedPaths,
    name: RealmName,
    *,
    container_id: str,
    init_pid: int,
    podman_argv: Sequence[str],
    hooks: IdleHooks,
) -> GinnWarning | None:
    """Arm the watcher of realm `name` for its container's init (FR-018), or say why it is not.

    `podman_argv` is the podman binary with its global flags (PodmanCli.global_argv), so the stop
    uses the same storage and runtime. Return None when a watcher for this init runs already, or
    when one was started now. Otherwise return the GINN-W-IDLE-STOP warning. Nothing here raises
    for a missing helper or a failed spawn.
    """
    if not os.path.isabs(hooks.helper) or not os.access(hooks.helper, os.X_OK):
        return _unarmed(name, f"{hooks.helper} is not installed")
    if not podman_argv or not os.path.isabs(podman_argv[0]):
        return _unarmed(name, "the podman binary is not an absolute path")
    init_start = hooks.start_ticks(init_pid)
    if init_start is None:
        return _unarmed(name, f"the container's init {init_pid} is not running")
    current = read_record(paths, name)
    if (
        current is not None
        and current.container_id == container_id
        and current.init_pid == init_pid
        and current.init_start == init_start
        and is_pid_alive(current.watcher_pid, current.watcher_start)
    ):
        return None
    argv = [hooks.helper, str(init_pid), str(hooks.grace_s), *podman_argv, "--", container_id]
    try:
        _dir(paths).mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
        pid = hooks.spawn(argv, log_path(paths, name))
    except OSError as exc:
        return _unarmed(name, f"ginn-idle did not start: {exc.strerror or exc}")
    watcher_start = hooks.start_ticks(pid)
    if watcher_start is None:
        return _unarmed(name, "ginn-idle exited at once; see its log")
    try:
        _write_record(
            paths, name, IdleRecord(pid, watcher_start, container_id, init_pid, init_start)
        )
    except OSError as exc:
        return _unarmed(name, f"its record cannot be written: {exc.strerror or exc}")
    return None


def _read_log(paths: OwnedPaths, name: RealmName) -> str:
    """The start of the realm's watcher log. A missing log reads as empty."""
    try:
        with log_path(paths, name).open("rb") as fh:
            return fh.read(_LOG_LIMIT).decode("utf-8", errors="replace")
    except OSError:
        return ""


def idle_stopped(
    paths: OwnedPaths,
    name: RealmName,
    *,
    container_id: str,
    container_running: bool,
) -> bool:
    """True when the realm's watcher stopped `container_id` for being idle (FR-018).

    Four facts must hold. The container does not run (the caller says so, from podman). The watcher
    record names this container, so the log is the one of the watcher that was armed for it. The log
    has the helper's stopping line for this container. And the log has no "podman stop failed" line,
    because then the container may not have been stopped by the helper at all. The helper's exit
    status is not knowable from ginn, since the watcher is not a child of this process, so the
    stopping line and a container that does not run are the evidence.

    The watcher's pid is not part of the answer. A reused pid cannot change the log, and the log is
    replaced at each arm. Nothing here signals a process.
    """
    if container_running:
        return False
    record = read_record(paths, name)
    if record is None or record.container_id != container_id:
        return False
    text = _read_log(paths, name)
    if _STOP_FAILED in text:
        return False
    return any(match.group(1) == container_id for match in _STOPPING.finditer(text))


def remove(paths: OwnedPaths, name: RealmName) -> None:
    """Remove the realm's watcher record and log (on delete). Absent files are a no-op."""
    record_path(paths, name).unlink(missing_ok=True)
    log_path(paths, name).unlink(missing_ok=True)
