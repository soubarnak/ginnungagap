# SPDX-License-Identifier: Apache-2.0
"""Scratch directories of a services-on create, under the runtime root (F3, F6).

A services-on create reads `/sbin/init` out of its created container (`check_init`, in
ops/provision.py), and it stages the realm's machine-id as a file for `podman cp`
(`_copy_machine_id`). Both use the scratch directory that `scratch_dir` makes. It lies under the
runtime root, which is owned by the user with mode 0700, and its name starts with SCRATCH_PREFIX. A
create killed with SIGKILL leaves its directory behind, so ops/recovery.py `sweep_scratch` calls
`remove_leftovers`, which removes what it finds with that prefix. It touches only what the user
owns, and it never follows a link.
"""

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ginn.store.paths import OwnedPaths

SCRATCH_PREFIX: Final = "create-scratch-"
"""The name prefix of every scratch directory. A sweep looks at no other entry."""


@contextmanager
def scratch_dir(paths: OwnedPaths) -> Iterator[Path]:
    """A new scratch directory under the runtime root, removed when the block exits.

    The caller must hold the image lock of the create's distribution (ops/recovery.py
    `sweep_scratch` relies on it): the sweep removes a scratch directory only while no create runs.
    """
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX, dir=paths.runtime) as name:
        yield Path(name)


@dataclass(frozen=True)
class ScratchSweep:
    """What a sweep removed, and what it kept. `kept` holds (name, reason) pairs."""

    removed: tuple[str, ...]
    kept: tuple[tuple[str, str], ...]


def leftover_names(root: Path) -> list[str]:
    """The names under `root` that carry SCRATCH_PREFIX, sorted. Nothing is followed or opened.

    The scan lists each entry with its name only, so a link with the prefix is listed as a name
    and nothing more. A missing root has no leftovers. Any other failure to read the root raises
    OSError.
    """
    try:
        with os.scandir(root) as entries:
            return sorted(e.name for e in entries if e.name.startswith(SCRATCH_PREFIX))
    except FileNotFoundError:
        return []


def remove_leftovers(root: Path, uid: int) -> ScratchSweep:
    """Remove the scratch directories that a killed create left under `root`.

    Only entries whose name starts with SCRATCH_PREFIX are looked at. A directory that the user
    owns is emptied of its plain files and links, and then removed with rmdir. Each link is
    unlinked, never followed, so its target survives. A directory that holds another directory is
    kept, and so is a link, a file, or a directory of another owner: each one is reported with its
    reason. A missing root is an empty sweep.
    """
    names = leftover_names(root)
    removed: list[str] = []
    kept: list[tuple[str, str]] = []
    for name in names:
        reason = _remove_one(root / name, uid)
        if reason is None:
            removed.append(name)
        else:
            kept.append((name, reason))
    return ScratchSweep(tuple(removed), tuple(kept))


def _remove_one(path: Path, uid: int) -> str | None:
    """Empty and remove one scratch directory. Return None when it is gone, else why it is kept."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot be read ({exc.strerror or exc})"
    if not stat.S_ISDIR(before.st_mode) or before.st_uid != uid:
        return "is not a directory of this user (a link, a file or another owner)"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        return f"cannot be opened without following a link ({exc.strerror or exc})"
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return "changed while it was read"
        children = os.listdir(fd)
        for child in children:
            if stat.S_ISDIR(os.stat(child, dir_fd=fd, follow_symlinks=False).st_mode):
                return "holds a directory, so it is kept"
        for child in children:
            os.unlink(child, dir_fd=fd)
    except OSError as exc:
        return f"cannot be emptied ({exc.strerror or exc})"
    finally:
        os.close(fd)
    try:
        os.rmdir(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot be removed ({exc.strerror or exc})"
    return None
