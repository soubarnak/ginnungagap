# SPDX-License-Identifier: Apache-2.0
"""The supervised start and finish of a services-on realm (FR-081, AS-028, NFR-033).

ops/service.py against FakePodman, under the pinned host of realm_cli_support. No podman, runsv or
ginn-wait runs: the helper is a stand-in file that only has to be executable, the sleep is recorded,
and the runsv control FIFO has a reader that the test holds. The clock is a FakeClock, so the crash
window is exact.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from ginn.domain import RealmName, RealmState
from ginn.errors import GinnError
from ginn.ops import delete as delete_ops
from ginn.ops.lifecycle import HostUser, LifecycleHooks
from ginn.ops.service import (
    ServiceExit,
    ServiceHooks,
    finish_service,
    set_autostart,
    set_services_mode,
    start_service,
)
from ginn.podman.adapter import CreateSpec
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.services.runit_user import (
    DOWN,
    conmon_pidfile,
    crash_log,
    service_dir,
    write_service_dir,
)
from ginn.store.records import RealmStore
from tests.fakes.clock import FakeClock
from tests.unit.realm_cli_support import DIGEST, World, build_world, record_of

REALM = RealmName("lab")
GLOBAL_ARGV = [
    "/usr/bin/podman",
    "--remote=false",
    "--root",
    "/data/podman",
    "--runroot",
    "/run/ginn/1000/podman",
    "--tmpdir",
    "/run/ginn/1000/podman-tmp",
    "--runtime",
    "crun",
]
PROGRAM = "/usr/bin/ginn"
T0 = 1_800_000_000.0

pytestmark = [pytest.mark.fr("FR-081"), pytest.mark.fr("AS-028")]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def env(world: World, clock: FakeClock):  # type: ignore[no-untyped-def]
    from ginn.ops._mutating import OpEnv

    return OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)


@pytest.fixture
def helper(tmp_path: Path) -> Path:
    """A stand-in ginn-wait: executable, and never run by these tests."""
    path = tmp_path / "ginn-wait"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def user(world: World) -> HostUser:
    return HostUser(uid=os.getuid(), host_home=world.home, host_env={})


def hooks_for(helper: Path, sleeps: list[float]) -> ServiceHooks:
    """Hooks where no host path exists, so the Basic grants all skip and no helper runs."""
    lifecycle = LifecycleHooks(exists=lambda _path: False)
    return ServiceHooks(lifecycle=lifecycle, wait_helper=str(helper), sleep=sleeps.append)


def services_realm(world: World, state: RealmState, *, running: bool, services: bool = True):  # type: ignore[no-untyped-def]
    """A stored realm with a fake container. `running` starts the container in the fake.

    The realm's runit service directory exists, as a services-on create makes it. A test that
    needs it made before the realm (the reader of the control FIFO) makes it first.
    """
    if not service_dir(world.paths, REALM).exists():
        write_service_dir(world.paths, REALM, PROGRAM)
    labels = {LABEL_REALM: "lab", LABEL_DISTRO: "debian", LABEL_GENERATION: "1"}
    spec = CreateSpec(name="ginn-lab-g1", image_digest_ref=DIGEST, network="host", labels=labels)
    cid = world.fake.create(spec)
    if running:
        world.fake.start(cid)
    (world.paths.homes / "lab").mkdir(parents=True, exist_ok=True)
    record = record_of("lab", state, cid, services=services)
    RealmStore(world.paths).save(record)
    world.fake.calls.clear()
    return record


def stored(world: World):  # type: ignore[no-untyped-def]
    return RealmStore(world.paths).load(REALM)


def ops_of(world: World) -> list[str]:
    return [call.op for call in world.fake.calls]


def audit_ops(env) -> list[str]:  # type: ignore[no-untyped-def]
    """The operations of the audit log, without the host integrity baseline entries."""
    return [entry.op for entry in env.audit.read() if entry.op != "integrity"]


def last_audit(env):  # type: ignore[no-untyped-def]
    return [entry for entry in env.audit.read() if entry.op != "integrity"][-1]


@pytest.fixture
def reader_of_control(world: World) -> Iterator[int]:
    """The reader that stands in for runsv on the service's control FIFO (runsv(8))."""
    write_service_dir(world.paths, REALM, PROGRAM)
    (service_dir(world.paths, REALM) / DOWN).unlink()  # the realm is up: runit started it
    fifo = service_dir(world.paths, REALM) / "supervise" / "control"
    fifo.parent.mkdir()
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        yield reader
    finally:
        os.close(reader)


@pytest.mark.fr("NFR-033")
def test_start_starts_the_container_and_returns_the_ginn_wait_argv(
    world: World, env, helper: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    record = services_realm(world, RealmState.STOPPED, running=False)
    stale = conmon_pidfile(world.paths, REALM)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("999999\n", encoding="utf-8")

    argv = start_service(
        env,
        world.fake,
        REALM,
        global_argv=GLOBAL_ARGV,
        session=user,
        hooks=hooks_for(helper, sleeps),
    )

    assert argv == [str(helper), str(stale), *GLOBAL_ARGV, "--", record.container_id]
    assert not stale.exists(), "the stale conmon pidfile is removed before podman starts"
    assert "start" in ops_of(world)
    assert stored(world).state is RealmState.RUNNING
    assert audit_ops(env) == ["start"]


@pytest.mark.fr("FR-084")
def test_start_runs_the_grant_attachment_of_the_start_before_it_returns(
    world: World, env, helper: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    start_service(
        env,
        world.fake,
        REALM,
        global_argv=GLOBAL_ARGV,
        session=user,
        hooks=hooks_for(helper, sleeps),
    )
    record = stored(world)
    assert record.state is RealmState.RUNNING
    assert record.share_skipped, "the Basic grants are planned and each skip is recorded"


@pytest.mark.fr("FR-081")
def test_a_realm_that_podman_already_runs_keeps_its_pidfile(
    world: World, env, helper: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    record = services_realm(world, RealmState.RUNNING, running=True)
    live = conmon_pidfile(world.paths, REALM)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(f"{os.getpid()}\n", encoding="utf-8")

    argv = start_service(
        env,
        world.fake,
        REALM,
        global_argv=GLOBAL_ARGV,
        session=user,
        hooks=hooks_for(helper, sleeps),
    )

    assert argv[-1] == record.container_id
    assert live.exists(), "a live conmon pidfile is not removed when nothing is started"
    assert "start" not in ops_of(world)


@pytest.mark.fr("FR-081")
def test_a_missing_helper_refuses_the_start_before_podman_starts(
    world: World, env, tmp_path: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    absent = tmp_path / "no-such-ginn-wait"
    with pytest.raises(GinnError) as caught:
        start_service(
            env,
            world.fake,
            REALM,
            global_argv=GLOBAL_ARGV,
            session=user,
            hooks=hooks_for(absent, sleeps),
        )
    assert caught.value.code == "GINN-E-INTERNAL"
    assert "start" not in ops_of(world)
    assert stored(world).state is RealmState.STOPPED


@pytest.mark.fr("FR-081")
def test_a_relative_podman_path_refuses_the_start_before_podman_starts(
    world: World, env, helper: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    with pytest.raises(GinnError) as caught:
        start_service(
            env,
            world.fake,
            REALM,
            global_argv=["podman", "--root", "/data/podman"],
            session=user,
            hooks=hooks_for(helper, sleeps),
        )
    assert caught.value.code == "GINN-E-INTERNAL"
    assert "start" not in ops_of(world)


@pytest.mark.fr("FR-081")
def test_the_supervisor_starts_only_a_services_on_realm(
    world: World, env, helper: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False, services=False)
    with pytest.raises(GinnError) as caught:
        start_service(
            env,
            world.fake,
            REALM,
            global_argv=GLOBAL_ARGV,
            session=user,
            hooks=hooks_for(helper, sleeps),
        )
    assert caught.value.code == "GINN-E-INTERNAL"
    assert "start" not in ops_of(world)


@pytest.mark.fr("FR-081")
def test_a_stop_request_records_the_realm_stopped_and_clears_the_crash_history(
    world: World, env, helper: Path, sleeps: list[float]
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)
    log = crash_log(world.paths, REALM)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("[1.0, 2.0]", encoding="utf-8")

    outcome = finish_service(env, REALM, exit_code=0, signal_no=0, hooks=hooks_for(helper, sleeps))

    assert outcome is ServiceExit.STOPPED
    assert stored(world).state is RealmState.STOPPED
    assert not log.exists()
    assert sleeps == [], "a stop request does not wait"
    assert audit_ops(env) == ["stop"]


@pytest.mark.fr("NFR-033")
def test_a_crash_waits_the_restart_delay_and_leaves_the_realm_running(
    world: World, env, helper: Path, sleeps: list[float]
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)

    outcome = finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks_for(helper, sleeps))

    assert outcome is ServiceExit.RESTART
    assert sleeps == [5.0]
    assert stored(world).state is RealmState.RUNNING
    assert crash_log(world.paths, REALM).exists()


@pytest.mark.fr("AS-028")
def test_five_crashes_within_sixty_seconds_give_the_realm_up(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)
    hooks = hooks_for(helper, sleeps)
    outcomes = []
    for offset in (0.0, 10.0, 20.0, 30.0, 40.0):
        clock.set(T0 + offset)
        outcomes.append(finish_service(env, REALM, exit_code=-1, signal_no=9, hooks=hooks))

    assert outcomes == [ServiceExit.RESTART] * 4 + [ServiceExit.GAVE_UP]
    assert sleeps == [5.0] * 4, "the fifth crash does not wait: runsv is told to stop"
    assert (service_dir(world.paths, REALM) / DOWN).is_file()
    assert os.read(reader_of_control, 8) == b"d"
    assert stored(world).state is RealmState.FAILED
    assert not crash_log(world.paths, REALM).exists()
    last = last_audit(env)
    assert (last.op, last.outcome) == ("service-exit", "ok")
    assert last.params["outcome"] == "gave-up"


@pytest.mark.fr("AS-028")
def test_five_crashes_spanning_more_than_sixty_seconds_do_not_give_up(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)
    hooks = hooks_for(helper, sleeps)
    outcomes = []
    for offset in (0.0, 15.0, 30.0, 45.0, 61.0):
        clock.set(T0 + offset)
        outcomes.append(finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks))

    assert outcomes == [ServiceExit.RESTART] * 5
    assert sleeps == [5.0] * 5
    assert not (service_dir(world.paths, REALM) / DOWN).exists()
    assert stored(world).state is RealmState.RUNNING
    assert os.read(reader_of_control, 8) == b""  # no `d` reached runsv: nothing was given up


@pytest.mark.fr("AS-028")
def test_a_give_up_of_a_stopped_realm_writes_down_and_leaves_the_state_stopped(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    hooks = hooks_for(helper, sleeps)
    for offset in (0.0, 5.0, 10.0, 15.0, 20.0):
        clock.set(T0 + offset)
        last = finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks)
    assert last is ServiceExit.GAVE_UP
    assert (service_dir(world.paths, REALM) / DOWN).is_file()
    assert stored(world).state is RealmState.STOPPED, "DR-020 has no stopped-to-failed move"
    assert last_audit(env).params["state_kept"] == "stopped"


@pytest.mark.fr("FR-019")
def test_deleting_a_stopped_services_realm_removes_its_service_directory(world: World, env) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)

    delete_ops.delete_realm(env, world.fake, REALM)

    assert not service_dir(world.paths, REALM).exists()
    assert not conmon_pidfile(world.paths, REALM).exists()


def _set_autostart_flag(world: World, flag: bool) -> None:
    """Store the realm's autostart flag, as a `ginn autostart` would, without the operation."""
    store = RealmStore(world.paths)
    record = store.load(REALM)
    store.save(replace(record, settings=replace(record.settings, autostart=flag)))


@pytest.mark.fr("FR-084")
@pytest.mark.fr("DR-012")
def test_the_supervised_start_makes_the_down_file_match_autostart_under_the_lock(
    world: World, env, helper: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    _set_autostart_flag(world, True)
    down = service_dir(world.paths, REALM) / DOWN
    assert down.exists()

    start_service(
        env,
        world.fake,
        REALM,
        global_argv=GLOBAL_ARGV,
        session=user,
        hooks=hooks_for(helper, sleeps),
    )

    assert not down.exists(), "autostart on removes the down file at the supervised start"
    assert audit_ops(env) == ["start"]


@pytest.mark.fr("AS-028")
@pytest.mark.fr("FR-084")
def test_a_give_up_leaves_down_and_the_next_supervised_start_undoes_it_for_autostart(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
    user: HostUser,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    _set_autostart_flag(world, True)
    hooks = hooks_for(helper, sleeps)
    for offset in (0.0, 5.0, 10.0, 15.0, 20.0):
        clock.set(T0 + offset)
        finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks)
    down = service_dir(world.paths, REALM) / DOWN
    assert down.exists(), "the give-up writes down, so runit stops restarting the realm"
    assert stored(world).settings.autostart is True

    start_service(
        env,
        world.fake,
        REALM,
        global_argv=GLOBAL_ARGV,
        session=user,
        hooks=hooks,
    )

    assert not down.exists()


@pytest.mark.fr("FR-083")
def test_the_same_services_mode_is_a_no_op_and_a_change_is_refused_until_t83(
    world: World, env
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    before = stored(world)
    assert set_services_mode(env, REALM, on=True) == before
    assert audit_ops(env) == []

    with pytest.raises(GinnError) as caught:
        set_services_mode(env, REALM, on=False)
    assert caught.value.code == "GINN-E-NOT-SUPPORTED-YET"
    assert "T8.3" in caught.value.message
    assert stored(world) == before
    assert audit_ops(env) == []
    assert env.journal.pending() == []


@pytest.mark.fr("FR-084")
@pytest.mark.fr("DR-012")
def test_autostart_on_writes_the_flag_and_removes_the_down_file_under_one_audit_entry(
    world: World, env
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    down = service_dir(world.paths, REALM) / DOWN

    record = set_autostart(env, REALM, on=True)

    assert record.settings.autostart is True
    assert stored(world).settings.autostart is True
    assert not down.exists()
    assert audit_ops(env) == ["autostart"]
    assert last_audit(env).outcome == "ok"
    assert env.journal.pending() == []


@pytest.mark.fr("FR-084")
@pytest.mark.fr("DR-012")
def test_autostart_off_creates_the_down_file_and_saves_the_flag(world: World, env) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    _set_autostart_flag(world, True)
    set_autostart(env, REALM, on=True)
    down = service_dir(world.paths, REALM) / DOWN
    assert not down.exists()

    set_autostart(env, REALM, on=False)

    assert down.is_file()
    assert stored(world).settings.autostart is False


@pytest.mark.fr("DR-012")
def test_autostart_on_a_services_off_realm_is_refused_before_any_write(world: World, env) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False, services=False)
    before = stored(world)

    with pytest.raises(GinnError) as caught:
        set_autostart(env, REALM, on=True)

    assert caught.value.code == "GINN-E-OPTION-INVALID"
    assert "DR-012" in caught.value.details["rule"]
    assert stored(world) == before
    assert audit_ops(env) == []


@pytest.mark.fr("DR-020")
@pytest.mark.parametrize("state", [RealmState.CREATING, RealmState.DELETING, RealmState.BROKEN])
def test_autostart_of_a_realm_being_created_deleted_or_broken_is_refused_before_any_write(
    world: World, env, state: RealmState
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, state, running=False)
    record_file = world.paths.realms / "lab.json"
    before = record_file.read_bytes()
    down = service_dir(world.paths, REALM) / DOWN
    existed = down.exists()

    with pytest.raises(GinnError) as caught:
        set_autostart(env, REALM, on=True)

    assert caught.value.code == "GINN-E-REALM-STATE"
    assert record_file.read_bytes() == before
    assert down.exists() == existed
    assert audit_ops(env) == []


@pytest.mark.fr("FR-084")
def test_autostart_with_no_service_directory_is_service_missing_and_saves_no_flag(
    world: World, env
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    shutil.rmtree(service_dir(world.paths, REALM))

    with pytest.raises(GinnError) as caught:
        set_autostart(env, REALM, on=True)

    assert caught.value.code == "GINN-E-SERVICE-MISSING"
    assert stored(world).settings.autostart is False
