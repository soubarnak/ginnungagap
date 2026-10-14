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
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from ginn.domain import RealmName, RealmState
from ginn.domain.states import IllegalTransition
from ginn.errors import GinnError
from ginn.errors import Warning as GinnWarning
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
from ginn.podman.models import ContainerState
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.services.runit_user import (
    DOWN,
    conmon_pidfile,
    crash_log,
    service_dir,
    write_service_dir,
)
from ginn.services.supervisor import Result, SupervisorHooks
from ginn.store.journal import Journal
from ginn.store.locks import realm_lock
from ginn.store.records import RealmStore
from tests.fakes.clock import FakeClock
from tests.unit.realm_cli_support import DIGEST, World, build_world, record_of
from tests.unit.test_ops_delete import kill_journal_owner

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

UP_STATUS = "run: /x/lab: (pid 4321) 2s\n"
"""The `sv status` of a realm that runs, or that a crash has restarted: no `want down`."""
DOWN_STATUS = "run: /x/lab: (pid 4321) 2s, want down\n"
"""The `sv status` after `sv down`: runsv carries the `want down` mark (CL-24)."""

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


def supervisor_saying(line: str) -> SupervisorHooks:
    """The sv control, with `sv status` answering `line` and no sv process run (FR-081)."""

    def run(argv: Sequence[str], timeout: float) -> Result:
        return Result(0, line, "")

    return SupervisorHooks(sv="/usr/bin/sv", run=run)


def hooks_for(helper: Path, sleeps: list[float], status: str = UP_STATUS) -> ServiceHooks:
    """Hooks where no host path exists, so the Basic grants all skip and no helper runs.

    `status` is what `sv status` says about the realm's runsv: UP_STATUS or DOWN_STATUS.
    """
    lifecycle = LifecycleHooks(exists=lambda _path: False)
    return ServiceHooks(
        lifecycle=lifecycle,
        wait_helper=str(helper),
        sleep=sleeps.append,
        supervisor=supervisor_saying(status),
    )


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
@pytest.mark.fr("NFR-033")
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
    assert last.params["state"] == "failed (was running)"


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
@pytest.mark.fr("DR-020")
@pytest.mark.fr("FR-124")
def test_a_give_up_of_a_stopped_realm_writes_down_and_sets_failed(
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
    assert os.read(reader_of_control, 8) == b"d"
    assert stored(world).state is RealmState.FAILED, "CL-24: the give-up moves stopped to failed"
    assert last_audit(env).params["state"] == "failed (was stopped)"


@pytest.mark.fr("FR-081")
def test_four_start_phase_crashes_within_sixty_seconds_do_not_give_up(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    hooks = hooks_for(helper, sleeps)
    outcomes = []
    for offset in (0.0, 10.0, 20.0, 30.0):
        clock.set(T0 + offset)
        outcomes.append(finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks))

    assert outcomes == [ServiceExit.RESTART] * 4
    assert sleeps == [5.0] * 4
    assert stored(world).state is RealmState.STOPPED
    assert not (service_dir(world.paths, REALM) / DOWN).exists()
    assert os.read(reader_of_control, 8) == b""


@pytest.mark.fr("FR-081")
@pytest.mark.fr("NFR-033")
def test_five_start_phase_crashes_spread_past_sixty_seconds_do_not_give_up(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    hooks = hooks_for(helper, sleeps)
    outcomes = []
    for offset in (0.0, 15.0, 30.0, 45.0, 61.0):
        clock.set(T0 + offset)
        outcomes.append(finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks))

    assert outcomes == [ServiceExit.RESTART] * 5
    assert stored(world).state is RealmState.STOPPED
    assert not (service_dir(world.paths, REALM) / DOWN).exists()
    assert os.read(reader_of_control, 8) == b""


@pytest.mark.fr("FR-081")
@pytest.mark.fr("DR-020")
def test_a_crash_loop_the_supervisor_starts_without_a_user_command_gives_failed(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    hooks = hooks_for(helper, sleeps)
    outcomes = []
    for offset in (0.0, 1.0, 2.0, 3.0, 4.0):
        clock.set(T0 + offset)
        outcomes.append(finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks))

    assert outcomes == [ServiceExit.RESTART] * 4 + [ServiceExit.GAVE_UP]
    assert stored(world).state is RealmState.FAILED
    assert os.read(reader_of_control, 8) == b"d"
    assert "start" not in ops_of(world), "no user command started the realm"
    assert audit_ops(env) == ["service-exit"], "only the give-up is audited"


@pytest.mark.fr("FR-081")
def test_a_give_up_from_failed_keeps_the_state_and_says_so(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.FAILED, running=False)
    hooks = hooks_for(helper, sleeps)
    for offset in (0.0, 1.0, 2.0, 3.0, 4.0):
        clock.set(T0 + offset)
        last = finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks)

    assert last is ServiceExit.GAVE_UP
    assert stored(world).state is RealmState.FAILED
    assert last_audit(env).params["state_kept"] == "failed"
    assert "state" not in last_audit(env).params


@pytest.mark.fr("FR-081")
def test_a_want_down_stop_with_exit_zero_records_the_realm_stopped_and_counts_no_crash(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)
    log = crash_log(world.paths, REALM)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("[1.0, 2.0, 3.0, 4.0]", encoding="utf-8")
    hooks = hooks_for(helper, sleeps, DOWN_STATUS)
    clock.set(T0 + 5.0)

    outcome = finish_service(env, REALM, exit_code=0, signal_no=0, hooks=hooks)

    assert outcome is ServiceExit.STOPPED
    assert stored(world).state is RealmState.STOPPED
    assert not log.exists(), "a stop request clears the crash history"
    assert sleeps == [], "a user stop does not wait the restart delay"
    assert not (service_dir(world.paths, REALM) / DOWN).exists()
    assert os.read(reader_of_control, 8) == b""
    assert audit_ops(env) == ["stop"]


@pytest.mark.fr("FR-081")
def test_a_want_down_end_with_exit_one_is_conmon_gone_and_records_the_realm_stopped(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    # ginn-wait 1: conmon exited on its own during a stop request, so the container is gone.
    services_realm(world, RealmState.RUNNING, running=True)
    log = crash_log(world.paths, REALM)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("[1.0, 2.0, 3.0, 4.0]", encoding="utf-8")
    hooks = hooks_for(helper, sleeps, DOWN_STATUS)
    outcomes = []
    for offset in (5.0, 6.0, 7.0, 8.0, 9.0, 10.0):
        clock.set(T0 + offset)
        outcomes.append(finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks))

    assert outcomes == [ServiceExit.STOPPED] * 6, "six stop requests never give up"
    assert stored(world).state is RealmState.STOPPED
    assert not log.exists(), "a stop request clears the crash history"
    assert sleeps == [], "a user stop does not wait the restart delay"
    assert not (service_dir(world.paths, REALM) / DOWN).exists()
    assert os.read(reader_of_control, 8) == b""
    assert "service-exit" not in audit_ops(env)


@pytest.mark.fr("FR-081")
@pytest.mark.parametrize("code", [-1, 2, 3, 4, 5])
def test_a_want_down_stop_with_another_code_keeps_the_state_counts_no_crash_and_logs(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
    capsys: pytest.CaptureFixture[str],
    code: int,
) -> None:  # type: ignore[no-untyped-def]
    # ginn-wait 4 means `podman stop` failed while conmon still runs, so the record must not move.
    services_realm(world, RealmState.RUNNING, running=True)
    log = crash_log(world.paths, REALM)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("[1.0, 2.0, 3.0, 4.0]", encoding="utf-8")
    hooks = hooks_for(helper, sleeps, DOWN_STATUS)
    outcomes = []
    for offset in (5.0, 6.0, 7.0, 8.0, 9.0, 10.0):
        clock.set(T0 + offset)
        outcomes.append(
            finish_service(
                env, REALM, exit_code=code, signal_no=9 if code == -1 else 0, hooks=hooks
            )
        )

    assert outcomes == [ServiceExit.STOPPED] * 6, "six want-down ends never give up"
    assert stored(world).state is RealmState.RUNNING, "the record keeps its state"
    assert not log.exists(), "a stop request clears the crash history"
    assert sleeps == [], "a user stop does not wait the restart delay"
    assert not (service_dir(world.paths, REALM) / DOWN).exists()
    assert os.read(reader_of_control, 8) == b""
    err = capsys.readouterr().err
    assert "GINN-W-STOP-UNSETTLED" in err, "the runit log carries the kept state"
    assert f"exit {code}" in err, "the warning names the exit code"
    assert "service-exit" not in audit_ops(env)
    assert "stop" not in audit_ops(env), "no stop was recorded"


@pytest.mark.fr("FR-081")
def test_the_kept_state_is_a_warning_on_the_callers_list_and_not_on_stderr(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    capsys: pytest.CaptureFixture[str],
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)
    hooks = hooks_for(helper, sleeps, DOWN_STATUS)
    notices: list[GinnWarning] = []

    outcome = finish_service(env, REALM, exit_code=4, signal_no=0, hooks=hooks, warnings=notices)

    assert outcome is ServiceExit.STOPPED
    assert [notice.code for notice in notices] == ["GINN-W-STOP-UNSETTLED"]
    assert "exit 4" in notices[0].message
    assert "ginn stop lab" in notices[0].remedy
    assert capsys.readouterr().err == "", "the CLI prints the list, so stderr stays quiet"


@pytest.mark.fr("DR-020")
def test_the_store_refuses_stopped_to_failed_without_give_up_and_writes_nothing(
    world: World,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    store = RealmStore(world.paths)
    before = store.load(REALM)

    with pytest.raises(IllegalTransition):
        store.set_state(REALM, RealmState.FAILED)
    assert store.load(REALM) == before, "a refused move writes nothing"

    moved = store.set_state(REALM, RealmState.FAILED, give_up=True)
    assert moved.state is RealmState.FAILED
    assert store.load(REALM).state is RealmState.FAILED


@pytest.mark.fr("FR-081")
def test_a_status_that_cannot_be_read_counts_the_end_as_a_crash(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)

    def unreadable(argv: Sequence[str], timeout: float) -> Result:
        return Result(1, "", "warning: /x/lab: unable to open supervise/ok: file does not exist\n")

    hooks = replace(
        hooks_for(helper, sleeps),
        supervisor=SupervisorHooks(sv="/usr/bin/sv", run=unreadable),
    )
    outcome = finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks)

    assert outcome is ServiceExit.RESTART, "no proof of a stop: the crash is counted"
    assert sleeps == [5.0]


@pytest.mark.fr("FR-081")
@pytest.mark.fr("FR-124")
def test_a_crash_while_running_gives_failed_and_the_audit_names_the_move(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)
    hooks = hooks_for(helper, sleeps)
    for offset in (0.0, 1.0, 2.0, 3.0, 4.0):
        clock.set(T0 + offset)
        finish_service(env, REALM, exit_code=-1, signal_no=9, hooks=hooks)

    assert stored(world).state is RealmState.FAILED
    assert last_audit(env).params["state"] == "failed (was running)"


@pytest.mark.fr("FR-081")
def test_a_stop_request_during_the_crash_window_restarts_the_count(
    world: World,
    env,
    helper: Path,
    sleeps: list[float],
    clock: FakeClock,
    reader_of_control: int,
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)
    hooks = hooks_for(helper, sleeps)
    for offset in (0.0, 1.0, 2.0, 3.0):
        clock.set(T0 + offset)
        finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks)
    clock.set(T0 + 4.0)
    finish_service(env, REALM, exit_code=0, signal_no=0, hooks=hooks)
    assert not crash_log(world.paths, REALM).exists()

    clock.set(T0 + 5.0)
    outcomes = [finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks) for _ in range(4)]

    assert outcomes == [ServiceExit.RESTART] * 4
    assert stored(world).state is RealmState.STOPPED
    assert not (service_dir(world.paths, REALM) / DOWN).exists()


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
def test_the_same_services_mode_is_a_no_op_and_a_change_is_recorded_for_the_next_start(
    world: World, env
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False)
    before = stored(world)
    assert set_services_mode(env, REALM, on=True) == before
    assert audit_ops(env) == []

    record = set_services_mode(env, REALM, on=False)
    assert record.settings.services is False
    assert record.settings.autostart is False
    assert stored(world).settings.services is False
    assert stored(world).services_at_create is True, "the container keeps the mode it was made with"
    assert stored(world).restart_required is False
    assert audit_ops(env) == ["services"]
    assert env.journal.pending() == []
    assert world.fake.calls == [], "a stopped realm's change calls no podman"


@pytest.mark.fr("FR-083")
def test_turning_services_off_with_no_service_directory_is_already_down_and_is_recorded(
    world: World, env
) -> None:  # type: ignore[no-untyped-def]
    """A service directory that is gone counts as already down, so the change is recorded and no
    GINN-E-SERVICE-MISSING is raised (FR-083)."""
    services_realm(world, RealmState.STOPPED, running=False)
    shutil.rmtree(service_dir(world.paths, REALM))

    record = set_services_mode(env, REALM, on=False)

    assert record.settings.services is False
    assert stored(world).settings.services is False
    assert stored(world).services_at_create is True, "the container keeps the mode it was made with"
    assert not service_dir(world.paths, REALM).exists()
    assert audit_ops(env) == ["services"]


@pytest.mark.fr("FR-083")
@pytest.mark.fr("DR-012")
@pytest.mark.parametrize("autostart", [True, False])
def test_turning_services_off_and_back_before_a_start_restores_autostart_and_its_down_file(
    world: World, env, autostart: bool
) -> None:  # type: ignore[no-untyped-def]
    """A change cancelled before any start leaves the realm as it was: the autostart flag that
    turning services off displaced comes back, and the `down` file follows it (FR-083, DR-012)."""
    services_realm(world, RealmState.STOPPED, running=False)
    set_autostart(env, REALM, on=autostart)
    down = service_dir(world.paths, REALM) / DOWN
    assert down.exists() is not autostart

    set_services_mode(env, REALM, on=False)
    assert stored(world).settings.autostart is False, "autostart needs services on (DR-012)"
    assert stored(world).autostart_before_off is autostart
    assert down.exists()

    warnings: list[GinnWarning] = []
    record = set_services_mode(env, REALM, on=True, warnings=warnings)

    assert record.settings.services is True
    assert record.settings.autostart is autostart
    assert down.exists() is not autostart
    assert stored(world).autostart_before_off is None
    assert warnings == [], "a cancelled change is not pending, so it has no rebuild notice"
    assert world.fake.calls == [], "a stopped realm's change calls no podman"


@pytest.mark.fr("FR-083")
def test_turning_services_off_while_running_marks_the_restart_and_writes_the_down_file(
    world: World, env
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)
    set_autostart(env, REALM, on=True)
    record = set_services_mode(env, REALM, on=False)
    assert record.restart_required is True
    assert record.state is RealmState.RUNNING
    assert (service_dir(world.paths, REALM) / DOWN).is_file(), "autostart off: no boot start"
    assert world.fake.calls == [], "the running container is left to its next stop and start"


@pytest.mark.fr("FR-083")
def test_turning_services_on_writes_no_runit_file_before_the_rebuild(world: World, env) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False, services=False)
    shutil.rmtree(service_dir(world.paths, REALM))
    record = set_services_mode(env, REALM, on=True)
    assert record.settings.services is True
    assert record.services_at_create is False
    assert not service_dir(world.paths, REALM).exists(), "the rebuild writes the directory"


@pytest.mark.fr("FR-083")
def test_a_services_change_is_refused_for_a_realm_that_is_not_stopped_or_running(
    world: World, env
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.CREATING, running=False)
    with pytest.raises(GinnError) as caught:
        set_services_mode(env, REALM, on=False)
    assert caught.value.code == "GINN-E-REALM-STATE"
    assert stored(world).settings.services is True
    (entry,) = [e for e in env.audit.read() if e.op == "services"]
    assert entry.error_code == "GINN-E-REALM-STATE"


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


@pytest.mark.fr("FR-081")
@pytest.mark.parametrize("holder", ["stop", "delete"])
def test_a_finish_that_finds_a_stop_or_delete_holding_the_lock_writes_nothing_and_exits_zero(
    world: World, env, helper: Path, sleeps: list[float], clock: FakeClock, holder: str
) -> None:  # type: ignore[no-untyped-def]
    """The stop or delete runs sv down under the lock and records the stop itself (ADR-003)."""
    services_realm(world, RealmState.RUNNING, running=True)
    crash = crash_log(world.paths, REALM)
    crash.parent.mkdir(parents=True, exist_ok=True)
    crash.write_text("[]", encoding="utf-8")
    before = (world.paths.realms / "lab.json").read_bytes()

    with realm_lock(world.paths, REALM, holder):
        result = finish_service(
            env, REALM, exit_code=0, signal_no=0, hooks=hooks_for(helper, sleeps)
        )

    assert result is ServiceExit.STOPPED
    assert (world.paths.realms / "lab.json").read_bytes() == before
    assert not crash.exists()
    assert [entry.op for entry in env.audit.read() if entry.op != "integrity"] == []


@pytest.mark.fr("FR-081")
def test_a_finish_that_finds_another_operation_holding_the_lock_is_refused_as_before(
    world: World, env, helper: Path, sleeps: list[float], clock: FakeClock
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.RUNNING, running=True)

    with realm_lock(world.paths, REALM, "update"):
        with pytest.raises(GinnError) as caught:
            finish_service(env, REALM, exit_code=0, signal_no=0, hooks=hooks_for(helper, sleeps))

    assert caught.value.code == "GINN-E-REALM-BUSY"
    assert stored(world).state is RealmState.RUNNING


def rebuilding_realm(world: World, *, building: bool) -> str | None:  # type: ignore[no-untyped-def]
    """A services-on realm that reads rebuilding (ops/rebuild.py), with the container it builds.

    The building container exists and is not running, as a rebuild leaves it before the runit start.
    With `building` False the record names no building container.
    """
    base = services_realm(world, RealmState.STOPPED, running=False)
    labels = {LABEL_REALM: "lab", LABEL_DISTRO: "debian", LABEL_GENERATION: "2"}
    spec = CreateSpec(name="ginn-lab-g2", image_digest_ref=DIGEST, network="host", labels=labels)
    cid = world.fake.create(spec)
    rebuilding = replace(
        base,
        state=RealmState.REBUILDING,
        previous_state=RealmState.STOPPED,
        building_container_id=cid if building else None,
        building_generation=2 if building else None,
    )
    RealmStore(world.paths).save(rebuilding)
    if building:
        # The rebuild that builds the container is alive: its journal entry names this process.
        Journal(world.paths).begin(
            "rebuild", REALM, {"distro": "debian", "generation": 1, "new_generation": 2}
        )
    world.fake.calls.clear()
    return cid if building else None


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-081")
@pytest.mark.fr("FR-083")
def test_a_supervised_start_during_a_rebuild_starts_the_building_container_and_writes_nothing(
    world: World, env, helper: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    """The rebuild holds the realm lock and the record while its runit `up` runs `_service-run`.
    The start takes no lock. It starts the container the rebuild is building, not the one the
    record names, and then it writes nothing (ADR-003). That is the one exception to decision 2 of
    the start amendment."""
    cid = rebuilding_realm(world, building=True)
    before = (world.paths.realms / "lab.json").read_bytes()
    with realm_lock(world.paths, REALM, "rebuild"):
        argv = start_service(
            env,
            world.fake,
            REALM,
            global_argv=GLOBAL_ARGV,
            session=user,
            hooks=hooks_for(helper, sleeps),
        )
    assert argv == [str(helper), str(conmon_pidfile(world.paths, REALM)), *GLOBAL_ARGV, "--", cid]
    assert ops_of(world) == ["start", "inspect"]
    assert world.fake.containers[cid].state is ContainerState.RUNNING
    assert (world.paths.realms / "lab.json").read_bytes() == before
    assert audit_ops(env) == []


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-122")
def test_a_runsv_start_after_a_killed_rebuild_starts_nothing_before_recovery(
    world: World, env, helper: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    """A killed rebuild leaves its record at rebuilding and its journal entry dead. When runsv
    restarts the service before recovery has run, `_service-run` must not start the building
    container, because no live rebuild stands behind it (ops/service.py)."""
    cid = rebuilding_realm(world, building=True)
    kill_journal_owner(env, REALM.value)
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
    assert ops_of(world) == []
    assert world.fake.containers[cid].state is not ContainerState.RUNNING
    assert stored(world).state is RealmState.REBUILDING


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-122")
def test_a_runsv_start_of_a_building_container_without_a_rebuild_journal_entry_starts_nothing(
    world: World, env, helper: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    cid = rebuilding_realm(world, building=True)
    (world.paths.journal / f"{REALM.value}.json").unlink()
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
    assert ops_of(world) == []
    assert world.fake.containers[cid].state is not ContainerState.RUNNING


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-081")
def test_a_supervised_start_during_a_rebuild_that_names_no_container_is_refused_before_podman(
    world: World, env, helper: Path, sleeps: list[float], user: HostUser
) -> None:  # type: ignore[no-untyped-def]
    rebuilding_realm(world, building=False)
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
    assert ops_of(world) == []
    assert stored(world).state is RealmState.REBUILDING


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-081")
def test_a_finish_during_a_rebuild_writes_no_record_for_a_stop_or_a_give_up(
    world: World, env, helper: Path, sleeps: list[float], clock: FakeClock
) -> None:  # type: ignore[no-untyped-def]
    """While the record reads rebuilding, the end of run writes no record (ops/service.py). A stop
    and a give-up leave the record alone, and the supervisor's own files still record what runsv was
    told: a give-up writes `down`, and the rebuild, which holds the record, fails on it."""
    rebuilding_realm(world, building=True)
    before = (world.paths.realms / "lab.json").read_bytes()
    stop = finish_service(env, REALM, exit_code=0, signal_no=0, hooks=hooks_for(helper, sleeps))
    assert stop is ServiceExit.STOPPED
    crash = crash_log(world.paths, REALM)
    crash.parent.mkdir(parents=True, exist_ok=True)
    crash.write_text("[]", encoding="utf-8")
    for offset in (0.0, 10.0, 20.0, 30.0, 40.0):
        clock.set(T0 + offset)
        last = finish_service(
            env, REALM, exit_code=-1, signal_no=9, hooks=hooks_for(helper, sleeps)
        )
    assert last is ServiceExit.GAVE_UP
    assert (service_dir(world.paths, REALM) / DOWN).is_file()
    assert (world.paths.realms / "lab.json").read_bytes() == before
    assert audit_ops(env) == []


@pytest.mark.fr("DR-012")
def test_autostart_off_on_a_services_off_realm_is_refused_before_any_write(
    world: World, env
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.STOPPED, running=False, services=False)
    before = stored(world)

    with pytest.raises(GinnError) as caught:
        set_autostart(env, REALM, on=False)

    assert caught.value.code == "GINN-E-OPTION-INVALID"
    assert stored(world) == before
    assert audit_ops(env) == []


@pytest.mark.fr("DR-020")
def test_the_autostart_refusal_names_the_states_it_accepts_without_the_updating_ones(
    world: World, env
) -> None:  # type: ignore[no-untyped-def]
    services_realm(world, RealmState.BROKEN, running=False)

    with pytest.raises(GinnError) as caught:
        set_autostart(env, REALM, on=True)

    assert caught.value.code == "GINN-E-REALM-STATE"
    assert "stopped, running or failed" in caught.value.message
    assert "updating" not in caught.value.message
