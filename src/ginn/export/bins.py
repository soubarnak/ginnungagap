# SPDX-License-Identifier: Apache-2.0
"""`ginn export --bin` and `ginn unexport --bin`: CLI launchers in the ginn bin directory (FR-047).

A launcher is a POSIX sh script in `$XDG_DATA_HOME/ginn/bin` (Owned Paths, DR-010):

    #!/bin/sh
    # ginn-launcher: realm=<realm> binary=<binary>
    # Written by ginn (FR-047, AS-008). Remove it with: ginn unexport --bin <realm> <binary>
    exec '<ginn>' exec '<realm>' -- '<binary>' "$@"

The second line names the owner. ginn writes a file at a launcher path only when the path is
free, or already a launcher of the same realm and binary. It removes a file only when it is that
launcher, so a file that ginn did not write is never overwritten or removed (DR-010). The write is
atomic and the mode is 0755 (`_files.write_file_atomic`).

Nothing is exported automatically, and ginn never edits a shell startup file (AS-008, AS-011). The
success message says when the bin directory is not on PATH; the user adds it.

The binary is not looked up in the realm. Looking would start a stopped realm, and a launcher for
a name the realm lacks fails when it runs, with the exit status of `ginn exec`.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ginn.domain import RealmName
from ginn.errors import make
from ginn.export._files import write_file_atomic
from ginn.ops._mutating import OpEnv, mutating
from ginn.store.paths import OwnedPaths, fsync_dir
from ginn.store.records import RealmStore

LAUNCHER_MODE: Final = 0o755
_BINARY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_RESERVED: Final = re.compile(r"ginn(-|$)")
_RESERVED_NAMES: Final = frozenset({"podman"})
_MARKER: Final = re.compile(r"# ginn-launcher: realm=([a-z][a-z0-9-]{0,31}) binary=(\S+)")
_MAX_LAUNCHER_BYTES: Final = 64 * 1024


@dataclass(frozen=True)
class BinResult:
    """The outcome of one export or unexport of a launcher."""

    realm: str
    binary: str
    path: Path
    changed: bool


def check_binary(binary: str) -> str:
    """Return `binary` when it is a plain file name, else raise GINN-E-BIN-NAME-INVALID.

    A name with a slash, a leading dash, or any other character is refused before a path is built.
    The names `ginn`, `ginn-*` and `podman` are reserved. A launcher named `ginn` would shadow the
    ginn command that every launcher runs, so the launchers would call themselves. A launcher named
    `podman` would stand in for podman on PATH (PodmanCli starts podman by name).
    """
    if _BINARY.fullmatch(binary) is None or _RESERVED.match(binary) or binary in _RESERVED_NAMES:
        raise make("GINN-E-BIN-NAME-INVALID", binary=binary)
    return binary


def shell_quote(text: str) -> str:
    """Quote `text` as one POSIX sh word, with single quotes (no shell reads it unquoted)."""
    return "'" + text.replace("'", "'\\''") + "'"


def launcher_text(program: str, realm: str, binary: str) -> str:
    """The launcher script for `binary` in `realm`. `program` is the ginn command to run."""
    return (
        "#!/bin/sh\n"
        f"# ginn-launcher: realm={realm} binary={binary}\n"
        "# Written by ginn (FR-047, AS-008). Remove it with: "
        f"ginn unexport --bin {realm} {binary}\n"
        f'exec {shell_quote(program)} exec {shell_quote(realm)} -- {shell_quote(binary)} "$@"\n'
    )


def bin_on_path(directory: Path, path_env: str) -> bool:
    """True when `directory` is one of the entries of `path_env` (a PATH value)."""
    wanted = os.path.normpath(str(directory))
    return any(os.path.normpath(entry) == wanted for entry in path_env.split(os.pathsep) if entry)


def _owner(path: Path) -> tuple[str, str] | None:
    """(realm, binary) named by the launcher at `path`, or None when it is not a ginn launcher."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_LAUNCHER_BYTES:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if len(lines) < 2 or lines[0] != "#!/bin/sh":
        return None
    match = _MARKER.fullmatch(lines[1])
    if match is None:
        return None
    return match.group(1), match.group(2)


def remove_realm_launchers(paths: OwnedPaths, name: RealmName) -> list[str]:
    """Remove every launcher in the bin directory that names realm `name`. Return the binaries.

    A file that is not a launcher of this realm is left alone, whatever its name. The caller holds
    the realm lock; a missing bin directory is no launchers.
    """
    if not paths.bin.is_dir() or paths.bin.is_symlink():
        return []
    removed: list[str] = []
    for path in sorted(paths.bin.iterdir()):
        owner = _owner(path)
        if owner is not None and owner == (name.value, path.name):
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


def _require_realm(env: OpEnv, name: RealmName) -> None:
    if not RealmStore(env.paths, env.clock).exists(name):
        raise make("GINN-E-NO-SUCH-REALM", realm=name.value)


def export_bin(env: OpEnv, name: RealmName, binary: str, *, program: str) -> BinResult:
    """Write the launcher of `binary` for realm `name` (FR-047). Mutating operation `export`.

    A launcher that is already there for the same realm and binary is rewritten only when its text
    differs. A path owned by another launcher, or by a file ginn did not write, is refused with
    GINN-E-BIN-EXISTS and nothing changes.
    """
    check_binary(binary)
    _require_realm(env, name)
    with mutating(env, "export", name, {"binary": binary}) as mutation:
        env.paths.ensure()
        path = env.paths.bin / binary
        text = launcher_text(program, name.value, binary)
        owner = _owner(path)
        if owner is None and os.path.lexists(path):
            raise make("GINN-E-BIN-EXISTS", path=str(path), realm=name.value)
        if owner is not None and owner != (name.value, binary):
            raise make("GINN-E-BIN-EXISTS", path=str(path), realm=name.value)
        if owner is not None and path.read_text(encoding="utf-8") == text:
            mutation.step("noop")
            return BinResult(name.value, binary, path, changed=False)
        write_file_atomic(path, text.encode("utf-8"), LAUNCHER_MODE)
        mutation.step("written")
    return BinResult(name.value, binary, path, changed=True)


def unexport_bin(env: OpEnv, name: RealmName, binary: str) -> BinResult:
    """Remove the launcher of `binary` for realm `name` (FR-047). Mutating operation `unexport`.

    Only a launcher that names this realm and binary is removed. Any other path, and a missing
    one, is refused with GINN-E-BIN-NOT-EXPORTED. The realm record is not read: a launcher of a
    realm that no longer exists can still be removed.
    """
    check_binary(binary)
    with mutating(env, "unexport", name, {"binary": binary}) as mutation:
        path = env.paths.bin / binary
        if _owner(path) != (name.value, binary):
            raise make("GINN-E-BIN-NOT-EXPORTED", binary=binary, realm=name.value)
        path.unlink()
        fsync_dir(path.parent)
        mutation.step("removed")
    return BinResult(name.value, binary, path, changed=True)
