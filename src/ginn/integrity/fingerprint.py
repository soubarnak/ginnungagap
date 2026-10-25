# SPDX-License-Identifier: Apache-2.0
"""Host Integrity Fingerprint (FR-006, AS-020, NFR-011): a stat-only snapshot of the host scope.

The scope is exactly the five roots of AS-020(b): /etc, /var/db/xbps, /var/service, /usr/local and
/opt. Every path below a root gets one PathStat from lstat: its type, permission bits, owner,
group, size, mtime, ctime, inode and, for a symbolic link, the target text. File contents are never
read, so the cost grows with the number of files and not with their size (NFR-011).

The walk holds a directory descriptor for each directory it is inside. A directory is opened with
O_NOFOLLOW | O_DIRECTORY, and each child is examined with lstat relative to its parent descriptor.
A name swapped for a link during the walk is therefore recorded as a link and not entered.
A symbolic link is never followed: it is recorded with its target, and the walk does not enter it,
so a link cycle cannot loop and a link into another tree does not widen the scope. A directory
that cannot be opened or listed is recorded once, with `unreadable` set, and the walk goes on.
A name that is gone when the walk reaches it is not recorded at all: it was removed, not
unreadable. A missing root is recorded as `absent`, so its later creation is a change.

Limits, kept on purpose:
- /var/service is a symbolic link on Void. Only the link is recorded, with its target. The service
  directory it points to is under /etc, so enabling or disabling a service is caught there.
- A directory that the walk cannot open is recorded as one unreadable entry. Edits inside it are
  not seen, and the entry is reported as a change only when its own stat changes.

Paths in the fingerprint are host paths (/etc/hosts), whatever `root_dir` is. `root_dir` only
moves where the walk starts, which is how tests run it under a temporary directory.

The digest is a sha256 over the sorted (path, stat) tuples. `diff` compares two fingerprints and
returns the changed, added and removed paths, sorted. ctime is part of the record, so a content
change that is undone by restoring the mtime (os.utime) is still a change.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

HOST_ROOT = Path("/")
SCOPE_ROOTS: tuple[str, ...] = ("etc", "var/db/xbps", "var/service", "usr/local", "opt")
"""The fingerprinted roots, relative to the host root (AS-020(b)). Order is not significant."""

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class PathStat(NamedTuple):
    """The lstat record of one path. `target` is the link text for a symbolic link, else None.

    `kind` is one of file, dir, symlink, other (a device, FIFO or socket), absent (a missing
    root), unstatable (lstat failed for another reason). `unreadable` marks a directory that
    could not be opened or listed, or a link whose target could not be read.
    """

    kind: str
    mode: int
    uid: int
    gid: int
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    target: str | None
    unreadable: bool


ABSENT = PathStat("absent", 0, 0, 0, 0, 0, 0, 0, None, False)
_UNSTATABLE = PathStat("unstatable", 0, 0, 0, 0, 0, 0, 0, None, True)


@dataclass(frozen=True)
class Fingerprint:
    """The host path to stat record map, with the sha256 digest over its sorted tuples."""

    entries: Mapping[str, PathStat]
    digest: str

    @classmethod
    def of(cls, entries: Mapping[str, PathStat]) -> Fingerprint:
        """Build a fingerprint from `entries`, computing the digest from them."""
        return cls(entries=dict(entries), digest=_digest(entries))


def capture(root_dir: Path = HOST_ROOT) -> Fingerprint:
    """Walk every scope root under `root_dir` and return the fingerprint. Never follows links."""
    entries: dict[str, PathStat] = {}
    for scope in SCOPE_ROOTS:
        host = f"/{scope}"
        if not _visit(None, os.path.join(root_dir, scope), host, entries):
            entries[host] = ABSENT
    return Fingerprint.of(entries)


def diff(old: Fingerprint, new: Fingerprint) -> list[str]:
    """Return the host paths that were changed, added or removed between `old` and `new`, sorted.

    Equal digests mean nothing changed, so the per-path comparison runs only when they differ.
    """
    if old.digest == new.digest:
        return []
    keys = set(old.entries) | set(new.entries)
    return sorted(path for path in keys if old.entries.get(path) != new.entries.get(path))


def _visit(parent: int | None, name: str, host: str, entries: dict[str, PathStat]) -> bool:
    """Record one path and, for a directory, everything below it.

    `name` is relative to the descriptor `parent`, or a full path when `parent` is None (a scope
    root). Returns False when nothing exists at `name`. Nothing is recorded then: a name removed
    during the walk is not a change this walk can see, and it is not unreadable.
    """
    try:
        info = os.lstat(name, dir_fd=parent)
    except FileNotFoundError:
        return False
    except OSError:
        entries[host] = _UNSTATABLE
        return True
    if stat.S_ISLNK(info.st_mode):
        try:
            entries[host] = _record(info, "symlink", os.readlink(name, dir_fd=parent), False)
        except OSError:
            entries[host] = _record(info, "symlink", None, True)
        return True
    if not stat.S_ISDIR(info.st_mode):
        entries[host] = _record(info, _kind(info.st_mode), None, False)
        return True
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return False
    except OSError:
        entries[host] = _record(info, "dir", None, True)
        return True
    try:
        entries[host] = _record(info, "dir", None, False)
        _list(fd, host, entries)
    finally:
        os.close(fd)
    return True


def _list(fd: int, host: str, entries: dict[str, PathStat]) -> None:
    """Visit every child of the open directory `fd`, which is recorded under `host`."""
    try:
        with os.scandir(fd) as listing:
            names = [entry.name for entry in listing]
    except OSError:
        entries[host] = entries[host]._replace(unreadable=True)
        return
    for name in names:
        _visit(fd, name, f"{host}/{name}", entries)


def _record(info: os.stat_result, kind: str, target: str | None, unreadable: bool) -> PathStat:
    return PathStat(
        kind=kind,
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
        gid=info.st_gid,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
        inode=info.st_ino,
        target=target,
        unreadable=unreadable,
    )


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _digest(entries: Mapping[str, PathStat]) -> str:
    hasher = hashlib.sha256()
    for path in sorted(entries):
        line = repr((path, *entries[path])).encode("utf-8", "surrogateescape")
        hasher.update(line + b"\n")
    return hasher.hexdigest()
