# SPDX-License-Identifier: Apache-2.0
"""The inner podman's config files in the Realm Home, for a realm with nested containers (ADR-006).

A realm with the `nested` toggle gets two files in its Realm Home, at the paths that rootless podman
reads by default inside the realm: `~/.config/containers/storage.conf` and `containers.conf`. The
Realm Home is the host home path inside the realm (FR-048), so these are the same files.

- `storage.conf` selects the overlay driver with fuse-overlayfs and `ignore_chown_errors`. The
  single-ID mode of ADR-006 needs the second option: without it the image unpack fails on
  `/etc/shadow`. The inner command passes no storage flag, because podman 5.4.2 drops the
  storage.conf options when `--root` is given (S-07e Finding 1).
- `containers.conf` pins the cgroup manager and the event logger that podman otherwise infers
  (ADR-006). Its tests use the services-off realm, so it is not needed for AC-3 there.

The files are ginn-owned data (DR-006). The writes are atomic, and the directories are made with the
owned mode. Nothing else in the Realm Home changes.
"""

import errno
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ginn.store.paths import OWNED_DIR_MODE, write_bytes_atomic

CONFIG_DIR: Final = Path(".config") / "containers"
STORAGE_CONF: Final = """\
[storage]
driver = "overlay"

[storage.options.overlay]
mount_program = "/usr/bin/fuse-overlayfs"
ignore_chown_errors = "true"
"""
CONTAINERS_CONF: Final = """\
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
"""
FILES: Final[dict[str, str]] = {"storage.conf": STORAGE_CONF, "containers.conf": CONTAINERS_CONF}


def write_config(realm_home: Path) -> list[Path]:
    """Write both files under `realm_home`, and return their paths. Existing files are replaced.

    `realm_home` is the Owned Path that the create made (ops/create.py), so it is empty of these
    files on the first write.
    """
    directory = realm_home / CONFIG_DIR
    directory.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in FILES.items():
        path = directory / name
        write_bytes_atomic(path, text.encode("utf-8"))
        written.append(path)
    return written


@dataclass(frozen=True)
class ConfigWrite:
    """What one `write_missing_config` made: its files, and the directories it created.

    `dirs` is outermost first. `undo` removes the files, then the directories that are empty, so a
    rollback leaves the Realm Home as it was (ops/rebuild.py).
    """

    files: tuple[Path, ...] = ()
    dirs: tuple[Path, ...] = ()

    def undo(self) -> None:
        """Remove the files this write made, then its directories, deepest first, when empty."""
        for path in self.files:
            path.unlink(missing_ok=True)
        for directory in reversed(self.dirs):
            try:
                directory.rmdir()
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY):
                    raise


def write_missing_config(realm_home: Path) -> ConfigWrite:
    """Write each of the two config files that is absent, and never one that is there (ADR-006).

    A rebuild uses this for a realm that becomes nested. A file the user edited, or one that is
    already there, is kept as it is, so only what this call makes is returned, for a rollback. If a
    write fails part way, what was made is undone before the error is raised.
    """
    directory = realm_home / CONFIG_DIR
    made_dirs: list[Path] = []
    made_files: list[Path] = []
    try:
        for depth in range(1, len(CONFIG_DIR.parts) + 1):
            level = realm_home.joinpath(*CONFIG_DIR.parts[:depth])
            if not os.path.lexists(level):
                level.mkdir(mode=OWNED_DIR_MODE)
                made_dirs.append(level)
        for name, text in FILES.items():
            path = directory / name
            if os.path.lexists(path):
                continue
            write_bytes_atomic(path, text.encode("utf-8"))
            made_files.append(path)
    except BaseException:
        ConfigWrite(files=tuple(made_files), dirs=tuple(made_dirs)).undo()
        raise
    return ConfigWrite(files=tuple(made_files), dirs=tuple(made_dirs))


__all__ = [
    "CONFIG_DIR",
    "CONTAINERS_CONF",
    "FILES",
    "STORAGE_CONF",
    "ConfigWrite",
    "write_config",
    "write_missing_config",
]
