# SPDX-License-Identifier: Apache-2.0
"""Subordinate UID/GID ranges in /etc/subuid and /etc/subgid (FR-001, FR-002, AS-005).

A line is `name_or_uid:start:count`. Blank lines, comments and lines this module does not
understand are kept byte for byte. `ensure_range` only ever appends, so the bytes already in the
file are never rewritten. A user who already holds at least `size` IDs in total is left alone,
which makes a second run a no-op (FR-002). The read-modify-replace runs under `<name>.lock` next to
the file (etc/subuid.lock, etc/subgid.lock), so two Host Setup runs cannot both append (FR-001).
The lock file holds its holder's PID. A lock whose PID is gone and whose file is old enough is
left behind by a crash, so it is reclaimed instead of blocking every later run. Reclaiming runs
under `<name>.lock.reclaim`, a sidecar file held with flock, so two waiters cannot both unlink the
same stale lock (see `_reclaim_stale`).
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

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
LOCK_STALE_SECONDS = 3.0
"""A lock is reclaimed only when its PID is gone and the lock file is at least this old."""

_DECIMAL = re.compile(r"[0-9]+")
"""Digits of a subuid or subgid start or count field. Any length is accepted."""
_PID = re.compile(r"[0-9]{1,10}")
"""Digits of a PID on the first line of a lock file. Longer text is not a PID."""
_MAX_PID = 2**31 - 1
_READ_LIMIT = 64
"""Bytes read from a lock file. A PID line is at most 11 bytes."""
_BAD_NAME_CHARS = frozenset(":\r\n\0")


class SubIdExhausted(Exception):
    """No free range of the requested size fits below the ceiling.

    This is a host configuration problem, not a user error. The module has no catalogue code
    for it, so Host Setup reports it as an internal failure.
    """


class SubIdLocked(Exception):
    """Another process holds `<name>.lock` for the subordinate ID file and did not let go in time.

    The lock is not removed while its holder is alive, and the message names the holder's PID
    when the lock records one. Host Setup should be run again later.
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
    uid: int | None = None,
    floor: int = DEFAULT_FLOOR,
    ceiling: int = DEFAULT_CEILING,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> Change:
    """Make sure `user` holds at least `size` subordinate IDs in the file at `path`.

    Lines are matched by name, and also by `str(uid)` when `uid` is given, because newuidmap
    accepts either form. If the existing entries already total at least `size`, nothing is
    written. Otherwise a new entry is appended and the file is replaced atomically with its
    mode kept (0644 for a new file). The read and the replace happen under the file's lock, and
    `SubIdLocked` is raised if another holder keeps the lock for `lock_timeout` seconds.
    """
    _check_name(user)
    _check_size(size)
    with _lock(path, lock_timeout):
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
    """One lock file as read through a single open descriptor. `pid` is None when unknown."""

    dev: int
    ino: int
    mtime_ns: int
    pid: int | None


@contextmanager
def _lock(path: Path, timeout: float) -> Iterator[None]:
    """Hold `<path>.lock` (etc/subuid.lock for etc/subuid) while the caller edits `path`.

    The lock is a file created with O_CREAT|O_EXCL, so only one creator can succeed, and it
    holds the creator's PID. A second caller retries every LOCK_POLL_SECONDS until `timeout`,
    then raises SubIdLocked and leaves the other holder's file in place. An empty or unreadable
    lock counts as held, because it may be a creator that has not written its PID yet. A stale
    lock (see `_is_stale`) is reclaimed at most once per call, through `_reclaim_stale`. When that
    call creates the new lock itself, this caller holds it at once. The file this call created is
    removed on exit, including when the body raises.
    """
    lock = path.with_name(f"{path.name}.lock")
    deadline = time.monotonic() + timeout
    tried_reclaim = False
    while True:
        if _create_lock(lock):
            break
        seen = _read_lock(lock)
        if seen is not None and not tried_reclaim and _is_stale(seen):
            tried_reclaim = True
            if _reclaim_stale(lock, seen):
                break
            continue
        if time.monotonic() >= deadline:
            pid = None if seen is None else seen.pid
            holder = f" (pid {pid})" if pid is not None else ""
            raise SubIdLocked(f"{lock} is held by another process{holder}")
        time.sleep(LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _create_lock(lock: Path) -> bool:
    """Create `lock` exclusively, write this process's PID into it. False if it already exists."""
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(f"{os.getpid()}\n")
    return True


def _read_lock(lock: Path) -> _LockIdentity | None:
    """Read `lock` through one descriptor, so the stat and the PID describe the same file.

    Returns None when the lock does not exist (any more).
    """
    try:
        fd = os.open(lock, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        text = os.read(fd, _READ_LIMIT).decode("ascii", errors="replace")
    finally:
        os.close(fd)
    return _LockIdentity(st.st_dev, st.st_ino, st.st_mtime_ns, _parse_pid(text))


def _parse_pid(text: str) -> int | None:
    """Return the PID on the first line of a lock file, or None when that line is not a PID."""
    first = text.split("\n", 1)[0].strip()
    if _PID.fullmatch(first) is None:
        return None
    pid = int(first)
    return pid if 0 < pid <= _MAX_PID else None


def _is_stale(seen: _LockIdentity) -> bool:
    """True when `seen` records a PID that no longer exists and the file is old enough.

    A lock with no PID yet (its creator has not written it) is never stale.
    """
    if seen.pid is None:
        return False
    if time.time_ns() - seen.mtime_ns < LOCK_STALE_SECONDS * 1_000_000_000:
        return False
    try:
        os.kill(seen.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # The process exists but belongs to another user, so it is alive.
        return False
    return False


def _reclaim_stale(lock: Path, seen: _LockIdentity) -> bool:
    """Reclaim the stale lock that `seen` describes. Return True when this call now holds the lock.

    The race this closes: two waiters judge the same crashed lock stale. Without a gate, the
    first one re-reads it, unlinks it and creates its own lock. The second one had re-read the
    crashed lock before that, so it still unlinks, and removes the first one's fresh lock. Two
    callers then hold the lock at once.

    Every reclaimer therefore takes the gate, `<lock>.reclaim`, with flock(LOCK_EX) before it
    re-reads the lock. The sidecar is opened with O_NOFOLLOW, so a symbolic link in its place is
    refused. The gate is not removed after use: an empty file is harmless, and removing it would
    reopen the race. See `_reclaim_locked` for what runs under the gate.
    """
    gate_path = lock.with_name(f"{lock.name}.reclaim")
    gate = os.open(gate_path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(gate, fcntl.LOCK_EX)
        return _reclaim_locked(lock, seen)
    finally:
        os.close(gate)


def _reclaim_locked(lock: Path, seen: _LockIdentity) -> bool:
    """Runs under the reclaim gate. Unlink `lock` if it is still `seen`, then create a new one.

    The re-read compares every field of the identity: device, inode, mtime_ns and PID. If any of
    them differs, another caller reclaimed or replaced the lock after `seen` was taken, so it is
    left alone and this call returns False. Otherwise the stale lock is unlinked and this call
    creates the new lock while it still holds the gate. Other reclaimers cannot touch that lock,
    because they must take the gate first. A caller that is not reclaiming can still win the
    O_EXCL race for the path. Then this call returns False and leaves that lock alone.

    One gap remains, and it is accepted: PID reuse. After the holder dies, an unrelated process
    may take its PID, and the stale lock then looks held. The caller waits and times out, which is
    the safe side.
    """
    now = _read_lock(lock)
    if now is None or now != seen:
        return False
    lock.unlink(missing_ok=True)
    return _create_lock(lock)


def _append_line(before: bytes, line: bytes) -> bytes:
    prefix = before if not before or before.endswith(b"\n") else before + b"\n"
    return prefix + line + b"\n"


def _check_size(size: int) -> None:
    if size < MIN_SIZE:
        raise ValueError(f"AS-005 requires at least {MIN_SIZE} IDs, not {size}")


def _check_name(user: str) -> None:
    if not user or user != user.strip() or _BAD_NAME_CHARS & set(user):
        raise ValueError(f"invalid user name for a subordinate ID entry: {user!r}")
