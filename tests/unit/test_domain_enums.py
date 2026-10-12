# SPDX-License-Identifier: Apache-2.0
"""Closed value sets (DR-005, DR-011) and the realm settings rules (DR-012, DR-013)."""

import dataclasses
import itertools

import pytest

from ginn.domain.enums import DistroId, RealmState, Tier
from ginn.domain.names import RealmName
from ginn.domain.realm import RealmSettings
from ginn.errors import GinnError

pytestmark = [
    pytest.mark.fr("DR-005"),
    pytest.mark.fr("DR-011"),
    pytest.mark.fr("DR-012"),
    pytest.mark.fr("DR-013"),
]


def test_distro_values_are_the_four_listed_in_dr005() -> None:
    assert [member.value for member in DistroId] == ["arch", "debian", "fedora", "kali"]


def test_tier_values_are_the_three_listed_in_dr011() -> None:
    assert [member.value for member in Tier] == ["basic", "development", "custom"]


def test_realm_state_values_are_the_eight_listed_in_dr020() -> None:
    assert [member.value for member in RealmState] == [
        "creating",
        "stopped",
        "running",
        "updating",
        "rebuilding",
        "failed",
        "broken",
        "deleting",
    ]


@pytest.mark.parametrize("enum_cls", [DistroId, Tier, RealmState], ids=["distro", "tier", "state"])
def test_every_member_parses_from_its_value(enum_cls: type) -> None:
    for member in enum_cls:
        assert enum_cls.parse(member.value) is member


@pytest.mark.parametrize(
    ("enum_cls", "raw"),
    [
        (DistroId, "gentoo"),
        (DistroId, "ARCH"),
        (DistroId, " arch"),
        (DistroId, "arch "),
        (DistroId, "arch\n"),
        (DistroId, ""),
        (DistroId, "arch;rm"),
        (Tier, "Basic"),
        (Tier, "gold"),
        (Tier, ""),
        (RealmState, "Running"),
        (RealmState, "removed"),
        (RealmState, "ghost"),
    ],
)
def test_unknown_values_raise_option_invalid_exit_2(enum_cls: type, raw: str) -> None:
    with pytest.raises(GinnError) as info:
        enum_cls.parse(raw)  # type: ignore[attr-defined]
    err = info.value
    assert err.code == "GINN-E-OPTION-INVALID"
    assert err.exit_code == 2
    assert isinstance(err.details["option"], str)
    assert isinstance(err.details["rule"], str)


def _settings(distro: DistroId, services: bool, autostart: bool, everything: bool) -> RealmSettings:
    return RealmSettings(
        name=RealmName.parse("work"),
        distro=distro,
        tier=Tier.BASIC,
        services=services,
        autostart=autostart,
        everything=everything,
    )


@pytest.mark.parametrize(
    ("distro", "services", "autostart", "everything"),
    list(itertools.product(list(DistroId), [False, True], [False, True], [False, True])),
)
def test_realm_settings_follow_dr012_and_dr013_for_every_combination(
    distro: DistroId, services: bool, autostart: bool, everything: bool
) -> None:
    # Written out from the rules, not from the code: autostart needs services (DR-012),
    # everything needs kali (DR-013).
    allowed = (not autostart or services) and (not everything or distro is DistroId.KALI)
    if allowed:
        built = _settings(distro, services, autostart, everything)
        assert built.autostart is autostart
    else:
        with pytest.raises(GinnError) as info:
            _settings(distro, services, autostart, everything)
        assert info.value.code == "GINN-E-OPTION-INVALID"
        assert info.value.exit_code == 2


def test_autostart_without_services_names_the_option() -> None:
    with pytest.raises(GinnError) as info:
        _settings(DistroId.ARCH, services=False, autostart=True, everything=False)
    assert info.value.details["option"] == "--autostart without services"
    assert info.value.details["distro"] == "arch"


def test_everything_on_a_non_kali_distro_is_refused() -> None:
    with pytest.raises(GinnError) as info:
        _settings(DistroId.FEDORA, services=True, autostart=False, everything=True)
    assert info.value.details["option"] == "--everything"


def test_everything_is_allowed_on_kali() -> None:
    assert _settings(DistroId.KALI, False, False, True).everything is True


def test_realm_settings_are_frozen() -> None:
    settings = _settings(DistroId.ARCH, False, False, False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.services = True  # type: ignore[misc]


def test_realm_settings_reject_a_raw_string_distro() -> None:
    with pytest.raises(TypeError):
        RealmSettings(
            name=RealmName.parse("work"),
            distro="kali",  # type: ignore[arg-type]
            tier=Tier.BASIC,
            services=False,
            autostart=False,
            everything=True,
        )
