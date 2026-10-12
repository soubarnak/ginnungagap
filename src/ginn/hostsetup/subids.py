# SPDX-License-Identifier: Apache-2.0
"""Subordinate UID/GID ranges in /etc/subuid and /etc/subgid (FR-001, FR-002, AS-005).

A line is `name_or_uid:start:count`. Blank lines, comments and lines this module does not
understand are kept byte for byte. `ensure_range` only ever appends, so the bytes already in the
file are never rewritten. A user who already holds at least `size` IDs in total is left alone,
which makes a second run a no-op (FR-002). The read-modify-replace runs under `<name>.lock` next to
the file (etc/subuid.lock, etc/subgid.lock), so two Host Setup runs cannot both append (FR-001).
The lock file holds its holder's PID. A lock whose PID is gone and whose file is old enough is
left behind by a crash, so it is reclaimed instead of blocking every later run.
"""

from __future__ import annotations

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

_DECIMAL = re.compile(r"[0-9]{1,10}")
_MAX_PID = 2**31 - 1
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


@contextmanager
def _lock(path: Path, timeout: float) -> Iterator[None]:
    """Hold `<path>.lock` (etc/subuid.lock for etc/subuid) while the caller edits `path`.

    The lock is a file created with O_CREAT|O_EXCL, so only one creator can succeed, and it
    holds the creator's PID. A second caller retries every LOCK_POLL_SECONDS until `timeout`,
    then raises SubIdLocked and leaves the other holder's file in place. An empty or unreadable
    lock counts as held, because it may be a creator that has not written its PID yet. A lock
    whose PID is gone (os.kill(pid, 0) raises ProcessLookupError) and whose file is at least
    LOCK_STALE_SECONDS old is removed once, and the caller tries again. The file this call
    created is removed on exit, including when the body raises.
    """
    lock = path.with_name(f"{path.name}.lock")
    deadline = time.monotonic() + timeout
    reclaimed = False
    while True:
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pid = _holder_pid(lock)
            if pid is not None and not reclaimed and _is_stale(lock, pid):
                lock.unlink(missing_ok=True)
                reclaimed = True
                continue
            if time.monotonic() >= deadline:
                holder = f" (pid {pid})" if pid is not None else ""
                raise SubIdLocked(f"{lock} is held by another process{holder}") from None
            time.sleep(LOCK_POLL_SECONDS)
            continue
        break
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(f"{os.getpid()}\n")
        yield
    finally:
        lock.unlink(missing_ok=True)


def _holder_pid(lock: Path) -> int | None:
    """Return the PID written in `lock`, or None when it is missing, empty or not a PID."""
    try:
        text = lock.read_text(encoding="ascii", errors="replace")
    except FileNotFoundError:
        return None
    first = text.split("\n", 1)[0].strip()
    if _DECIMAL.fullmatch(first) is None:
        return None
    pid = int(first)
    return pid if 0 < pid <= _MAX_PID else None


def _is_stale(lock: Path, pid: int) -> bool:
    """True when `pid` no longer exists and `lock` is at least LOCK_STALE_SECONDS old."""
    try:
        age = time.time() - os.stat(lock).st_mtime
    except FileNotFoundError:
        return False
    if age < LOCK_STALE_SECONDS:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # The process exists but belongs to another user, so it is alive.
        return False
    return False


def _append_line(before: bytes, line: bytes) -> bytes:
    prefix = before if not before or before.endswith(b"\n") else before + b"\n"
    return prefix + line + b"\n"


def _check_size(size: int) -> None:
    if size < MIN_SIZE:
        raise ValueError(f"AS-005 requires at least {MIN_SIZE} IDs, not {size}")


def _check_name(user: str) -> None:
    if not user or user != user.strip() or _BAD_NAME_CHARS & set(user):
        raise ValueError(f"invalid user name for a subordinate ID entry: {user!r}")
