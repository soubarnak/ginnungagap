# SPDX-License-Identifier: Apache-2.0
"""Realm settings that cannot describe an impossible realm (DR-001, DR-012, DR-013).

`RealmSettings.__post_init__` enforces the two cross-field rules:
- DR-012: autostart may be on only when services mode is on.
- DR-013: the "everything" flag may be on only for Kali.
Field types are checked too, so a raw string cannot stand in for a `DistroId` and slip past the
Kali check.
"""

from dataclasses import dataclass

from ginn.domain._errors import reject
from ginn.domain.enums import OPTION_CODE, DistroId, Tier
from ginn.domain.names import RealmName


@dataclass(frozen=True)
class RealmSettings:
    """The user-chosen settings of one realm. Immutable and valid once constructed."""

    name: RealmName
    distro: DistroId
    tier: Tier
    services: bool
    autostart: bool
    everything: bool

    def __post_init__(self) -> None:
        if not isinstance(self.name, RealmName):
            raise TypeError("name must be a RealmName")
        if not isinstance(self.distro, DistroId):
            raise TypeError("distro must be a DistroId")
        if not isinstance(self.tier, Tier):
            raise TypeError("tier must be a Tier")
        for flag in (self.services, self.autostart, self.everything):
            if not isinstance(flag, bool):
                raise TypeError("services, autostart and everything must be bool")
        if self.autostart and not self.services:
            raise reject(
                OPTION_CODE,
                {"rule": "autostart needs services on (DR-012)"},
                option="--autostart without services",
                distro=self.distro.value,
            )
        if self.everything and self.distro is not DistroId.KALI:
            raise reject(
                OPTION_CODE,
                {"rule": "everything is for kali only (DR-013)"},
                option="--everything",
                distro=self.distro.value,
            )
