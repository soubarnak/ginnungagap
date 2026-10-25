# SPDX-License-Identifier: Apache-2.0
"""The Install Manifest (T5.2, DR-007): entry shape, the pure rules, and the stored record.

Covered: the entry round trip and its refusals, the three rules (installed, removed, recorded
native), the record field read back from JSON, and a record written before the field existed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ginn.domain import (
    DistroId,
    InstallMethod,
    InstallSource,
    ManifestEntry,
    RealmName,
    RealmSettings,
    RealmState,
    Tier,
)
from ginn.packages.manifest import installed, recorded_native, removed
from ginn.store.records import RealmRecord

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
DIGEST = "registry.example/arch@sha256:" + "ab" * 32


def _entry(name: str, method: InstallMethod = InstallMethod.GINN) -> ManifestEntry:
    return ManifestEntry(name=name, source=InstallSource.REPO, method=method)


def _record(manifest: list[ManifestEntry] | None = None) -> RealmRecord:
    settings = RealmSettings(
        name=RealmName("arch"),
        distro=DistroId("arch"),
        tier=Tier.BASIC,
        services=False,
        autostart=False,
        everything=False,
    )
    return RealmRecord(
        settings=settings,
        state=RealmState.STOPPED,
        previous_state=None,
        image_ref=DIGEST,
        image_digest=DIGEST.partition("@")[2],
        container_id=None,
        generation=1,
        created_at=T0,
        updated_at=T0,
        manifest=list(manifest or []),
    )


@pytest.mark.fr("DR-007")
def test_an_entry_round_trips_through_its_json_object() -> None:
    entry = ManifestEntry(name="htop", source=InstallSource.AUR, method=InstallMethod.NATIVE)
    data = entry.to_dict()
    assert data == {"name": "htop", "source": "aur", "method": "native"}
    assert ManifestEntry.from_dict(data) == entry


@pytest.mark.fr("DR-007")
@pytest.mark.parametrize(
    "data",
    [
        {"name": "", "source": "repo", "method": "ginn"},
        {"name": 3, "source": "repo", "method": "ginn"},
        {"name": "htop", "source": "pip", "method": "ginn"},
        {"name": "htop", "source": "repo", "method": "docker"},
        {"name": "htop", "source": None, "method": "ginn"},
    ],
)
def test_a_bad_entry_field_is_refused(data: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        ManifestEntry.from_dict(data)


@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-040")
def test_installed_sets_each_name_and_keeps_the_other_entries_sorted() -> None:
    before = [_entry("vim", InstallMethod.NATIVE)]
    after = installed(
        before, ["htop", "gimp"], source=InstallSource.REPO, method=InstallMethod.GINN
    )
    assert [entry.name for entry in after] == ["gimp", "htop", "vim"]
    assert after[0] == _entry("gimp")
    assert after[2] == _entry("vim", InstallMethod.NATIVE)


@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-040")
def test_installed_takes_the_ginn_method_for_a_name_that_was_native() -> None:
    before = [_entry("htop", InstallMethod.NATIVE)]
    after = installed(before, ["htop"], source=InstallSource.REPO, method=InstallMethod.GINN)
    assert after == [_entry("htop", InstallMethod.GINN)]


@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-041")
def test_removed_drops_the_named_entries_and_ignores_unknown_names() -> None:
    before = [_entry("gimp"), _entry("htop")]
    assert removed(before, ["gimp", "not-there"]) == [_entry("htop")]
    assert removed(before, []) == before


@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-042")
def test_recorded_native_adds_only_the_names_without_an_entry() -> None:
    before = [_entry("htop")]
    after = recorded_native(before, ["htop", "tmux"])
    assert after == [
        _entry("htop"),
        ManifestEntry("tmux", InstallSource.REPO, InstallMethod.NATIVE),
    ]


@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-042")
def test_recorded_native_never_turns_a_ginn_install_native() -> None:
    before = [_entry("htop", InstallMethod.GINN)]
    assert recorded_native(before, ["htop"]) == before


@pytest.mark.fr("DR-001")
@pytest.mark.fr("DR-007")
def test_the_record_stores_its_manifest_and_reads_it_back() -> None:
    record = _record([_entry("htop", InstallMethod.NATIVE), _entry("gimp")])
    data = record.to_json()
    assert data["manifest"] == [
        {"name": "htop", "source": "repo", "method": "native"},
        {"name": "gimp", "source": "repo", "method": "ginn"},
    ]
    assert RealmRecord.from_json(data).manifest == record.manifest


@pytest.mark.fr("DR-001")
@pytest.mark.fr("DR-035")
def test_a_record_written_before_the_manifest_reads_as_an_empty_manifest() -> None:
    data = _record().to_json()
    del data["manifest"]
    assert RealmRecord.from_json(data).manifest == []


@pytest.mark.fr("DR-001")
def test_a_stored_manifest_entry_that_is_not_an_object_is_corrupt() -> None:
    from ginn.errors import GinnError

    data = _record().to_json()
    data["manifest"] = ["htop"]
    with pytest.raises(GinnError) as caught:
        RealmRecord.from_json(data)
    assert caught.value.code == "GINN-E-INTERNAL"
