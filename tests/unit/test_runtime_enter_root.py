# SPDX-License-Identifier: Apache-2.0
"""The `root_session` seam of enter (T5.2, runtime/enter.py, FR-042 AC-1, NFR-004).

The hook wraps the exec of a `--root` session and nothing else. These tests check that it is
called only under `--root`, that it gets the record, the container and the exec, that the status it
returns is the status of enter, and that the non-root path makes exactly one exec.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.ops._mutating import OpEnv
from ginn.ops.lifecycle import HostUser, LifecycleHooks
from ginn.podman.adapter import CreateSpec
from ginn.podman.models import ExecResult
from ginn.runtime.enter import EnterHooks, RootSession, enter_realm
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from tests.fakes.clock import FakeClock
from tests.fakes.fake_podman import FakePodman

UID = 1000
GID = 1000
REALM = "debian"
DIGEST = "registry.example/debian@sha256:" + "ab" * 32
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
SHELL_ARGV = ["sh", "-c", "exit 0"]


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    yield resolved


@pytest.fixture
def env(paths: OwnedPaths, tmp_path: Path) -> OpEnv:
    return OpEnv.for_paths(paths, "alice", root_dir=tmp_path, clock=FakeClock())


@pytest.fixture
def user(tmp_path: Path) -> HostUser:
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700)
    host_env = {"XDG_RUNTIME_DIR": str(runtime), "LANG": "C.UTF-8", "TZ": "UTC"}
    return HostUser(uid=UID, host_home=home, host_env=host_env)


def _running(env: OpEnv, fake: FakePodman) -> None:
    """A running debian realm with no grants, so enter has nothing to re-attach."""
    labels = {LABEL_REALM: REALM, LABEL_DISTRO: "debian", LABEL_GENERATION: "1"}
    spec = CreateSpec(
        name=f"ginn-{REALM}-g1", image_digest_ref=DIGEST, network="host", labels=labels
    )
    cid = fake.create(spec)
    fake.start(cid)
    settings = RealmSettings(
        name=RealmName(REALM),
        distro=DistroId("debian"),
        tier=Tier.BASIC,
        services=False,
        autostart=False,
        everything=False,
    )
    record = RealmRecord(
        settings=settings,
        state=RealmState.RUNNING,
        previous_state=None,
        image_ref=DIGEST,
        image_digest=DIGEST.partition("@")[2],
        container_id=cid,
        generation=1,
        created_at=T0,
        updated_at=T0,
    )
    (env.paths.homes / REALM).mkdir(parents=True, exist_ok=True)
    RealmStore(env.paths, env.clock).save(record)
    fake.calls.clear()
    fake.exec_calls.clear()


def _enter(
    env: OpEnv,
    fake: FakePodman,
    user: HostUser,
    *,
    root: bool,
    hook: RootSession | None,
    argv: list[str] | None = SHELL_ARGV,
) -> int:
    hooks = EnterHooks(lifecycle=LifecycleHooks(), root_session=hook)
    return enter_realm(
        env,
        fake,
        RealmName(REALM),
        session=user,
        gid=GID,
        argv=argv,
        root=root,
        hooks=hooks,
    )


def _spy(calls: list[tuple[str, str]], extra: int) -> RootSession:
    """A hook that records what it got, runs the exec, and adds `extra` to its status."""

    def session(record: RealmRecord, cid: str, run: Callable[[], int]) -> int:
        calls.append((record.name.value, cid))
        return run() + extra

    return session


def _execs(fake: FakePodman) -> list[str]:
    return [call.op for call in fake.exec_calls if call.op == "exec_interactive"]


@pytest.mark.fr("FR-042")
@pytest.mark.fr("FR-049")
def test_under_root_the_hook_wraps_the_one_exec_and_its_status_is_returned(
    env: OpEnv, user: HostUser
) -> None:
    fake = FakePodman()
    _running(env, fake)
    fake.exec_results[tuple(SHELL_ARGV)] = ExecResult(5, "", "")
    calls: list[tuple[str, str]] = []
    status = _enter(env, fake, user, root=True, hook=_spy(calls, extra=100), argv=SHELL_ARGV)
    assert status == 105
    assert len(calls) == 1
    assert calls[0][0] == REALM
    (call,) = [c for c in fake.exec_calls if c.op == "exec_interactive"]
    assert call.argv == tuple(SHELL_ARGV)
    assert call.user == "0:0"
    assert calls[0][1] == call.container_id


@pytest.mark.fr("NFR-004")
@pytest.mark.fr("FR-049")
def test_without_root_the_hook_is_never_called_and_the_exec_is_the_only_call(
    env: OpEnv, user: HostUser
) -> None:
    fake = FakePodman()
    _running(env, fake)
    calls: list[tuple[str, str]] = []
    status = _enter(env, fake, user, root=False, hook=_spy(calls, extra=100))
    assert status == 0
    assert calls == []
    assert fake.ops() == ["ps", "exec_interactive"]
    assert _execs(fake) == ["exec_interactive"]


@pytest.mark.fr("FR-049")
def test_without_a_hook_a_root_session_runs_as_before(env: OpEnv, user: HostUser) -> None:
    fake = FakePodman()
    _running(env, fake)
    fake.exec_results[tuple(SHELL_ARGV)] = ExecResult(9, "", "")
    assert _enter(env, fake, user, root=True, hook=None) == 9
    assert _execs(fake) == ["exec_interactive"]
