# SPDX-License-Identifier: Apache-2.0
"""Atomic file write with an explicit mode, for the export writers (DR-010, DR-035).

`store.paths.write_bytes_atomic` writes mode 0600 and has no mode argument. The host menu entries
are 0644 and the launchers are 0755, so the three export writers share this one instead. The steps
are those of `write_bytes_atomic`: a temporary file in the same directory, fchmod to the mode,
write, fsync, rename over the target, and an fsync of the directory. A failure removes the
temporary file and leaves the old file in place.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ginn.store.paths import fsync_dir


def write_file_atomic(path: Path, data: bytes, mode: int) -> None:
    """Write `data` to `path` with `mode` (fchmod, so the umask does not apply)."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        try:
            os.fchmod(fd, mode)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        _unlink_quiet(tmp)
        raise
    fsync_dir(path.parent)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
