# SPDX-License-Identifier: Apache-2.0
"""The host menu follows native package transactions in a realm (FR-044, FR-045, NFR-009, ADR-016).

`pacman -S firefox` inside a realm changes the realm root, which the host cannot watch without
reading podman's storage. So the realm signals the host instead. Three parts:

- A hook of the realm's package manager (distros/data/*.toml `[export_hook]`) touches a stamp file
  after every transaction. The stamp lives in the Realm Home, which is already a host directory that
  the realm sees as its home: `<realm home>/.local/state/ginn-export/transaction.desktop`. No bind
  is added, so a realm created before ADR-016 gets the signal at its next start (`ensure_hooks`).
  The hook never makes the directory (a `mkdir` by container root would give a subordinate-id
  owner); a missing directory just makes its `touch` fail, which no package manager treats as fatal.
- `ginn-watch` runs on the host, on that directory, for as long as the realm's init lives
  (`arm`). A touch of the stamp runs `ginn _sync <realm>` after a short quiet time. The stamp name
  ends in `.desktop` because ginn-watch looks only at such names. Its content is never read. When
  the realm removes the directory, the watcher runs once more and exits (status 3). That last
  `ginn _sync` calls `rearm` inside its lock, which makes the directory again and starts a fresh
  watcher (at most 3 times a minute).
- `ginn _sync` re-reads the realm through ginn itself (export/sync.py), so what the realm wrote into
  the stamp cannot steer anything.

Both parts are best effort. A hook that cannot be installed, a helper that is not installed, or a
spawn that fails is a GINN-W-EXPORT-SYNC warning, and the start goes on. The package manager's own
hook always exits 0 (apt: `|| true`; pacman and dnf5 only warn on a failed hook), so the signal can
never fail a package transaction.

The marker. `ensure_hooks` writes the hook as container root, then the stamp directory, the stamp
and last a marker `hooks-for` as the realm user. The marker holds the container id. A start whose
container id equals the marker's runs no exec at all. A rebuilt or re-created container has another
id, so it gets its hooks again. The realm can write the marker (it is in its home), and the worst
it can do is keep its own hooks from being installed.

Host side safety. The realm owns the directories that hold the stamp, so a link in the path could
point at a host directory. `open_stamp_dir` therefore walks from the resolved Realm Home one
component at a time with `openat(O_PATH | O_NOFOLLOW | O_DIRECTORY)`; a link anywhere on the way
gives None. The walk's file descriptor is what the host uses afterwards: ginn-watch inherits it
and watches `/proc/self/fd/<n>`, and the marker is read with `openat` from it. A component that
the realm swaps for a link after the walk changes nothing that the host holds. The host writes into
the realm's directories only in `rearm`, with `mkdirat` below a descriptor of the walk (CR-006
limits writes).
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ginn.distros import DistroDef
from ginn.domain import RealmName
from ginn.errors import GinnError, warn
from ginn.errors import Warning as GinnWarning
from ginn.podman.adapter import ExecTarget, Podman
from ginn.runtime.idle import spawn_detached
from ginn.store.journal import is_pid_alive, process_start_ticks
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths, write_bytes_atomic

WATCH_HELPER: Final = "/usr/libexec/ginn/ginn-watch"
"""The installed watcher (native/Makefile, LIBEXECDIR)."""
STAMP_RELATIVE: Final = (".local", "state", "ginn-export")
"""The stamp directory below the Realm Home."""
STAMP_NAME: Final = "transaction.desktop"
"""The stamp. It ends in `.desktop` because ginn-watch looks only at such names."""
MARKER_NAME: Final = "hooks-for"
"""The marker with the container id that the hooks were installed for. Not a `.desktop` name."""
REARMS_PER_MINUTE: Final = 3
"""The most re-arms in 60 s; a realm that removes its directory in a loop gets no more."""
QUIET_MS: Final = 500
"""Milliseconds without a stamp before `ginn _sync` runs. NFR-009 leaves the rest of 5 s to it."""
MAX_MS: Final = 1500
"""Milliseconds after the first stamp at which `ginn _sync` runs at the latest."""
EXEC_TIMEOUT_S: Final = 30.0
_DIR: Final = "watch"
_SAFE_PATH: Final = re.compile(r"/[A-Za-z0-9_./-]+")
"""A realm path the hook files may carry. No blank, quote, backslash, `%`, `$` or `{`."""
_MARKER_LIMIT: Final = 256
_WRITE_HOOK: Final = 'mkdir -p -- "${1%/*}" && printf %s "$2" > "$1.tmp" && mv -f -- "$1.tmp" "$1"'
"""Run by `sh -c` as container root. $1 is the hook path and $2 its text (argv, never spliced). The
text goes to `$1.tmp` and moves over the hook, so a package manager never reads half a file."""
_WRITE_STAMP: Final = (
    'umask 077 && mkdir -p -- "$1" && rm -f -- "$1/$2" "$1/$4" && : > "$1/$2"'
    ' && printf %s "$3" > "$1/$4"'
)
"""Run by `sh -c` as the realm user. $1 is the stamp directory, $2 the stamp, $3 the container id
and $4 the marker. Both are removed first: a stamp that container root made is owned by a
subordinate id, and the unlink works because the directory is the realm user's. The marker is
written last, so a marker means the rest is in place."""


def stamp_realm_path(host_home: str) -> str:
    """The stamp file as the realm sees it (`<home>/.local/state/ginn-export/<stamp>`)."""
    return "/".join((host_home.rstrip("/"), *STAMP_RELATIVE, STAMP_NAME))


_DIR_FLAGS: Final = os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC


def _walk(paths: OwnedPaths, name: RealmName, *, create: bool) -> int | None:
    """A descriptor of the stamp directory, found without following a link, or None.

    The Realm Home is resolved once (the host owns it). Each component below it is opened with
    `O_NOFOLLOW | O_DIRECTORY` relative to the one before. With `create`, a component that is
    missing is made with `mkdirat` (mode 0700) and opened; nothing else is ever written.
    """
    try:
        fd = os.open(
            os.path.realpath(paths.homes / name.value), os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
        )
    except OSError:
        return None
    for part in STAMP_RELATIVE:
        try:
            try:
                below = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, OWNED_DIR_MODE, dir_fd=fd)
                below = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except OSError:
            os.close(fd)
            return None
        os.close(fd)
        fd = below
    return fd


@contextmanager
def open_stamp_dir(
    paths: OwnedPaths, name: RealmName, *, create: bool = False
) -> Iterator[int | None]:
    """Yield a descriptor of the realm's stamp directory, or None when it is not safe to use.

    Safe means every component from the resolved Realm Home down is a plain directory, not a link
    (ADR-016, CR-006). The descriptor is closed on exit. Use it, not a path, for anything the host
    does with the directory: a path could name another directory by the next call.
    """
    fd = _walk(paths, name, create=create)
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _proc_path(fd: int) -> str:
    return f"/proc/self/fd/{fd}"


_host_uid = os.getuid
"""The owner a stamp or marker must have. A seam for tests (root cannot chown there)."""


def own_stamp_files(directory: int) -> None:
    """Make the stamp the host user's, and drop a marker that is not.

    The hook runs as container root, which is a subordinate id on the host. A stamp it made, or a
    marker it replaced, is owned by that id. The directory is the host user's, so the host can
    unlink either one, and it makes the stamp again empty (container root can still touch it, with
    CAP_DAC_OVERRIDE, and keeps its owner). Raises OSError for the caller to turn into a warning.
    """
    uid = _host_uid()
    for name in (STAMP_NAME, MARKER_NAME):
        try:
            found = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            found = None
        if found is not None and found.st_uid == uid and stat.S_ISREG(found.st_mode):
            continue
        if found is not None:
            os.unlink(name, dir_fd=directory)
        if name == STAMP_NAME:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            os.close(os.open(name, flags, 0o600, dir_fd=directory))


def _marker(directory: int) -> str | None:
    """The container id in the marker, read without following a link. None when not readable."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(MARKER_NAME, flags, dir_fd=directory)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, _MARKER_LIMIT).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def unsynced(name: RealmName, reason: str) -> GinnWarning:
    return warn(
        "GINN-W-EXPORT-SYNC",
        realm=name.value,
        reason=f"a package change inside the realm will not reach the menu by itself: {reason}",
    )


def _reason(err: GinnError) -> str:
    return str(err.details.get("reason", err.details.get("detail", err.message)))


def ensure_hooks(
    podman: Podman,
    paths: OwnedPaths,
    name: RealmName,
    distro: DistroDef,
    *,
    container_id: str,
    host_home: str,
    user: str,
    services: bool,
) -> GinnWarning | None:
    """Install the realm's package-manager hook for container `container_id`, once per container.

    `user` is the realm user's `uid:gid`. Return None when the hooks are in place, or the distro
    has no hook. Otherwise a GINN-W-EXPORT-SYNC warning says why not. Nothing here raises for a
    failed exec: the start has happened, and it stays a success (FR-044).
    """
    hook = distro.export_hook
    if hook is None:
        return None
    stamp = stamp_realm_path(host_home)
    if _SAFE_PATH.fullmatch(stamp) is None or "/../" in stamp:
        return unsynced(name, f"the home path {host_home} has characters the hook cannot carry")
    with open_stamp_dir(paths, name) as current:
        if current is not None and _marker(current) == container_id:
            return None
    target = ExecTarget(realm=name.value, services=services, mutating=False)
    steps: Sequence[tuple[str, list[str]]] = (
        ("0:0", ["sh", "-c", _WRITE_HOOK, "sh", hook.path, hook.render(stamp)]),
        (
            user,
            [
                "sh",
                "-c",
                _WRITE_STAMP,
                "sh",
                str(Path(stamp).parent),
                STAMP_NAME,
                container_id,
                MARKER_NAME,
            ],
        ),
    )
    for who, argv in steps:
        try:
            result = podman.exec(
                container_id, argv, target=target, user=who, capture=True, timeout_s=EXEC_TIMEOUT_S
            )
        except GinnError as err:
            return unsynced(name, f"the hook step did not run ({_reason(err)})")
        if result.exit_code != 0:
            return unsynced(name, f"the hook step exited with status {result.exit_code}")
    with open_stamp_dir(paths, name) as made:
        if made is None:
            return unsynced(name, "the stamp directory in the Realm Home is not a plain directory")
    return None


def has_inotify_watch(pid: int) -> bool:
    """True when process `pid` holds a live inotify watch (the kernel lists them in fdinfo).

    The kernel drops a watch when its directory is deleted, so a watcher whose stamp directory is
    gone, or was replaced by another inode with the same number, has none.
    """
    try:
        names = os.listdir(f"/proc/{pid}/fdinfo")
        for name in names:
            with open(f"/proc/{pid}/fdinfo/{name}", encoding="utf-8") as info:
                if any(line.startswith("inotify wd:") for line in info):
                    return True
    except OSError:
        return False
    return False


@dataclass(frozen=True)
class WatchHooks:
    """The seams of the watcher. The defaults are the real ones.

    `helper` is the absolute path of ginn-watch. `program` is the absolute ginn that the watcher
    runs as `<program> _sync <realm>` (the CLI sets it, `_context.ginn_program`; None starts no
    watcher). `spawn` starts a process detached and passes the descriptors in `pass_fds` on, and
    `start_ticks` reads a process's start time (None when /proc has none), and `watching` says
    whether a watcher process still holds an inotify watch. `clock` is the wall clock that
    times re-arms.
    `enabled` False installs no hook and starts no watcher (a caller that tests something else).
    """

    enabled: bool = True
    helper: str = WATCH_HELPER
    program: str | None = None
    quiet_ms: int = QUIET_MS
    max_ms: int = MAX_MS
    spawn: Callable[..., int] = spawn_detached
    start_ticks: Callable[[int], int | None] = process_start_ticks
    watching: Callable[[int], bool] = has_inotify_watch
    clock: Callable[[], float] = time.time
    kill: Callable[[int, int], None] = field(default=os.kill)


@dataclass(frozen=True)
class WatchRecord:
    """The watcher of one realm and the container init it ends with."""

    watcher_pid: int
    watcher_start: int
    init_pid: int
    init_start: int
    rearms: int = 0
    rearm_at: float = 0.0
    """The re-arms in the minute that began at `rearm_at` (see `rearm`)."""


def _dir(paths: OwnedPaths) -> Path:
    return paths.runtime / _DIR


def record_path(paths: OwnedPaths, name: RealmName) -> Path:
    """The record of the realm's watcher (`runtime/watch/<realm>.json`)."""
    return _dir(paths) / f"{name.value}.json"


def log_path(paths: OwnedPaths, name: RealmName) -> Path:
    """The stderr log of the realm's watcher (`runtime/watch/<realm>.log`)."""
    return _dir(paths) / f"{name.value}.log"


def read_record(paths: OwnedPaths, name: RealmName) -> WatchRecord | None:
    """The realm's watcher record. A missing or unreadable record is None."""
    try:
        data = json.loads(record_path(paths, name).read_text(encoding="utf-8"))
        return WatchRecord(
            watcher_pid=int(data["watcher_pid"]),
            watcher_start=int(data["watcher_start"]),
            init_pid=int(data["init_pid"]),
            init_start=int(data["init_start"]),
            rearms=int(data.get("rearms", 0)),
            rearm_at=float(data.get("rearm_at", 0.0)),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


def arm(
    paths: OwnedPaths,
    name: RealmName,
    *,
    init_pid: int,
    hooks: WatchHooks,
    force: bool = False,
    rearms: tuple[int, float] = (0, 0.0),
) -> GinnWarning | None:
    """Start ginn-watch on the realm's stamp directory for the init `init_pid`, or say why not.

    The watcher ends when the init is gone, so a stop needs no signal. Return None when a watcher
    for this init runs already, or when one was started now. A watcher of an earlier init that is
    still alive is stopped first, so a realm never has two. `force` starts a new watcher although
    one runs for this init (`rearm`: the old one lost its directory), with the re-arm count that it
    keeps in the record. The stamp is made the host user's first (`own_stamp_files`). Nothing here
    raises.
    """
    if not os.path.isabs(hooks.helper) or not os.access(hooks.helper, os.X_OK):
        return unsynced(name, f"{hooks.helper} is not installed")
    command = hooks.program
    if command is None or not os.path.isabs(command):
        return unsynced(name, "the ginn command is not an absolute path")
    init_start = hooks.start_ticks(init_pid)
    if init_start is None:
        return unsynced(name, f"the container's init {init_pid} is not running")
    current = read_record(paths, name)
    alive = current is not None and is_pid_alive(current.watcher_pid, current.watcher_start)
    if (
        alive
        and not force
        and current is not None
        and current.init_pid == init_pid
        and current.init_start == init_start
    ):
        return None
    with open_stamp_dir(paths, name) as fd:
        if fd is None:
            return unsynced(name, "the stamp directory in the Realm Home is missing or a link")
        try:
            own_stamp_files(fd)
        except OSError as exc:
            return unsynced(
                name, f"the stamp cannot be made the host user's: {exc.strerror or exc}"
            )
        if alive and current is not None:
            try:
                hooks.kill(current.watcher_pid, signal.SIGTERM)
            except OSError:
                pass
        argv = [
            hooks.helper,
            "--exit-with",
            str(init_pid),
            _proc_path(fd),
            str(hooks.quiet_ms),
            str(hooks.max_ms),
            command,
            "_sync",
            name.value,
        ]
        try:
            _dir(paths).mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
            pid = hooks.spawn(argv, log_path(paths, name), pass_fds=(fd,))
        except OSError as exc:
            return unsynced(name, f"ginn-watch did not start: {exc.strerror or exc}")
    watcher_start = hooks.start_ticks(pid)
    if watcher_start is None:
        return unsynced(name, "ginn-watch exited at once; see its log")
    body = {
        "watcher_pid": pid,
        "watcher_start": watcher_start,
        "init_pid": init_pid,
        "init_start": init_start,
        "rearms": rearms[0],
        "rearm_at": rearms[1],
    }
    try:
        write_bytes_atomic(record_path(paths, name), json.dumps(body).encode("utf-8"))
    except OSError as exc:
        return unsynced(name, f"its record cannot be written: {exc.strerror or exc}")
    return None


def rearm(paths: OwnedPaths, name: RealmName, *, hooks: WatchHooks) -> GinnWarning | None:
    """Keep the watcher on the realm's stamp directory after the realm removed or replaced it.

    `ginn _sync` calls this after each sync. Nothing happens when the realm has no watcher record,
    when its init is not the recorded one any more (the realm stopped; the next start arms), or
    when the live watcher still holds a watch (`has_inotify_watch`) and the directory is there.
    Otherwise the directory is made again (`mkdirat` below the safe walk) and a fresh watcher
    replaces the old one, which is about to exit with status 3. Without this, a cleaned
    `~/.local/state` would end the export of native installs until the next start. More than
    `REARMS_PER_MINUTE` in a minute is refused with a warning, so a hostile realm cannot keep a
    fresh watcher starting. The caller holds the realm lock. Nothing here raises.
    """
    if not hooks.enabled:
        return None
    record = read_record(paths, name)
    if record is None or hooks.start_ticks(record.init_pid) != record.init_start:
        return None
    watching = is_pid_alive(record.watcher_pid, record.watcher_start) and hooks.watching(
        record.watcher_pid
    )
    with open_stamp_dir(paths, name) as fd:
        if fd is not None and watching:
            return None
    now = hooks.clock()
    if now - record.rearm_at < 60.0:
        count, since = record.rearms + 1, record.rearm_at
    else:
        count, since = 1, now
    if count > REARMS_PER_MINUTE:
        return unsynced(name, "the stamp directory was removed more than 3 times in a minute")
    with open_stamp_dir(paths, name, create=True) as made:
        if made is None:
            return unsynced(name, "the stamp directory cannot be made again")
    return arm(
        paths, name, init_pid=record.init_pid, hooks=hooks, force=True, rearms=(count, since)
    )


def remove(paths: OwnedPaths, name: RealmName) -> None:
    """Remove the realm's watcher record and log (on delete). Absent files are a no-op."""
    record_path(paths, name).unlink(missing_ok=True)
    log_path(paths, name).unlink(missing_ok=True)
