# SPDX-License-Identifier: Apache-2.0
"""Owned Paths (DR-010, DR-021, FR-005): every location ginn may write after Host Setup.

Roots, from the environment (XDG Base Directory spec; empty or relative values count as unset):
- data    = $XDG_DATA_HOME/ginn      (default ~/.local/share/ginn)
- config  = $XDG_CONFIG_HOME/ginn    (default ~/.config/ginn)
- state   = $XDG_STATE_HOME/ginn     (default ~/.local/state/ginn)
- runtime = /run/ginn/<uid>, the directory the supervisor run script creates (ADR-004). Every
  ginn process of the user resolves the same runtime root, whatever the session: the locks, the
  Podman runroot and the Podman tmpdir all live under it (DR-033, FR-123).
  GINN_RUNTIME_DIR, when set, must be absolute and replaces the default. Only tests and the
  boot-time supervisor use it, and the supervisor exports the same path.

$XDG_RUNTIME_DIR is never read here. Under elogind it exists only while a login session lives, so
a session ginn and the boot-time supervisor would disagree on the runtime root (ADR-004).

The runtime directory, the default one or the override, is checked on every `from_env`:
- missing: GINN-E-HOST-NOT-SET-UP (77). It never falls back to a directory on disk: Podman needs
  its runroot and tmpdir on tmpfs to detect a reboot.
- a symbolic link, a non-directory, not owned by the user, or not mode 0700: the unsafe-runtime
  code (77, GINN-E-RUNTIME-DIR-UNSAFE). It is refused, never repaired, because the owner check
  exists to catch a directory that someone else prepared.

`applications_dir` ($XDG_DATA_HOME/applications) is shared with the host and is NOT owned as a
whole. Only `ginn-*.desktop` files in it are owned (`desktop_entry_glob`). It is therefore left out
of `all_owned_roots()`. DR-004 forbids it through its own AS-011 rule.

`ensure()` creates every owned directory with mode 0700 (DR-021, NFR-SEC-009). It tightens
existing owned directories that grant group or other access and reports each one through a
callback. It prints nothing.

`ensure_config()` writes ginn's own containers.conf and storage.conf for Podman (ADR-004). Podman
fails when a file named by CONTAINERS_CONF or CONTAINERS_STORAGE_CONF is missing, so these are
written before any Podman call. Writes are atomic and mode 0600 (DR-035).
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ginn.errors import make

OWNED_DIR_MODE = 0o700
OWNED_FILE_MODE = 0o600
_GROUP_OTHER_BITS = 0o077
_APP_NAME = "ginn"
_CONTAINERS_CONF = """\
# Written by ginn (ADR-004, DR-010). ginn rewrites this file; do not edit it.
# Written from containers.conf(5). Podman's --runtime flag selects the OCI runtime per call.
[engine]
runtime = "crun"
events_logger = "file"
"""


def fsync_dir(directory: Path) -> None:
    """Flush a directory entry change (rename, create, unlink) to disk."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path`: temporary file, fsync, rename, directory fsync (DR-035).

    The temporary file is `path.with_suffix(".tmp")` in the same directory, created with mode
    0600. If the write or the rename fails, the temporary file is removed and the error is raised.
    The old file at `path` is then unchanged.
    """
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, OWNED_FILE_MODE)
    try:
        try:
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


def _xdg_base(env: Mapping[str, str], var: str, default: Path) -> Path:
    """Return the XDG base directory from `var`, or `default` when unset, empty or relative."""
    raw = env.get(var, "")
    if raw and os.path.isabs(raw):
        return Path(raw)
    return default


def _runtime_dir(env: Mapping[str, str], uid: int, system_run: Path) -> Path:
    """Return the one runtime directory: the GINN_RUNTIME_DIR override, else /run/ginn/<uid>."""
    override = env.get("GINN_RUNTIME_DIR", "")
    if override:
        if not os.path.isabs(override):
            raise make("GINN-E-USAGE", detail="GINN_RUNTIME_DIR must be an absolute path")
        runtime = Path(override)
    else:
        runtime = system_run / _APP_NAME / str(uid)
    _check_runtime_dir(runtime, uid)
    return runtime


def _check_runtime_dir(runtime: Path, uid: int) -> None:
    """Refuse a runtime directory that is missing or is not a 0700 directory owned by `uid`.

    lstat, not stat: a symbolic link in place of the directory is refused, as `_ensure_dir` does.
    """
    try:
        info = os.lstat(runtime)
    except FileNotFoundError:
        raise make("GINN-E-HOST-NOT-SET-UP", path=str(runtime)) from None
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        found = "a symbolic link"
    elif not stat.S_ISDIR(info.st_mode):
        found = "a file that is not a directory"
    elif info.st_uid != uid or mode != OWNED_DIR_MODE:
        found = f"owner uid {info.st_uid}, mode {mode:03o}"
    else:
        return
    raise make("GINN-E-RUNTIME-DIR-UNSAFE", path=str(runtime), uid=str(uid), found=found)


def _toml_string(path: Path) -> str:
    """A TOML basic string for `path`. JSON string escapes are valid TOML basic-string escapes."""
    return json.dumps(str(path))


def _storage_conf_text(root: Path, runroot: Path) -> str:
    """containers-storage.conf(5) for ginn's Podman. The paths match the --root and --runroot flags.

    The flags win over the file, so the two are kept equal on purpose.
    """
    return (
        "# Written by ginn (ADR-004, DR-010). ginn rewrites this file; do not edit it.\n"
        "# Written from containers-storage.conf(5).\n"
        "[storage]\n"
        'driver = "overlay"\n'
        f"runroot = {_toml_string(runroot)}\n"
        f"graphroot = {_toml_string(root)}\n"
    )


def _write_owned_file(path: Path, data: bytes) -> None:
    """Write `path` only when its bytes or mode differ. An unchanged file is not rewritten."""
    if path.is_symlink():
        # The write would follow the link and change a file ginn does not own.
        raise OSError(f"owned file {path} is a symbolic link; refusing to use it")
    if (
        path.is_file()
        and path.read_bytes() == data
        and stat.S_IMODE(path.stat().st_mode) == OWNED_FILE_MODE
    ):
        return
    write_bytes_atomic(path, data)


@dataclass(frozen=True)
class OwnedPaths:
    """The host locations ginn owns. Build it with `from_env`; the fields are the four roots."""

    data: Path
    config: Path
    state: Path
    runtime: Path
    applications_dir: Path

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        uid: int | None = None,
        system_run: Path = Path("/run"),
    ) -> OwnedPaths:
        """Compute the owned paths from `env` (default: os.environ) and the user id.

        `system_run` is the root that holds the runtime directory `ginn/<uid>`. Tests pass a
        temporary directory. Raises GINN-E-HOST-NOT-SET-UP when the runtime directory is missing,
        and GINN-E-RUNTIME-DIR-UNSAFE when it is not a 0700 directory owned by `uid`. Both checks
        apply to the GINN_RUNTIME_DIR override too.
        """
        environ: Mapping[str, str] = os.environ if env is None else env
        home_raw = environ.get("HOME", "")
        home = Path(home_raw) if home_raw and os.path.isabs(home_raw) else Path.home()
        real_uid = os.getuid() if uid is None else uid
        data_base = _xdg_base(environ, "XDG_DATA_HOME", home / ".local" / "share")
        config_base = _xdg_base(environ, "XDG_CONFIG_HOME", home / ".config")
        state_base = _xdg_base(environ, "XDG_STATE_HOME", home / ".local" / "state")
        state = state_base / _APP_NAME
        return cls(
            data=data_base / _APP_NAME,
            config=config_base / _APP_NAME,
            state=state,
            runtime=_runtime_dir(environ, real_uid, system_run),
            applications_dir=data_base / "applications",
        )

    # Derived locations. Each is inside one of the four roots.

    @property
    def podman_root(self) -> Path:
        return self.data / "podman"

    @property
    def podman_runroot(self) -> Path:
        return self.runtime / "podman"

    @property
    def podman_tmpdir(self) -> Path:
        """Podman's --tmpdir: ginn's own, never the user's $XDG_RUNTIME_DIR/libpod/tmp."""
        return self.runtime / "podman-tmp"

    @property
    def image_tmp(self) -> Path:
        """Podman's TMPDIR: image pulls and layer unpacking write here (CR-006).

        It is on disk under the data root, not on tmpfs, because images are large. TMPDIR is not
        `--tmpdir`: Podman uses it for downloads, which `--tmpdir` does not cover.
        """
        return self.data / "image-tmp"

    @property
    def podman_containers_conf(self) -> Path:
        """The file CONTAINERS_CONF points to. ensure_config() writes it."""
        return self.config / "containers.conf"

    @property
    def podman_storage_conf(self) -> Path:
        """The file CONTAINERS_STORAGE_CONF points to. ensure_config() writes it."""
        return self.config / "storage.conf"

    @property
    def realms(self) -> Path:
        return self.state / "realms"

    @property
    def homes(self) -> Path:
        return self.data / "homes"

    @property
    def icons(self) -> Path:
        return self.data / "icons"

    @property
    def bin(self) -> Path:
        return self.data / "bin"

    @property
    def cdi(self) -> Path:
        return self.data / "cdi"

    @property
    def sv(self) -> Path:
        return self.state / "sv"

    @property
    def audit_log(self) -> Path:
        return self.state / "audit.log"

    @property
    def journal(self) -> Path:
        return self.state / "journal"

    @property
    def locks(self) -> Path:
        return self.runtime / "locks"

    @property
    def desktop_entry_glob(self) -> str:
        """Glob, relative to `applications_dir`, that matches the desktop entries ginn owns."""
        return "ginn-*.desktop"

    def owned_directories(self) -> list[Path]:
        """Every owned directory, parents before children, without duplicates."""
        ordered = [
            self.data,
            self.config,
            self.state,
            self.runtime,
            self.podman_root,
            self.podman_runroot,
            self.podman_tmpdir,
            self.image_tmp,
            self.realms,
            self.homes,
            self.icons,
            self.bin,
            self.cdi,
            self.sv,
            self.journal,
            self.locks,
        ]
        seen: set[Path] = set()
        unique: list[Path] = []
        for path in ordered:
            if path not in seen:
                seen.add(path)
                unique.append(path)
        return unique

    def all_owned_roots(self) -> list[Path]:
        """The top-level owned roots. DR-004 and FR-005 check paths against this list.

        Anything at or under one of these roots is an Owned Path. The applications directory is
        excluded (see the module docstring).
        """
        return [self.data, self.config, self.state, self.runtime]

    def owned_desktop_entries(self) -> list[Path]:
        """The `ginn-*.desktop` files currently in `applications_dir`, sorted by name."""
        if not self.applications_dir.is_dir():
            return []
        return sorted(self.applications_dir.glob(self.desktop_entry_glob))

    def ensure(self, on_tighten: Callable[[str], None] | None = None) -> None:
        """Create every owned directory with mode 0700 and tighten any that are more open.

        `on_tighten` gets one line per directory whose group or other bits were removed. Missing
        non-owned parents (for example ~/.local/share) are created with the default mode.
        """
        for directory in self.owned_directories():
            _ensure_dir(directory, on_tighten)

    def ensure_config(self) -> None:
        """Write ginn's containers.conf and storage.conf, mode 0600, atomically (ADR-004).

        containers.conf holds `[engine] runtime = "crun"` and `events_logger = "file"`. storage.conf
        holds `driver = "overlay"`, with runroot and graphroot equal to the --runroot and --root
        flags. The text follows containers.conf(5) and containers-storage.conf(5). A file is
        rewritten only when its bytes or mode differ, so repeated calls change nothing.
        """
        _ensure_dir(self.config, None)
        _write_owned_file(self.podman_containers_conf, _CONTAINERS_CONF.encode("utf-8"))
        storage = _storage_conf_text(self.podman_root, self.podman_runroot)
        _write_owned_file(self.podman_storage_conf, storage.encode("utf-8"))


def _ensure_dir(directory: Path, on_tighten: Callable[[str], None] | None) -> None:
    if directory.is_symlink():
        # chmod would follow the link and change a directory ginn does not own.
        raise OSError(f"owned path {directory} is a symbolic link; refusing to use it")
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=OWNED_DIR_MODE)
    # New directories already have mode 0700. Existing ones may be wider; a chmod fixes them.
    current = directory.stat().st_mode & 0o777
    if current == OWNED_DIR_MODE:
        return
    os.chmod(directory, OWNED_DIR_MODE)
    if current & _GROUP_OTHER_BITS and on_tighten is not None:
        on_tighten(f"tightened {directory} from {current:03o} to {OWNED_DIR_MODE:03o} (DR-021)")
