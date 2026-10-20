# SPDX-License-Identifier: Apache-2.0
"""Atomic file replacement with an explicit mode, for the export writers (DR-010, DR-035).

`store.paths.write_bytes_atomic` always writes mode 0600 and takes no mode argument. The host
menu entries are 0644 and the launchers are 0755, so the export writers use this module instead.
A new file is opened beside the target under a fresh name, given the mode, filled, flushed to
disk and renamed over the target; the directory is flushed last. When a step before the rename
fails, the new file is removed and the old target is left as it was.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from ginn.store.paths import fsync_dir


def write_file_atomic(path: Path, data: bytes, mode: int) -> None:
    """Replace `path` with `data`, with exactly `mode` (the umask does not apply)."""
    staged = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    # O_EXCL makes a name that is already taken fail here, before the cleanup below could run.
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    renamed = False
    try:
        try:
            sink = os.fdopen(fd, "wb")
        except BaseException:
            # A file object owns the descriptor only once fdopen returns, so a failure here
            # leaves it open unless it is closed now. The staged file is removed below.
            os.close(fd)
            raise
        with sink:
            sink.write(data)
            sink.flush()
            os.fchmod(sink.fileno(), mode)
            os.fsync(sink.fileno())
        os.replace(staged, path)
        renamed = True
    finally:
        if not renamed:
            staged.unlink(missing_ok=True)
    fsync_dir(path.parent)
