# SPDX-License-Identifier: Apache-2.0
"""Create-realm wizard viewmodel (FR-101, IR-002). Pure Python, no GTK.

The wizard checks each input as it is typed, with the domain validators (DR-002 for names,
DR-003 for packages, DR-005 for distributions). It builds the argv of `ginn --json create` only
when every check passes. Its defaults follow FR-010: the name is the distribution ID until the
user types a name, the tier is Basic and services are off (AS-013). A services-on create with
packages or the everything set is refused here, before any argv is built (ADR-003).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from ginn.domain import DistroId, PackageName, RealmName, Tier
from ginn.errors import GinnError
from ginn.gui.viewmodels.labels import Failure, failure_of_error

KALI: Final = "kali"
"""The only distribution that offers the "everything" option (FR-014, FR-101 AC-2)."""

SERVICES_CONFLICT_TEXT: Final = (
    "A services-on realm is created stopped, and ginn does not start it from a create, so its "
    "packages and the Kali everything set need services off. Turn services off to include them, "
    "or add packages later with 'ginn install' (ADR-003)."
)
"""Why a services-on create cannot carry packages or the everything set (FR-101 AC-1)."""


class CreateWizard:
    """Holds the choices of one create-realm run and turns them into a `ginn --json create`."""

    def __init__(self, distros: Sequence[str]) -> None:
        self._distros = tuple(distros)
        self._distro: str | None = None
        self._name = ""
        self._name_edited = False
        self._tier = Tier.BASIC
        self._services = False
        self._packages: tuple[str, ...] = ()
        self._everything = False

    @property
    def distros(self) -> tuple[str, ...]:
        return self._distros

    @property
    def distro(self) -> str | None:
        return self._distro

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> Tier:
        return self._tier

    @property
    def services(self) -> bool:
        return self._services

    @property
    def packages(self) -> tuple[str, ...]:
        return self._packages

    @property
    def everything(self) -> bool:
        return self._everything

    @property
    def show_everything(self) -> bool:
        """The "everything" option is shown only for Kali (FR-101 AC-2)."""
        return self._distro == KALI

    def choose_distro(self, distro: str) -> tuple[str, ...]:
        """Select a distribution from the offered list. Return the packages it dropped.

        The name follows the distribution until the user types one. A package that does not match
        the new distribution's pattern is dropped, and the returned tuple names it.
        """
        if distro not in self._distros:
            raise ValueError(f"distribution {distro!r} is not offered")
        self._distro = distro
        if not self._name_edited:
            self._name = distro
        if not self.show_everything:
            self._everything = False
        kept: list[str] = []
        dropped: list[str] = []
        for package in self._packages:
            if _package_failure(package, distro) is None:
                kept.append(package)
            else:
                dropped.append(package)
        self._packages = tuple(kept)
        return tuple(dropped)

    def set_name(self, raw: str) -> None:
        self._name = raw
        self._name_edited = True

    def name_failure(self) -> Failure | None:
        """The DR-002 error for the typed name, or None when the name is valid (FR-101 AC-1)."""
        try:
            RealmName.parse(self._name)
        except GinnError as err:
            return failure_of_error(err)
        return None

    def set_tier(self, tier: Tier) -> None:
        self._tier = tier

    def set_services(self, on: bool) -> None:
        self._services = on

    def set_everything(self, on: bool) -> None:
        """Turn on the "everything" option. Off for every distribution but Kali."""
        self._everything = on and self.show_everything

    def add_package(self, raw: str) -> Failure | None:
        """Add a package after the DR-003 check. An invalid name is not added."""
        if self._distro is None:
            raise ValueError("choose a distribution before adding packages")
        failure = _package_failure(raw, self._distro)
        if failure is None and raw not in self._packages:
            self._packages = (*self._packages, raw)
        return failure

    def remove_package(self, raw: str) -> None:
        self._packages = tuple(package for package in self._packages if package != raw)

    def services_conflict(self) -> str | None:
        """The reason the services-on create cannot run, or None when it can (ADR-003).

        A services-on create takes no packages and no everything set: the CLI cannot start the
        container, so the wizard refuses the combination instead of sending it (FR-101 AC-1).
        """
        if self._services and (self._packages or self._everything):
            return SERVICES_CONFLICT_TEXT
        return None

    def can_create(self) -> bool:
        """True when a distribution is chosen, the name is valid and no option is out of place."""
        return (
            self._distro is not None
            and self.name_failure() is None
            and (not self._everything or self.show_everything)
            and self.services_conflict() is None
        )

    def create_args(self) -> tuple[str, ...]:
        """The argv of `ginn --json create`, for example `--json create arch --name arch ...`.

        Raises ValueError when `can_create` is false. The Create button stays disabled then.
        """
        if self._distro is None or not self.can_create():
            raise ValueError("the wizard is not complete")
        args: list[str] = [
            "--json",
            "create",
            self._distro,
            "--name",
            self._name,
            "--tier",
            self._tier.value,
            "--services",
            "on" if self._services else "off",
        ]
        if self._packages:
            args.extend(["--with", *self._packages])
        if self._everything:
            # The wizard's confirmation has no size estimate, because the CLI computes it inside
            # create. --yes skips the CLI's prompt, which would have shown that size (FR-014 AC-1).
            args.extend(["--everything", "--yes"])
        return tuple(args)


def _package_failure(raw: str, distro: str) -> Failure | None:
    try:
        PackageName.parse(raw, DistroId.parse(distro))
    except GinnError as err:
        return failure_of_error(err)
    return None
