# SPDX-License-Identifier: Apache-2.0
"""The Agent Proxy of a Development realm (FR-067, NFR-SEC-007): the plan, the lifecycle rule, and
the stored record that lets one ginn command see the filter that another one started.

The lifecycle tests simulate ginn "processes" as successive `sync` calls, each one given the record
that the previous one returned after a JSON round trip, as the realm records store it. The filter
is a `sleep` session (tests/fakes/fake_helpers.py), except in the last test, which runs the compiled
helper against FakeAgent. No ssh-agent, gpg-agent or host agent socket is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from ginn.domain.enums import Tier
from ginn.errors import GinnError
from ginn.runtime.mounts import SessionBind
from ginn.runtime.realm_security import agent_enabled
from ginn.security import agent
from ginn.security._proc import Child, Proc, stop_group
from tests.fakes.fake_helpers import NoLaunch, SleepLauncher, sleep_binary, spawn_sleeper
from tests.fakes.fake_ssh_agent import (
    REQUEST_IDENTITIES,
    FakeAgent,
    build_filter,
    encode_frame,
    exchange,
)

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "native" / "ginn-agent-filter.c"
UID = 1000
HELPER = "/usr/libexec/ginn/ginn-agent-filter"
SSH_REALM = "/run/user/1000/ginn-ssh-agent"
GPG_REALM = "/run/user/1000/gnupg/S.gpg-agent"
GPG_PARENT = "/run/user/1000/gnupg"
UPSTREAM = "/run/user/1000/ssh-host.sock"
UPSTREAM_NEW = "/run/user/1000/ssh-host-new.sock"
GPG_HOST = "/run/user/1000/gnupg/S.gpg-agent.extra"

pytestmark = [pytest.mark.fr("FR-067"), pytest.mark.fr("NFR-SEC-007")]


class FakeTypes:
    """A host where the given paths have the given file types. Other paths are missing."""

    def __init__(self, kinds: dict[str, int]) -> None:
        self._kinds = kinds

    def __call__(self, path: str) -> int | None:
        return self._kinds.get(path)


SOCKET = stat.S_IFSOCK
REGULAR = stat.S_IFREG


def _plan(
    env: dict[str, str] | None = None,
    *,
    kinds: dict[str, int] | None = None,
    listen: str = "/run/ginn/1000/agent/ssh.sock",
    gpg: str | None = GPG_HOST,
) -> agent.AgentPlan:
    # The filter helper is installed unless a test says otherwise (ADR-008 skips a missing one).
    base = {HELPER: REGULAR, UPSTREAM: SOCKET, GPG_HOST: SOCKET}
    host = base if kinds is None else {HELPER: REGULAR, **kinds}
    return agent.plan_realm(
        {"SSH_AUTH_SOCK": UPSTREAM} if env is None else env,
        uid=UID,
        helper=HELPER,
        listen=listen,
        gpg_extra=gpg,
        file_type=FakeTypes(host),
    )


@pytest.mark.fr("FR-067")
def test_only_a_development_realm_gets_the_agent_proxy() -> None:
    """The tier gate is `agent_enabled`, which the callers ask before they plan the filter."""
    assert agent_enabled(Tier.DEVELOPMENT, frozenset()) is True
    assert agent_enabled(Tier.BASIC, frozenset()) is False
    assert agent_enabled(Tier.CUSTOM, frozenset()) is False


def test_the_filter_socket_is_bound_at_the_fixed_realm_name_and_exec_points_there() -> None:
    plan = _plan()
    assert plan.binds[0] == SessionBind(
        host="/run/ginn/1000/agent/ssh.sock",
        realm=SSH_REALM,
        mode="rw",
        is_dir=False,
        internal=True,
    )
    assert plan.env == {"SSH_AUTH_SOCK": SSH_REALM}


def test_the_filter_runs_with_its_upstream_and_listen_path_as_argv() -> None:
    plan = _plan()
    assert plan.filter is not None
    assert plan.filter.argv == (HELPER, UPSTREAM, "/run/ginn/1000/agent/ssh.sock")


def test_the_gpg_extra_socket_is_bound_under_gnupg_and_its_directory_is_listed() -> None:
    plan = _plan()
    assert SessionBind(host=GPG_HOST, realm=GPG_REALM, mode="rw", is_dir=False) in plan.binds
    assert plan.parents == [GPG_PARENT]


@pytest.mark.fr("FR-067")
def test_the_gpg_grant_is_the_extra_socket_and_never_the_main_one() -> None:
    """AC-2: the realm gets gpg-agent's restricted extra socket, the path the caller resolved."""
    plan = _plan(kinds={UPSTREAM: SOCKET, GPG_HOST: SOCKET})
    gpg_binds = [b for b in plan.binds if b.realm == GPG_REALM]
    assert gpg_binds == [SessionBind(host=GPG_HOST, realm=GPG_REALM)]
    assert GPG_HOST.endswith(".extra")


def test_no_ssh_agent_is_a_skip_with_no_filter_and_no_bind() -> None:
    plan = _plan(env={})
    assert plan.filter is None
    assert plan.env == {}
    assert [b.realm for b in plan.binds] == [GPG_REALM]
    skip = plan.skipped[0]
    assert (skip.kind, skip.name, skip.missing) == ("agent", "SSH agent", False)


def test_a_missing_host_agent_socket_is_a_retriable_skip() -> None:
    plan = _plan(kinds={GPG_HOST: SOCKET})
    assert plan.filter is None
    skip = next(s for s in plan.skipped if s.name == "SSH agent")
    assert skip.missing is True
    assert skip.realm == SSH_REALM
    assert skip.host == "/run/ginn/1000/agent/ssh.sock"


def test_an_ssh_auth_sock_that_is_not_a_socket_is_refused_without_a_bind() -> None:
    plan = _plan(kinds={UPSTREAM: REGULAR, GPG_HOST: SOCKET})
    assert plan.filter is None
    assert plan.env == {}
    skip = next(s for s in plan.skipped if s.name == "SSH agent")
    assert (skip.reason, skip.missing) == ("SSH_AUTH_SOCK is not a socket", False)


def test_no_gpg_extra_socket_is_a_skip_and_the_ssh_side_is_unaffected() -> None:
    plan = _plan(gpg=None)
    assert plan.filter is not None
    assert plan.parents == []
    assert [b.realm for b in plan.binds] == [SSH_REALM]
    assert any(s.name == "gpg agent" for s in plan.skipped)


def test_a_gpg_extra_path_that_is_not_a_socket_gets_no_bind() -> None:
    plan = _plan(kinds={UPSTREAM: SOCKET, GPG_HOST: REGULAR})
    assert [b.realm for b in plan.binds] == [SSH_REALM]
    assert any(s.reason == "the gpg extra socket is not a socket" for s in plan.skipped)


def test_a_relative_helper_or_listen_path_raises() -> None:
    with pytest.raises(ValueError, match="absolute"):
        agent.plan_realm(
            {"SSH_AUTH_SOCK": UPSTREAM},
            uid=UID,
            helper="ginn-agent-filter",
            listen="/run/ginn/1000/agent/ssh.sock",
            gpg_extra=None,
            file_type=FakeTypes({UPSTREAM: SOCKET}),
        )


@pytest.mark.parametrize(
    ("running", "filter_running", "agent_present", "expected"),
    [
        (set(), False, True, agent.Action.IDLE),
        ({"dev"}, False, True, agent.Action.START),
        ({"dev"}, True, True, agent.Action.KEEP),
        (set(), True, True, agent.Action.STOP),
        ({"dev"}, False, False, agent.Action.IDLE),
        ({"dev"}, True, False, agent.Action.STOP),
    ],
)
def test_the_filter_runs_while_a_development_realm_runs_and_an_agent_exists(
    running: set[str], filter_running: bool, agent_present: bool, expected: agent.Action
) -> None:
    action = agent.decide(
        development_running=running, filter_running=filter_running, agent=agent_present
    )
    assert action is expected


def _spec(listen: Path, upstream: str = UPSTREAM) -> agent.FilterSpec:
    return agent.FilterSpec(upstream, str(listen), (HELPER, upstream, str(listen)))


def _store(record: agent.AgentRecord) -> agent.AgentRecord:
    """The record as a realm record keeps it: written to JSON and read back by the next command."""
    return agent.AgentRecord.from_dict(json.loads(json.dumps(record.to_dict())))


@pytest.fixture
def launcher() -> SleepLauncher:
    """Sleeping stand-ins for the filter. The test owns them, and the fixture stops what is left."""
    if sleep_binary() is None:
        pytest.skip("sleep is needed for the lifecycle tests")
    fake = SleepLauncher()
    yield fake
    fake.close()


def test_the_filter_follows_the_running_development_realms(
    tmp_path: Path, launcher: SleepLauncher
) -> None:
    listen = tmp_path / "ssh.sock"
    record = agent.AgentRecord()
    record, action = agent.sync(record, _spec(listen), development_running=set(), launch=launcher)
    assert (action, launcher.argvs) == (agent.Action.IDLE, [])
    record, action = agent.sync(record, _spec(listen), development_running={"one"}, launch=launcher)
    assert action is agent.Action.START
    assert launcher.argvs == [(HELPER, UPSTREAM, str(listen))]
    record, action = agent.sync(
        record, _spec(listen), development_running={"one", "two"}, launch=launcher
    )
    assert action is agent.Action.KEEP
    assert len(launcher.children) == 1
    record, action = agent.sync(record, _spec(listen), development_running={"two"}, launch=launcher)
    assert action is agent.Action.KEEP
    record, action = agent.sync(record, _spec(listen), development_running=set(), launch=launcher)
    assert action is agent.Action.STOP
    assert record == agent.AgentRecord()
    assert not launcher.children[0].proc.alive()


def test_two_ginn_commands_share_the_filter_the_first_one_started(
    tmp_path: Path, launcher: SleepLauncher
) -> None:
    """Command A starts the filter. Command B, a new process, reads the record and keeps it."""
    listen = tmp_path / "ssh.sock"
    stored, action = agent.sync(
        agent.AgentRecord(), _spec(listen), development_running={"one"}, launch=launcher
    )
    assert action is agent.Action.START
    loaded = _store(stored)
    assert loaded == stored
    kept, action = agent.sync(
        loaded, _spec(listen), development_running={"one", "two"}, launch=NoLaunch()
    )
    assert action is agent.Action.KEEP
    assert kept == agent.AgentRecord(stored.proc, stored.upstream, stored.listen, ("one", "two"))
    assert len(launcher.children) == 1


def test_a_lost_record_does_not_duplicate_or_delete_a_live_filter(tmp_path: Path) -> None:
    """The record was lost, but a filter still listens on the socket: keep it, do not replace it."""
    listen = tmp_path / "ssh.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(listen))
    server.listen(4)
    try:
        record, action = agent.sync(
            agent.AgentRecord(), _spec(listen), development_running={"one"}, launch=NoLaunch()
        )
        assert action is agent.Action.LEFT
        assert record == agent.AgentRecord(members=("one",))
        assert stat.S_ISSOCK(os.lstat(listen).st_mode)
    finally:
        server.close()


def test_a_record_whose_process_is_gone_starts_a_new_filter(
    tmp_path: Path, launcher: SleepLauncher
) -> None:
    listen = tmp_path / "ssh.sock"
    gone = spawn_sleeper()
    dead = Proc.of(gone.pid)
    assert dead is not None
    gone.kill()
    gone.wait(timeout=10)
    stale = agent.AgentRecord(dead, UPSTREAM, str(listen))
    record, action = agent.sync(
        _store(stale), _spec(listen), development_running={"one"}, launch=launcher
    )
    assert action is agent.Action.START
    assert record.proc is not None and record.proc.alive()
    assert record.proc != dead


def test_a_reused_pid_is_not_taken_for_the_filter_and_is_not_signalled(
    tmp_path: Path, launcher: SleepLauncher
) -> None:
    """The record names this test process with another start time. It is not the filter, so the
    sync starts one, and it never signals the pid it does not own."""
    listen = tmp_path / "ssh.sock"
    reused = agent.AgentRecord(Proc(os.getpid(), 1), UPSTREAM, str(listen))
    record, action = agent.sync(reused, _spec(listen), development_running={"one"}, launch=launcher)
    assert action is agent.Action.START
    assert record.proc is not None and record.proc.pid != os.getpid()


def test_a_changed_upstream_restarts_the_filter(tmp_path: Path, launcher: SleepLauncher) -> None:
    """SSH_AUTH_SOCK changes after a re-login. The running filter must not keep the old upstream."""
    listen = tmp_path / "ssh.sock"
    first, _ = agent.sync(
        agent.AgentRecord(), _spec(listen), development_running={"one"}, launch=launcher
    )
    old_child = launcher.children[0]
    second, action = agent.sync(
        _store(first),
        _spec(listen, UPSTREAM_NEW),
        development_running={"one"},
        launch=launcher,
    )
    assert action is agent.Action.RESTART
    assert not old_child.proc.alive()
    assert len(launcher.children) == 2
    assert second.upstream == UPSTREAM_NEW
    assert launcher.argvs[-1] == (HELPER, UPSTREAM_NEW, str(listen))


def test_a_changed_upstream_with_no_development_realm_stops_the_filter(
    tmp_path: Path, launcher: SleepLauncher
) -> None:
    listen = tmp_path / "ssh.sock"
    first, _ = agent.sync(
        agent.AgentRecord(), _spec(listen), development_running={"one"}, launch=launcher
    )
    record, action = agent.sync(
        _store(first), _spec(listen, UPSTREAM_NEW), development_running=set(), launch=launcher
    )
    assert action is agent.Action.STOP
    assert record == agent.AgentRecord()
    assert len(launcher.children) == 1


def test_stop_removes_the_filter_socket_that_nothing_listens_on(
    tmp_path: Path, launcher: SleepLauncher
) -> None:
    listen = tmp_path / "ssh.sock"
    first, _ = agent.sync(
        agent.AgentRecord(), _spec(listen), development_running={"one"}, launch=launcher
    )
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    leftover.bind(str(listen))
    leftover.close()
    agent.sync(_store(first), None, development_running=set(), launch=launcher)
    assert not listen.exists()


def test_a_regular_file_at_the_listen_path_is_kept(tmp_path: Path, launcher: SleepLauncher) -> None:
    other = tmp_path / "other.sock"
    other.write_text("keep me", encoding="utf-8")
    agent.sync(agent.AgentRecord(), _spec(other), development_running={"one"}, launch=launcher)
    assert other.read_text(encoding="utf-8") == "keep me"


@pytest.mark.fr("NFR-SEC-007")
def test_the_compiled_filter_is_shared_by_two_commands_and_stopped_by_the_last(
    tmp_path: Path,
) -> None:
    """Command A starts the compiled filter, and it answers a list request through a fake agent.
    Command B shares it from the stored record. Command C stops it, and the socket is gone."""
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    helper = build_filter(tmp_path, SOURCE, cc)
    upstream = tmp_path / "agent.sock"
    listen = tmp_path / "filter.sock"
    log = tmp_path / "filter.log"
    spec = agent.FilterSpec(str(upstream), str(listen), (str(helper), str(upstream), str(listen)))
    real_launch = agent.filter_launcher(str(listen), str(log))
    started: list[Proc] = []

    def launch(argv: Sequence[str]) -> Child:
        child = real_launch(argv)
        started.append(child.proc)
        return child

    try:
        with FakeAgent(upstream):
            stored, action = agent.sync(
                agent.AgentRecord(), spec, development_running={"one"}, launch=launch
            )
            assert action is agent.Action.START
            assert exchange(listen, encode_frame(REQUEST_IDENTITIES), 1) == [(12, b"")]
            shared, action = agent.sync(
                _store(stored), spec, development_running={"one", "two"}, launch=NoLaunch()
            )
            assert action is agent.Action.KEEP
            expected = agent.AgentRecord(
                stored.proc, stored.upstream, stored.listen, ("one", "two")
            )
            assert shared == expected
            stopped, action = agent.sync(
                _store(shared), spec, development_running=set(), launch=launch
            )
            assert action is agent.Action.STOP
            assert stopped == agent.AgentRecord()
    finally:
        # A failed assertion must not leave the filter running: every helper started here stops.
        for record in started:
            stop_group(record)
    assert not listen.exists()
    with pytest.raises(OSError):
        exchange(listen, encode_frame(REQUEST_IDENTITIES), 1)


def test_a_filter_that_cannot_start_raises_the_agent_error(tmp_path: Path) -> None:
    if not Path("/bin/false").exists():
        pytest.skip("/bin/false is needed for the failing helper")
    launch = agent.filter_launcher(str(tmp_path / "ssh.sock"), str(tmp_path / "filter.log"))
    with pytest.raises(GinnError) as caught:
        launch(["/bin/false", "/nonexistent/agent.sock", str(tmp_path / "ssh.sock")])
    assert caught.value.code == "GINN-E-AGENT-FILTER"
    assert "exited before it listened" in caught.value.message


def test_a_stored_record_with_bad_fields_is_refused() -> None:
    with pytest.raises(ValueError):
        agent.AgentRecord.from_dict({"proc": None, "upstream": 3, "listen": "/x"})
    with pytest.raises(ValueError):
        agent.AgentRecord.from_dict({"proc": {"pid": True, "start_ticks": 1}, "upstream": "u"})


def test_the_realm_paths_are_fixed_under_the_runtime_directory() -> None:
    assert agent.realm_paths(UID) == (SSH_REALM, GPG_REALM)
    assert os.path.dirname(GPG_REALM) == GPG_PARENT


def test_the_gpg_socket_path_comes_from_gpgconf_and_a_bad_answer_is_none() -> None:
    assert agent.GPGCONF_ARGV == ("gpgconf", "--list-dirs", "agent-extra-socket")
    assert agent.extra_socket_from(f"{GPG_HOST}\n") == GPG_HOST
    assert agent.extra_socket_from("") is None
    assert agent.extra_socket_from("relative/S.gpg-agent.extra\n") is None


@pytest.mark.fr("FR-067")
def test_a_missing_filter_helper_skips_the_ssh_grant_and_keeps_the_gpg_grant() -> None:
    """A missing helper is a skip, not a failure (ADR-008): no filter, no SSH bind, gpg kept."""
    plan = agent.plan_realm(
        {"SSH_AUTH_SOCK": UPSTREAM},
        uid=UID,
        helper=HELPER,
        listen="/run/ginn/1000/agent/ssh.sock",
        gpg_extra=GPG_HOST,
        file_type=FakeTypes({UPSTREAM: SOCKET, GPG_HOST: SOCKET}),
    )
    assert plan.filter is None
    assert [b.realm for b in plan.binds] == [GPG_REALM]
    (skip,) = [s for s in plan.skipped if s.name == "SSH agent"]
    assert skip.reason == agent.HELPER_MISSING
    assert (skip.kind, skip.missing) == ("agent", False)
