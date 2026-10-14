# SPDX-License-Identifier: Apache-2.0
"""The relay service `_exec`, its runtime paths and its call points (FR-084, FR-082, FR-123).

services/exec_relay.py against an OwnedPaths under tmp_path, from realm_cli_support. PodmanCli is
built over that host, and only its argv and environment are read, so no podman runs. A recorder
stands in for sv: it keeps each argv and answers with the reply the test set. No runsv, relay or
podman runs here.

The call points (ops/create, delete, service and lifecycle) are checked by replacing the sync in
the module that calls it with a recorder, and by running the operation itself. An adapter that is
not PodmanCli has no podman command to render, so it installs nothing (the lifecycle convention).
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import RealmName, RealmState
from ginn.errors import GinnError, make
from ginn.ops import create as create_ops
from ginn.ops import delete as delete_ops
from ginn.ops import lifecycle as lifecycle_ops
from ginn.ops import service as service_ops
from ginn.ops._mutating import OpEnv
from ginn.ops.lifecycle import LifecycleHooks
from ginn.ops.service import ServiceExit, ServiceHooks, finish_service
from ginn.podman.adapter import CreateSpec
from ginn.podman.cli import PodmanCli
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.services import exec_relay
from ginn.services.exec_relay import (
    EXEC_SERVICE,
    OWNED_ENV,
    SYNC_LOCK_NAME,
    TEMP_PREFIX,
    exec_lock_busy,
    exec_lock_dir,
    exec_lock_path,
    exec_orphan_path,
    exec_service_dir,
    exec_socket,
    relay_path,
    render,
    sync_exec_service,
    up_exec_service,
)
from ginn.services.runit_user import crash_log, write_service_dir
from ginn.services.supervise import write_crashes
from ginn.services.supervisor import DOWN_WAIT_S, UP_WAIT_S, Result, SupervisorHooks
from ginn.store.audit import AuditLog
from ginn.store.paths import check_runtime_dir
from ginn.store.records import RealmStore
from tests.fakes.clock import FakeClock
from tests.fakes.fake_podman import FakePodman
from tests.unit.realm_cli_support import DIGEST, World, build_world, make_realm, record_of
from tests.unit.test_ops_create import ScriptedPodman, hooks_for, request

REALM = RealmName("lab")
CREATED = RealmName("debian")
"""The realm name that request() gives a create (test_ops_create)."""
PROGRAM = "/usr/bin/ginn"
SV = "/usr/bin/sv"
RUN = "run"
DOWN = "down"


class SvRecorder:
    """A stand-in for sv (SupervisorHooks.run). It keeps each argv and answers with `reply`."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.reply = Result(0, "", "")

    def __call__(self, argv: Sequence[str], timeout: float) -> Result:
        self.calls.append(list(argv))
        return self.reply

    def verbs(self) -> list[str]:
        """The verb of each call. sv takes `-w <wait> <verb> <dir>`, so the verb is argv[3]."""
        return [call[3] for call in self.calls]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return build_world(tmp_path, monkeypatch)


@pytest.fixture
def cli(world: World) -> PodmanCli:
    return PodmanCli.from_paths(world.paths, binary="/usr/bin/podman")


@pytest.fixture
def sv() -> SvRecorder:
    return SvRecorder()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def hooks_of(sv: SvRecorder) -> SupervisorHooks:
    return SupervisorHooks(sv=SV, run=sv)


def _save(
    world: World, name: str, *, services: bool, state: RealmState = RealmState.STOPPED
) -> None:
    record = record_of(name, RealmState.STOPPED, None, services=services)
    if state in (RealmState.REBUILDING, RealmState.UPDATING):
        # DR-020: these states record the state they return to.
        record = replace(record, state=state, previous_state=RealmState.STOPPED)
    else:
        record = replace(record, state=state)
    RealmStore(world.paths).save(record)


def _forget(world: World, name: str) -> None:
    (world.paths.realms / f"{name}.json").unlink()


def _supervise(world: World) -> None:
    """runsv has made supervise/ok for the directory (runsv(8))."""
    supervise = exec_service_dir(world.paths) / "supervise"
    supervise.mkdir(exist_ok=True)
    (supervise / "ok").write_text("", encoding="utf-8")


def _sync(world: World, cli: PodmanCli, sv: SvRecorder) -> None:
    sync_exec_service(world.paths, cli, hooks=hooks_of(sv))


def _tree(root: Path) -> set[Path]:
    return set(root.rglob("*"))


def _record_syncs(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[tuple[Path, object]]:
    """Replace `module.sync_exec_service` with a recorder of (paths, podman), and return it."""
    seen: list[tuple[Path, object]] = []

    def recorder(paths: Any, podman: object, **_: object) -> None:
        seen.append((paths.runtime, podman))

    monkeypatch.setattr(module, "sync_exec_service", recorder)
    return seen


@pytest.mark.fr("FR-084")
def test_a_services_on_realm_installs_the_relay_service_with_its_run_script(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    _sync(world, cli, sv)
    service = exec_service_dir(world.paths)
    run = service / RUN
    assert run.read_text(encoding="utf-8") == render(world.paths, cli)
    assert stat.S_IMODE(run.stat().st_mode) == 0o755
    assert (service / DOWN).exists(), "no realm runs, so the relay stays down (NFR-001)"
    assert sv.calls == [], "runsvdir starts a new directory by itself"


@pytest.mark.fr("FR-084")
def test_a_second_sync_leaves_the_installed_script_in_place(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    _sync(world, cli, sv)
    run = exec_service_dir(world.paths) / RUN
    before = run.stat().st_ino
    _sync(world, cli, sv)
    assert run.stat().st_ino == before
    assert sorted(p.name for p in exec_service_dir(world.paths).iterdir()) == [DOWN, RUN]


@pytest.mark.fr("FR-084")
def test_the_service_is_marked_down_and_stopped_when_no_services_on_realm_remains(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    _sync(world, cli, sv)
    _supervise(world)
    _forget(world, "lab")
    _sync(world, cli, sv)
    service = exec_service_dir(world.paths)
    assert (service / DOWN).exists()
    assert sv.calls == [[SV, "-w", str(DOWN_WAIT_S), "down", str(service)]]


@pytest.mark.fr("FR-084")
def test_the_service_is_never_removed_when_the_last_services_on_realm_goes(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    _sync(world, cli, sv)
    _forget(world, "lab")
    _sync(world, cli, sv)
    assert (exec_service_dir(world.paths) / RUN).is_file()


@pytest.mark.fr("FR-084")
def test_down_is_cleared_and_the_service_is_brought_up_when_a_services_on_realm_returns(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    _sync(world, cli, sv)
    _supervise(world)
    _forget(world, "lab")
    _sync(world, cli, sv)
    _save(world, "lab", services=True, state=RealmState.RUNNING)
    _sync(world, cli, sv)
    assert not (exec_service_dir(world.paths) / DOWN).exists()
    assert sv.verbs() == ["down", "up"]
    assert sv.calls[1][:3] == [SV, "-w", str(UP_WAIT_S)]


@pytest.mark.fr("FR-084")
def test_a_changed_script_is_replaced_and_a_supervised_service_restarts(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True, state=RealmState.RUNNING)
    _sync(world, cli, sv)
    run = exec_service_dir(world.paths) / RUN
    run.write_text("#!/bin/sh\n# an older script\n", encoding="utf-8")
    _supervise(world)
    _sync(world, cli, sv)
    assert run.read_text(encoding="utf-8") == render(world.paths, cli)
    assert sv.verbs() == ["restart"]


@pytest.mark.fr("FR-082")
def test_a_services_off_host_never_writes_the_service_directory(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=False)
    _sync(world, cli, sv)
    assert not exec_service_dir(world.paths).exists()
    assert sv.calls == []


@pytest.mark.fr("FR-084")
def test_a_record_that_does_not_load_counts_as_services_on(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    world.paths.realms.mkdir(parents=True, exist_ok=True)
    (world.paths.realms / "broken.json").write_text("{not json", encoding="utf-8")
    _sync(world, cli, sv)
    assert (exec_service_dir(world.paths) / RUN).is_file()


@pytest.mark.fr("FR-084")
def test_the_script_exports_only_the_owned_keys_and_execs_the_relay_with_the_podman_prefix(
    world: World, cli: PodmanCli
) -> None:
    lines = render(world.paths, cli).splitlines()
    exported = [
        line.split("=", 1)[0].removeprefix("export ") for line in lines if "export " in line
    ]
    assert set(exported) == {*OWNED_ENV, "PATH"}
    assert "HOME" not in exported
    exec_line = lines[-1]
    assert exec_line.startswith("exec '/usr/libexec/ginn/ginn-exec-relay' ")
    assert exec_line.startswith(
        f"exec '/usr/libexec/ginn/ginn-exec-relay' '{world.paths.runtime}' '/usr/bin/podman' "
    ), "argv[1] is the runtime root, which the listener checks and binds exec.sock under"
    assert "'/usr/bin/podman' '--remote=false' '--root'" in exec_line


@pytest.mark.fr("FR-084")
def test_two_callers_with_different_paths_render_the_same_script(
    world: World, cli: PodmanCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/home/alice/.local/bin:/opt/tools/bin:/usr/bin")
    first_path = cli._env()["PATH"]
    first = render(world.paths, cli)
    monkeypatch.setenv("PATH", "/srv/other/bin:/usr/sbin")
    assert cli._env()["PATH"] != first_path, "the two callers really have different PATHs"
    assert render(world.paths, cli) == first


@pytest.mark.fr("FR-084")
def test_the_relay_path_is_the_podman_directory_then_the_system_directories(world: World) -> None:
    opt = PodmanCli.from_paths(world.paths, binary="/opt/podman/bin/podman")
    assert (
        "export PATH='/opt/podman/bin:/usr/local/bin:/usr/bin:/bin'"
        in render(world.paths, opt).splitlines()
    )
    system = PodmanCli.from_paths(world.paths, binary="/usr/bin/podman")
    assert "export PATH='/usr/bin:/usr/local/bin:/bin'" in render(world.paths, system).splitlines()


@pytest.mark.fr("FR-084")
def test_ginns_own_bin_directory_is_never_on_the_relay_path(world: World) -> None:
    binary = str(world.paths.bin / "podman")
    assert relay_path(world.paths, binary) == "/usr/local/bin:/usr/bin:/bin"
    in_bin = PodmanCli.from_paths(world.paths, binary=binary)
    assert "export PATH='/usr/local/bin:/usr/bin:/bin'" in render(world.paths, in_bin).splitlines()


@pytest.mark.fr("FR-084")
def test_a_relative_podman_path_is_refused_so_no_path_lookup_finds_podman(world: World) -> None:
    bare = PodmanCli.from_paths(world.paths, binary="podman")
    with pytest.raises(GinnError) as info:
        render(world.paths, bare)
    assert info.value.code == "GINN-E-INTERNAL"


@pytest.mark.fr("FR-084")
def test_a_value_with_a_line_break_is_refused_and_no_script_is_written(
    world: World, cli: PodmanCli, sv: SvRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(world, "lab", services=True)
    monkeypatch.setattr(cli, "global_argv", lambda: ["/usr/bin/podman", "a\nb"])
    with pytest.raises(GinnError) as info:
        _sync(world, cli, sv)
    assert info.value.code == "GINN-E-INTERNAL"
    assert not exec_service_dir(world.paths).exists()


@pytest.mark.fr("CR-006")
def test_the_sync_writes_only_under_the_supervision_directory(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    before = _tree(world.tmp)
    _sync(world, cli, sv)
    created = _tree(world.tmp) - before
    assert created, "the service directory is created"
    supervision = world.paths.sv
    assert all(supervision in path.parents for path in created)


@pytest.mark.fr("DR-010")
def test_the_relay_paths_sit_under_the_runtime_root_which_passes_the_runtime_check(
    world: World,
) -> None:
    runtime = world.paths.runtime
    relay_paths = (
        exec_socket(world.paths),
        exec_lock_dir(world.paths),
        exec_lock_path(world.paths, REALM),
        exec_orphan_path(world.paths, REALM),
    )
    for path in relay_paths:
        assert runtime in path.parents
    check_runtime_dir(runtime, os.getuid())


@pytest.mark.fr("FR-123")
def test_a_missing_lock_file_is_not_busy_and_the_check_creates_nothing(world: World) -> None:
    assert exec_lock_busy(world.paths, REALM) is False
    assert not exec_lock_dir(world.paths).exists()
    assert not exec_lock_path(world.paths, REALM).exists()


@pytest.mark.fr("FR-123")
def test_a_held_exclusive_lock_is_busy_until_its_holder_lets_go(world: World) -> None:
    exec_lock_dir(world.paths).mkdir(mode=0o700)
    lock = exec_lock_path(world.paths, REALM)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert exec_lock_busy(world.paths, REALM) is True
    finally:
        os.close(fd)
    assert exec_lock_busy(world.paths, REALM) is False
    assert lock.exists(), "the check never removes the lock file"


@pytest.mark.fr("FR-084")
def test_a_creator_that_loses_the_rename_race_updates_the_service_it_finds(
    world: World, cli: PodmanCli, sv: SvRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(world, "lab", services=True)
    text = render(world.paths, cli)

    def other_creator_wins(src: object, dst: object) -> None:
        """The other creator renames its directory into place first, so ours finds it present."""
        Path(str(dst)).mkdir(mode=0o700)
        (Path(str(dst)) / RUN).write_text(text, encoding="utf-8")
        raise OSError(errno.ENOTEMPTY, "Directory not empty", str(dst))

    monkeypatch.setattr(exec_relay.os, "rename", other_creator_wins)
    _sync(world, cli, sv)
    assert sorted(p.name for p in world.paths.sv.iterdir()) == [SYNC_LOCK_NAME, EXEC_SERVICE]
    assert (exec_service_dir(world.paths) / RUN).read_text(encoding="utf-8") == text
    assert sv.calls == []


@pytest.mark.fr("FR-084")
def test_a_failed_sv_restart_is_raised_as_a_service_control_error(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True, state=RealmState.RUNNING)
    _sync(world, cli, sv)
    (exec_service_dir(world.paths) / RUN).write_text("#!/bin/sh\n", encoding="utf-8")
    _supervise(world)
    sv.reply = Result(1, "", "fail: /x/_exec: unable to change state\n")
    with pytest.raises(GinnError) as info:
        _sync(world, cli, sv)
    assert info.value.code == "GINN-E-SERVICE-CONTROL"
    assert EXEC_SERVICE in info.value.message


@pytest.mark.fr("FR-084")
def test_a_down_that_finds_no_runsv_for_the_service_is_not_an_error(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    _sync(world, cli, sv)
    _supervise(world)
    _forget(world, "lab")
    sv.reply = Result(1, "", "fail: /x/_exec: runsv not running\n")
    _sync(world, cli, sv)
    assert (exec_service_dir(world.paths) / DOWN).exists()


@pytest.mark.fr("FR-084")
def test_an_adapter_that_is_not_podman_cli_installs_nothing(world: World, sv: SvRecorder) -> None:
    _save(world, "lab", services=True)
    sync_exec_service(world.paths, FakePodman(), hooks=hooks_of(sv))
    assert not exec_service_dir(world.paths).exists()
    assert sv.calls == []


@pytest.mark.fr("FR-084")
def test_set_services_mode_syncs_the_relay_with_the_adapter_it_is_given(
    world: World, cli: PodmanCli, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    seen = _record_syncs(monkeypatch, service_ops)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    make_realm(world, "lab", RealmState.STOPPED, running=False)
    service_ops.set_services_mode(env, REALM, on=True)
    service_ops.set_services_mode(env, REALM, on=False, podman=cli)
    assert seen == [(world.paths.runtime, None), (world.paths.runtime, cli)]


@pytest.mark.fr("FR-084")
def test_delete_syncs_the_relay_after_the_record_is_removed(
    world: World, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    removed_at_sync: list[bool] = []

    def recorder(paths: Any, podman: object, **_: object) -> None:
        removed_at_sync.append(not RealmStore(paths).exists(REALM))

    monkeypatch.setattr(delete_ops, "sync_exec_service", recorder)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    make_realm(world, "lab", RealmState.STOPPED, running=False)
    result = delete_ops.delete_realm(env, world.fake, REALM)
    assert removed_at_sync == [True]
    assert not any(w.code == "GINN-W-RELAY-SYNC" for w in result.warnings)


@pytest.mark.fr("FR-084")
def test_a_relay_failure_after_a_delete_is_a_warning_because_the_realm_is_gone(
    world: World, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    def failing(paths: Any, podman: object, **_: object) -> None:
        raise make("GINN-E-SERVICE-CONTROL", realm=EXEC_SERVICE, action="down", detail="boom")

    monkeypatch.setattr(delete_ops, "sync_exec_service", failing)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    make_realm(world, "lab", RealmState.STOPPED, running=False)
    result = delete_ops.delete_realm(env, world.fake, REALM)
    assert not RealmStore(world.paths).exists(REALM)
    assert [w.code for w in result.warnings] == ["GINN-W-RELAY-SYNC"]
    assert not any("relay service" in note for note in result.notes)


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_services_on_start_syncs_the_relay_once_the_realm_reads_running(
    world: World, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    store = RealmStore(world.paths)
    events: list[str] = []

    def recorder(paths: Any, podman: object, **_: object) -> None:
        events.append(f"sync:{store.load(REALM).state.value}")

    monkeypatch.setattr(lifecycle_ops, "sync_exec_service", recorder)
    _save(world, "lab", services=True)
    monkeypatch.setattr(lifecycle_ops, "refuse_before_supervisor", lambda *_a, **_k: None)

    def through_runit(env: object, podman: object, name: RealmName, hooks: object) -> Any:
        events.append("start")
        store.save(record_of("lab", RealmState.RUNNING, None, services=True))
        return store.load(name)

    monkeypatch.setattr(lifecycle_ops, "start_through_sv", through_runit)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    user = lifecycle_ops.HostUser(uid=os.getuid(), host_home=world.home, host_env={})
    lifecycle_ops.start_realm(env, world.fake, REALM, session=user, rebuild=False, warnings=[])
    assert events == ["start", "sync:running"]


@pytest.mark.fr("FR-082")
def test_a_services_off_create_writes_no_relay_service(world: World, clock: FakeClock) -> None:
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    fake = ScriptedPodman(root=str(world.tmp))
    hooks = hooks_for(world.tmp, [], world.tmp / "mountinfo", program=PROGRAM)
    create_ops.create_realm(env, fake, request(world.home), hooks=hooks)
    assert RealmStore(world.paths).exists(CREATED)
    assert not exec_service_dir(world.paths).exists()


@pytest.mark.fr("FR-084")
def test_a_services_on_create_syncs_the_relay_after_the_service_directory(
    world: World, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    seen = _record_syncs(monkeypatch, create_ops)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    fake = ScriptedPodman(root=str(world.tmp))
    hooks = hooks_for(world.tmp, [], world.tmp / "mountinfo", program=PROGRAM)
    create_ops.create_realm(env, fake, request(world.home, services=True), hooks=hooks)
    assert seen == [(world.paths.runtime, fake)]


@pytest.mark.fr("FR-084")
def test_a_failed_services_on_create_syncs_the_relay_again_once_its_record_is_gone(
    world: World, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    present_at_sync: list[bool] = []

    def recorder(paths: Any, podman: object, **_: object) -> None:
        present_at_sync.append(RealmStore(paths).exists(CREATED))

    monkeypatch.setattr(create_ops, "sync_exec_service", recorder)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    fake = ScriptedPodman(root=str(world.tmp))

    def fail_at_stopped(name: str) -> None:
        if name == "stopped":
            raise make("GINN-E-INTERNAL")

    hooks = hooks_for(
        world.tmp, [], world.tmp / "mountinfo", on_step=fail_at_stopped, program=PROGRAM
    )
    with pytest.raises(GinnError):
        create_ops.create_realm(env, fake, request(world.home, services=True), hooks=hooks)
    assert present_at_sync == [True, False]


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_services_on_realm_that_stops_takes_the_relay_down_when_nothing_else_needs_it(
    world: World, cli: PodmanCli, sv: SvRecorder, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _save(world, "lab", services=True, state=RealmState.RUNNING)
    _sync(world, cli, sv)
    _supervise(world)
    _save(world, "lab", services=True, state=RealmState.STOPPED)
    _sync(world, cli, sv)
    assert (exec_service_dir(world.paths) / DOWN).exists()
    assert sv.verbs() == ["down"]


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
@pytest.mark.parametrize("state", [RealmState.RUNNING, RealmState.REBUILDING, RealmState.UPDATING])
def test_each_state_that_runs_exec_from_the_session_keeps_the_relay_up(
    world: World, cli: PodmanCli, sv: SvRecorder, state: RealmState
) -> None:
    _save(world, "lab", services=True, state=state)
    _sync(world, cli, sv)
    assert not (exec_service_dir(world.paths) / DOWN).exists()


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
@pytest.mark.parametrize(
    "state",
    [
        RealmState.STOPPED,
        RealmState.FAILED,
        RealmState.CREATING,
        RealmState.BROKEN,
        RealmState.DELETING,
    ],
)
def test_each_state_that_needs_no_exec_leaves_the_relay_down(
    world: World, cli: PodmanCli, sv: SvRecorder, state: RealmState
) -> None:
    _save(world, "lab", services=True, state=state)
    _sync(world, cli, sv)
    assert (exec_service_dir(world.paths) / DOWN).exists()


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_running_services_on_realm_brings_the_supervised_service_up(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    _sync(world, cli, sv)
    assert (exec_service_dir(world.paths) / DOWN).exists()
    _supervise(world)
    _save(world, "lab", services=True, state=RealmState.RUNNING)
    _sync(world, cli, sv)
    assert not (exec_service_dir(world.paths) / DOWN).exists()
    assert sv.verbs() == ["up"]


@pytest.mark.fr("FR-082")
@pytest.mark.fr("NFR-001")
def test_a_running_services_off_realm_does_not_bring_the_relay_up(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    _sync(world, cli, sv)
    _save(world, "other", services=False, state=RealmState.RUNNING)
    _sync(world, cli, sv)
    assert (exec_service_dir(world.paths) / DOWN).exists()


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_record_that_does_not_load_keeps_the_relay_up(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    world.paths.realms.mkdir(parents=True, exist_ok=True)
    (world.paths.realms / "broken.json").write_text("{not json", encoding="utf-8")
    _sync(world, cli, sv)
    assert not (exec_service_dir(world.paths) / DOWN).exists()


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_the_running_state_follows_the_records_without_an_adapter(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    _sync(world, cli, sv)
    _supervise(world)
    _save(world, "lab", services=True, state=RealmState.RUNNING)
    sync_exec_service(world.paths, hooks=hooks_of(sv))
    assert not (exec_service_dir(world.paths) / DOWN).exists()
    _save(world, "lab", services=True, state=RealmState.STOPPED)
    sync_exec_service(world.paths, hooks=hooks_of(sv))
    assert (exec_service_dir(world.paths) / DOWN).exists()
    assert sv.verbs() == ["up", "down"]


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_stop_of_a_services_on_realm_syncs_the_relay_once_the_realm_reads_stopped(
    world: World, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    labels = {LABEL_REALM: "lab", LABEL_DISTRO: "debian", LABEL_GENERATION: "1"}
    cid = world.fake.create(
        CreateSpec(name="ginn-lab-g1", image_digest_ref=DIGEST, network="host", labels=labels)
    )
    world.fake.start(cid)
    (world.paths.homes / "lab").mkdir(parents=True, exist_ok=True)
    write_service_dir(world.paths, REALM, PROGRAM)
    RealmStore(world.paths).save(record_of("lab", RealmState.RUNNING, cid, services=True))
    store = RealmStore(world.paths)
    seen: list[str] = []

    def recorder(paths: Any, podman: object, **_: object) -> None:
        seen.append(store.load(REALM).state.value)

    monkeypatch.setattr(lifecycle_ops, "sync_exec_service", recorder)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    lifecycle_ops.stop_realm(env, world.fake, REALM, warnings=[])
    assert seen == ["stopped"]


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_realm_that_exits_cleanly_syncs_the_relay_once_it_reads_stopped(
    world: World, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    write_service_dir(world.paths, REALM, PROGRAM)
    RealmStore(world.paths).save(record_of("lab", RealmState.RUNNING, "c0", services=True))
    store = RealmStore(world.paths)
    seen: list[str] = []

    def recorder(paths: Any, podman: object = None, **_: object) -> None:
        seen.append(store.load(REALM).state.value)

    monkeypatch.setattr(service_ops, "sync_exec_service", recorder)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    hooks = ServiceHooks(lifecycle=LifecycleHooks(exists=lambda _p: False), wait_helper=PROGRAM)
    assert finish_service(env, REALM, exit_code=0, signal_no=0, hooks=hooks) is ServiceExit.STOPPED
    assert seen == ["stopped"]


def _refusing_sync(paths: Any, podman: object = None, **_: object) -> None:
    """A sync that fails with an OSError, as a full disk or a denied write would."""
    raise OSError(errno.EACCES, "Permission denied")


@pytest.mark.fr("FR-084")
def test_two_syncs_serialise_and_the_second_waits_until_the_first_has_settled(
    world: World, cli: PodmanCli, sv: SvRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(world, "lab", services=True)
    entered = threading.Event()
    release = threading.Event()
    written = exec_relay.write_file_atomic

    def paused_write(path: Path, data: bytes, mode: int) -> None:
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "the test did not release the first sync"
        written(path, data, mode)

    monkeypatch.setattr(exec_relay, "write_file_atomic", paused_write)
    errors: list[BaseException] = []

    def sync_once() -> None:
        try:
            _sync(world, cli, sv)
        except BaseException as exc:  # noqa: BLE001 - the test fails on any error below
            errors.append(exc)

    first = threading.Thread(target=sync_once)
    second = threading.Thread(target=sync_once)
    first.start()
    assert entered.wait(10)
    second.start()
    second.join(0.3)
    assert second.is_alive(), "the second sync must wait on the sync lock"
    release.set()
    first.join(10)
    second.join(10)
    assert errors == []
    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(p.name for p in world.paths.sv.iterdir()) == [SYNC_LOCK_NAME, EXEC_SERVICE]


@pytest.mark.fr("FR-084")
def test_two_concurrent_syncs_leave_one_relay_service_and_no_temporary_directory(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    _save(world, "lab", services=True)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def sync_once() -> None:
        try:
            barrier.wait(10)
            _sync(world, cli, sv)
        except BaseException as exc:  # noqa: BLE001 - the test fails on any error below
            errors.append(exc)

    threads = [threading.Thread(target=sync_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert errors == []
    assert sorted(p.name for p in world.paths.sv.iterdir()) == [SYNC_LOCK_NAME, EXEC_SERVICE]
    assert (exec_service_dir(world.paths) / RUN).is_file()


@pytest.mark.fr("FR-084")
def test_two_creators_that_bypass_the_lock_get_one_service_and_no_leftover(
    world: World, cli: PodmanCli
) -> None:
    text = render(world.paths, cli)
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def create_once() -> None:
        try:
            barrier.wait(10)
            results.append(exec_relay._create(world.paths, text, down=False))
        except BaseException as exc:  # noqa: BLE001 - the test fails on any error below
            errors.append(exc)

    threads = [threading.Thread(target=create_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert errors == []
    assert sorted(results) == [False, True], "one creator installs, the other finds it present"
    assert sorted(p.name for p in world.paths.sv.iterdir()) == [EXEC_SERVICE]
    assert not any(p.name.startswith(TEMP_PREFIX) for p in world.paths.sv.iterdir())
    assert (exec_service_dir(world.paths) / RUN).read_text(encoding="utf-8") == text


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_realm_created_services_off_keeps_the_relay_down_when_its_flag_is_turned_on(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    record = record_of("lab", RealmState.RUNNING, None, services=False)
    RealmStore(world.paths).save(replace(record, settings=replace(record.settings, services=True)))
    _sync(world, cli, sv)
    assert (exec_service_dir(world.paths) / DOWN).exists(), "its execs go direct, so no relay"
    assert sv.calls == [], "nothing is started for a container that runs its execs directly"


@pytest.mark.fr("FR-084")
def test_a_relay_failure_after_a_services_change_is_a_warning_and_the_saved_change_stands(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(service_ops, "sync_exec_service", _refusing_sync)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    make_realm(world, "lab", RealmState.STOPPED, running=False)
    notices: list[Any] = []
    record = service_ops.set_services_mode(env, REALM, on=True, warnings=notices)
    assert record.settings.services is True
    assert RealmStore(world.paths).load(REALM).settings.services is True
    outcomes = [entry.outcome for entry in AuditLog(world.paths).read() if entry.op == "services"]
    assert outcomes == ["ok"], "the body completed, so the operation is finished, not interrupted"
    relay = [notice for notice in notices if notice.code == "GINN-W-RELAY-SYNC"]
    assert len(relay) == 1
    assert "denied" in relay[0].message
    assert capsys.readouterr().err == "", "a CLI warning goes through the warning list, not stderr"


@pytest.mark.fr("FR-084")
def test_a_relay_failure_of_any_type_after_a_delete_is_a_warning(
    world: World, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    monkeypatch.setattr(delete_ops, "sync_exec_service", _refusing_sync)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    make_realm(world, "lab", RealmState.STOPPED, running=False)
    result = delete_ops.delete_realm(env, world.fake, REALM)
    assert not RealmStore(world.paths).exists(REALM)
    assert [w.code for w in result.warnings] == ["GINN-W-RELAY-SYNC"]
    assert "denied" in result.warnings[0].message
    assert not any("relay service" in note for note in result.notes)


@pytest.mark.fr("FR-081")
def test_a_failed_relay_sync_after_a_clean_exit_still_clears_the_crash_count_and_stops(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_service_dir(world.paths, REALM, PROGRAM)
    RealmStore(world.paths).save(record_of("lab", RealmState.RUNNING, "c0", services=True))
    write_crashes(crash_log(world.paths, REALM), [clock() - 10.0, clock() - 5.0])
    monkeypatch.setattr(service_ops, "sync_exec_service", _refusing_sync)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    hooks = ServiceHooks(lifecycle=LifecycleHooks(exists=lambda _p: False), wait_helper=PROGRAM)
    assert finish_service(env, REALM, exit_code=0, signal_no=0, hooks=hooks) is ServiceExit.STOPPED
    assert not crash_log(world.paths, REALM).exists(), "the clean stop clears the crash count"
    assert "GINN-W-RELAY-SYNC: " in capsys.readouterr().err, "the runit log gets the warning"


@pytest.mark.fr("FR-081")
@pytest.mark.fr("NFR-033")
def test_a_failed_relay_sync_does_not_replace_a_give_up(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    sv: SvRecorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_service_dir(world.paths, REALM, PROGRAM)
    RealmStore(world.paths).save(record_of("lab", RealmState.RUNNING, "c0", services=True))
    now = clock()
    write_crashes(crash_log(world.paths, REALM), [now - 40.0, now - 30.0, now - 20.0, now - 10.0])
    monkeypatch.setattr(service_ops, "sync_exec_service", _refusing_sync)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    # The fake sv answers no state line, so the end is a crash and no real sv runs (FR-081).
    hooks = ServiceHooks(
        lifecycle=LifecycleHooks(exists=lambda _p: False),
        wait_helper=PROGRAM,
        supervisor=hooks_of(sv),
    )
    assert finish_service(env, REALM, exit_code=1, signal_no=0, hooks=hooks) is ServiceExit.GAVE_UP
    assert "GINN-W-RELAY-SYNC: " in capsys.readouterr().err


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_relay_failure_after_a_services_on_start_does_not_fail_the_start(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RealmStore(world.paths)
    _save(world, "lab", services=True)
    monkeypatch.setattr(lifecycle_ops, "sync_exec_service", _refusing_sync)
    monkeypatch.setattr(lifecycle_ops, "refuse_before_supervisor", lambda *_a, **_k: None)

    def through_runit(env: object, podman: object, name: RealmName, hooks: object) -> Any:
        store.save(record_of("lab", RealmState.RUNNING, None, services=True))
        return store.load(name)

    monkeypatch.setattr(lifecycle_ops, "start_through_sv", through_runit)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    user = lifecycle_ops.HostUser(uid=os.getuid(), host_home=world.home, host_env={})
    notices: list[Any] = []
    record = lifecycle_ops.start_realm(
        env, world.fake, REALM, session=user, rebuild=False, warnings=notices
    )
    assert record.state is RealmState.RUNNING
    assert [notice.code for notice in notices] == ["GINN-W-RELAY-SYNC"]
    assert capsys.readouterr().err == ""


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_relay_failure_after_a_services_on_stop_does_not_fail_the_stop(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    labels = {LABEL_REALM: "lab", LABEL_DISTRO: "debian", LABEL_GENERATION: "1"}
    cid = world.fake.create(
        CreateSpec(name="ginn-lab-g1", image_digest_ref=DIGEST, network="host", labels=labels)
    )
    world.fake.start(cid)
    (world.paths.homes / "lab").mkdir(parents=True, exist_ok=True)
    write_service_dir(world.paths, REALM, PROGRAM)
    RealmStore(world.paths).save(record_of("lab", RealmState.RUNNING, cid, services=True))
    monkeypatch.setattr(lifecycle_ops, "sync_exec_service", _refusing_sync)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    notices: list[Any] = []
    stopped = lifecycle_ops.stop_realm(env, world.fake, REALM, warnings=notices)
    assert stopped.state is RealmState.STOPPED
    assert RealmStore(world.paths).load(REALM).state is RealmState.STOPPED
    assert [notice.code for notice in notices] == ["GINN-W-RELAY-SYNC"]
    assert capsys.readouterr().err == ""


@pytest.mark.fr("FR-084")
def test_a_relay_failure_after_a_services_on_create_is_a_warning_and_the_realm_stands(
    world: World, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    monkeypatch.setattr(create_ops, "sync_exec_service", _refusing_sync)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    fake = ScriptedPodman(root=str(world.tmp))
    hooks = hooks_for(world.tmp, [], world.tmp / "mountinfo", program=PROGRAM)
    result = create_ops.create_realm(env, fake, request(world.home, services=True), hooks=hooks)
    assert RealmStore(world.paths).exists(CREATED), "the body finished, so it was not rolled back"
    assert result.record.state is RealmState.STOPPED
    assert [w.code for w in result.warnings if w.code == "GINN-W-RELAY-SYNC"] == [
        "GINN-W-RELAY-SYNC"
    ]


@pytest.mark.fr("FR-081")
def test_a_failed_relay_sync_after_a_clean_exit_goes_to_the_callers_list_when_one_is_given(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_service_dir(world.paths, REALM, PROGRAM)
    RealmStore(world.paths).save(record_of("lab", RealmState.RUNNING, "c0", services=True))
    monkeypatch.setattr(service_ops, "sync_exec_service", _refusing_sync)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    hooks = ServiceHooks(lifecycle=LifecycleHooks(exists=lambda _p: False), wait_helper=PROGRAM)
    notices: list[Any] = []
    outcome = finish_service(env, REALM, exit_code=0, signal_no=0, hooks=hooks, warnings=notices)
    assert outcome is ServiceExit.STOPPED
    relay = [notice.code for notice in notices if notice.code == "GINN-W-RELAY-SYNC"]
    assert relay == ["GINN-W-RELAY-SYNC"]
    assert capsys.readouterr().err == "", "the CLI prints the list, so stderr stays empty"


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_start_that_finds_a_services_on_realm_running_still_syncs_the_relay(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
) -> None:
    labels = {LABEL_REALM: "lab", LABEL_DISTRO: "debian", LABEL_GENERATION: "1"}
    cid = world.fake.create(
        CreateSpec(name="ginn-lab-g1", image_digest_ref=DIGEST, network="host", labels=labels)
    )
    world.fake.start(cid)
    (world.paths.homes / "lab").mkdir(parents=True, exist_ok=True)
    RealmStore(world.paths).save(record_of("lab", RealmState.RUNNING, cid, services=True))
    seen = _record_syncs(monkeypatch, lifecycle_ops)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    user = lifecycle_ops.HostUser(uid=os.getuid(), host_home=world.home, host_env={})
    record = lifecycle_ops.start_realm(env, world.fake, REALM, session=user, warnings=[])
    assert record.state is RealmState.RUNNING
    assert seen == [(world.paths.runtime, world.fake)]


@pytest.mark.fr("FR-084")
@pytest.mark.fr("NFR-001")
def test_a_failed_relay_sync_on_a_running_start_is_a_warning_and_the_start_succeeds(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
) -> None:
    labels = {LABEL_REALM: "lab", LABEL_DISTRO: "debian", LABEL_GENERATION: "1"}
    cid = world.fake.create(
        CreateSpec(name="ginn-lab-g1", image_digest_ref=DIGEST, network="host", labels=labels)
    )
    world.fake.start(cid)
    (world.paths.homes / "lab").mkdir(parents=True, exist_ok=True)
    RealmStore(world.paths).save(record_of("lab", RealmState.RUNNING, cid, services=True))
    monkeypatch.setattr(lifecycle_ops, "sync_exec_service", _refusing_sync)
    env = OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=clock)
    user = lifecycle_ops.HostUser(uid=os.getuid(), host_home=world.home, host_env={})
    notices: list[Any] = []
    record = lifecycle_ops.start_realm(env, world.fake, REALM, session=user, warnings=notices)
    assert record.state is RealmState.RUNNING
    assert [notice.code for notice in notices] == ["GINN-W-RELAY-SYNC"]


@pytest.mark.fr("FR-084")
def test_a_rebuild_that_turns_services_on_keeps_the_relay_up_when_its_directory_is_present(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    """A rebuild turns services on: the record still says services-off at create, and its settings
    ask for services on. Its replay runs through the relay, so the directory must not be marked
    down (ADR-014, T7.10 review)."""
    _save(world, "lab", services=True, state=RealmState.RUNNING)
    _sync(world, cli, sv)
    _supervise(world)
    store = RealmStore(world.paths)
    record = store.load(RealmName("lab"))
    store.save(
        replace(
            record,
            services_at_create=False,
            state=RealmState.REBUILDING,
            previous_state=RealmState.STOPPED,
        )
    )
    sv.calls.clear()
    _sync(world, cli, sv)
    assert not (exec_service_dir(world.paths) / DOWN).exists()
    assert sv.verbs() == ["up"]


@pytest.mark.fr("FR-082")
def test_a_stopped_services_off_container_with_services_asked_on_needs_no_relay(
    world: World, cli: PodmanCli, sv: SvRecorder
) -> None:
    """Only the rebuild needs the relay for a new services-on mode. A stopped realm whose container
    is services-off runs its execs directly, so its directory is marked down (ADR-014)."""
    _save(world, "lab", services=True, state=RealmState.RUNNING)
    _sync(world, cli, sv)
    _supervise(world)
    store = RealmStore(world.paths)
    record = store.load(RealmName("lab"))
    store.save(replace(record, services_at_create=False, state=RealmState.STOPPED))
    sv.calls.clear()
    _sync(world, cli, sv)
    assert (exec_service_dir(world.paths) / DOWN).exists()
    assert sv.verbs() == ["down"]


@pytest.mark.fr("FR-084")
def test_up_exec_service_runs_sv_up_on_the_relay_directory(world: World, sv: SvRecorder) -> None:
    service = exec_service_dir(world.paths)
    up_exec_service(service, hooks=hooks_of(sv))
    assert sv.verbs() == ["up"]
    assert sv.calls[0][-1] == str(service)


@pytest.mark.fr("FR-084")
def test_a_failed_up_exec_service_is_a_service_control_error_with_sv_detail(
    world: World, sv: SvRecorder
) -> None:
    sv.reply = Result(1, "", "sv: fail: boom\n")
    with pytest.raises(GinnError) as caught:
        up_exec_service(exec_service_dir(world.paths), hooks=hooks_of(sv))
    assert caught.value.code == "GINN-E-SERVICE-CONTROL"
    assert "boom" in str(caught.value.details.get("detail", ""))
