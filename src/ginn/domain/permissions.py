# SPDX-License-Identifier: Apache-2.0
"""Permission toggles and Custom shares of a realm (FR-060, FR-063, DR-004, DR-011).

`Toggle` names each permission from FR-061 and FR-062 that a Custom realm can switch on or off
(FR-063). A Basic or Development realm has its toggles fixed by its tier, so only a Custom realm
keeps its own set, in `Permissions`. The same record also keeps the Custom shares, each one a
host path that DR-004 accepted, with its mode.

`initial_permissions` is what a new realm starts with. A tier change to Custom keeps the toggles of
the tier it came from (ops/permissions.py). These are data types only. The rules that map a toggle
to podman options are in security/caps.py, and to attach grants in runtime/mounts.py.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Self

from ginn.domain.enums import Tier
from ginn.domain.paths import Mode


class Toggle(StrEnum):
    """One Custom permission (FR-063). The value is the name `ginn perms` takes."""

    DOWNLOADS = "downloads"
    """FR-061: `~/Downloads`, read-write."""
    MOUNT_ROOTS = "mount_roots"
    """FR-061: the mount roots `/run/media/<user>`, `/media` and `/mnt`, read-write."""
    DISPLAY = "display"
    """FR-061: the display sockets, Wayland and X11, with `DISPLAY` and `XAUTHORITY`.
    Attach-time."""
    AUDIO = "audio"
    """FR-061: the PipeWire and PulseAudio sockets. Attach-time."""
    GPU = "gpu"
    """FR-061, FR-069: the GPU device nodes `/dev/dri`, and the NVIDIA CDI grant when the host has
    the driver. Create-time."""
    FONTS_TIMEZONE = "fonts_timezone"
    """FR-061, AS-009: the host fonts and the host timezone, read-only, with `TZ`. Create-time."""
    PROJECTS = "projects"
    """FR-062: `~/Projects`, read-write."""
    SHELL_RC = "shell_rc"
    """FR-062: `~/.bashrc` and `~/.zshrc`, read-only (AS-010)."""
    AGENT = "agent"
    """FR-062: SSH and GPG agent access through the Agent Proxy (FR-067)."""
    NESTED = "nested"
    """FR-062: nested rootless containers (AC-3). Adds the options of ADR-006."""
    KVM = "kvm"
    """FR-062: `/dev/kvm` (AC-4), when the host has it (IR-040)."""


BASIC_TOGGLES: Final[frozenset[Toggle]] = frozenset(
    {
        Toggle.DOWNLOADS,
        Toggle.MOUNT_ROOTS,
        Toggle.DISPLAY,
        Toggle.AUDIO,
        Toggle.GPU,
        Toggle.FONTS_TIMEZONE,
    }
)
"""What Basic grants (FR-061). A Custom realm starts from these, and a fresh one has no others."""
ALL_TOGGLES: Final[frozenset[Toggle]] = frozenset(Toggle)
"""Development grants every toggle: Basic plus the FR-062 set."""
TOGGLE_FORMAT: Final = 2
"""The stored format of a Custom part (`Permissions.to_dict`). Format 1 has no `format` key."""
_UNTOGGLED: Final[frozenset[Toggle]] = frozenset(
    {Toggle.DISPLAY, Toggle.AUDIO, Toggle.GPU, Toggle.FONTS_TIMEZONE}
)
"""The grants that format 1 attached for every realm, before they were toggles. A format 1 Custom
realm keeps them, so an upgrade changes no realm's grants."""


def effective_toggles(tier: Tier, custom: frozenset[Toggle]) -> frozenset[Toggle]:
    """The toggles that apply to a realm of `tier`. Only a Custom realm reads `custom`."""
    if tier is Tier.BASIC:
        return BASIC_TOGGLES
    if tier is Tier.DEVELOPMENT:
        return ALL_TOGGLES
    return custom


@dataclass(frozen=True)
class CustomShare:
    """One Custom shared path (FR-063). `path` is absolute and resolved, as DR-004 checked it."""

    path: str
    mode: Mode

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            raise ValueError(f"a shared path must be absolute, not {self.path!r}")
        if self.mode not in ("ro", "rw"):
            raise ValueError(f"mode must be 'ro' or 'rw', not {self.mode!r}")

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "mode": self.mode}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        path, mode = data.get("path"), data.get("mode")
        if not (isinstance(path, str) and isinstance(mode, str)):
            raise ValueError(f"not a custom share: {dict(data)!r}")
        if mode not in ("ro", "rw"):
            raise ValueError(f"mode must be 'ro' or 'rw', not {mode!r}")
        return cls(path, "ro" if mode == "ro" else "rw")


@dataclass(frozen=True)
class Permissions:
    """The Custom part of a realm: its toggles and its shares (FR-063).

    An empty value is what a Basic or Development realm holds. The record refuses a Custom part on
    any other tier, so a stored realm never carries shares its tier would not attach.
    """

    toggles: frozenset[Toggle] = frozenset()
    shares: tuple[CustomShare, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "format": TOGGLE_FORMAT,
            "toggles": sorted(toggle.value for toggle in self.toggles),
            "shares": [share.to_dict() for share in self.shares],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object], *, tier: Tier) -> Self:
        """Read a stored Custom part of a realm of `tier`.

        A record without the `format` key is format 1. Its toggle list is authoritative for the
        toggles it had, and a Custom realm also keeps `_UNTOGGLED`. A Basic or Development realm
        stores an empty part, which is not changed. `tier` is needed only for that rule.
        """
        raw_toggles, raw_shares = data.get("toggles"), data.get("shares")
        if not (isinstance(raw_toggles, list) and isinstance(raw_shares, list)):
            raise ValueError(f"not a permissions record: {dict(data)!r}")
        toggles = frozenset(Toggle(str(value)) for value in raw_toggles)
        if "format" not in data:
            if tier is Tier.CUSTOM:
                toggles |= _UNTOGGLED
        elif data["format"] != TOGGLE_FORMAT:
            raise ValueError(f"unknown permissions format {data['format']!r}")
        shares = tuple(CustomShare.from_dict(item) for item in raw_shares)
        return cls(toggles, shares)


def initial_permissions(tier: Tier) -> Permissions:
    """The Custom part of a new realm: Basic's toggles for Custom, and nothing for other tiers."""
    if tier is Tier.CUSTOM:
        return Permissions(toggles=BASIC_TOGGLES)
    return Permissions()


__all__ = [
    "ALL_TOGGLES",
    "BASIC_TOGGLES",
    "CustomShare",
    "Permissions",
    "Toggle",
    "effective_toggles",
    "initial_permissions",
]
