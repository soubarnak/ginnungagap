# SPDX-License-Identifier: Apache-2.0
"""Distribution IDs (DR-005), closed value sets (DR-011, DR-020) and the realm settings rules
(DR-012, DR-013)."""

import dataclasses
import itertools

import pytest

from ginn.distros import TEST_SWITCH_ENV, available
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

# Every name the data directory offers, the `_test` fixture included (NFR-060).
DISTROS = [DistroId(name) for name in available(include_test=True)]
ARCH = DistroId("arch")
FEDORA = DistroId("fedora")
KALI = DistroId("kali")


def test_shipped_distribution_ids_are_the_four_listed_in_dr005() -> None:
    assert available() == ("arch", "debian", "fedora", "kali")


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


@pytest.mark.parametrize("enum_cls", [Tier, RealmState], ids=["tier", "state"])
def test_every_member_parses_from_its_value(enum_cls: type) -> None:
    for member in enum_cls:
        assert enum_cls.parse(member.value) is member


@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("distro", DISTROS, ids=lambda distro: distro.value)
def test_every_definition_name_is_a_distro_id(distro: DistroId) -> None:
    assert DistroId.parse(distro.value) == distro
    assert str(distro) == distro.value


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
        (DistroId, "../arch"),
        (DistroId, "arch.toml"),
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


@pytest.mark.fr("DR-005")
def test_unknown_distro_message_lists_the_shipped_names_only() -> None:
    with pytest.raises(GinnError) as info:
        DistroId.parse("gentoo")
    assert info.value.details["rule"] == f"allowed values: {', '.join(available())} (DR-005)"
    assert info.value.details["option"] == "--distro=gentoo"


@pytest.mark.fr("NFR-060")
def test_the_fixture_is_not_a_distro_id_without_the_test_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TEST_SWITCH_ENV)
    with pytest.raises(GinnError) as info:
        DistroId.parse("_test")
    assert info.value.code == "GINN-E-OPTION-INVALID"
    assert info.value.details["option"] == "--distro=_test"
    assert "_test" not in info.value.details["rule"]


@pytest.mark.fr("DR-005")
def test_distro_id_must_be_a_str() -> None:
    with pytest.raises(TypeError):
        DistroId(1)  # type: ignore[arg-type]


@pytest.mark.fr("DR-005")
def test_distro_id_is_a_frozen_value_compared_by_name() -> None:
    assert DistroId("arch") == ARCH
    assert hash(DistroId("arch")) == hash(ARCH)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ARCH.value = "debian"  # type: ignore[misc]


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
    list(itertools.product(DISTROS, [False, True], [False, True], [False, True])),
)
def test_realm_settings_follow_dr012_and_dr013_for_every_combination(
    distro: DistroId, services: bool, autostart: bool, everything: bool
) -> None:
    # Written out from the rules, not from the code: autostart needs services (DR-012),
    # everything needs kali (DR-013).
    allowed = (not autostart or services) and (not everything or distro.value == "kali")
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
        _settings(ARCH, services=False, autostart=True, everything=False)
    assert info.value.details["option"] == "--autostart without services"
    assert info.value.details["distro"] == "arch"


def test_everything_on_a_non_kali_distro_is_refused() -> None:
    with pytest.raises(GinnError) as info:
        _settings(FEDORA, services=True, autostart=False, everything=True)
    assert info.value.details["option"] == "--everything"


def test_everything_is_allowed_on_kali() -> None:
    assert _settings(KALI, False, False, True).everything is True


def test_realm_settings_are_frozen() -> None:
    settings = _settings(ARCH, False, False, False)
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
