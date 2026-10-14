# SPDX-License-Identifier: Apache-2.0
"""A realm's tier and Custom permissions change through the choke point (FR-060, FR-063, FR-066).

Covers ops/permissions.py on the record alone: no podman call is made, so the fake is not needed.
Each refused change is checked for no record write and no journal entry, and each accepted change
for the saved record, the restart flag and one audit entry (DR-032, FR-124).
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.domain.permissions import (
    ALL_TOGGLES,
    BASIC_TOGGLES,
    CustomShare,
    Permissions,
    Toggle,
    effective_toggles,
)
from ginn.errors import GinnError
from ginn.ops._mutating import OpEnv
from ginn.ops.lifecycle import HostUser
from ginn.ops.permissions import read_permissions, set_permissions, set_tier
from ginn.security.caps import option_keys
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from tests.fakes.clock import FakeClock

UID = 1000
USER = "alice"
DIGEST = "registry.example/debian@sha256:" + "ab" * 32
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
NAME = RealmName("lab")
NESTED_KVM = option_keys(ALL_TOGGLES, kvm_present=True)
NESTED_NO_KVM = option_keys(ALL_TOGGLES, kvm_present=False)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    resolved = OwnedPaths.from_env(uid=UID)
    resolved.ensure()
    yield resolved


@pytest.fixture
def env(paths: OwnedPaths, tmp_path: Path) -> OpEnv:
    host = tmp_path / "host"
    host.mkdir(exist_ok=True)
    return OpEnv.for_paths(paths, USER, root_dir=host, clock=FakeClock())


@pytest.fixture
def host(tmp_path: Path) -> HostUser:
    home = tmp_path / "home" / USER
    home.mkdir(parents=True, exist_ok=True)
    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700, exist_ok=True)
    return HostUser(uid=UID, host_home=home, host_env={"XDG_RUNTIME_DIR": str(runtime)})


def _store(env: OpEnv, record: RealmRecord) -> None:
    RealmStore(env.paths, env.clock).save(record)


def _realm(
    tier: Tier,
    *,
    state: RealmState = RealmState.STOPPED,
    services: bool = False,
    permissions: Permissions | None = None,
    created_options: list[str] | None = None,
    kvm_at_create: bool = False,
    gpu_at_create: bool | None = None,
    fonts_at_create: bool | None = None,
    restart: bool = False,
    container: str | None = "c" * 64,
) -> RealmRecord:
    """A record as create would have stored it. The create-time flags follow the toggles unless
    a test names them, so a record with no GPU toggle is not one whose container has the GPU."""
    settings = RealmSettings(
        name=NAME,
        distro=DistroId("debian"),
        tier=tier,
        services=services,
        autostart=False,
        everything=False,
    )
    part = Permissions() if permissions is None else permissions
    toggles = effective_toggles(tier, part.toggles)
    return RealmRecord(
        settings=settings,
        state=state,
        previous_state=None,
        image_ref=DIGEST,
        image_digest=DIGEST.partition("@")[2],
        container_id=container,
        generation=1,
        created_at=T0,
        updated_at=T0,
        restart_required=restart,
        permissions=part,
        created_options=list(created_options or []),
        kvm_at_create=kvm_at_create,
        gpu_at_create=Toggle.GPU in toggles if gpu_at_create is None else gpu_at_create,
        fonts_at_create=(
            Toggle.FONTS_TIMEZONE in toggles if fonts_at_create is None else fonts_at_create
        ),
    )


def _stored(env: OpEnv) -> RealmRecord:
    return RealmStore(env.paths, env.clock).load(NAME)


def _ops(env: OpEnv) -> list[str]:
    """The audit ops of the permission changes. The first run's host baseline is not one."""
    return [entry.op for entry in env.audit.read() if entry.op != "integrity"]


def _custom_realm() -> RealmRecord:
    """A Custom realm as create leaves it: the Basic toggles, plus Projects and a share. A Custom
    realm without the GPU toggle cannot be made, because the GPU device is fixed at create."""
    shares = (CustomShare("/home/alice/Music", "ro"),)
    permissions = Permissions(toggles=BASIC_TOGGLES | {Toggle.PROJECTS}, shares=shares)
    return _realm(Tier.CUSTOM, permissions=permissions)


@pytest.mark.fr("FR-063")
def test_a_basic_realm_becomes_custom_with_the_basic_toggles_and_no_share(env: OpEnv) -> None:
    _store(env, _realm(Tier.BASIC))
    record = set_tier(env, NAME, Tier.CUSTOM)
    assert record.settings.tier is Tier.CUSTOM
    assert record.permissions == Permissions(toggles=BASIC_TOGGLES)
    assert record.restart_required is False
    assert _stored(env) == record
    assert _ops(env) == ["tier"]
    assert env.journal.pending() == []


@pytest.mark.fr("FR-066")
def test_a_running_realm_reports_a_restart_until_the_next_start(env: OpEnv) -> None:
    _store(env, _realm(Tier.BASIC, state=RealmState.RUNNING))
    record = set_tier(env, NAME, Tier.CUSTOM)
    assert record.state is RealmState.RUNNING
    assert record.restart_required is True
    assert _stored(env).restart_required is True


@pytest.mark.fr("NFR-SEC-002")
def test_a_change_that_adds_a_create_option_is_refused_and_writes_nothing(env: OpEnv) -> None:
    _store(env, _realm(Tier.BASIC))
    with pytest.raises(GinnError, match="T8.3") as caught:
        set_tier(env, NAME, Tier.DEVELOPMENT)
    assert caught.value.code == "GINN-E-NOT-SUPPORTED-YET"
    assert _stored(env).settings.tier is Tier.BASIC
    assert env.journal.pending() == []


@pytest.mark.fr("FR-063")
def test_development_to_custom_keeps_its_nested_containers(env: OpEnv) -> None:
    """The toggles of the tier a realm came from are the start of its Custom set (ops docstring)."""
    _store(env, _realm(Tier.DEVELOPMENT, created_options=NESTED_KVM, kvm_at_create=True))
    record = set_tier(env, NAME, Tier.CUSTOM)
    assert record.permissions.toggles == ALL_TOGGLES
    assert record.permissions.shares == ()


@pytest.mark.fr("FR-061")
def test_custom_to_basic_clears_the_toggles_and_the_shares(env: OpEnv) -> None:
    _store(env, _custom_realm())
    record = set_tier(env, NAME, Tier.BASIC)
    assert record.permissions == Permissions()
    assert _stored(env).permissions == Permissions()


@pytest.mark.fr("FR-060")
def test_a_tier_that_is_already_set_writes_nothing(env: OpEnv) -> None:
    _store(env, _realm(Tier.BASIC))
    before = _stored(env)
    record = set_tier(env, NAME, Tier.BASIC)
    assert record == before
    assert _stored(env).updated_at == before.updated_at


@pytest.mark.fr("FR-060")
def test_services_on_is_refused_until_t74_with_no_record_write(env: OpEnv) -> None:
    _store(env, _realm(Tier.BASIC, services=True))
    with pytest.raises(GinnError, match="T7.4") as caught:
        set_tier(env, NAME, Tier.CUSTOM)
    assert caught.value.code == "GINN-E-NOT-SUPPORTED-YET"
    assert _stored(env).settings.tier is Tier.BASIC
    (entry,) = [e for e in env.audit.read() if e.op == "tier"]
    assert entry.error_code == "GINN-E-NOT-SUPPORTED-YET"


@pytest.mark.fr("FR-060")
def test_a_realm_that_is_not_stopped_or_running_is_refused_by_state(env: OpEnv) -> None:
    _store(env, _realm(Tier.BASIC, state=RealmState.CREATING, container=None))
    with pytest.raises(GinnError) as caught:
        set_tier(env, NAME, Tier.CUSTOM)
    assert caught.value.code == "GINN-E-REALM-STATE"
    assert _stored(env).settings.tier is Tier.BASIC


@pytest.mark.fr("FR-063")
def test_perms_on_a_basic_realm_is_refused_with_nothing_written(env: OpEnv, host: HostUser) -> None:
    _store(env, _realm(Tier.BASIC))
    with pytest.raises(GinnError) as caught:
        set_permissions(env, NAME, host, enable=["projects"])
    assert caught.value.code == "GINN-E-TIER-NOT-CUSTOM"
    assert _stored(env).permissions == Permissions()
    assert _ops(env) == ["perms"]


@pytest.mark.fr("FR-063")
@pytest.mark.fr("NFR-SEC-006")
@pytest.mark.parametrize("forbidden", ["/etc/ssh", "/usr/share", "~/.ssh", "/var/lib/outside"])
def test_a_share_that_dr004_forbids_is_refused_with_an_audit_entry_and_no_record_write(
    env: OpEnv, host: HostUser, forbidden: str
) -> None:
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    raw = str(host.host_home / ".ssh") if forbidden == "~/.ssh" else forbidden
    with pytest.raises(GinnError) as caught:
        set_permissions(env, NAME, host, share=[(raw, "rw")])
    assert caught.value.code == "GINN-E-PATH-FORBIDDEN"
    assert _stored(env).permissions == Permissions(toggles=BASIC_TOGGLES)
    (entry,) = [e for e in env.audit.read() if e.op == "perms"]
    assert entry.error_code == "GINN-E-PATH-FORBIDDEN"
    assert entry.params["share"] == [{"path": raw, "mode": "rw"}]


@pytest.mark.fr("FR-063")
def test_a_share_is_stored_under_its_resolved_path_with_its_mode(
    env: OpEnv, host: HostUser
) -> None:
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    music = host.host_home / "Music"
    record = set_permissions(env, NAME, host, share=[(str(music), "ro")])
    assert record.permissions.shares == (CustomShare(str(music), "ro"),)
    assert record.restart_required is False


@pytest.mark.fr("FR-063")
@pytest.mark.fr("AS-011")
def test_a_startup_file_may_be_shared_read_only_only(env: OpEnv, host: HostUser) -> None:
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    rc = str(host.host_home / ".bashrc")
    record = set_permissions(env, NAME, host, share=[(rc, "ro")])
    assert record.permissions.shares == (CustomShare(rc, "ro"),)
    with pytest.raises(GinnError) as caught:
        set_permissions(env, NAME, host, share=[(rc, "rw")])
    assert caught.value.code == "GINN-E-PATH-FORBIDDEN"


@pytest.mark.fr("FR-063")
def test_an_unshare_takes_the_path_as_named_or_resolved(env: OpEnv, host: HostUser) -> None:
    music = str(host.host_home / "Music")
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(shares=(CustomShare(music, "rw"),))))
    record = set_permissions(env, NAME, host, unshare=[music])
    assert record.permissions.shares == ()


@pytest.mark.fr("FR-063")
def test_an_unshare_of_a_path_that_is_not_shared_is_refused(env: OpEnv, host: HostUser) -> None:
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    with pytest.raises(GinnError) as caught:
        set_permissions(env, NAME, host, unshare=[str(host.host_home / "Music")])
    assert caught.value.code == "GINN-E-USAGE"
    assert _stored(env).permissions == Permissions(toggles=BASIC_TOGGLES)


@pytest.mark.fr("FR-063")
def test_enable_and_disable_change_the_toggles(env: OpEnv, host: HostUser) -> None:
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    record = set_permissions(env, NAME, host, enable=["projects"], disable=["downloads"])
    assert record.permissions.toggles == frozenset(BASIC_TOGGLES - {Toggle.DOWNLOADS}) | {
        Toggle.PROJECTS
    }


@pytest.mark.fr("NFR-SEC-005")
def test_an_unknown_toggle_is_refused_with_no_record_write(env: OpEnv, host: HostUser) -> None:
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    with pytest.raises(GinnError) as caught:
        set_permissions(env, NAME, host, enable=["telepathy"])
    assert caught.value.code == "GINN-E-USAGE"
    assert "telepathy" in caught.value.message
    assert _stored(env).permissions == Permissions(toggles=BASIC_TOGGLES)
    (entry,) = [e for e in env.audit.read() if e.op == "perms"]
    assert entry.error_code == "GINN-E-USAGE"


@pytest.mark.fr("FR-063")
@pytest.mark.fr("NFR-SEC-002")
def test_enabling_nested_containers_needs_a_new_container(env: OpEnv, host: HostUser) -> None:
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    with pytest.raises(GinnError, match="T8.3") as caught:
        set_permissions(env, NAME, host, enable=["nested"])
    assert caught.value.code == "GINN-E-NOT-SUPPORTED-YET"
    assert _stored(env).permissions == Permissions(toggles=BASIC_TOGGLES)


@pytest.mark.fr("FR-066")
def test_a_running_custom_realm_flags_the_restart(env: OpEnv, host: HostUser) -> None:
    _store(
        env,
        _realm(
            Tier.CUSTOM,
            state=RealmState.RUNNING,
            permissions=Permissions(toggles=BASIC_TOGGLES),
        ),
    )
    record = set_permissions(env, NAME, host, enable=["projects"])
    assert record.restart_required is True


@pytest.mark.fr("FR-066")
def test_a_change_with_nothing_to_change_writes_nothing(env: OpEnv, host: HostUser) -> None:
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    before = _stored(env)
    assert set_permissions(env, NAME, host, enable=["downloads"]) == before
    assert _stored(env).updated_at == before.updated_at


@pytest.mark.fr("FR-124")
def test_every_accepted_change_leaves_one_audit_entry_and_no_journal_entry(
    env: OpEnv, host: HostUser
) -> None:
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    set_permissions(env, NAME, host, enable=["projects"])
    assert _ops(env) == ["perms"]
    assert env.journal.pending() == []


@pytest.mark.fr("FR-063")
def test_read_permissions_writes_nothing(env: OpEnv) -> None:
    _store(env, _custom_realm())
    before = _stored(env)
    assert read_permissions(env, NAME) == before
    assert _ops(env) == []


@pytest.mark.fr("FR-063")
def test_a_missing_realm_is_refused_before_any_write(env: OpEnv) -> None:
    with pytest.raises(GinnError) as caught:
        set_tier(env, NAME, Tier.CUSTOM)
    assert caught.value.code == "GINN-E-NO-SUCH-REALM"
    assert _ops(env) == []


@pytest.mark.fr("FR-124")
def test_an_accepted_change_records_the_permission_diff_in_the_audit_entry(
    env: OpEnv, host: HostUser
) -> None:
    """FR-124 AC-1: the audit entry carries the tier, toggles and shares before and after."""
    _store(env, _realm(Tier.BASIC))
    set_tier(env, NAME, Tier.CUSTOM)
    set_permissions(env, NAME, host, enable=["projects"])
    basic = sorted(toggle.value for toggle in BASIC_TOGGLES)
    (tier_entry,) = [e for e in env.audit.read() if e.op == "tier"]
    assert tier_entry.before == {"tier": "basic", "toggles": basic, "shares": []}
    assert tier_entry.after == {"tier": "custom", "toggles": basic, "shares": []}
    (perms_entry,) = [e for e in env.audit.read() if e.op == "perms"]
    assert perms_entry.before["toggles"] == basic
    assert perms_entry.after["toggles"] == sorted([*basic, "projects"])


@pytest.mark.fr("NFR-SEC-002")
def test_enabling_kvm_is_refused_when_the_host_had_kvm_at_create(
    env: OpEnv, host: HostUser
) -> None:
    """The kvm option was left out at create on a KVM host, so the container lacks it (ADR-006)."""
    _store(
        env,
        _realm(
            Tier.CUSTOM,
            permissions=Permissions(toggles=BASIC_TOGGLES),
            kvm_at_create=True,
        ),
    )
    with pytest.raises(GinnError, match="T8.3") as caught:
        set_permissions(env, NAME, host, enable=["kvm"])
    assert caught.value.code == "GINN-E-NOT-SUPPORTED-YET"
    assert _stored(env).permissions == Permissions(toggles=BASIC_TOGGLES)


@pytest.mark.fr("NFR-SEC-002")
def test_enabling_kvm_is_accepted_when_the_host_had_no_kvm_at_create(
    env: OpEnv, host: HostUser
) -> None:
    """Without the device at create, the kvm toggle adds nothing to the container (IR-040)."""
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    record = set_permissions(env, NAME, host, enable=["kvm"])
    assert Toggle.KVM in record.permissions.toggles


@pytest.mark.fr("FR-124")
def test_the_perms_audit_entry_carries_the_shares_and_the_toggles_asked_for(
    env: OpEnv, host: HostUser
) -> None:
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    music = str(host.host_home / "Music")
    set_permissions(env, NAME, host, share=[(music, "ro")], enable=["projects"])
    (entry,) = [e for e in env.audit.read() if e.op == "perms"]
    assert entry.params["share"] == [{"path": music, "mode": "ro"}]
    assert entry.params["enable"] == ["projects"]
    assert entry.params["unshare"] == []


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-066")
def test_a_display_or_audio_toggle_on_a_running_custom_realm_is_restart_required(
    env: OpEnv, host: HostUser
) -> None:
    """The display and audio sockets are attach-time grants, so the change waits for a start."""
    off = Permissions(toggles=BASIC_TOGGLES - {Toggle.DISPLAY, Toggle.AUDIO})
    _store(env, _realm(Tier.CUSTOM, state=RealmState.RUNNING, permissions=off))
    record = set_permissions(env, NAME, host, enable=["display", "audio"])
    assert record.permissions.toggles == BASIC_TOGGLES
    assert record.restart_required is True
    assert _stored(env).restart_required is True


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_a_gpu_toggle_change_is_refused_because_the_device_is_fixed_at_create(
    env: OpEnv, host: HostUser
) -> None:
    """The GPU device and the CDI grant are made at create. A container made without them, or
    with them, cannot take the other state (T8.3), on a stopped or a running realm."""
    off = Permissions(toggles=BASIC_TOGGLES - {Toggle.GPU})
    _store(env, _realm(Tier.CUSTOM, permissions=off, gpu_at_create=False))
    with pytest.raises(GinnError) as caught:
        set_permissions(env, NAME, host, enable=["gpu"])
    assert caught.value.code == "GINN-E-NOT-SUPPORTED-YET"
    assert _stored(env).permissions == off
    assert _stored(env).restart_required is False

    basic = Permissions(toggles=BASIC_TOGGLES)
    _store(env, _realm(Tier.CUSTOM, state=RealmState.RUNNING, permissions=basic))
    with pytest.raises(GinnError) as caught:
        set_permissions(env, NAME, host, disable=["gpu"])
    assert caught.value.code == "GINN-E-NOT-SUPPORTED-YET"
    assert _stored(env).permissions == basic


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_a_fonts_timezone_toggle_change_is_refused_by_a_tier_change_too(
    env: OpEnv, host: HostUser
) -> None:
    """Moving a Custom realm to Basic would bring the fonts and timezone binds back, which the
    container made without them does not have, so the tier change is refused."""
    off = Permissions(toggles=BASIC_TOGGLES - {Toggle.FONTS_TIMEZONE})
    _store(env, _realm(Tier.CUSTOM, permissions=off, fonts_at_create=False))
    with pytest.raises(GinnError) as caught:
        set_tier(env, NAME, Tier.BASIC)
    assert caught.value.code == "GINN-E-NOT-SUPPORTED-YET"
    assert _stored(env).settings.tier is Tier.CUSTOM


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_a_custom_realm_that_matches_its_create_flags_takes_a_no_op_change(
    env: OpEnv, host: HostUser
) -> None:
    """Enabling a grant that the container has, on a stopped realm, is accepted (no refusal)."""
    _store(env, _realm(Tier.CUSTOM, permissions=Permissions(toggles=BASIC_TOGGLES)))
    record = set_permissions(env, NAME, host, enable=["display"])
    assert record.permissions.toggles == BASIC_TOGGLES
    assert record.restart_required is False


def _write_raw(env: OpEnv, data: dict[str, Any]) -> None:
    """Write a record file as the committed code emits it, bypassing RealmStore.save."""
    path = env.paths.realms / f"{NAME.value}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.fr("FR-063")
@pytest.mark.fr("FR-061")
@pytest.mark.parametrize(
    ("stored", "listed"),
    [
        pytest.param({"toggles": ["downloads", "mount_roots"], "shares": []}, True, id="listed"),
        pytest.param(None, False, id="no-permissions-key"),
    ],
)
def test_a_record_from_before_the_format_key_loads_and_changes_without_a_refusal(
    env: OpEnv, host: HostUser, stored: dict[str, Any] | None, listed: bool
) -> None:
    """A record with no `format` key and no create flags loads with the grants it had: the four
    FR-061 grants that were always on, and the toggles it listed. Each of the two variants is
    refused by no change, and a Custom realm with no key holds the grants and no listed toggle."""
    data = _realm(Tier.CUSTOM).to_json()
    del data["gpu_at_create"], data["fonts_at_create"]
    if stored is None:
        del data["permissions"]
    else:
        data["permissions"] = stored
    _write_raw(env, data)
    before = effective_toggles(Tier.CUSTOM, _stored(env).permissions.toggles)
    expected = BASIC_TOGGLES if listed else BASIC_TOGGLES - {Toggle.DOWNLOADS, Toggle.MOUNT_ROOTS}
    assert before == expected
    changed = set_permissions(env, NAME, host, enable=["downloads", "mount_roots"])
    assert changed.permissions.toggles == BASIC_TOGGLES
    tiered = set_tier(env, NAME, Tier.CUSTOM)
    assert tiered.permissions.toggles == BASIC_TOGGLES
    assert _stored(env).permissions.toggles == BASIC_TOGGLES
