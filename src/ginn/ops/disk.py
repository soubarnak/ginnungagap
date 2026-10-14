# SPDX-License-Identifier: Apache-2.0
"""The FR-121 disk precheck: the space an operation needs, against the free space (T8.2).

`estimate_required` applies the 10 percent margin in whole bytes. `check_free` compares the result
with the space an unprivileged user may write on the filesystem of a path (f_bavail, the figure
shutil.disk_usage reports). When there is less, it raises GINN-E-DISK-SPACE before anything is
written. `check_free_paths` checks each path on its own, so every filesystem that an operation
writes to must hold the space (FR-121).

The size estimate is the caller's. The podman adapter has no call that reports an image's size
before it is pulled (`image_inspect` is valid only once the image is local), so a caller passes the
integer. A `Podman.manifest_size` method would supply it.
"""

import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Final, Protocol

from ginn.errors import make

DISK_MARGIN_PERCENT: Final = 110
"""FR-121: free space must cover the estimate times 110 percent, so the factor is 110 over 100."""


class _Vfs(Protocol):
    """The two fields of a statvfs result that the precheck reads."""

    @property
    def f_bavail(self) -> int: ...

    @property
    def f_frsize(self) -> int: ...


StatVfs = Callable[[str], _Vfs]
"""The statvfs seam. The default is os.statvfs. A test passes a fake."""


def estimate_required(bytes_estimate: int) -> int:
    """The free space FR-121 requires for an estimate: the estimate times 1.10, rounded up.

    The arithmetic is in integers. A float product is not exact (10 * 1.1 is 11.000000000000002),
    so the ceiling of estimate * 110 / 100 is (estimate * 110 + 99) // 100.
    """
    if bytes_estimate < 0:
        raise ValueError(f"FR-121: a size estimate is not negative, got {bytes_estimate}")
    return (bytes_estimate * DISK_MARGIN_PERCENT + 99) // 100


def free_bytes(path: Path, *, statvfs: StatVfs = os.statvfs) -> int:
    """Bytes an unprivileged user may write on the filesystem that holds `path` (FR-121).

    A path that does not exist yet is probed at its nearest existing parent, as the create plan
    does, because a realm home is made after the precheck.
    """
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    vfs = statvfs(os.fspath(probe))
    return vfs.f_bavail * vfs.f_frsize


def check_free(path: Path, required: int, *, statvfs: StatVfs = os.statvfs) -> None:
    """Raise GINN-E-DISK-SPACE when the filesystem of `path` has less than `required` bytes free.

    Exactly `required` bytes passes. The details hold `needed` and `available` as "N bytes", the
    catalogue's placeholders, and `path`.
    """
    available = free_bytes(path, statvfs=statvfs)
    if available < required:
        err = make("GINN-E-DISK-SPACE", needed=f"{required} bytes", available=f"{available} bytes")
        err.details["path"] = str(path)
        raise err


def check_free_paths(
    paths: Iterable[Path], required: int, *, statvfs: StatVfs = os.statvfs
) -> None:
    """`check_free` for each path, in order. The first path that is short raises (FR-121)."""
    for path in paths:
        check_free(path, required, statvfs=statvfs)
