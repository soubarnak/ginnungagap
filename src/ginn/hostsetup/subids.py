# SPDX-License-Identifier: Apache-2.0
"""Subordinate UID/GID ranges in /etc/subuid and /etc/subgid (FR-001, FR-002, AS-005).

A line is `name_or_uid:start:count`. Blank lines, comments and lines this module does not
understand are kept byte for byte. `ensure_range` only ever appends, so the bytes already in the
file are never rewritten. A user who already holds at least `size` IDs in total is left alone,
which makes a second run a no-op (FR-002).
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ginn.hostsetup._atomic import atomic_write, file_mode

MIN_SIZE = 524_288
"""Minimum IDs per user (AS-005, enough for nested rootless containers)."""

DEFAULT_SIZE = MIN_SIZE
DEFAULT_FLOOR = 100_000
DEFAULT_CEILING = 2**32 - 1 - 65_536
DEFAULT_MODE = 0o644

_DECIMAL = re.compile(r"[0-9]+")
_BAD_NAME_CHARS = frozenset(":\r\n\0")


class SubIdExhausted(Exception):
    """No free range of the requested size fits below the ceiling.

    This is a host configuration problem, not a user error. The module has no catalogue code
    for it, so Host Setup reports it as an internal failure.
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
) -> Change:
    """Make sure `user` holds at least `size` subordinate IDs in the file at `path`.

    Lines are matched by name, and also by `str(uid)` when `uid` is given, because newuidmap
    accepts either form. If the existing entries already total at least `size`, nothing is
    written. Otherwise a new entry is appended and the file is replaced atomically with its
    mode kept (0644 for a new file).
    """
    _check_name(user)
    _check_size(size)
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


def _append_line(before: bytes, line: bytes) -> bytes:
    prefix = before if not before or before.endswith(b"\n") else before + b"\n"
    return prefix + line + b"\n"


def _check_size(size: int) -> None:
    if size < MIN_SIZE:
        raise ValueError(f"AS-005 requires at least {MIN_SIZE} IDs, not {size}")


def _check_name(user: str) -> None:
    if not user or user != user.strip() or _BAD_NAME_CHARS & set(user):
        raise ValueError(f"invalid user name for a subordinate ID entry: {user!r}")
