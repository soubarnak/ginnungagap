# SPDX-License-Identifier: Apache-2.0
"""Atomic replacement of small host files written by Host Setup (FR-001, FR-002, DR-035).

The new bytes go to a temporary file in the target's directory. The temporary file is flushed,
given the target mode explicitly (umask would otherwise change it), renamed over the target, and
the directory is flushed. On any failure the temporary file is removed and the target is left
as it was.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def file_mode(path: Path, default: int) -> int:
    """Return the permission bits of `path`, or `default` when it does not exist."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return default


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Replace `path` with `data` and set its permission bits to `mode`."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            os.fchmod(fd, mode)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
