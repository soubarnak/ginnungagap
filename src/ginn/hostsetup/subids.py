# SPDX-License-Identifier: Apache-2.0
"""Subordinate UID/GID ranges in /etc/subuid and /etc/subgid (FR-001, FR-002, AS-005).

A line is `name_or_uid:start:count`. Blank lines, comments and lines this module does not
understand are kept byte for byte. `ensure_range` only ever appends, so the bytes already in the
file are never rewritten. A user who already holds at least `size` IDs in total is left alone,
which makes a second run a no-op (FR-002).

The read-modify-replace runs under a lock file, `<root_dir>/run/lock/ginn-<name>.lock`. On a real
host that is /run/lock/ginn-subuid.lock and /run/lock/ginn-subgid.lock, so two Host Setup runs
cannot both append (FR-001). The lock lives in /run/lock and not beside the file in /etc, for two
reasons. On Void, /run/lock is root-owned and on tmpfs, so a lock left by a crash cannot survive a
reboot. And nothing is left in /etc for undo to miss.

A lock holds its creator's PID. It is created in two steps: the PID is written to a temporary file
that is fsynced, and that file is hard-linked to the lock path. link() refuses an existing name, so
exactly one creator wins, and a lock never exists without its PID. A lock whose PID names no live
process is stale and is reclaimed. Any other lock is held, including an empty or unreadable one,
because its holder may be alive. This module never reclaims it, and the caller reports it after
its timeout (see SubIdLocked).

Reclaiming runs under `<lock>.reclaim`, a sidecar file held with flock. Waiters take it without
blocking, polling until the same deadline, so two waiters cannot both unlink the same stale lock
(see `_reclaim_stale`).
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ginn.errors import GinnError, make
from ginn.hostsetup._atomic import atomic_write, file_mode

MIN_SIZE = 524_288
"""Minimum IDs per user (AS-005, enough for nested rootless containers)."""

DEFAULT_SIZE = MIN_SIZE
DEFAULT_FLOOR = 100_000
DEFAULT_CEILING = 2**32 - 1 - 65_536
DEFAULT_MODE = 0o644
LOCK_TIMEOUT_SECONDS = 5.0
"""How long `ensure_range` waits for another holder of the lock before it gives up."""
LOCK_POLL_SECONDS = 0.05
"""Pause between attempts to create the lock, and between tries of the reclaim gate."""
LOCK_DIR_MODE = 0o755

_DECIMAL = re.compile(r"[0-9]+")
"""Digits of a subuid or subgid start or count field. Any length is accepted."""
_PID = re.compile(r"[0-9]{1,10}")
"""Digits of a PID on the first line of a lock file. Longer text is not a PID."""
_MAX_PID = 2**31 - 1
_READ_LIMIT = 64
"""Bytes read from a lock file. A PID line is at most 11 bytes."""
_BAD_NAME_CHARS = frozenset(":\r\n\0")
_Inode = tuple[int, int]
"""(st_dev, st_ino) of one lock file."""


class SubIdExhausted(Exception):
    """No free range of the requested size fits below the ceiling.

    This is a host configuration problem, not a user error. The module has no catalogue code
    for it, so Host Setup reports it as an internal failure.
    """


class SubIdLocked(GinnError):
    """Another process holds the lock for a subordinate ID file and did not let go in time.

    This is the catalogue error GINN-E-SUBID-LOCKED (exit 75). The lock is not removed while its
    holder is alive, and a lock with no usable PID is never reclaimed. The catalogue remedy tells
    the admin to check for a running `ginn host-setup` and, if none is running, to remove the lock
    file by hand. The message names the holder's PID when the lock records one.

    A reclaim gate that is a symbolic link is a different case: the stale lock cannot be reclaimed
    until a person removes the link. That raises GINN-E-SUBID-LOCK-UNSAFE instead (see
    `_reclaim_stale`).
    """


@dataclass(frozen=True)
class SubIdEntry:
    """One `name:start:count` line. `end` is exclusive."""

    name: str
    start: int
    count: int

    @property
    def end(self) -> int:
        return self.start + self.count

    def render(self) -> str:
        return f"{self.name}:{self.start}:{self.count}"


@dataclass(frozen=True)
class Change:
    """Result of `ensure_range`. `before` and `after` are the exact file bytes."""

    before: bytes
    after: bytes
    changed: bool
    added: SubIdEntry | None = None


def parse_entries(text: str) -> list[SubIdEntry]:
    """Return every well-formed `name:start:count` line in `text`, in file order.

    Comments (from `#` to end of line), blank lines and malformed lines are skipped here. The
    file itself is never rebuilt from these entries, so they are not kept as data.
    """
    found: list[SubIdEntry] = []
    for raw in text.splitlines():
        entry = _parse_line(raw)
        if entry is not None:
            found.append(entry)
    return found


def _parse_line(raw: str) -> SubIdEntry | None:
    body = raw.split("#", 1)[0].strip()
    if not body:
        return None
    parts = body.split(":")
    if len(parts) != 3:
        return None
    name, start, count = (part.strip() for part in parts)
    if not name or _DECIMAL.fullmatch(start) is None or _DECIMAL.fullmatch(count) is None:
        return None
    return SubIdEntry(name=name, start=int(start), count=int(count))


def allocate(
    entries: Sequence[SubIdEntry],
    user: str,
    size: int = DEFAULT_SIZE,
    *,
    floor: int = DEFAULT_FLOOR,
    ceiling: int = DEFAULT_CEILING,
) -> SubIdEntry:
    """Return a new range for `user` that starts at or above every existing range.

    The start is `max(highest existing end, floor)`. Gaps below that are not reused. This is the
    simplest choice that cannot overlap.
    """
    _check_name(user)
    _check_size(size)
    highest_end = max((entry.end for entry in entries), default=0)
    start = max(highest_end, floor)
    if start + size > ceiling:
        raise SubIdExhausted(
            f"no free range of {size} IDs below {ceiling}; highest existing end is {highest_end}"
        )
    return SubIdEntry(name=user, start=start, count=size)


def ensure_range(
    path: Path,
    user: str,
    size: int = DEFAULT_SIZE,
    *,
    root_dir: Path,
    uid: int | None = None,
    floor: int = DEFAULT_FLOOR,
    ceiling: int = DEFAULT_CEILING,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> Change:
    """Make sure `user` holds at least `size` subordinate IDs in the file at `path`.

    Lines are matched by name, and also by `str(uid)` when `uid` is given, because newuidmap
    accepts either form. If the existing entries already total at least `size`, nothing is
    written. Otherwise a new entry is appended and the file is replaced atomically with its
    mode kept (0644 for a new file). The read and the replace happen under the lock, and
    `SubIdLocked` is raised if another holder keeps the lock for `lock_timeout` seconds.

    `root_dir` is the directory that holds `run/lock`: `/` on a real host, a temporary directory
    in tests. The lock directory is created with mode 0755 when it is missing, but its parent
    must exist already.
    """
    _check_name(user)
    _check_size(size)
    with _lock(path, root_dir, lock_timeout):
        try:
            before = path.read_bytes()
        except FileNotFoundError:
            before = b""
        names = {user} if uid is None else {user, str(uid)}
        entries = parse_entries(before.decode("utf-8", errors="surrogateescape"))
        held = sum(entry.count for entry in entries if entry.name in names)
        if held >= size:
            return Change(before=before, after=before, changed=False)
        added = allocate(entries, user, size, floor=floor, ceiling=ceiling)
        after = _append_line(before, added.render().encode("utf-8"))
        atomic_write(path, after, file_mode(path, DEFAULT_MODE))
        return Change(before=before, after=after, changed=True, added=added)


def ensure_single(
    path: Path,
    user: str,
    size: int = DEFAULT_SIZE,
    *,
    root_dir: Path,
    uid: int | None = None,
    floor: int = DEFAULT_FLOOR,
    ceiling: int = DEFAULT_CEILING,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> tuple[Change, SubIdEntry]:
    """Make sure `user` holds one line of at least `size` IDs, and return that line.

    Host Setup records one range per file, so unlike `ensure_range` the total of several lines
    does not count. A line that matches by name (or by `str(uid)`) and holds at least `size` IDs
    is kept as it is. Otherwise a full-size line is appended, placed by `allocate`. Runs under the
    same lock as `ensure_range`.
    """
    _check_name(user)
    _check_size(size)
    with _lock(path, root_dir, lock_timeout):
        before = _read_bytes(path)
        entries = parse_entries(before.decode("utf-8", errors="surrogateescape"))
        names = {user} if uid is None else {user, str(uid)}
        for entry in entries:
            if entry.name in names and entry.count >= size:
                return Change(before=before, after=before, changed=False), entry
        added = allocate(entries, user, size, floor=floor, ceiling=ceiling)
        after = _append_line(before, added.render().encode("utf-8"))
        atomic_write(path, after, file_mode(path, DEFAULT_MODE))
        return Change(before=before, after=after, changed=True, added=added), added


def ensure_exact(
    path: Path,
    entry: SubIdEntry,
    *,
    root_dir: Path,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> Change:
    """Make sure the file holds `entry` as one line, and never choose another range.

    Used on a repair, to reuse the range that the Host Setup Record stored. If the line is already
    there, nothing is written. If another line overlaps the range, GINN-E-SUBID-CONFLICT is raised
    and the file is left alone. Otherwise the line is appended under the lock that `ensure_range`
    uses.
    """
    _check_name(entry.name)
    _check_size(entry.count)
    with _lock(path, root_dir, lock_timeout):
        before = _read_bytes(path)
        entries = parse_entries(before.decode("utf-8", errors="surrogateescape"))
        if entry in entries:
            return Change(before=before, after=before, changed=False)
        for other in entries:
            if other.count and other.start < entry.end and entry.start < other.end:
                raise make(
                    "GINN-E-SUBID-CONFLICT",
                    start=entry.start,
                    count=entry.count,
                    user=entry.name,
                    path=str(path),
                )
        after = _append_line(before, entry.render().encode("utf-8"))
        atomic_write(path, after, file_mode(path, DEFAULT_MODE))
        return Change(before=before, after=after, changed=True, added=entry)


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def newuidmap_present(root_dir: Path) -> bool:
    """Report whether usr/bin/newuidmap and usr/bin/newgidmap exist and are setuid.

    Only the mode bits are read. A file that has cap_setuid as a file capability and no setuid
    bit is reported as absent (the capability xattr is not parsed).
    """
    return all(_is_setuid(root_dir / "usr" / "bin" / tool) for tool in ("newuidmap", "newgidmap"))


def _is_setuid(path: Path) -> bool:
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode) and bool(mode & stat.S_ISUID)


@dataclass(frozen=True)
class _LockIdentity:
    """One lock file as read through a single open descriptor. `pid` is None when unusable.

    `mtime_ns` is part of the identity because an inode number can be reused after an unlink.
    """

    dev: int
    ino: int
    mtime_ns: int
    pid: int | None


def _lock_path(path: Path, root_dir: Path) -> Path:
    """Return `<root_dir>/run/lock/ginn-<name>.lock` for the file at `path`."""
    return root_dir / "run" / "lock" / f"ginn-{path.name}.lock"


@contextmanager
def _lock(path: Path, root_dir: Path, timeout: float) -> Iterator[None]:
    """Hold the lock for `path` while the caller edits it. See the module docstring for the design.

    A second caller retries every LOCK_POLL_SECONDS until `timeout`, then raises SubIdLocked and
    leaves the holder's file in place. A lock is reclaimed only when its PID names no live process,
    and at most once per call, through `_reclaim_stale`. Any other lock is held. The lock this call
    created is removed on exit, including when the body raises, but only while it is still that
    same file.
    """
    lock = _lock_path(path, root_dir)
    _ensure_lock_dir(lock.parent)
    deadline = time.monotonic() + timeout
    tried_reclaim = False
    while True:
        inode = _create_lock(lock)
        if inode is not None:
            break
        seen = _read_lock(lock)
        if seen is not None and not tried_reclaim and _is_stale(seen):
            tried_reclaim = True
            inode = _reclaim_stale(lock, seen, deadline)
            if inode is not None:
                break
            continue
        if time.monotonic() >= deadline:
            pid = None if seen is None else seen.pid
            holder = "no readable pid" if pid is None else f"pid {pid}"
            err = make("GINN-E-SUBID-LOCKED", lock=str(lock), holder=holder)
            raise SubIdLocked(err.code, err.message, err.remedy, err.exit_code, details=err.details)
        time.sleep(LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        _unlink_if_same(lock, inode)


def _ensure_lock_dir(directory: Path) -> None:
    """Create the lock directory with mode 0755 when it is missing. An existing one is kept.

    Only the directory itself is created, never its parents. The process is root on a real host,
    so a new directory is root-owned.
    """
    try:
        os.mkdir(directory, LOCK_DIR_MODE)
    except FileExistsError:
        return
    os.chmod(directory, LOCK_DIR_MODE)


def _create_lock(lock: Path) -> _Inode | None:
    """Create `lock` holding this process's PID. Return its (dev, ino), or None if it exists.

    The PID is written to a temporary file in the same directory, which is fsynced. That file is
    then hard-linked to the lock path. link() refuses an existing name, so only one creator wins,
    and the lock never exists without its PID. The temporary name has a random part, because
    threads of one process share its PID. The temporary file is removed on every path, including
    a failed write, fsync or link.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=lock.parent, prefix=f"{lock.name}.{os.getpid()}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        try:
            _write_all(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
            st = os.fstat(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, lock)
        except FileExistsError:
            return None
        return (st.st_dev, st.st_ino)
    finally:
        tmp.unlink(missing_ok=True)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _read_lock(lock: Path) -> _LockIdentity | None:
    """Read `lock` through one descriptor, so the stat and the PID describe the same file.

    Returns None when the lock does not exist (any more). A symbolic link at the lock path is not
    a lock this module writes. It is reported with no PID, so it is held and never reclaimed.
    """
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        try:
            link_st = os.lstat(lock)
        except FileNotFoundError:
            return None
        return _LockIdentity(link_st.st_dev, link_st.st_ino, link_st.st_mtime_ns, None)
    try:
        st = os.fstat(fd)
        data = os.read(fd, _READ_LIMIT)
    finally:
        os.close(fd)
    text = data.decode("ascii", errors="replace")
    return _LockIdentity(st.st_dev, st.st_ino, st.st_mtime_ns, _parse_pid(text))


def _parse_pid(text: str) -> int | None:
    """Return the PID on the first line of a lock file, or None when that line is not a PID."""
    first = text.split("\n", 1)[0].strip()
    if _PID.fullmatch(first) is None:
        return None
    pid = int(first)
    return pid if 0 < pid <= _MAX_PID else None


def _is_stale(seen: _LockIdentity) -> bool:
    """True when `seen` names a PID and no process has that PID now.

    A lock with no usable PID is held, whether it is empty, garbage or a symbolic link. Nothing in
    such a file shows that its holder is dead, and no age rule can show it either. A lock made by
    `_create_lock` already holds its PID, so a live creator never leaves a young lock without one.
    A PID that belongs to another user still exists, so it is not stale.
    """
    if seen.pid is None:
        return False
    try:
        os.kill(seen.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _reclaim_stale(lock: Path, seen: _LockIdentity, deadline: float) -> _Inode | None:
    """Reclaim the stale lock that `seen` describes. Return the new lock's (dev, ino), or None.

    The race this closes: two waiters judge the same stale lock. Without a gate, the first one
    unlinks it and creates its own lock. The second one judged the old lock too, so it could unlink
    the first one's fresh lock. Every reclaimer therefore takes the gate, `<lock>.reclaim`, with
    flock before it re-reads the lock. The gate is taken without blocking and polled until
    `deadline`. If another reclaimer holds it until then, this call returns None, and the caller
    reports SubIdLocked. The gate is opened with O_NOFOLLOW, so a symbolic link in its place is
    refused. The gate is not removed after use: an empty file is harmless, and removing it would
    reopen the race. /run/lock is cleared at reboot. See `_reclaim_locked` for what runs under the
    gate.

    A symbolic link in place of the gate fails the open with ELOOP. That is raised as
    GINN-E-SUBID-LOCK-UNSAFE, because the stale lock cannot be reclaimed until a person removes
    the link. Any other error from the open propagates as it is.

    Another process may unlink the gate and put a new file at its path while this call holds the
    old one. The lock the call took then guards nothing. So once the gate is taken, the descriptor
    must still be the file at its path (`_gate_is_at_path`). Otherwise the descriptor is closed and
    the path is opened again, until the same deadline. A symbolic link put in its place is refused
    in the same way as above.
    """
    gate_path = lock.with_name(f"{lock.name}.reclaim")
    while True:
        gate = _open_gate(lock, gate_path)
        try:
            if not _take_gate(gate, deadline):
                return None
            if _gate_is_at_path(gate, gate_path):
                return _reclaim_locked(lock, seen)
        finally:
            os.close(gate)
        if time.monotonic() >= deadline:
            return None
        time.sleep(LOCK_POLL_SECONDS)


def _open_gate(lock: Path, gate_path: Path) -> int:
    """Open the reclaim gate, creating it with mode 0600 if missing (see `_reclaim_stale`)."""
    try:
        return os.open(gate_path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise make("GINN-E-SUBID-LOCK-UNSAFE", lock=str(lock), path=str(gate_path)) from exc
        raise


def _gate_is_at_path(gate: int, gate_path: Path) -> bool:
    """True when the open descriptor `gate` is still the file at `gate_path` (st_dev, st_ino).

    lstat, so a symbolic link at the path never counts as the descriptor's own file. A path that
    is missing does not count either.
    """
    try:
        at_path = os.lstat(gate_path)
    except FileNotFoundError:
        return False
    held = os.fstat(gate)
    return (held.st_dev, held.st_ino) == (at_path.st_dev, at_path.st_ino)


def _take_gate(gate: int, deadline: float) -> bool:
    """Take the reclaim gate without blocking, polling until `deadline`. False if it stayed busy."""
    while True:
        try:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL_SECONDS)
        else:
            return True


def _reclaim_locked(lock: Path, seen: _LockIdentity) -> _Inode | None:
    """Runs under the reclaim gate. Unlink `lock` if it is still `seen`, then create a new one.

    The re-read compares every field of the identity: device, inode, mtime, and PID. If any of
    them differs, another caller reclaimed or replaced the lock after `seen` was taken, so it is
    left alone and this call returns None. Otherwise the stale lock is unlinked, but only while its
    inode still matches the one just checked, and this call creates the new lock while it still
    holds the gate. Other reclaimers cannot touch that lock, because they must take the gate first.
    A caller that is not reclaiming can still win the create race for the path. Then this call
    returns None and leaves that lock alone.

    One gap remains, and it is accepted: PID reuse. After the holder dies, an unrelated process may
    take its PID, and the stale lock then looks held. The caller waits and times out, which is the
    safe side.
    """
    now = _read_lock(lock)
    if now is None or now != seen:
        return None
    _unlink_if_same(lock, (now.dev, now.ino))
    return _create_lock(lock)


def _unlink_if_same(lock: Path, inode: _Inode) -> None:
    """Unlink `lock` only while it is still the file with `inode` (st_dev, st_ino).

    The check and the unlink are two calls, so a replacement between them is not caught. That
    window is accepted. A lock is never replaced while its holder is alive: creators fail with
    EEXIST, and a reclaimer judges only dead PIDs. The check is a guard against a stale identity.
    """
    try:
        st = os.lstat(lock)
    except FileNotFoundError:
        return
    if (st.st_dev, st.st_ino) == inode:
        lock.unlink(missing_ok=True)


def _append_line(before: bytes, line: bytes) -> bytes:
    prefix = before if not before or before.endswith(b"\n") else before + b"\n"
    return prefix + line + b"\n"


def _check_size(size: int) -> None:
    if size < MIN_SIZE:
        raise ValueError(f"AS-005 requires at least {MIN_SIZE} IDs, not {size}")


def _check_name(user: str) -> None:
    if not user or user != user.strip() or _BAD_NAME_CHARS & set(user):
        raise ValueError(f"invalid user name for a subordinate ID entry: {user!r}")
