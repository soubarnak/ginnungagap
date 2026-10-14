# SPDX-License-Identifier: Apache-2.0
"""The runit service directory of one services-on realm (FR-081, ADR-003, AS-028).

The user's runit supervisor (hostsetup/runit_system.py, `runsvdir -P <SV_DIR>`) starts one runsv for
each directory in SV_DIR. A realm's directory is `<SV_DIR>/<realm>/`, with three entries:

- `run` is `exec <ginn> _service-run <realm>`. It starts the container and then execs ginn-wait,
  which waits on conmon, so runsv supervises ginn-wait (NFR-033).
- `finish` is `exec <ginn> _service-finish <realm> "$1" "$2"`. runsv passes the exit code of run
  and the signal that ended it (runsv(8)).
- `down` is present when runit must not start the realm (DR-012, autostart off). T7.5 toggles it.

`render` is pure: it builds the two scripts and writes nothing. `write_service_dir` builds the
directory under a dotted name, which runsvdir skips, and renames it into place once every file is
in it, so runsvdir never starts a half-made service. Every path it writes is an Owned Path
(DR-010): the directory is under the state root, the conmon pidfile under the runtime root and the
crash log under the state root.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Final

from ginn.domain import RealmName
from ginn.errors import GinnError, make
from ginn.export._files import write_file_atomic
from ginn.runtime.spec import JOURNALD_DROPIN_TEXT
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths, fsync_dir

RUN: Final = "run"
FINISH: Final = "finish"
DOWN: Final = "down"
SCRIPT_MODE: Final = 0o755
"""The mode of `run` and `finish`, as runsv needs them to be executable (ADR-003)."""
DOWN_MODE: Final = 0o644
WAIT_HELPER: Final = "/usr/libexec/ginn/ginn-wait"
"""The installed ginn-wait (native/ginn-wait.c), which the supervised process execs."""
_TEMP_SUFFIX: Final = ".tmp"
_GONE_SUFFIX: Final = ".gone"
_FORBIDDEN_IN_SCRIPT: Final = ("\n", "\r", "\0")


def _quote(value: str) -> str:
    """One POSIX sh word in single quotes, whatever the value holds (the scripts run as a user)."""
    return "'" + value.replace("'", "'\\''") + "'"


def service_dir(paths: OwnedPaths, name: RealmName) -> Path:
    """The realm's runit service directory, `<SV_DIR>/<realm>` (ADR-003)."""
    return paths.sv / name.value


def conmon_pidfile(paths: OwnedPaths, name: RealmName) -> Path:
    """The conmon pidfile of the realm's container, under the runtime root (FR-081).

    The runtime root is tmpfs, so the file does not survive a reboot. `ginn-wait` reads it.
    """
    return paths.runtime / "services" / f"{name.value}.conmon.pid"


def crash_log(paths: OwnedPaths, name: RealmName) -> Path:
    """The crash timestamps of the realm, kept by the finish script (AS-028)."""
    return paths.state / "services" / f"{name.value}.crashes"


def journald_dropin(paths: OwnedPaths) -> Path:
    """The ginn-owned journald drop-in that ADR-007 mitigation a binds into a services-on realm."""
    return paths.data / "services" / "journald-ginn.conf"


def render(name: RealmName, program: str) -> dict[str, str]:
    """The text of the `run` and `finish` scripts of realm `name` (FR-081, ADR-003).

    `program` is the absolute path of the ginn command. Each value is one single-quoted word, so a
    path with spaces or quotes stays one argument. The realm name is validated by RealmName.
    """
    if not os.path.isabs(program) or any(c in program for c in _FORBIDDEN_IN_SCRIPT):
        raise ValueError(f"the ginn command must be an absolute plain path: {program!r}")
    ginn = _quote(program)
    realm = _quote(name.value)
    header = (
        "#!/bin/sh\n"
        f"# ginn services-on realm {name.value} (FR-081, ADR-003).\n"
        "# Written by ginn. ginn rewrites this file; do not edit it.\n"
    )
    return {
        RUN: header
        + "# Starts the realm through ginn, which execs ginn-wait on its conmon (NFR-033).\n"
        + f"exec {ginn} _service-run {realm}\n",
        FINISH: header
        + "# runsv passes the exit code of run and the signal that ended it (runsv(8)).\n"
        + f'exec {ginn} _service-finish {realm} "$1" "$2"\n',
    }


def write_service_dir(paths: OwnedPaths, name: RealmName, program: str) -> Path:
    """Install the realm's service directory, all at once, and return it (FR-081, AS-028).

    The directory is built as `<SV_DIR>/.<realm>.tmp` and renamed to `<SV_DIR>/<realm>` once `run`,
    `finish` and `down` are in it. runsvdir skips dotted names, so it never sees the temporary
    directory. A directory that already holds the realm's name is refused with FileExistsError,
    because the caller checked that no such realm exists.
    """
    scripts = render(name, program)
    sv = paths.sv
    sv.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
    target = service_dir(paths, name)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"a runit service directory already exists: {target}")
    temp = sv / f".{name.value}{_TEMP_SUFFIX}"
    _remove_tree(temp)
    temp.mkdir(mode=OWNED_DIR_MODE)
    for file_name, text in scripts.items():
        write_file_atomic(temp / file_name, text.encode("utf-8"), SCRIPT_MODE)
    write_file_atomic(temp / DOWN, b"", DOWN_MODE)
    os.rename(temp, target)
    fsync_dir(sv)
    _mkdir_private(conmon_pidfile(paths, name).parent)
    _mkdir_private(crash_log(paths, name).parent)
    return target


def write_journald_dropin(paths: OwnedPaths) -> Path:
    """Write the ginn-owned journald drop-in (ADR-007 a) and return its path. Shared by realms."""
    path = journald_dropin(paths)
    _mkdir_private(path.parent)
    write_file_atomic(path, _dropin_bytes(), DOWN_MODE)
    return path


def _dropin_bytes() -> bytes:
    return JOURNALD_DROPIN_TEXT.encode("utf-8")


def mark_down(paths: OwnedPaths, name: RealmName) -> None:
    """Create the realm's `down` file, so runit does not start it at its next runsv start (DR-012).

    A running runsv keeps its own state, so the file alone does not stop a restart (runsv(8)).
    ops/service.py also sends runsv the `down` control byte for that.
    """
    path = service_dir(paths, name) / DOWN
    write_file_atomic(path, b"", DOWN_MODE)


def remove_service(paths: OwnedPaths, name: RealmName) -> None:
    """Remove the realm's service directory and its per-realm files. Absent is a no-op.

    The directory is renamed away first, so runsvdir stops seeing it at once, and then removed.
    The caller stops the realm first (ops/delete.py). A runsv that still runs its directory
    exits when runsvdir notices the directory is gone.
    """
    sv = paths.sv
    target = service_dir(paths, name)
    gone = sv / f".{name.value}{_GONE_SUFFIX}"
    if target.is_symlink() or target.exists():
        _remove_tree(gone)
        os.rename(target, gone)
        _remove_tree(gone)
    _remove_tree(sv / f".{name.value}{_TEMP_SUFFIX}")
    if sv.is_dir():
        fsync_dir(sv)
    for path in (conmon_pidfile(paths, name), crash_log(paths, name)):
        path.unlink(missing_ok=True)


def _mkdir_private(directory: Path) -> None:
    """Make an owned directory with mode 0700. A symbolic link in its place is refused."""
    if directory.is_symlink():
        raise _internal(f"an owned directory is a symbolic link: {directory}")
    directory.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)


def _internal(detail: str) -> GinnError:
    err = make("GINN-E-INTERNAL")
    err.details.update({"detail": detail})
    return err


def _remove_tree(path: Path) -> None:
    """Remove a directory tree, or a file or link, that ginn made. Absent is a no-op."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
