# SPDX-License-Identifier: Apache-2.0
"""Owned Paths (DR-010, DR-021, FR-005): every location ginn may write after Host Setup.

Roots, from the environment (XDG Base Directory spec; empty or relative values count as unset):
- data    = $XDG_DATA_HOME/ginn      (default ~/.local/share/ginn)
- config  = $XDG_CONFIG_HOME/ginn    (default ~/.config/ginn)
- state   = $XDG_STATE_HOME/ginn     (default ~/.local/state/ginn)
- runtime, first candidate that applies:
    1. GINN_RUNTIME_DIR (must be absolute; a configurable override, see below)
    2. $XDG_RUNTIME_DIR/ginn
    3. /run/ginn/<uid>, only if that directory exists
    4. state/run (not tmpfs; used only when no runtime directory is available)

Runtime directory decision: DR-010 names $XDG_RUNTIME_DIR/ginn. S-04 (the cgroup and runtime
spike) has not run, so the choice is open and the root is configurable. Its final location is
for ADR-004 to decide. Until then GINN_RUNTIME_DIR overrides every other candidate.

`applications_dir` ($XDG_DATA_HOME/applications) is shared with the host and is NOT owned as a
whole. Only `ginn-*.desktop` files in it are owned (`desktop_entry_glob`). It is therefore left out
of `all_owned_roots()`. DR-004 forbids it through its own AS-011 rule.

`ensure()` creates every owned directory with mode 0700 (DR-021, NFR-SEC-009). It tightens
existing owned directories that grant group or other access and reports each one through a
callback. It prints nothing.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ginn.errors import make

OWNED_DIR_MODE = 0o700
_GROUP_OTHER_BITS = 0o077
_APP_NAME = "ginn"


def _xdg_base(env: Mapping[str, str], var: str, default: Path) -> Path:
    """Return the XDG base directory from `var`, or `default` when unset, empty or relative."""
    raw = env.get(var, "")
    if raw and os.path.isabs(raw):
        return Path(raw)
    return default


def _runtime_dir(env: Mapping[str, str], uid: int, state: Path, system_run: Path) -> Path:
    """Choose the runtime directory per the order in the module docstring."""
    override = env.get("GINN_RUNTIME_DIR", "")
    if override:
        if not os.path.isabs(override):
            raise make("GINN-E-USAGE", detail="GINN_RUNTIME_DIR must be an absolute path")
        return Path(override)
    xdg = env.get("XDG_RUNTIME_DIR", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / _APP_NAME
    system = system_run / _APP_NAME / str(uid)
    if system.is_dir():
        return system
    return state / "run"


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

        `system_run` is the root that holds the Host Setup runtime directory `ginn/<uid>`. Tests
        pass a temporary directory.
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
            runtime=_runtime_dir(environ, real_uid, state, system_run),
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
