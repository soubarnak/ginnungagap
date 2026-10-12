# SPDX-License-Identifier: Apache-2.0
"""Closed value sets: tiers (DR-011) and realm states (DR-020), and distribution IDs (DR-005).

Each enum parses a raw string with `parse()`. Any other value raises GINN-E-OPTION-INVALID.
Matching is exact and case-sensitive.

Distribution IDs are not a closed set in this module. The valid IDs are the names of the
definitions in `ginn.distros` (NFR-060), so a new distribution needs no change here. This module
imports `ginn.distros`, which must never import `ginn.domain`.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

from ginn.distros import available
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


@dataclass(frozen=True)
class DistroId:
    """A distribution ID (DR-005): the name of a distribution definition in `ginn.distros`.

    Validated on construction against the definitions that exist. The `_test` fixture counts as
    one only while the test switch is on (GINN_TEST_DISTROS=1, see ginn.distros.loader), so tests
    can run through this path and production never accepts it (NFR-060). An unknown name raises
    GINN-E-OPTION-INVALID and the message lists the names users may choose.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError(f"distro ID must be a str, not {type(self.value).__name__}")
        if self.value not in available(include_test=True):
            allowed = ", ".join(available())
            raise reject(
                OPTION_CODE,
                {"rule": f"allowed values: {allowed} (DR-005)"},
                option=f"--distro={self.value}",
                distro="any",
            )

    @classmethod
    def parse(cls, raw: str) -> Self:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


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
