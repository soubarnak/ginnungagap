# SPDX-License-Identifier: Apache-2.0
"""Atomic replacement of small host files written by Host Setup (FR-001, FR-002, DR-035).

The new bytes go to a temporary file in the target's directory. When the target exists, the
temporary file is given the target's owner and group, so a rewrite never changes who owns a host
file. It is flushed, given the target mode explicitly (umask would otherwise change it), renamed
over the target, and the directory is flushed. On any failure the temporary file is removed and
the target is left as it was.

Two temporary names exist. The default is a random name, which `store/` keeps. Host Setup passes
`fixed_tmp=True`, so the temporary file is `.<name>.ginn-tmp` beside the target. A SIGKILL then
leaves a predictable sibling, and the next run removes it under its lock before it writes
(ADR-013, Decision 16). Nothing else is swept.
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path

TMP_SUFFIX = ".ginn-tmp"
"""Suffix of the fixed temporary name: `.<name>.ginn-tmp`, next to the target."""


def file_mode(path: Path, default: int) -> int:
    """Return the permission bits of `path`, or `default` when it does not exist."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return default


def file_owner(path: Path) -> tuple[int, int] | None:
    """Return (uid, gid) of `path` without following a symlink, or None when it does not exist."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return st.st_uid, st.st_gid


def fixed_tmp_path(path: Path) -> Path:
    """Return the fixed temporary name for `path`: `.<name>.ginn-tmp` in the same directory."""
    return path.with_name(f".{path.name}{TMP_SUFFIX}")


def remove_stale_tmp(tmp: Path) -> None:
    """Remove a temporary file that an earlier run left behind. A directory there is refused."""
    try:
        st = os.lstat(tmp)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        raise IsADirectoryError(errno.EISDIR, "a directory is where a temp file goes", str(tmp))
    os.unlink(tmp)


def atomic_write(path: Path, data: bytes, mode: int, *, fixed_tmp: bool = False) -> None:
    """Replace `path` with `data` and set its permission bits to `mode`.

    A new file is owned by the calling process. An existing file keeps its owner and group. With
    `fixed_tmp`, the temporary file has the fixed name of `fixed_tmp_path`, and a stale one there
    is removed first. Otherwise a random name is used.
    """
    owner = file_owner(path)
    if fixed_tmp:
        tmp = fixed_tmp_path(path)
        remove_stale_tmp(tmp)
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
    else:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
    try:
        try:
            if owner is not None:
                os.fchown(fd, owner[0], owner[1])
            os.fchmod(fd, mode)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    sync_dir(path.parent)


def atomic_symlink(path: Path, target: str) -> None:
    """Point the symbolic link `path` at `target`, replacing any link, with a fixed temp name."""
    tmp = fixed_tmp_path(path)
    remove_stale_tmp(tmp)
    os.symlink(target, tmp)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    sync_dir(path.parent)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def sync_dir(directory: Path) -> None:
    """Flush a directory so that renames and new names in it survive a power loss."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
