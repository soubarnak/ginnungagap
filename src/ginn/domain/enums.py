# SPDX-License-Identifier: Apache-2.0
"""Closed value sets: distributions (DR-005), tiers (DR-011) and realm states (DR-020).

Each enum parses a raw string with `parse()`. Any other value raises GINN-E-OPTION-INVALID.
Matching is exact and case-sensitive.
"""

from enum import StrEnum
from typing import Final, Self

from ginn.domain._errors import reject

OPTION_CODE: Final = "GINN-E-OPTION-INVALID"


def _parse_enum[E: StrEnum](cls: type[E], raw: str, option: str) -> E:
    """Return the member whose value equals `raw`, or raise GINN-E-OPTION-INVALID."""
    try:
        return cls(raw)
    except ValueError:
        allowed = ", ".join(member.value for member in cls)
        raise reject(
            OPTION_CODE,
            {"rule": f"allowed values: {allowed}"},
            option=f"{option}={raw}",
            distro="any",
        ) from None


class DistroId(StrEnum):
    """Supported distributions (DR-005)."""

    ARCH = "arch"
    DEBIAN = "debian"
    FEDORA = "fedora"
    KALI = "kali"

    @classmethod
    def parse(cls, raw: str) -> Self:
        return _parse_enum(cls, raw, "--distro")


class Tier(StrEnum):
    """Permission tiers (DR-011)."""

    BASIC = "basic"
    DEVELOPMENT = "development"
    CUSTOM = "custom"

    @classmethod
    def parse(cls, raw: str) -> Self:
        return _parse_enum(cls, raw, "--tier")


class RealmState(StrEnum):
    """Realm states (DR-020). Transitions between them are in `states.py`."""

    CREATING = "creating"
    STOPPED = "stopped"
    RUNNING = "running"
    UPDATING = "updating"
    REBUILDING = "rebuilding"
    FAILED = "failed"
    BROKEN = "broken"
    DELETING = "deleting"

    @classmethod
    def parse(cls, raw: str) -> Self:
        return _parse_enum(cls, raw, "--state")


__all__ = ["OPTION_CODE", "DistroId", "RealmState", "Tier"]
