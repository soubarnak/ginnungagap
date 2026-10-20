# SPDX-License-Identifier: Apache-2.0
"""Atomic JSON writes, realm records and the RealmStore (DR-001, DR-020, DR-035)."""

import dataclasses
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ginn.domain import (
    REMOVED,
    DistroId,
    IllegalTransition,
    RealmName,
    RealmSettings,
    RealmState,
    Tier,
)
from ginn.domain.grants import GrantRecord, SourceIdentity
from ginn.errors import GinnError
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore, read_json, write_json_atomic
from tests.fakes.clock import FakeClock

CREATED = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
ARCH = DistroId("arch")


def _paths(tmp_path: Path) -> OwnedPaths:
    # from_env refuses a missing runtime directory (ADR-004), so create Host Setup's one. It is
    # 0700 and owned by the test user, which from_env checks.
    uid = os.getuid()
    (tmp_path / "no-run" / "ginn" / str(uid)).mkdir(parents=True, mode=0o700)
    paths = OwnedPaths.from_env(
        {"HOME": str(tmp_path / "home")}, uid=uid, system_run=tmp_path / "no-run"
    )
    paths.ensure()
    return paths


def _settings(
    name: str = "arch",
    distro: DistroId = ARCH,
    *,
    services: bool = False,
    autostart: bool = False,
) -> RealmSettings:
    return RealmSettings(
        name=RealmName(name),
        distro=distro,
        tier=Tier.BASIC,
        services=services,
        autostart=autostart,
        everything=False,
    )


def _record(
    name: str = "arch",
    *,
    state: RealmState = RealmState.STOPPED,
    previous_state: RealmState | None = None,
    settings: RealmSettings | None = None,
    restart_required: bool = False,
    share_skipped: list[str] | None = None,
) -> RealmRecord:
    return RealmRecord(
        settings=settings if settings is not None else _settings(name),
        state=state,
        previous_state=previous_state,
        image_ref="docker.io/library/archlinux@sha256:abc",
        image_digest="sha256:abc",
        container_id="c0ffee",
        generation=1,
        created_at=CREATED,
        updated_at=CREATED,
        restart_required=restart_required,
        share_skipped=list(share_skipped or []),
    )


@pytest.mark.fr("DR-035")
def test_atomic_write_produces_readable_json_with_mode_0600(tmp_path: Path) -> None:
    target = tmp_path / "record.json"
    write_json_atomic(target, {"schema_version": 1, "value": "x"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema_version": 1, "value": "x"}
    assert target.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "record.tmp").exists()


@pytest.mark.fr("DR-035")
def test_crash_before_rename_leaves_old_file_intact_and_no_partial_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "arch.json"
    write_json_atomic(target, {"schema_version": 1, "generation": 1})
    old_bytes = target.read_bytes()

    def crash(src: object, dst: object) -> None:
        raise OSError("simulated power loss before rename")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="simulated power loss"):
        write_json_atomic(target, {"schema_version": 1, "generation": 2, "padding": "x" * 4096})

    assert target.read_bytes() == old_bytes
    assert json.loads(target.read_text(encoding="utf-8"))["generation"] == 1
    assert not (tmp_path / "arch.tmp").exists()


@pytest.mark.fr("DR-035")
def test_crash_mid_write_never_exposes_a_partial_record_under_the_real_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill the writer after the temporary file holds some bytes but before the rename."""
    target = tmp_path / "arch.json"
    write_json_atomic(target, {"schema_version": 1, "generation": 1})
    real_write = os.write

    def partial_write(fd: int, data: bytes | memoryview) -> int:
        real_write(fd, bytes(data)[:7])
        raise KeyboardInterrupt("simulated SIGKILL during write")

    monkeypatch.setattr(os, "write", partial_write)
    with pytest.raises(KeyboardInterrupt):
        write_json_atomic(target, {"schema_version": 1, "generation": 2})

    assert read_json(target, 1)["generation"] == 1
    assert not (tmp_path / "arch.tmp").exists()


@pytest.mark.fr("DR-035")
def test_read_json_rejects_malformed_and_wrong_version_files(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(GinnError) as bad_json:
        read_json(broken, 1)
    assert bad_json.value.code == "GINN-E-INTERNAL"
    assert bad_json.value.details["path"] == str(broken)

    old = tmp_path / "old.json"
    write_json_atomic(old, {"schema_version": 2})
    with pytest.raises(GinnError) as wrong_version:
        read_json(old, 1)
    assert wrong_version.value.code == "GINN-E-INTERNAL"


@pytest.mark.fr("DR-001")
def test_realm_record_round_trips_through_json(tmp_path: Path) -> None:
    record = _record(
        state=RealmState.REBUILDING,
        previous_state=RealmState.RUNNING,
        restart_required=True,
        share_skipped=["/etc/shadow"],
        settings=_settings("debian", DistroId("debian"), services=True, autostart=True),
    )
    data = record.to_json()
    assert data["schema_version"] == 1
    assert data["settings"]["name"] == "debian"
    assert data["settings"]["distro"] == "debian"
    assert data["created_at"] == "2026-10-12T09:30:00.000Z"

    restored = RealmRecord.from_json(json.loads(json.dumps(data)))
    assert restored == record


@pytest.mark.fr("NFR-060")
@pytest.mark.fr("DR-001")
def test_realm_record_stores_the_distro_as_its_plain_name_and_reads_the_fixture(
    tmp_path: Path,
) -> None:
    # The on-disk form is the bare name, as before the ID became a value type. The `_test`
    # fixture is a definition like any other, so a record for it round-trips without code changes.
    record = _record(settings=_settings("fixture", DistroId("_test")))
    data = record.to_json()
    assert data["settings"]["distro"] == "_test"
    restored = RealmRecord.from_json(json.loads(json.dumps(data)))
    assert restored.settings.distro == DistroId("_test")
    assert restored == record


@pytest.mark.fr("DR-001")
def test_realm_record_with_an_invalid_field_is_reported_as_internal(tmp_path: Path) -> None:
    data = _record().to_json()
    settings: dict[str, object] = data["settings"]
    settings["distro"] = "windows"
    with pytest.raises(GinnError) as excinfo:
        RealmRecord.from_json(data, source=str(tmp_path / "x.json"))
    assert excinfo.value.code == "GINN-E-INTERNAL"
    assert "reason" in excinfo.value.details


LEGAL_PREVIOUS = [
    (RealmState.UPDATING, RealmState.RUNNING),
    (RealmState.UPDATING, RealmState.STOPPED),
    (RealmState.REBUILDING, RealmState.RUNNING),
    (RealmState.REBUILDING, RealmState.STOPPED),
]
ILLEGAL_PREVIOUS = [
    (RealmState.UPDATING, None),
    (RealmState.REBUILDING, None),
    (RealmState.UPDATING, RealmState.FAILED),
    (RealmState.REBUILDING, RealmState.BROKEN),
    (RealmState.UPDATING, RealmState.REBUILDING),
    (RealmState.STOPPED, RealmState.RUNNING),
    (RealmState.RUNNING, RealmState.STOPPED),
    (RealmState.BROKEN, RealmState.STOPPED),
    (RealmState.CREATING, RealmState.RUNNING),
]
NO_PREVIOUS_STATE = [s for s in RealmState if s not in (RealmState.UPDATING, RealmState.REBUILDING)]


@pytest.mark.fr("DR-020")
@pytest.mark.parametrize("state", NO_PREVIOUS_STATE)
def test_states_that_carry_no_previous_state_construct_without_one(state: RealmState) -> None:
    assert _record(state=state).previous_state is None


@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(("state", "previous"), LEGAL_PREVIOUS)
def test_a_legal_previous_state_survives_a_json_round_trip(
    state: RealmState, previous: RealmState
) -> None:
    record = _record(state=state, previous_state=previous)
    assert RealmRecord.from_json(json.loads(json.dumps(record.to_json()))) == record


@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(("state", "previous"), ILLEGAL_PREVIOUS)
def test_an_illegal_previous_state_is_refused_when_the_record_is_built(
    state: RealmState, previous: RealmState | None
) -> None:
    with pytest.raises(ValueError, match="previous_state"):
        _record(state=state, previous_state=previous)


@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(("state", "previous"), ILLEGAL_PREVIOUS)
def test_a_stored_record_with_an_illegal_previous_state_is_reported_as_internal(
    tmp_path: Path, state: RealmState, previous: RealmState | None
) -> None:
    data = _record(state=RealmState.STOPPED).to_json()
    data["state"] = state.value
    data["previous_state"] = None if previous is None else previous.value
    with pytest.raises(GinnError) as excinfo:
        RealmRecord.from_json(data, source=str(tmp_path / "arch.json"))
    assert excinfo.value.code == "GINN-E-INTERNAL"
    assert "previous_state" in excinfo.value.details["reason"]


@pytest.mark.fr("DR-001")
def test_store_save_load_exists_and_list_names(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = RealmStore(paths, clock=FakeClock())
    arch = RealmName("arch")
    assert not store.exists(arch)
    assert store.list_names() == []

    store.save(_record("debian"))
    store.save(_record("arch"))
    assert store.exists(arch)
    assert store.load(arch) == _record("arch")
    assert store.list_names() == [RealmName("arch"), RealmName("debian")]


@pytest.mark.fr("DR-035")
def test_list_names_skips_leftover_temporary_files(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = RealmStore(paths, clock=FakeClock())
    store.save(_record("arch"))
    (paths.realms / "debian.tmp").write_text("{partial", encoding="utf-8")
    (paths.realms / "Bad_Name.json").write_text("{}", encoding="utf-8")
    assert store.list_names() == [RealmName("arch")]


@pytest.mark.fr("DR-001")
def test_load_of_a_missing_realm_raises_no_such_realm(tmp_path: Path) -> None:
    store = RealmStore(_paths(tmp_path), clock=FakeClock())
    with pytest.raises(GinnError) as excinfo:
        store.load(RealmName("ghost"))
    assert excinfo.value.code == "GINN-E-NO-SUCH-REALM"
    assert excinfo.value.exit_code == 3
    assert str(excinfo.value) == "GINN-E-NO-SUCH-REALM: no realm named 'ghost' for this user"


@pytest.mark.fr("DR-001")
def test_delete_removes_the_record_and_missing_delete_is_an_error(tmp_path: Path) -> None:
    store = RealmStore(_paths(tmp_path), clock=FakeClock())
    store.save(_record("arch", state=RealmState.DELETING))
    store.delete(RealmName("arch"))
    assert not store.exists(RealmName("arch"))
    with pytest.raises(GinnError) as excinfo:
        store.delete(RealmName("arch"))
    assert excinfo.value.code == "GINN-E-NO-SUCH-REALM"


@pytest.mark.fr("DR-020")
def test_set_state_applies_a_legal_transition_and_stamps_updated_at(tmp_path: Path) -> None:
    clock = FakeClock(start=1_800_000_060.5)
    store = RealmStore(_paths(tmp_path), clock=clock)
    store.save(_record("arch", state=RealmState.STOPPED))

    updated = store.set_state(RealmName("arch"), RealmState.RUNNING)

    assert updated.state is RealmState.RUNNING
    assert updated.previous_state is None
    assert updated.updated_at == datetime(2027, 1, 15, 8, 1, 0, 500000, tzinfo=UTC)
    assert store.load(RealmName("arch")).state is RealmState.RUNNING


@pytest.mark.fr("DR-020")
def test_set_state_records_and_restores_the_previous_state_for_rebuilding(tmp_path: Path) -> None:
    store = RealmStore(_paths(tmp_path), clock=FakeClock())
    store.save(_record("arch", state=RealmState.RUNNING))
    arch = RealmName("arch")

    rebuilding = store.set_state(arch, RealmState.REBUILDING)
    assert rebuilding.previous_state is RealmState.RUNNING

    # A rebuild may return only to the state it started from (DR-020).
    with pytest.raises(IllegalTransition):
        store.set_state(arch, RealmState.STOPPED)
    assert store.load(arch).state is RealmState.REBUILDING

    back = store.set_state(arch, RealmState.RUNNING)
    assert back.state is RealmState.RUNNING
    assert back.previous_state is None


@pytest.mark.fr("DR-020")
def test_illegal_transition_raises_and_writes_nothing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = RealmStore(paths, clock=FakeClock())
    store.save(_record("arch", state=RealmState.BROKEN))
    record_file = paths.realms / "arch.json"
    before = record_file.read_bytes()

    with pytest.raises(IllegalTransition):
        store.set_state(RealmName("arch"), RealmState.RUNNING)

    assert record_file.read_bytes() == before


@pytest.mark.fr("DR-020")
def test_set_state_refuses_removal_which_is_delete_instead(tmp_path: Path) -> None:
    store = RealmStore(_paths(tmp_path), clock=FakeClock())
    store.save(_record("arch", state=RealmState.DELETING))
    with pytest.raises(ValueError, match="delete"):
        store.set_state(RealmName("arch"), REMOVED)
    assert store.load(RealmName("arch")).state is RealmState.DELETING


@pytest.mark.fr("DR-020")
@pytest.mark.parametrize("state", [RealmState.CREATING, RealmState.DELETING])
def test_delete_removes_a_record_from_the_two_states_that_lead_to_removal(
    tmp_path: Path, state: RealmState
) -> None:
    store = RealmStore(_paths(tmp_path), clock=FakeClock())
    store.save(_record("arch", state=state))
    store.delete(RealmName("arch"))
    assert not store.exists(RealmName("arch"))


@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    ("state", "previous"),
    [
        (RealmState.STOPPED, None),
        (RealmState.RUNNING, None),
        (RealmState.UPDATING, RealmState.RUNNING),
        (RealmState.REBUILDING, RealmState.STOPPED),
        (RealmState.FAILED, None),
        (RealmState.BROKEN, None),
    ],
)
def test_delete_is_refused_from_every_state_that_does_not_lead_to_removal(
    tmp_path: Path, state: RealmState, previous: RealmState | None
) -> None:
    paths = _paths(tmp_path)
    store = RealmStore(paths, clock=FakeClock())
    store.save(_record("arch", state=state, previous_state=previous))
    record_file = paths.realms / "arch.json"
    before = record_file.read_bytes()

    with pytest.raises(IllegalTransition):
        store.delete(RealmName("arch"))

    assert record_file.read_bytes() == before


@pytest.mark.fr("FR-122")
def test_delete_cannot_remove_a_corrupt_record(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = RealmStore(paths, clock=FakeClock())
    record_file = paths.realms / "arch.json"
    record_file.write_text("{not json", encoding="utf-8")
    # delete() loads first, so a corrupt record is reported and kept. remove_corrupt is the way out.
    with pytest.raises(GinnError) as excinfo:
        store.delete(RealmName("arch"))
    assert excinfo.value.code == "GINN-E-INTERNAL"
    assert record_file.exists()


@pytest.mark.fr("FR-122")
@pytest.mark.parametrize(
    "content",
    ["{not json", "[1, 2]", '{"schema_version": 2}', '{"schema_version": 1}'],
)
def test_remove_corrupt_removes_a_record_that_does_not_parse(tmp_path: Path, content: str) -> None:
    paths = _paths(tmp_path)
    store = RealmStore(paths, clock=FakeClock())
    record_file = paths.realms / "arch.json"
    record_file.write_text(content, encoding="utf-8")

    store.remove_corrupt(RealmName("arch"))

    assert not record_file.exists()
    assert store.list_names() == []


@pytest.mark.fr("FR-122")
def test_remove_corrupt_removes_a_record_that_breaks_the_dr020_rule(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = RealmStore(paths, clock=FakeClock())
    data = _record(state=RealmState.STOPPED).to_json()
    # DR-020: an updating realm must name the state it returns to, and this record names none.
    data["state"] = RealmState.UPDATING.value
    record_file = paths.realms / "arch.json"
    record_file.write_text(json.dumps(data), encoding="utf-8")

    store.remove_corrupt(RealmName("arch"))

    assert not record_file.exists()


@pytest.mark.fr("FR-122")
def test_remove_corrupt_refuses_a_record_that_loads(tmp_path: Path) -> None:
    store = RealmStore(_paths(tmp_path), clock=FakeClock())
    store.save(_record("arch", state=RealmState.STOPPED))
    with pytest.raises(ValueError, match="delete"):
        store.remove_corrupt(RealmName("arch"))
    assert store.load(RealmName("arch")).state is RealmState.STOPPED


@pytest.mark.fr("FR-122")
def test_remove_corrupt_on_a_missing_realm_is_no_such_realm(tmp_path: Path) -> None:
    store = RealmStore(_paths(tmp_path), clock=FakeClock())
    with pytest.raises(GinnError) as excinfo:
        store.remove_corrupt(RealmName("ghost"))
    assert excinfo.value.code == "GINN-E-NO-SUCH-REALM"


@pytest.mark.fr("FR-066")
def test_realm_record_keeps_its_grant_list_through_json(tmp_path: Path) -> None:
    grants = [
        GrantRecord(
            "share",
            "/home/alice/Downloads",
            "/home/alice/Downloads",
            "rw",
            True,
            SourceIdentity(7, 11, ("/home/alice/Downloads/mnt",)),
            None,
        ),
        GrantRecord("session", "", "/run/user/1000/wayland-0", "rw", False),
    ]
    record = dataclasses.replace(_record(), grants=grants)
    restored = RealmRecord.from_json(json.loads(json.dumps(record.to_json())))
    assert restored == record
    assert restored.grants[0].identity == SourceIdentity(7, 11, ("/home/alice/Downloads/mnt",))


@pytest.mark.fr("FR-066")
def test_a_record_written_before_grants_existed_reads_with_an_empty_list(tmp_path: Path) -> None:
    older = _record().to_json()
    del older["grants"]
    assert older["schema_version"] == 1
    assert RealmRecord.from_json(older).grants == []


@pytest.mark.fr("FR-083")
def test_a_record_written_before_the_services_mode_was_kept_reads_it_from_its_settings(
    tmp_path: Path,
) -> None:
    """No realm could change its services mode before the field existed, so the container's mode
    is the settings' mode, for a services-off and for a services-on record alike."""
    for services in (False, True):
        older = _record(settings=_settings(services=services)).to_json()
        del older["services_at_create"]
        loaded = RealmRecord.from_json(older)
        assert loaded.services_at_create is services


@pytest.mark.fr("FR-083")
def test_the_services_mode_the_container_has_is_stored_apart_from_the_settings(
    tmp_path: Path,
) -> None:
    record = _record(settings=_settings(services=False))
    assert record.services_at_create is False
    settings = dataclasses.replace(record.settings, services=True)
    pending = dataclasses.replace(record, settings=settings)
    assert pending.services_at_create is False
    stored = RealmRecord.from_json(pending.to_json())
    assert stored.settings.services is True
    assert stored.services_at_create is False
    assert pending.to_json()["services_at_create"] is False


@pytest.mark.fr("FR-066")
def test_a_grant_that_does_not_parse_is_reported_as_internal(tmp_path: Path) -> None:
    data = _record().to_json()
    data["grants"] = [{"kind": 5}]
    with pytest.raises(GinnError) as excinfo:
        RealmRecord.from_json(data, source=str(tmp_path / "x.json"))
    assert excinfo.value.code == "GINN-E-INTERNAL"


@pytest.mark.fr("FR-066")
@pytest.mark.parametrize("item", [5, "x", None, ["a"]])
def test_a_grant_item_that_is_not_an_object_is_reported_as_internal(
    tmp_path: Path, item: object
) -> None:
    data = _record().to_json()
    data["grants"] = [item]
    with pytest.raises(GinnError) as excinfo:
        RealmRecord.from_json(data, source=str(tmp_path / "x.json"))
    assert excinfo.value.code == "GINN-E-INTERNAL"


def _grant_json(**changes: object) -> dict[str, object]:
    """A valid share grant as the record stores it, with `changes` applied."""
    grant: dict[str, object] = {
        "kind": "share",
        "host": "/home/alice/Downloads",
        "realm": "/home/alice/Downloads",
        "mode": "rw",
        "is_dir": True,
        "identity": None,
        "not_visible": None,
    }
    grant.update(changes)
    return grant


@pytest.mark.fr("FR-066")
@pytest.mark.parametrize(
    "changes",
    [
        {"realm": "relative/Downloads"},
        {"realm": ""},
        {"host": "relative/Downloads"},
        {"kind": "share", "host": ""},
    ],
)
def test_a_grant_with_a_relative_path_is_reported_as_internal(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    data = _record().to_json()
    data["grants"] = [_grant_json(**changes)]
    with pytest.raises(GinnError) as excinfo:
        RealmRecord.from_json(data, source=str(tmp_path / "x.json"))
    assert excinfo.value.code == "GINN-E-INTERNAL"


@pytest.mark.fr("FR-066")
def test_a_session_slot_with_an_empty_host_is_a_valid_grant(tmp_path: Path) -> None:
    """A session path that no session binds yet has an empty host (runtime/env.py)."""
    data = _record().to_json()
    data["grants"] = [
        _grant_json(kind="session", host="", realm="/run/user/1000/wayland-0", mode="rw")
    ]
    restored = RealmRecord.from_json(data, source=str(tmp_path / "x.json"))
    assert restored.grants[0].host == ""
    assert restored.grants[0].realm == "/run/user/1000/wayland-0"


@pytest.mark.fr("NFR-004")
@pytest.mark.parametrize("socket", ["/run/user/1000/gnupg/S.gpg-agent.extra", None])
def test_the_gpg_socket_resolved_at_start_round_trips_even_when_the_host_has_none(
    socket: str | None,
) -> None:
    """A known value is stored, so enter does not run gpgconf again (NFR-004). None is a value."""
    record = dataclasses.replace(_record(), gpg_extra=socket, gpg_extra_known=True)
    data = json.loads(json.dumps(record.to_json()))
    assert data["gpg_extra"] == socket
    assert RealmRecord.from_json(data) == record


@pytest.mark.fr("NFR-004")
def test_a_record_that_predates_the_gpg_socket_reads_as_unknown() -> None:
    """No key means unknown, so enter resolves the socket once. The key is written when known."""
    assert "gpg_extra" not in _record().to_json()
    older = _record().to_json()
    restored = RealmRecord.from_json(older)
    assert restored.gpg_extra_known is False
    assert restored.gpg_extra is None


@pytest.mark.fr("DR-034")
@pytest.mark.fr("FR-021")
def test_the_building_container_round_trips_and_a_record_without_it_reads_none(
    tmp_path: Path,
) -> None:
    """A rebuild records the container it builds while the record reads rebuilding (ADR-003). A
    record written before the two fields existed has no keys, and it reads them as None."""
    building = dataclasses.replace(
        _record(state=RealmState.REBUILDING, previous_state=RealmState.STOPPED),
        building_container_id="b0b0",
        building_generation=2,
    )
    stored = RealmRecord.from_json(building.to_json())
    assert stored.building_container_id == "b0b0"
    assert stored.building_generation == 2
    older = _record().to_json()
    del older["building_container_id"]
    del older["building_generation"]
    loaded = RealmRecord.from_json(older)
    assert loaded.building_container_id is None
    assert loaded.building_generation is None


@pytest.mark.fr("DR-034")
def test_a_building_container_is_refused_outside_a_rebuild() -> None:
    with pytest.raises(ValueError, match="must not record a building container"):
        dataclasses.replace(_record(), building_container_id="b0b0")
    with pytest.raises(ValueError, match="must not record a building container"):
        dataclasses.replace(_record(), building_generation=2)


@pytest.mark.fr("DR-034")
@pytest.mark.fr("FR-021")
def test_leaving_a_rebuild_clears_the_building_container_and_keeps_the_named_one(
    tmp_path: Path,
) -> None:
    """A rollback or a recovery returns the record to its previous state through set_state, which
    clears the building fields as it clears previous_state. The container the record names stays."""
    paths = _paths(tmp_path)
    store = RealmStore(paths, clock=FakeClock())
    store.save(
        dataclasses.replace(
            _record(state=RealmState.REBUILDING, previous_state=RealmState.STOPPED),
            building_container_id="b0b0",
            building_generation=2,
        )
    )
    stopped = store.set_state(RealmName("arch"), RealmState.STOPPED)
    assert stopped.building_container_id is None
    assert stopped.building_generation is None
    assert stopped.container_id == "c0ffee"
    assert store.load(RealmName("arch")).building_container_id is None


@pytest.mark.fr("FR-080")
def test_a_machine_id_survives_json_and_a_malformed_one_is_reported_as_internal(
    tmp_path: Path,
) -> None:
    machine_id = "0123456789abcdef0123456789abcdef"
    record = dataclasses.replace(_record(), machine_id=machine_id)
    data = record.to_json()
    assert data["machine_id"] == machine_id
    assert RealmRecord.from_json(json.loads(json.dumps(data))) == record
    legacy = {key: value for key, value in data.items() if key != "machine_id"}
    assert RealmRecord.from_json(legacy).machine_id is None
    for bad in ("0123", "0123456789ABCDEF0123456789ABCDEF"):
        with pytest.raises(GinnError) as excinfo:
            RealmRecord.from_json({**data, "machine_id": bad}, source=str(tmp_path / "x.json"))
        assert excinfo.value.code == "GINN-E-INTERNAL"


INIT_ID = "a1" * 32
BUILDING_ID = "b2" * 32


@pytest.mark.fr("DR-035")
def test_the_image_ids_and_machine_id_are_written_in_schema_1_and_read_back(
    tmp_path: Path,
) -> None:
    """A services-on rebuild that is making a derived image holds both image IDs and the
    machine-id (ADR-015 Decision 6). The stored file still reads as schema 1, and every key
    comes back."""
    machine_id = "0123456789abcdef0123456789abcdef"
    building = dataclasses.replace(
        _record(state=RealmState.REBUILDING, previous_state=RealmState.STOPPED),
        machine_id=machine_id,
        init_image_id=INIT_ID,
        building_init_image_id=BUILDING_ID,
        building_container_id="b0b0",
        building_generation=2,
    )
    paths = _paths(tmp_path)
    RealmStore(paths, clock=FakeClock()).save(building)
    stored = read_json(paths.realms / "arch.json", 1)
    assert stored["schema_version"] == 1
    assert stored["init_image_id"] == INIT_ID
    assert stored["building_init_image_id"] == BUILDING_ID
    assert stored["machine_id"] == machine_id
    assert RealmRecord.from_json(json.loads(json.dumps(stored))) == building


@pytest.mark.fr("DR-035")
def test_a_record_written_before_the_image_ids_reads_them_as_none() -> None:
    assert _record().to_json()["init_image_id"] is None
    older = _record().to_json()
    del older["init_image_id"]
    del older["building_init_image_id"]
    loaded = RealmRecord.from_json(older)
    assert loaded.init_image_id is None
    assert loaded.building_init_image_id is None


@pytest.mark.fr("DR-035")
@pytest.mark.parametrize("key", ["init_image_id", "building_init_image_id"])
@pytest.mark.parametrize(
    "bad",
    [
        "sha256:" + "a1" * 32,
        "sha512:" + "a1" * 32,
        "A1" * 32,
        "a1" * 31,
        "a1" * 33,
        "a1" * 31 + "g1",
        "",
    ],
)
def test_a_malformed_image_id_is_reported_as_internal(tmp_path: Path, key: str, bad: str) -> None:
    # A rebuild names both image IDs (the building one is refused outside a rebuild), so the
    # record is rebuilding for either key.
    rebuilding = _record(state=RealmState.REBUILDING, previous_state=RealmState.STOPPED)
    data = rebuilding.to_json()
    data[key] = bad
    with pytest.raises(GinnError) as excinfo:
        RealmRecord.from_json(data, source=str(tmp_path / "x.json"))
    assert excinfo.value.code == "GINN-E-INTERNAL"
    with pytest.raises(ValueError, match=key):
        dataclasses.replace(rebuilding, **{key: bad})


@pytest.mark.fr("DR-035")
@pytest.mark.parametrize("state", [RealmState.STOPPED, RealmState.RUNNING, RealmState.BROKEN])
def test_a_building_image_id_is_refused_outside_a_rebuild(
    tmp_path: Path, state: RealmState
) -> None:
    """The building image ID follows the building container: only a rebuild may name one."""
    with pytest.raises(ValueError, match="must not record a building container or image"):
        dataclasses.replace(_record(state=state), building_init_image_id=BUILDING_ID)
    data = _record(state=state).to_json()
    data["building_init_image_id"] = BUILDING_ID
    with pytest.raises(GinnError) as excinfo:
        RealmRecord.from_json(data, source=str(tmp_path / "x.json"))
    assert excinfo.value.code == "GINN-E-INTERNAL"


@pytest.mark.fr("DR-035")
def test_rolling_back_a_rebuild_clears_the_building_image_and_keeps_the_init_image(
    tmp_path: Path,
) -> None:
    """Leaving rebuilding clears the building image ID with the building container. The init
    image is the realm's current derived image, so the rollback keeps it."""
    paths = _paths(tmp_path)
    store = RealmStore(paths, clock=FakeClock())
    store.save(
        dataclasses.replace(
            _record(state=RealmState.REBUILDING, previous_state=RealmState.STOPPED),
            init_image_id=INIT_ID,
            building_init_image_id=BUILDING_ID,
            building_container_id="b0b0",
            building_generation=2,
        )
    )
    stopped = store.set_state(RealmName("arch"), RealmState.STOPPED)
    assert stopped.building_init_image_id is None
    assert stopped.init_image_id == INIT_ID
    stored = read_json(paths.realms / "arch.json", 1)
    assert stored["building_init_image_id"] is None
    assert stored["init_image_id"] == INIT_ID
    assert store.load(RealmName("arch")).building_init_image_id is None
