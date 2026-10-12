# SPDX-License-Identifier: Apache-2.0
"""Atomic JSON writes, realm records and the RealmStore (DR-001, DR-020, DR-035)."""

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
from ginn.errors import GinnError
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore, read_json, write_json_atomic
from tests.fakes.clock import FakeClock

CREATED = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)


def _paths(tmp_path: Path) -> OwnedPaths:
    paths = OwnedPaths.from_env(
        {"HOME": str(tmp_path / "home")}, uid=1000, system_run=tmp_path / "no-run"
    )
    paths.ensure()
    return paths


def _settings(
    name: str = "arch",
    distro: DistroId = DistroId.ARCH,
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
        settings=_settings("debian", DistroId.DEBIAN, services=True, autostart=True),
    )
    data = record.to_json()
    assert data["schema_version"] == 1
    assert data["settings"]["name"] == "debian"
    assert data["settings"]["distro"] == "debian"
    assert data["created_at"] == "2026-10-12T09:30:00.000Z"

    restored = RealmRecord.from_json(json.loads(json.dumps(data)))
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
