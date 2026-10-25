# SPDX-License-Identifier: Apache-2.0
"""Toggles, Custom shares and the record rules of a realm's permissions (FR-060, FR-063).

Covers domain/permissions.py and the new record fields of store/records.py. The record checks
use RealmRecord directly, so no store or podman is touched.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.domain.permissions import (
    ALL_TOGGLES,
    BASIC_TOGGLES,
    TOGGLE_FORMAT,
    CustomShare,
    Permissions,
    Toggle,
    effective_toggles,
    initial_permissions,
)
from ginn.store.records import RealmRecord

T0 = datetime(2026, 10, 13, tzinfo=UTC)


def _record(tier: Tier, **changes: Any) -> RealmRecord:
    settings = RealmSettings(
        name=RealmName("lab"),
        distro=DistroId("debian"),
        tier=tier,
        services=False,
        autostart=False,
        everything=False,
    )
    values: dict[str, Any] = {
        "settings": settings,
        "state": RealmState.STOPPED,
        "previous_state": None,
        "image_ref": "docker.io/library/debian@sha256:" + "ab" * 32,
        "image_digest": "sha256:" + "ab" * 32,
        "container_id": None,
        "generation": 1,
        "created_at": T0,
        "updated_at": T0,
    }
    values.update(changes)
    return RealmRecord(**values)


@pytest.mark.fr("FR-061")
def test_basic_is_every_fr061_grant_as_a_toggle() -> None:
    assert BASIC_TOGGLES == frozenset(
        {
            Toggle.DOWNLOADS,
            Toggle.MOUNT_ROOTS,
            Toggle.DISPLAY,
            Toggle.AUDIO,
            Toggle.GPU,
            Toggle.FONTS_TIMEZONE,
        }
    )
    assert effective_toggles(Tier.BASIC, frozenset()) == BASIC_TOGGLES


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-062")
def test_the_fr062_toggles_are_outside_basic() -> None:
    assert ALL_TOGGLES - BASIC_TOGGLES == frozenset(
        {Toggle.PROJECTS, Toggle.SHELL_RC, Toggle.AGENT, Toggle.NESTED, Toggle.KVM}
    )
    assert {toggle.value for toggle in Toggle} >= {"display", "audio", "gpu", "fonts_timezone"}


@pytest.mark.fr("FR-062")
def test_development_holds_every_toggle_whatever_its_stored_set() -> None:
    assert effective_toggles(Tier.DEVELOPMENT, frozenset()) == ALL_TOGGLES
    assert ALL_TOGGLES == frozenset(Toggle)


@pytest.mark.fr("FR-063")
def test_custom_reads_only_its_stored_toggles() -> None:
    chosen = frozenset({Toggle.PROJECTS})
    assert effective_toggles(Tier.CUSTOM, chosen) == chosen


@pytest.mark.fr("FR-063")
def test_a_custom_realm_starts_from_the_basic_toggles() -> None:
    assert initial_permissions(Tier.CUSTOM) == Permissions(toggles=BASIC_TOGGLES)
    assert initial_permissions(Tier.BASIC) == Permissions()
    assert initial_permissions(Tier.DEVELOPMENT) == Permissions()


@pytest.mark.fr("FR-063")
def test_a_custom_share_is_absolute_and_has_a_mode() -> None:
    assert CustomShare("/home/alice/Music", "ro").mode == "ro"
    with pytest.raises(ValueError, match="absolute"):
        CustomShare("Music", "rw")
    with pytest.raises(ValueError, match="mode"):
        CustomShare("/home/alice/Music", "wo")  # type: ignore[arg-type]


@pytest.mark.fr("FR-063")
def test_permissions_round_trip_through_their_json_form() -> None:
    permissions = Permissions(
        toggles=frozenset({Toggle.PROJECTS, Toggle.NESTED}),
        shares=(CustomShare("/home/alice/Music", "ro"), CustomShare("/mnt/data", "rw")),
    )
    data = permissions.to_dict()
    assert data["toggles"] == ["nested", "projects"]
    assert data["format"] == TOGGLE_FORMAT
    assert Permissions.from_dict(data, tier=Tier.CUSTOM) == permissions


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_a_switched_off_fr061_grant_stays_off_after_a_round_trip() -> None:
    permissions = Permissions(toggles=BASIC_TOGGLES - {Toggle.DISPLAY, Toggle.GPU})
    restored = Permissions.from_dict(permissions.to_dict(), tier=Tier.CUSTOM)
    assert restored == permissions
    assert Toggle.DISPLAY not in restored.toggles


@pytest.mark.fr("FR-063")
def test_an_unknown_toggle_in_the_stored_form_is_refused() -> None:
    with pytest.raises(ValueError):
        Permissions.from_dict({"toggles": ["telepathy"], "shares": []}, tier=Tier.CUSTOM)


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_a_format_one_custom_part_keeps_the_grants_that_were_always_on() -> None:
    """A part written before the format key holds the toggles it listed, and the FR-061 grants
    that were attached for every realm then. An upgrade changes no grant (capabilities.md)."""
    stored = {"toggles": ["downloads", "mount_roots", "projects"], "shares": []}
    restored = Permissions.from_dict(stored, tier=Tier.CUSTOM)
    assert restored.toggles == frozenset(
        {
            Toggle.DOWNLOADS,
            Toggle.MOUNT_ROOTS,
            Toggle.PROJECTS,
            Toggle.DISPLAY,
            Toggle.AUDIO,
            Toggle.GPU,
            Toggle.FONTS_TIMEZONE,
        }
    )


@pytest.mark.fr("FR-061")
def test_a_format_one_custom_part_keeps_a_grant_that_was_switched_off() -> None:
    """A downloads toggle the user switched off stays off. Only the grants that were not toggles
    are added back."""
    stored = {"toggles": ["mount_roots"], "shares": []}
    restored = Permissions.from_dict(stored, tier=Tier.CUSTOM)
    assert Toggle.DOWNLOADS not in restored.toggles
    assert Toggle.DISPLAY in restored.toggles


@pytest.mark.fr("FR-063")
def test_a_format_one_basic_or_development_part_stays_empty() -> None:
    empty = {"toggles": [], "shares": []}
    assert Permissions.from_dict(empty, tier=Tier.BASIC) == Permissions()
    assert Permissions.from_dict(empty, tier=Tier.DEVELOPMENT) == Permissions()


@pytest.mark.fr("FR-063")
def test_an_unknown_permissions_format_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown permissions format"):
        Permissions.from_dict({"format": 3, "toggles": [], "shares": []}, tier=Tier.CUSTOM)


@pytest.mark.fr("FR-063")
def test_a_custom_realm_round_trips_its_permissions_through_the_record() -> None:
    permissions = Permissions(
        toggles=frozenset({Toggle.KVM}),
        shares=(CustomShare("/home/alice/Music", "ro"),),
    )
    record = _record(Tier.CUSTOM, permissions=permissions, created_options=[])
    restored = RealmRecord.from_json(record.to_json())
    assert restored.permissions == permissions
    assert restored.created_options == []


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_the_create_time_flags_round_trip_and_a_record_without_them_has_the_grants() -> None:
    record = _record(Tier.CUSTOM, gpu_at_create=False, fonts_at_create=False)
    restored = RealmRecord.from_json(record.to_json())
    assert (restored.gpu_at_create, restored.fonts_at_create) == (False, False)
    data = _record(Tier.BASIC).to_json()
    del data["gpu_at_create"]
    del data["fonts_at_create"]
    legacy = RealmRecord.from_json(data)
    assert (legacy.gpu_at_create, legacy.fonts_at_create) == (True, True)


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_create_grants_match_compares_the_gpu_and_fonts_toggles_with_their_flags() -> None:
    record = _record(Tier.CUSTOM, gpu_at_create=True, fonts_at_create=False)
    assert record.create_grants_match(frozenset({Toggle.GPU}))
    assert not record.create_grants_match(frozenset({Toggle.GPU, Toggle.FONTS_TIMEZONE}))
    assert not record.create_grants_match(frozenset())


@pytest.mark.fr("FR-063")
def test_only_a_custom_realm_may_store_toggles_or_shares() -> None:
    with pytest.raises(ValueError, match="only a Custom realm"):
        _record(Tier.BASIC, permissions=Permissions(toggles=frozenset({Toggle.PROJECTS})))
    with pytest.raises(ValueError, match="only a Custom realm"):
        _record(Tier.DEVELOPMENT, permissions=Permissions(shares=(CustomShare("/mnt/x", "rw"),)))


@pytest.mark.fr("FR-066")
def test_a_record_written_before_these_fields_reads_as_basic_with_no_options() -> None:
    data = _record(Tier.BASIC).to_json()
    del data["permissions"]
    del data["created_options"]
    restored = RealmRecord.from_json(data)
    assert restored.permissions == Permissions()
    assert restored.created_options == []


@pytest.mark.fr("FR-066")
def test_created_options_round_trip() -> None:
    options = ["device=/dev/fuse", "security-opt=seccomp"]
    record = _record(Tier.DEVELOPMENT, created_options=options)
    assert RealmRecord.from_json(record.to_json()).created_options == options


@pytest.mark.fr("NFR-SEC-002")
def test_the_kvm_probe_at_create_round_trips_and_a_record_without_it_reads_as_absent() -> None:
    record = _record(Tier.DEVELOPMENT, kvm_at_create=True)
    assert RealmRecord.from_json(record.to_json()).kvm_at_create is True
    data = _record(Tier.DEVELOPMENT).to_json()
    del data["kvm_at_create"]
    assert RealmRecord.from_json(data).kvm_at_create is False
