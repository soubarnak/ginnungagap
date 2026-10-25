# SPDX-License-Identifier: Apache-2.0
"""The filter, the D-Bus proxy and the NVIDIA spec as realm operations (FR-067, FR-068, FR-069).

Covers ops/security_ops.py. The helpers are fakes: each launch starts a small Python child that
sleeps, so a stop can really end it and the test can see it, and no ssh, bus or driver is needed.
Every child is killed at teardown. The realm records and the Owned state files are real, in tmp.
"""

import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.errors import GinnError
from ginn.errors import Warning as GinnWarning
from ginn.ops import security_ops
from ginn.ops._mutating import OpEnv
from ginn.ops.security_ops import (
    agent_state_path,
    finish_app,
    leave_agent,
    proxy_state_path,
    read_agent_record,
    reconcile_proxy,
    start_app,
    sync_agent,
    write_nvidia,
)
from ginn.runtime.realm_security import SecurityHooks, bus_listen
from ginn.security import agent
from ginn.security._proc import Proc
from ginn.security.nvidia import NvidiaPlan, Status
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore, write_json_atomic
from tests.fakes.clock import FakeClock

USER = "alice"
UID = 1000
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
BINARY = "/usr/bin/xdg-dbus-proxy"
SLEEPER = [sys.executable, "-c", "import time; time.sleep(300)"]
DIGEST = "registry.example/debian@sha256:" + "ab" * 32


@dataclass
class _Child:
    """A `Child` over a real process: `proc` is its record, and the process sleeps."""

    proc: Proc
    popen: subprocess.Popen[bytes]

    def running(self) -> bool:
        return self.popen.poll() is None

    def stop(self) -> None:
        self.popen.kill()
        self.popen.wait()


class _Launches:
    """A launcher that records each argv and starts a sleeping child for it."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.children: list[_Child] = []
        self.fail = False

    def __call__(self, argv: object) -> _Child:
        if self.fail:
            raise GinnError("GINN-E-DBUS-PROXY", "proxy failed", "", 1, details={})
        self.argvs.append(list(argv))  # type: ignore[call-overload]
        popen = subprocess.Popen(SLEEPER, start_new_session=True)  # noqa: S603 - fixed argv
        proc = Proc.of(popen.pid)
        assert proc is not None
        child = _Child(proc, popen)
        self.children.append(child)
        return child


@pytest.fixture
def launches() -> Iterator[_Launches]:
    fakes = _Launches()
    yield fakes
    for child in fakes.children:
        if child.running():
            child.stop()


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OwnedPaths:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    return resolved


@pytest.fixture
def host_env(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700, exist_ok=True)
    return {
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
        "SSH_AUTH_SOCK": f"{runtime}/keyring/ssh",
    }


@pytest.fixture
def env(paths: OwnedPaths, tmp_path: Path) -> OpEnv:
    host = tmp_path / "host"
    host.mkdir(exist_ok=True)
    return OpEnv.for_paths(paths, USER, root_dir=host, clock=FakeClock())


def _hooks(launches: _Launches, **overrides: object) -> SecurityHooks:
    values: dict[str, object] = {
        "agent_launch": launches,
        "proxy_launch": launches,
        "dbus_binary": BINARY,
        "gpg_extra": lambda: None,
        "file_type": lambda _path: stat.S_IFSOCK,
    }
    values.update(overrides)
    return SecurityHooks(**values)  # type: ignore[arg-type]


def _save(env: OpEnv, name: str, tier: Tier, state: RealmState) -> RealmRecord:
    settings = RealmSettings(
        name=RealmName(name),
        distro=DistroId("debian"),
        tier=tier,
        services=False,
        autostart=False,
        everything=False,
    )
    record = RealmRecord(
        settings=settings,
        state=state,
        previous_state=None,
        image_ref=DIGEST,
        image_digest=DIGEST.partition("@")[2],
        container_id="c" * 64,
        generation=1,
        created_at=T0,
        updated_at=T0,
    )
    RealmStore(env.paths, env.clock).save(record)
    return record


def _set_state(env: OpEnv, name: str, state: RealmState) -> None:
    RealmStore(env.paths, env.clock).set_state(RealmName(name), state)


@pytest.mark.fr("FR-067")
def test_the_filter_starts_once_for_the_first_development_realm_and_is_kept_for_the_next(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    hooks = _hooks(launches)
    _save(env, "a", Tier.DEVELOPMENT, RealmState.RUNNING)
    _save(env, "b", Tier.DEVELOPMENT, RealmState.RUNNING)
    first = sync_agent(env, name=RealmName("a"), host_env=host_env, uid=UID, hooks=hooks)
    second = sync_agent(env, name=RealmName("b"), host_env=host_env, uid=UID, hooks=hooks)
    assert first is agent.Action.START
    assert second is agent.Action.KEEP
    assert len(launches.argvs) == 1
    assert read_agent_record(env.paths).proc == launches.children[0].proc


@pytest.mark.fr("FR-067")
def test_the_last_development_realm_out_stops_the_filter(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    hooks = _hooks(launches)
    _save(env, "a", Tier.DEVELOPMENT, RealmState.RUNNING)
    _save(env, "b", Tier.DEVELOPMENT, RealmState.RUNNING)
    sync_agent(env, name=RealmName("a"), host_env=host_env, uid=UID, hooks=hooks)
    _set_state(env, "b", RealmState.STOPPED)
    kept = leave_agent(env, RealmName("b"))
    assert kept is agent.Action.KEEP
    _set_state(env, "a", RealmState.STOPPED)
    stopped = leave_agent(env, RealmName("a"))
    assert stopped is agent.Action.STOP
    assert not launches.children[0].running()
    assert read_agent_record(env.paths) == agent.AgentRecord()


@pytest.mark.fr("FR-067")
def test_a_basic_realm_never_starts_the_filter(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    """The filter serves Development realms only, so a Basic realm that leaves starts nothing."""
    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    action = leave_agent(env, RealmName("lab"))
    assert action is agent.Action.IDLE
    assert launches.argvs == []


@pytest.mark.fr("FR-067")
def test_a_malformed_filter_state_is_refused_and_no_helper_is_started(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    agent_state_path(env.paths).write_text("not json", encoding="utf-8")
    _save(env, "a", Tier.DEVELOPMENT, RealmState.RUNNING)
    with pytest.raises(GinnError) as caught:
        sync_agent(
            env,
            name=RealmName("a"),
            host_env=host_env,
            uid=UID,
            hooks=_hooks(launches),
        )
    assert caught.value.code == "GINN-E-INTERNAL"
    assert launches.argvs == []


@pytest.mark.fr("FR-068")
@pytest.mark.fr("FR-068")
def test_the_first_app_starts_the_proxy_and_the_last_one_out_stops_it(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    hooks = _hooks(launches)
    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    app = start_app(env, RealmName("lab"), host_env=host_env, uid=UID, hooks=hooks)
    assert [argv[0] for argv in launches.argvs] == [BINARY]
    assert (
        json.loads(proxy_state_path(env.paths, RealmName("lab")).read_text())["proxy"] is not None
    )
    finish_app(env, RealmName("lab"), app)
    assert not launches.children[0].running()
    state = json.loads(proxy_state_path(env.paths, RealmName("lab")).read_text())
    assert state["proxy"] is None and state["apps"] == []


@pytest.mark.fr("FR-068")
def test_a_second_app_joins_the_running_proxy_and_is_not_started_twice(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    hooks = _hooks(launches)
    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    first = start_app(env, RealmName("lab"), host_env=host_env, uid=UID, hooks=hooks)
    second = start_app(env, RealmName("lab"), host_env=host_env, uid=UID, hooks=hooks)
    assert first == second  # the same process registers twice: it is one app
    assert len(launches.argvs) == 1


@pytest.mark.fr("NFR-004")
def test_registering_an_app_leaves_the_realm_record_byte_identical(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    path = env.paths.realms / "lab.json"
    before = path.read_bytes()
    start_app(env, RealmName("lab"), host_env=host_env, uid=UID, hooks=_hooks(launches))
    assert path.read_bytes() == before


@pytest.mark.fr("FR-068")
def test_a_realm_with_no_bus_registers_the_app_and_starts_no_proxy(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    del host_env["DBUS_SESSION_BUS_ADDRESS"]
    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    start_app(env, RealmName("lab"), host_env=host_env, uid=UID, hooks=_hooks(launches))
    state = json.loads(proxy_state_path(env.paths, RealmName("lab")).read_text())
    assert state["proxy"] is None and len(state["apps"]) == 1
    assert launches.argvs == []


@pytest.mark.fr("FR-068")
def test_a_proxy_that_fails_to_start_warns_and_the_app_runs_with_no_bus(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    """The enter is not refused: the app is registered with no proxy, and a warning says so.

    ADR-010 and FR-068: the app runs with no bus, which fails closed.
    """
    launches.fail = True
    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    notices: list[GinnWarning] = []
    app = start_app(
        env,
        RealmName("lab"),
        host_env=host_env,
        uid=UID,
        hooks=_hooks(launches),
        notify=notices.append,
    )
    assert [notice.code for notice in notices] == ["GINN-W-DBUS-PROXY"]
    state = json.loads(proxy_state_path(env.paths, RealmName("lab")).read_text())
    assert state["proxy"] is None
    assert state["apps"] == [app.to_dict()]


def _listen_is_a_file(env: OpEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """A regular file where the proxy socket goes: start_proxy raises ValueError, and keeps it."""
    listen = Path(bus_listen(env.paths.runtime, "lab"))
    listen.parent.mkdir(parents=True, exist_ok=True)
    listen.write_text("", encoding="utf-8")


def _socket_dir_is_a_file(env: OpEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """A regular file where the proxy's socket directory goes: its mkdir raises an OSError."""
    socket_dir = proxy_state_path(env.paths, RealmName("lab")).parent / "lab"
    socket_dir.parent.mkdir(parents=True, exist_ok=True)
    socket_dir.write_text("", encoding="utf-8")


def _start_refused(env: OpEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """start_proxy raises an OSError, as its unlink of a stale socket can."""

    def refuse(plan: object, launch: object) -> _Child:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(security_ops.dbus_filter, "start_proxy", refuse)


@pytest.mark.fr("FR-068")
@pytest.mark.parametrize(
    "breakage", [_listen_is_a_file, _socket_dir_is_a_file, _start_refused], ids=lambda f: f.__name__
)
def test_a_proxy_socket_that_cannot_be_prepared_warns_and_the_app_runs_with_no_bus(
    env: OpEnv,
    host_env: dict[str, str],
    launches: _Launches,
    monkeypatch: pytest.MonkeyPatch,
    breakage: Callable[[OpEnv, pytest.MonkeyPatch], None],
) -> None:
    """A ValueError or an OSError while the proxy is prepared or started is a warning, as a refused
    start is: the app is registered with no proxy, and nothing is launched (FR-068, ADR-010)."""
    breakage(env, monkeypatch)
    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    notices: list[GinnWarning] = []
    app = start_app(
        env,
        RealmName("lab"),
        host_env=host_env,
        uid=UID,
        hooks=_hooks(launches),
        notify=notices.append,
    )
    assert [notice.code for notice in notices] == ["GINN-W-DBUS-PROXY"]
    assert launches.argvs == []
    state = json.loads(proxy_state_path(env.paths, RealmName("lab")).read_text())
    assert state["proxy"] is None
    assert state["apps"] == [app.to_dict()]


@pytest.mark.fr("FR-068")
def test_a_failed_proxy_start_is_retried_by_the_next_launch(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    launches.fail = True
    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    start_app(env, RealmName("lab"), host_env=host_env, uid=UID, hooks=_hooks(launches))
    launches.fail = False
    start_app(env, RealmName("lab"), host_env=host_env, uid=UID, hooks=_hooks(launches))
    assert len(launches.argvs) == 1
    state = json.loads(proxy_state_path(env.paths, RealmName("lab")).read_text())
    assert state["proxy"] is not None


@pytest.mark.fr("FR-068")
def test_a_state_write_that_fails_after_the_proxy_starts_stops_the_proxy(
    env: OpEnv,
    host_env: dict[str, str],
    launches: _Launches,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy with no record would run unseen, so the one just started is stopped (FR-068)."""

    def refuse(path: Path, data: object) -> None:
        raise OSError("disk full")

    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    monkeypatch.setattr(security_ops, "write_json_atomic", refuse)
    with pytest.raises(OSError, match="disk full"):
        start_app(env, RealmName("lab"), host_env=host_env, uid=UID, hooks=_hooks(launches))
    assert len(launches.children) == 1
    assert not launches.children[0].running()


@pytest.mark.fr("NFR-004")
def test_an_idle_proxy_release_writes_no_proxy_state(
    env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no app and no proxy, the reconcile changes nothing, so it writes nothing (NFR-004)."""
    writes: list[Path] = []
    real = security_ops.write_json_atomic

    def record(path: Path, data: object) -> None:
        writes.append(path)
        real(path, data)  # type: ignore[arg-type]

    monkeypatch.setattr(security_ops, "write_json_atomic", record)
    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    reconcile_proxy(env, RealmName("lab"))
    assert writes == []
    assert not proxy_state_path(env.paths, RealmName("lab")).exists()


@pytest.mark.fr("FR-068")
def test_finishing_an_app_of_a_removed_realm_is_a_no_op(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    _save(env, "lab", Tier.BASIC, RealmState.RUNNING)
    app = start_app(env, RealmName("lab"), host_env=host_env, uid=UID, hooks=_hooks(launches))
    (env.paths.realms / "lab.json").unlink()
    finish_app(env, RealmName("lab"), app)


def _plan(status: Status, spec: Path, ld: Path) -> NvidiaPlan:
    return NvidiaPlan(
        status=status,
        reason="test",
        spec_path=str(spec),
        spec_text='{"cdiVersion": "0.6.0"}\n',
        ld_path=str(ld),
        ld_text="/usr/lib/ginn-nvidia\n",
    )


@pytest.mark.fr("FR-069")
def test_the_cdi_spec_is_written_once_per_digest(paths: OwnedPaths, tmp_path: Path) -> None:
    spec, ld = paths.cdi / "nvidia.json", paths.data / "ld" / "ginn-nvidia.conf"
    plan = _plan(Status.READY, spec, ld)
    assert write_nvidia(paths, plan) is True
    assert spec.read_text(encoding="utf-8") == plan.spec_text
    assert write_nvidia(paths, plan) is False
    changed = NvidiaPlan(
        status=Status.READY,
        reason="test",
        spec_path=str(spec),
        spec_text='{"cdiVersion": "0.7.0"}\n',
        ld_path=str(ld),
        ld_text=plan.ld_text,
    )
    assert write_nvidia(paths, changed) is True
    assert spec.read_text(encoding="utf-8") == changed.spec_text


@pytest.mark.fr("FR-069")
def test_a_grant_that_is_not_ready_writes_nothing(paths: OwnedPaths) -> None:
    spec, ld = paths.cdi / "nvidia.json", paths.data / "ld" / "ginn-nvidia.conf"
    assert write_nvidia(paths, _plan(Status.ABSENT, spec, ld)) is False
    assert not spec.exists()
    assert not ld.exists()


@pytest.mark.fr("FR-069")
def test_a_spec_that_is_a_link_is_replaced_not_followed(paths: OwnedPaths, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text("keep", encoding="utf-8")
    spec = paths.cdi / "nvidia.json"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.symlink_to(target)
    write_nvidia(paths, _plan(Status.READY, spec, paths.data / "ld" / "x.conf"))
    assert target.read_text(encoding="utf-8") == "keep"
    assert not spec.is_symlink()


@pytest.mark.fr("FR-067")
def test_the_next_sync_drops_a_member_whose_realm_is_gone_or_not_running(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    """A member that was never cleaned up (its realm deleted, or stopped without a leave) is pruned
    by any filter sync, so the filter cannot run forever (FR-067). A running member stays."""
    _save(env, "live", Tier.DEVELOPMENT, RealmState.RUNNING)
    _save(env, "idle", Tier.DEVELOPMENT, RealmState.STOPPED)
    write_json_atomic(
        agent_state_path(env.paths),
        agent.AgentRecord(members=("ghost", "idle", "lab", "live")).to_dict(),
    )
    leave_agent(env, RealmName("lab"))
    assert read_agent_record(env.paths).members == ("live",)


@pytest.mark.fr("FR-067")
def test_a_member_with_an_operation_in_flight_is_kept_by_the_sync(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    """A realm that is joining the filter has its record stopped until its attach, so a live
    journal entry for it means an operation is in flight. It is kept."""
    _save(env, "busy", Tier.DEVELOPMENT, RealmState.STOPPED)
    env.journal.begin("start", RealmName("busy"))
    write_json_atomic(
        agent_state_path(env.paths), agent.AgentRecord(members=("busy", "lab")).to_dict()
    )
    leave_agent(env, RealmName("lab"))
    assert read_agent_record(env.paths).members == ("busy",)


@pytest.mark.fr("FR-067")
def test_a_corrupt_journal_file_does_not_keep_a_non_running_member(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    """A journal file with no entry is not an operation in flight, and recovery never removes it,
    so it must not keep a stopped realm in the filter (FR-067)."""
    _save(env, "busy", Tier.DEVELOPMENT, RealmState.STOPPED)
    env.paths.journal.mkdir(parents=True, exist_ok=True)
    (env.paths.journal / "busy.json").write_text("{}", encoding="utf-8")
    write_json_atomic(
        agent_state_path(env.paths), agent.AgentRecord(members=("busy", "lab")).to_dict()
    )
    leave_agent(env, RealmName("lab"))
    assert read_agent_record(env.paths).members == ()


@pytest.mark.fr("FR-067")
def test_an_entry_whose_process_died_does_not_keep_a_non_running_member(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    """An entry whose process has died is settled by recovery, which calls `leave_agent` itself, so
    the sync does not keep it (FR-067). The entry is parsed and names a pid that no longer runs."""
    _save(env, "busy", Tier.DEVELOPMENT, RealmState.STOPPED)
    env.journal.begin("start", RealmName("busy"))
    entry_path = env.paths.journal / "busy.json"
    gone = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 - fixed argv
    gone.wait()
    data = json.loads(entry_path.read_text(encoding="utf-8"))
    data["pid"] = gone.pid
    entry_path.write_text(json.dumps(data), encoding="utf-8")
    write_json_atomic(
        agent_state_path(env.paths), agent.AgentRecord(members=("busy", "lab")).to_dict()
    )
    leave_agent(env, RealmName("lab"))
    assert read_agent_record(env.paths).members == ()


@pytest.mark.fr("FR-067")
def test_a_starting_realm_is_not_pruned_by_its_own_sync(
    env: OpEnv, host_env: dict[str, str], launches: _Launches
) -> None:
    """At `sync_agent` the starting realm's record is still stopped (it is running only after the
    attach), so the sync must keep it while it adds it (FR-067)."""
    _save(env, "lab", Tier.DEVELOPMENT, RealmState.STOPPED)
    sync_agent(env, name=RealmName("lab"), host_env=host_env, uid=UID, hooks=_hooks(launches))
    assert read_agent_record(env.paths).members == ("lab",)
