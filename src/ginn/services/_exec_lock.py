# SPDX-License-Identifier: Apache-2.0
"""The exec lock and orphan marker paths, and the read-only lock check (FR-123, FR-124, ADR-014).

This module is a leaf. It imports only the domain, the error catalogue and the Owned Paths. The
relay in services/exec_relay.py re-exports these names. It cannot be the place they are imported
from by ops/_mutating.py: exec_relay imports podman.cli, which imports ginn.ops, so an import of
exec_relay from the choke point would close a cycle (tests/unit/test_import_order.py).
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path
from typing import Final

from ginn.domain import RealmName
from ginn.errors import GinnError
from ginn.store.paths import OwnedPaths

LOCK_DIR_NAME: Final = "exec"
"""The directory under the runtime root that holds one exec lock and one orphan marker per realm."""
LOCK_SUFFIX: Final = ".lock"
ORPHAN_SUFFIX: Final = ".orphan"


def exec_lock_dir(paths: OwnedPaths) -> Path:
    """The directory of the exec locks and orphan markers, `<runtime root>/exec` (ADR-014)."""
    return paths.runtime / LOCK_DIR_NAME


def exec_lock_path(paths: OwnedPaths, name: RealmName) -> Path:
    """The realm's exec lock, `<runtime root>/exec/<realm>.lock` (FR-123, ADR-014)."""
    return exec_lock_dir(paths) / f"{name.value}{LOCK_SUFFIX}"


def exec_orphan_path(paths: OwnedPaths, name: RealmName) -> Path:
    """The realm's orphan marker, `<runtime root>/exec/<realm>.orphan` (FR-124, ADR-014)."""
    return exec_lock_dir(paths) / f"{name.value}{ORPHAN_SUFFIX}"


def exec_lock_busy(paths: OwnedPaths, name: RealmName) -> bool:
    """True while a mutating exec of the realm holds its lock (FR-123, ADR-014). Read only.

    The lock file is opened read-only and never created, and a missing file is not busy. So this
    creates neither `exec/` nor the lock. A held LOCK_EX makes LOCK_SH|LOCK_NB fail, and a lock that
    is free is released at once. The open is non-blocking, so a FIFO is refused rather than waited
    for. A path that is a symbolic link, a directory or any other non-regular file, or that cannot
    be opened, raises GINN-E-INTERNAL, because ginn never creates one. It is never read or followed.
    """
    path = exec_lock_path(paths, name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _lock_fault(path, "is a symbolic link, not a regular file") from exc
        raise _lock_fault(path, f"is not a regular file ({exc.strerror})") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _lock_fault(path, "is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _lock_fault(path: Path, reason: str) -> GinnError:
    """GINN-E-INTERNAL for an exec lock path that cannot be used, naming the path (ADR-014).

    The catalogue summary has no place for the path, so the message is built here. The user sees
    the path in text output, not only in the JSON details.
    """
    return GinnError(
        "GINN-E-INTERNAL",
        f"the exec lock {path} {reason}",
        "ginn never creates this path; remove it and run the command again, and report it if it "
        "comes back",
        70,
        details={"path": str(path), "reason": reason},
    )


__all__ = [
    "LOCK_DIR_NAME",
    "LOCK_SUFFIX",
    "ORPHAN_SUFFIX",
    "exec_lock_busy",
    "exec_lock_dir",
    "exec_lock_path",
    "exec_orphan_path",
]
