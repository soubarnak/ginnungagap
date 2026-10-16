# SPDX-License-Identifier: Apache-2.0
"""Enter, exec and `--root`: runtime/enter.py against FakePodman (T4.6, FR-042, FR-048, FR-049).

The fake runs no podman. Its helper rows come from the host, as ginn-rebind reports them, and only
paths under tmp_path answer, so an attach runs for real. No test runs podman, sudo or the helper
(AGENTS.md). Every test pins XDG_RUNTIME_DIR to an empty directory under tmp_path, so a live session
socket on the test host never becomes a grant.

Covered: the exit status of the command, the login shell and the argv, `--root` as container root,
HOME and the workdir, the proxy names as `--env NAME`, the refusals (unknown realm, the DR-020
states), auto-start, the lazy re-attach (only when a host source changed, none otherwise), a failed
re-attach that leaves the realm running, a services-on realm that is never stopped, a stop that
races the pre-check, and the share list rebuilt at enter.
"""

import json
import os
import signal
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.domain.grants import GrantRecord, SourceIdentity
from ginn.errors import GinnError
from ginn.errors import Warning as GinnWarning
from ginn.ops._mutating import OpEnv
from ginn.ops.lifecycle import HostUser, LifecycleHooks, start_realm
from ginn.ops.security_ops import proxy_state_path
from ginn.podman.adapter import PODMAN_ERROR_EXIT, CreateSpec, ExecTarget
from ginn.podman.models import ContainerInfo, ExecResult
from ginn.runtime import attach
from ginn.runtime import enter as enter_module
from ginn.runtime._enter_plan import display_name, exec_user, login_argv, share_after
from ginn.runtime.attach import AttachResult
from ginn.runtime.enter import BUSY_STEP_S, BUSY_WAIT_S, EnterHooks, enter_realm
from ginn.runtime.env import SessionEnv
from ginn.runtime.idle import IdleHooks
from ginn.runtime.mounts import Skipped
from ginn.runtime.realm_security import SecurityHooks
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.security._proc import Proc
from ginn.services.runit_user import service_dir, write_service_dir
from ginn.services.supervisor import Result, SupervisorHooks
from ginn.store.locks import realm_lock
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from tests.fakes.clock import FakeClock
from tests.fakes.fake_podman import ExecCall, FakePodman

DIRECT_TARGET = ExecTarget(realm="test", services=False, mutating=False)

UID = 1000
GID = 1000
USER = "alice"
DIGEST = "registry.example/debian@sha256:" + "ab" * 32
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
BASE_MOUNTINFO = (
    "36 35 98:0 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
    "37 36 98:1 / /opt rw - ext4 /dev/root rw\n"
)
SCOPE = ("etc", "var/db/xbps", "var/service", "usr/local", "opt")
SKIP_DOWNLOADS = "share skipped: ~/Downloads (missing)"


def _helper_rows(argv: Sequence[str], root: str) -> str:
    """The helper's stdout for `argv`: one row per attach group, read from the host."""
    rows: list[str] = []
    tokens = list(argv[2:])
    while tokens:
        if tokens[0] == "--unmount":
            rows.append("missing")
            tokens = tokens[2:]
            continue
        while tokens[0].startswith("--"):
            tokens = tokens[1:]
        host = tokens[0]
        tokens = tokens[2:]
        if host.startswith(root) and os.path.exists(host):
            st = os.stat(host)
            rows.append(f"attached {st.st_dev} {st.st_ino}")
        else:
            rows.append("missing")
    return "".join(f"{index} {row}\n" for index, row in enumerate(rows))


class EnterPodman(FakePodman):
    """The fake plus the helper's answers from the host (test seam only).

    A queued `unshare_results` entry wins. Otherwise the helper rows come from the host, under
    `root`.
    """

    def __init__(self, *, root: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.root = root

    def unshare(self, argv: Sequence[str]) -> ExecResult:
        if self.unshare_results:
            return super().unshare(argv)
        self._record("unshare", tuple(argv))
        return ExecResult(0, _helper_rows(argv, self.root), "")


class AtTheLockedListing(EnterPodman):
    """Runs `at_lock` at the listing that enter makes under the realm lock (the second `ps`).

    `listings` counts the `ps` calls since it was last reset. `at_lock` stands for an operation that
    ran between enter's look and its lock, such as a stop or another enter's re-attach.
    """

    def __init__(self, *, root: str, **kwargs: Any) -> None:
        super().__init__(root=root, **kwargs)
        self.listings = 0
        self.at_lock: Callable[[], None] | None = None

    def ps(self, label_filter: Mapping[str, str] | None = None) -> list[ContainerInfo]:
        self.listings += 1
        if self.listings == 2 and self.at_lock is not None:
            self.at_lock()
        return super().ps(label_filter)


class StopsDuringExec(EnterPodman):
    """The container stops as the exec starts, and podman reports the failure as `exec_status`:
    125 for its own failure, or 255 as podman 5.8.8 answers an exec on a stopped container."""

    exec_status: int = PODMAN_ERROR_EXIT

    def exec_interactive(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> int:
        self.stop(container_id, 0)
        self._record("exec_interactive", container_id, tuple(argv))
        return self.exec_status


class FakeTime:
    """A clock and a sleep that move together, so a wait is timed without real delays."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    """Owned Paths under tmp_path, with every XDG variable pinned (as in test_ops_lifecycle)."""
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
def host(tmp_path: Path) -> Path:
    root = tmp_path / "host"
    for scope in SCOPE:
        (root / scope).mkdir(parents=True)
    (root / "etc" / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    return root


@pytest.fixture
def env(paths: OwnedPaths, host: Path) -> OpEnv:
    return OpEnv.for_paths(paths, USER, root_dir=host, clock=FakeClock())


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """The host home of the user. Under tmp_path, so it is outside Owned Paths."""
    path = tmp_path / "home" / USER
    path.mkdir(parents=True)
    return path


@pytest.fixture
def fake(tmp_path: Path) -> EnterPodman:
    return EnterPodman(root=str(tmp_path))


@pytest.fixture
def mountinfo(tmp_path: Path) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(BASE_MOUNTINFO, encoding="utf-8")
    return path


@pytest.fixture
def user(tmp_path: Path, home: Path) -> HostUser:
    """The host user. The runtime variable is an empty directory, and LANG and TZ are set."""
    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700, exist_ok=True)
    host_env = {"XDG_RUNTIME_DIR": str(runtime), "LANG": "C.UTF-8", "TZ": "UTC"}
    return HostUser(uid=UID, host_home=home, host_env=host_env)


@pytest.fixture(autouse=True)
def host_identity_is_read_only_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Source identities are read with os.stat. Only paths under tmp_path answer, so the host's
    /media and /mnt never attach. The read goes through the module attribute, so the pre-check
    sees the same answers as the attach (ADR-008)."""
    real = attach.read_identity

    def under_tmp(host: str, *, mountinfo: Path = attach.MOUNTINFO) -> attach.SourceIdentity | None:
        if not host.startswith(str(tmp_path)):
            return None
        return real(host, mountinfo=mountinfo)

    monkeypatch.setattr(attach, "read_identity", under_tmp)


@pytest.fixture
def lifecycle_hooks(tmp_path: Path, mountinfo: Path) -> LifecycleHooks:
    return LifecycleHooks(
        exists=lambda p: p.startswith(str(tmp_path)) and os.path.exists(p),
        mountinfo=mountinfo,
        security=SecurityHooks(gpg_extra=lambda: None),
    )


def make_hooks(
    lifecycle: LifecycleHooks,
    *,
    tty: bool = False,
    observed: list[tuple[str, float]] | None = None,
    time_: FakeTime | None = None,
    sleep: Callable[[float], None] | None = None,
) -> EnterHooks:
    """The enter seams: a fixed terminal answer, every observation kept in `observed`, and, when
    `time_` is given, a clock and sleep that move together. `sleep` overrides the sleep."""
    sink = observed if observed is not None else []
    observe = lambda name, seconds: sink.append((name, seconds))  # noqa: E731 - a test seam
    if time_ is None:
        return EnterHooks(lifecycle=lifecycle, isatty=lambda: tty, observe=observe)
    return EnterHooks(
        lifecycle=lifecycle,
        isatty=lambda: tty,
        observe=observe,
        clock=time_.clock,
        sleep=sleep or time_.sleep,
    )


def _change_hosts(host: Path) -> None:
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")


def _record(
    name: str,
    state: RealmState,
    container_id: str | None,
    *,
    previous: RealmState | None = None,
    distro: str = "debian",
    services: bool = False,
    share: list[str] | None = None,
    grants: list[GrantRecord] | None = None,
) -> RealmRecord:
    settings = RealmSettings(
        name=RealmName(name),
        distro=DistroId(distro),
        tier=Tier.BASIC,
        services=services,
        autostart=False,
        everything=False,
    )
    return RealmRecord(
        settings=settings,
        state=state,
        previous_state=previous,
        image_ref=DIGEST,
        image_digest=DIGEST.partition("@")[2],
        container_id=container_id,
        generation=1,
        created_at=T0,
        updated_at=T0,
        share_skipped=list(share or []),
        grants=list(grants or []),
    )


def make_realm(
    env: OpEnv,
    fake: FakePodman,
    name: str,
    state: RealmState,
    *,
    running: bool,
    **kwargs: Any,
) -> RealmRecord:
    """A stored realm with a fake container. `running` starts the container in the fake."""
    labels = {LABEL_REALM: name, LABEL_DISTRO: "debian", LABEL_GENERATION: "1"}
    spec = CreateSpec(
        name=f"ginn-{name}-g1", image_digest_ref=DIGEST, network="host", labels=labels
    )
    cid = fake.create(spec)
    if running:
        fake.start(cid)
    (env.paths.homes / name).mkdir(parents=True, exist_ok=True)
    record = _record(name, state, cid, **kwargs)
    RealmStore(env.paths, env.clock).save(record)
    fake.calls.clear()
    fake.exec_calls.clear()
    return record


def stored(env: OpEnv, name: str) -> RealmRecord:
    return RealmStore(env.paths, env.clock).load(RealmName(name))


def container_id(env: OpEnv, name: str) -> str:
    cid = stored(env, name).container_id
    assert cid is not None
    return cid


def audit_of(env: OpEnv, op: str) -> list[Any]:
    return [entry for entry in env.audit.read() if entry.op == op]


def exec_call(fake: EnterPodman) -> ExecCall:
    """The last command that enter ran in the realm."""
    calls = [call for call in fake.exec_calls if call.op == "exec_interactive"]
    assert calls, "enter ran no command"
    return calls[-1]


def enter(
    env: OpEnv,
    fake: EnterPodman,
    name: str,
    user: HostUser,
    hooks: EnterHooks,
    **kwargs: Any,
) -> int:
    return enter_realm(env, fake, RealmName(name), session=user, gid=GID, hooks=hooks, **kwargs)


@pytest.mark.fr("FR-049")
@pytest.mark.fr("IR-011")
@pytest.mark.parametrize("status", [0, 7, 137])
def test_the_exit_status_of_the_command_comes_back_unchanged(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    status: int,
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    command = ["sh", "-c", f"exit {status}"]
    fake.exec_results[tuple(command)] = ExecResult(status, "", "")

    code = enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=command)

    assert code == status
    assert exec_call(fake).argv == tuple(command)


@pytest.mark.fr("FR-048")
@pytest.mark.fr("FR-049")
def test_no_command_runs_the_login_shell_as_the_realm_user_in_the_home(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)

    assert enter(env, fake, "lab", user, make_hooks(lifecycle_hooks)) == 0

    call = exec_call(fake)
    assert call.argv == ("/bin/bash", "-l")
    assert call.user == f"{UID}:{GID}"
    assert call.workdir == str(home)
    assert call.env["HOME"] == str(home)
    assert call.env["XDG_RUNTIME_DIR"] == f"/run/user/{UID}"
    assert call.env["LANG"] == "C.UTF-8"
    assert call.env["TZ"] == "UTC"
    assert call.interactive is True
    assert call.tty is False


@pytest.mark.fr("FR-042")
def test_root_runs_as_container_root_in_its_own_home_and_never_in_the_realm_home(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
) -> None:
    """Under keep-id, container root is a subordinate uid on the host, so `--root` starts in /root
    and never in the bound Realm Home (ROOT_HOME). The user's own exec keeps the host home (FR-048).
    """
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    command = ["pacman", "-S", "--noconfirm", "htop"]

    assert enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=command, root=True) == 0

    call = exec_call(fake)
    assert call.user == "0:0"
    assert call.env["HOME"] == "/root"
    assert call.workdir == "/root"
    assert "create" not in fake.ops()

    assert enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=command) == 0
    user_call = exec_call(fake)
    assert user_call.user == f"{UID}:{GID}"
    assert user_call.env["HOME"] == str(home)
    assert user_call.workdir == str(home)


@pytest.mark.fr("FR-042")
@pytest.mark.fr("FR-048")
def test_root_without_a_command_runs_the_login_shell_in_the_root_home(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)

    assert enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), root=True) == 0

    call = exec_call(fake)
    assert call.argv == ("/bin/bash", "-l")
    assert (call.user, call.env["HOME"], call.workdir) == ("0:0", "/root", "/root")


@pytest.mark.fr("IR-010")
def test_an_empty_command_is_a_usage_error_and_nothing_runs(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)

    with pytest.raises(GinnError) as info:
        enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=[])

    assert info.value.code == "GINN-E-USAGE"
    assert fake.exec_calls == []


@pytest.mark.fr("FR-048")
def test_a_terminal_on_both_ends_gives_the_exec_a_tty(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)

    enter(env, fake, "lab", user, make_hooks(lifecycle_hooks, tty=True), argv=["true"])

    assert exec_call(fake).tty is True


@pytest.mark.fr("FR-025")
def test_an_unknown_realm_is_refused_before_any_podman_call(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    with pytest.raises(GinnError) as info:
        enter(env, fake, "ghost", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert info.value.code == "GINN-E-NO-SUCH-REALM"
    assert fake.calls == []


@pytest.mark.fr("FR-049")
@pytest.mark.fr("IR-011")
def test_a_kali_realm_runs_a_command_and_returns_its_status_like_debian(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True, distro="kali")
    command = ["sh", "-c", "exit 3"]
    fake.exec_results[tuple(command)] = ExecResult(3, "", "")

    assert enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=command) == 3

    assert exec_call(fake).argv == tuple(command)


@pytest.mark.fr("FR-017")
@pytest.mark.fr("FR-048")
def test_a_stopped_kali_realm_starts_then_opens_the_login_shell_like_debian(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False, distro="kali")

    assert enter(env, fake, "lab", user, make_hooks(lifecycle_hooks)) == 0

    assert fake.ops().count("start") == 1
    assert exec_call(fake).argv == ("/bin/bash", "-l")
    assert stored(env, "lab").state is RealmState.RUNNING


@pytest.mark.fr("DR-020")
@pytest.mark.parametrize("state", [RealmState.BROKEN, RealmState.CREATING])
def test_a_state_that_does_not_allow_enter_is_refused_before_any_podman_call(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    state: RealmState,
) -> None:
    make_realm(env, fake, "lab", state, running=False)

    with pytest.raises(GinnError) as info:
        enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert info.value.code == "GINN-E-REALM-STATE"
    assert info.value.details["action"] == "enter"
    assert fake.calls == []


@pytest.mark.fr("FR-017")
@pytest.mark.fr("FR-049")
def test_a_stopped_realm_starts_once_with_its_grants_and_then_runs_the_command(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)

    status = enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert status == 0
    assert fake.ops().count("start") == 1
    assert fake.ops().index("start") < fake.ops().index("exec_interactive")
    record = stored(env, "lab")
    assert record.state is RealmState.RUNNING
    assert SKIP_DOWNLOADS in record.share_skipped
    assert exec_call(fake).container_id == container_id(env, "lab")
    (entry,) = audit_of(env, "start")
    assert entry.outcome == "ok"


@pytest.mark.fr("NFR-004")
@pytest.mark.fr("FR-066")
def test_a_running_realm_with_no_host_change_costs_one_listing_and_writes_nothing(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    start_realm(env, fake, RealmName("lab"), session=user, hooks=lifecycle_hooks)
    fake.calls.clear()
    fake.exec_calls.clear()
    before = (env.paths.realms / "lab.json").read_bytes()
    audits = len(list(env.audit.read()))
    observed: list[tuple[str, float]] = []

    enter(env, fake, "lab", user, make_hooks(lifecycle_hooks, observed=observed), argv=["true"])

    assert fake.ops() == ["ps", "exec_interactive"]
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert len(list(env.audit.read())) == audits
    assert [name for name, _ in observed] == ["listing", "precheck", "exec"]
    assert all(seconds >= 0 for _, seconds in observed)


@pytest.mark.fr("FR-006")
@pytest.mark.fr("FR-017")
def test_a_host_change_during_a_re_attach_is_notified_before_the_command_runs(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    start_realm(env, fake, RealmName("lab"), session=user, hooks=lifecycle_hooks)
    (home / "Downloads").mkdir()
    real_unshare = fake.unshare
    real_exec = fake.exec_interactive
    events: list[str] = []
    notices: list[Any] = []

    def unshare_and_change(argv: Any) -> Any:
        (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")
        return real_unshare(argv)

    def exec_and_record(*args: Any, **kwargs: Any) -> int:
        events.append("exec")
        return real_exec(*args, **kwargs)

    def notify(notice: Any) -> None:
        events.append(notice.code)
        notices.append(notice)

    monkeypatch.setattr(fake, "unshare", unshare_and_change)
    monkeypatch.setattr(fake, "exec_interactive", exec_and_record)
    hooks = replace(make_hooks(lifecycle_hooks), notify=notify)

    assert enter(env, fake, "lab", user, hooks, argv=["true"]) == 0

    assert events == ["GINN-W-HOST-CHANGED", "exec"]
    assert "/etc/hosts" in notices[0].message


@pytest.mark.fr("FR-064")
@pytest.mark.fr("FR-066")
def test_a_source_that_appears_after_start_is_attached_at_enter_and_not_again(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    start_realm(env, fake, RealmName("lab"), session=user, hooks=lifecycle_hooks)
    assert SKIP_DOWNLOADS in stored(env, "lab").share_skipped
    (home / "Downloads").mkdir()
    fake.calls.clear()

    enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    (unshare,) = [call for call in fake.calls if call.op == "unshare"]
    assert "--force" in unshare.args[0]
    record = stored(env, "lab")
    downloads = [g for g in record.grants if g.realm == str(home / "Downloads")]
    assert downloads[0].identity is not None
    assert SKIP_DOWNLOADS not in record.share_skipped
    (entry,) = audit_of(env, "reattach")
    assert entry.outcome == "ok"

    fake.calls.clear()
    enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])
    assert "unshare" not in fake.ops()


@pytest.mark.fr("FR-064")
@pytest.mark.fr("FR-066")
def test_a_source_that_vanishes_is_unmounted_and_recorded_as_skipped_at_enter(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
) -> None:
    (home / "Downloads").mkdir()
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    start_realm(env, fake, RealmName("lab"), session=user, hooks=lifecycle_hooks)
    (home / "Downloads").rmdir()
    fake.calls.clear()

    enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert "unshare" in fake.ops()
    record = stored(env, "lab")
    assert SKIP_DOWNLOADS in record.share_skipped
    downloads = [g for g in record.grants if g.realm == str(home / "Downloads")]
    assert downloads[0].identity is None


@pytest.mark.fr("FR-066")
@pytest.mark.fr("FR-081")
def test_a_failed_reattach_fails_this_enter_and_leaves_the_realm_running(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    start_realm(env, fake, RealmName("lab"), session=user, hooks=lifecycle_hooks)
    (home / "Downloads").mkdir()
    fake.unshare_results.append(ExecResult(1, "", "ginn-rebind: cannot move the mount\n"))
    fake.calls.clear()
    fake.exec_calls.clear()

    with pytest.raises(GinnError) as info:
        enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert info.value.code == "GINN-E-ATTACH-FAILED"
    assert fake.containers[container_id(env, "lab")].state.value == "running"
    assert "stop" not in fake.ops()
    assert fake.exec_calls == []
    assert stored(env, "lab").state is RealmState.RUNNING
    (entry,) = audit_of(env, "reattach")
    assert entry.outcome == "error"
    assert entry.error_code == "GINN-E-ATTACH-FAILED"
    assert env.journal.pending() == []


@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-081")
def test_enter_runs_a_command_in_a_running_services_on_realm_without_a_refusal(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True, services=True)

    status = enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert status == 0
    assert "start" not in fake.ops()
    assert "stop" not in fake.ops()
    assert exec_call(fake).container_id == container_id(env, "lab")
    assert stored(env, "lab").state is RealmState.RUNNING


@pytest.mark.fr("FR-081")
@pytest.mark.fr("FR-049")
def test_enter_starts_a_stopped_services_on_realm_through_sv_then_attaches_the_session_grants(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    """The supervised start runs under runit's sparse environment, so it attaches no display. The
    enter that follows attaches it through the re-attach path (ADR-008 amendment, FR-081)."""
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False, services=True)
    write_service_dir(env.paths, RealmName("lab"), "/usr/bin/ginn")
    supervise = service_dir(env.paths, RealmName("lab")) / "supervise"
    supervise.mkdir(exist_ok=True)
    os.mkfifo(supervise / "ok")
    runtime = Path(user.host_env["XDG_RUNTIME_DIR"])
    os.mknod(runtime / "wayland-0", stat.S_IFSOCK | 0o600)
    sparse = HostUser(uid=user.uid, host_home=user.host_home, host_env={})
    argvs: list[list[str]] = []

    def run(argv: Sequence[str], timeout: float) -> Result:
        argvs.append(list(argv))
        if argv[1] == "status":
            return Result(0, "down: x: 0s, normally up\n", "")
        # The supervised process: `ginn _service-run` starts the realm with runit's environment.
        start_realm(
            env, fake, RealmName("lab"), session=sparse, hooks=lifecycle_hooks, supervised=True
        )
        return Result(0, "ok: run: x: (pid 1) 0s\n", "")

    supervised = replace(
        lifecycle_hooks,
        supervisor=SupervisorHooks(
            sv="/usr/bin/sv", run=run, clock=FakeClock(), sleep=lambda _seconds: None
        ),
    )
    with_display = replace(user, host_env={**user.host_env, "WAYLAND_DISPLAY": "wayland-0"})

    status = enter(env, fake, "lab", with_display, make_hooks(supervised), argv=["true"])

    assert status == 0
    assert [argv[3] for argv in argvs if argv[1] != "status"] == ["up"]
    assert fake.ops().count("start") == 1, "the supervised start made the one start"
    assert stored(env, "lab").state is RealmState.RUNNING
    (reattach,) = audit_of(env, "reattach")
    assert reattach.outcome == "ok"
    assert exec_call(fake).container_id == container_id(env, "lab")


@pytest.mark.fr("FR-081")
@pytest.mark.fr("FR-066")
def test_enter_never_stops_a_services_on_realm_when_its_reattach_fails(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
) -> None:
    (home / "Downloads").mkdir()
    grant = GrantRecord("share", str(home / "Downloads"), str(home / "Downloads"), "rw", True)
    make_realm(
        env,
        fake,
        "lab",
        RealmState.RUNNING,
        running=True,
        services=True,
        grants=[grant],
    )
    fake.unshare_results.append(ExecResult(1, "", "ginn-rebind: failed\n"))

    with pytest.raises(GinnError) as info:
        enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert info.value.code == "GINN-E-ATTACH-FAILED"
    assert "stop" not in fake.ops()
    assert fake.containers[container_id(env, "lab")].state.value == "running"


@pytest.mark.fr("DR-033")
@pytest.mark.fr("FR-017")
def test_a_realm_that_stops_between_the_pre_check_and_the_lock_is_started_instead(
    env: OpEnv,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
    user: HostUser,
    home: Path,
) -> None:
    fake = AtTheLockedListing(root=str(tmp_path))
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    start_realm(env, fake, RealmName("lab"), session=user, hooks=lifecycle_hooks)
    (home / "Downloads").mkdir()
    fake.calls.clear()
    fake.exec_calls.clear()
    fake.listings = 0
    fake.at_lock = lambda: fake.stop(container_id(env, "lab"), 0)

    status = enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert status == 0
    assert "start" in fake.ops()
    assert exec_call(fake).container_id == container_id(env, "lab")


@pytest.mark.fr("FR-061")
@pytest.mark.fr("NFR-SEC-005")
def test_proxy_names_go_on_the_argv_and_no_value_reaches_an_argument(
    env: OpEnv,
    fake: EnterPodman,
    tmp_path: Path,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
) -> None:
    proxy = "http://user:hunter2@proxy.example:3128"
    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700, exist_ok=True)
    host_env = {"XDG_RUNTIME_DIR": str(runtime), "http_proxy": proxy, "NO_PROXY": "localhost"}
    user = HostUser(uid=UID, host_home=home, host_env=host_env)
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)

    enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    call = exec_call(fake)
    assert call.pass_env == ("http_proxy", "NO_PROXY")
    assert "http_proxy" not in call.env
    assert "NO_PROXY" not in call.env
    shown = json.dumps([call.argv, call.env, call.pass_env, call.user, call.workdir])
    assert "hunter2" not in shown
    assert "proxy.example" not in shown


@pytest.mark.fr("FR-066")
def test_the_pre_check_is_true_only_for_a_changed_grant(
    tmp_path: Path, home: Path, mountinfo: Path
) -> None:
    session = SessionEnv(env={}, binds=[])
    grant = GrantRecord("share", str(home / "Downloads"), str(home / "Downloads"), "rw", True)

    def allow(_: GrantRecord) -> str | None:
        return None

    assert attach.needs_reattach([grant], session, admit=allow, mountinfo=mountinfo) is False
    (home / "Downloads").mkdir()
    assert attach.needs_reattach([grant], session, admit=allow, mountinfo=mountinfo) is True
    attached = GrantRecord(
        "share",
        str(home / "Downloads"),
        str(home / "Downloads"),
        "rw",
        True,
        identity=attach.read_identity(str(home / "Downloads"), mountinfo=mountinfo),
    )
    assert attach.needs_reattach([attached], session, admit=allow, mountinfo=mountinfo) is False


@pytest.mark.fr("FR-064")
def test_a_share_message_takes_the_name_the_start_gives_it(home: Path) -> None:
    downloads = str(home / "Downloads")
    assert display_name("share", downloads, downloads, home) == "~/Downloads"
    assert display_name("share", "/run/media/alice", "/run/media/alice", home) == "/run/media/alice"
    session_host = "/run/user/1000/wayland-1"
    assert display_name("session", session_host, "/run/user/1000/wayland-0", home) == session_host


@pytest.mark.fr("FR-064")
def test_share_after_redoes_the_listed_skips_and_keeps_a_refused_path(home: Path) -> None:
    downloads = str(home / "Downloads")
    before = [GrantRecord("share", downloads, downloads, "rw", True)]
    after = [GrantRecord("share", downloads, downloads, "rw", True, identity=SourceIdentity(1, 2))]
    session = SessionEnv(env={}, binds=[])
    result = AttachResult(records=after, skipped=[])
    refused = "share skipped: ~/Projects (a symbolic link, not a mount root)"

    out = share_after([SKIP_DOWNLOADS, refused], before, after, session, result, home)

    assert out == [refused]


@pytest.mark.fr("FR-064")
def test_share_after_writes_a_session_skip_first_and_drops_the_stored_one(home: Path) -> None:
    x11 = "/tmp/.X11-unix"  # noqa: S108 - the fixed X11 socket directory, not a temp file
    session = SessionEnv(
        env={},
        binds=[],
        skipped=[Skipped("session", "DISPLAY", "only a local display is granted (:N)", realm=x11)],
    )
    stored_skip = ["session skipped: DISPLAY (an old reason)"]
    out = share_after(stored_skip, [], [], session, AttachResult([], []), home)
    assert out == ["session skipped: DISPLAY (only a local display is granted (:N))"]


@pytest.mark.fr("FR-042")
@pytest.mark.fr("FR-048")
def test_the_user_argument_and_the_login_shell_are_built_as_the_realm_expects() -> None:
    assert exec_user(1000, 1000, root=False) == "1000:1000"
    assert exec_user(1000, 1000, root=True) == "0:0"
    assert login_argv("/bin/bash") == ["/bin/bash", "-l"]


def _pending_realm(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
) -> None:
    """A running realm whose Downloads folder appeared after its start: a re-attach is pending."""
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    start_realm(env, fake, RealmName("lab"), session=user, hooks=lifecycle_hooks)
    (home / "Downloads").mkdir()
    fake.calls.clear()
    fake.exec_calls.clear()


def _stopped_realm(env: OpEnv, fake: EnterPodman) -> None:
    """A stopped realm, for a start that another operation holds the lock for."""
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)


@pytest.mark.fr("DR-033")
@pytest.mark.parametrize("holder", ["reattach", "start"])
def test_a_lock_held_by_a_reattach_or_a_start_is_waited_for_and_then_the_command_runs(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
    holder: str,
) -> None:
    if holder == "reattach":
        _pending_realm(env, fake, user, lifecycle_hooks, home)
    else:
        _stopped_realm(env, fake)
    time_ = FakeTime()
    held = ExitStack()
    held.enter_context(realm_lock(env.paths, RealmName("lab"), holder))

    def the_other_operation_finishes(seconds: float) -> None:
        # The holder ends, and another enter runs the same way while this one waits.
        time_.sleep(seconds)
        held.close()
        enter(env, fake, "lab", user, make_hooks(lifecycle_hooks, time_=time_), argv=["true"])

    hooks = make_hooks(lifecycle_hooks, time_=time_, sleep=the_other_operation_finishes)
    assert enter(env, fake, "lab", user, hooks, argv=["true"]) == 0

    assert time_.sleeps == [BUSY_STEP_S]
    assert stored(env, "lab").state is RealmState.RUNNING
    assert fake.ops().count("exec_interactive") == 2  # the other enter, then this one
    if holder == "reattach":
        assert fake.ops().count("unshare") == 1
    else:
        assert fake.ops().count("start") == 1


@pytest.mark.fr("DR-033")
@pytest.mark.parametrize("holder", ["reattach", "start"])
def test_a_holder_that_outlasts_the_wait_is_refused_with_realm_busy(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
    holder: str,
) -> None:
    if holder == "reattach":
        _pending_realm(env, fake, user, lifecycle_hooks, home)
    else:
        _stopped_realm(env, fake)
    time_ = FakeTime()
    held = ExitStack()
    held.enter_context(realm_lock(env.paths, RealmName("lab"), holder))
    try:
        with pytest.raises(GinnError) as info:
            enter(env, fake, "lab", user, make_hooks(lifecycle_hooks, time_=time_), argv=["true"])
    finally:
        held.close()

    assert info.value.code == "GINN-E-REALM-BUSY"
    assert info.value.details["op"] == holder
    assert time_.sleeps and set(time_.sleeps) == {BUSY_STEP_S}
    assert BUSY_WAIT_S - 1e-9 <= sum(time_.sleeps) <= BUSY_WAIT_S + BUSY_STEP_S + 1e-9
    assert "exec_interactive" not in fake.ops()
    assert "start" not in fake.ops()
    assert "stop" not in fake.ops()


@pytest.mark.fr("FR-068")
@pytest.mark.fr("DR-033")
@pytest.mark.fr("NFR-004")
def test_a_wait_for_a_holder_registers_the_app_once_and_releases_it_once(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each wait step repeats the lock-free look, but only the command's first look registers the
    app and tries its proxy. That app is the one released when the command ends (FR-068)."""
    _pending_realm(env, fake, user, lifecycle_hooks, home)
    time_ = FakeTime()
    held = ExitStack()
    held.enter_context(realm_lock(env.paths, RealmName("lab"), "reattach"))
    starts: list[Proc] = []
    releases: list[Proc] = []
    real_start = enter_module.start_app
    real_finish = enter_module.finish_app

    def counted_start(*args: Any, **kwargs: Any) -> Proc:
        app = real_start(*args, **kwargs)
        starts.append(app)
        return app

    def counted_finish(env_: OpEnv, name: RealmName, app: Proc) -> None:
        releases.append(app)
        real_finish(env_, name, app)

    def the_holder_finishes(seconds: float) -> None:
        # Three steps pass while the holder runs, then it ends and this enter reattaches.
        time_.sleep(seconds)
        if len(time_.sleeps) == 3:
            held.close()

    monkeypatch.setattr(enter_module, "start_app", counted_start)
    monkeypatch.setattr(enter_module, "finish_app", counted_finish)
    hooks = make_hooks(lifecycle_hooks, time_=time_, sleep=the_holder_finishes)
    assert enter(env, fake, "lab", user, hooks, argv=["true"]) == 0

    assert len(time_.sleeps) == 3
    assert len(starts) == 1
    assert releases == starts
    assert _launched_apps(env.paths, "lab") == []
    assert fake.ops().count("exec_interactive") == 1


@pytest.mark.fr("DR-033")
def test_any_other_lock_holder_is_refused_at_once_without_a_wait(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
) -> None:
    _pending_realm(env, fake, user, lifecycle_hooks, home)
    time_ = FakeTime()
    held = ExitStack()
    held.enter_context(realm_lock(env.paths, RealmName("lab"), "rebuild"))
    try:
        with pytest.raises(GinnError) as info:
            enter(env, fake, "lab", user, make_hooks(lifecycle_hooks, time_=time_), argv=["true"])
    finally:
        held.close()

    assert info.value.code == "GINN-E-REALM-BUSY"
    assert info.value.details["op"] == "rebuild"
    assert time_.sleeps == []
    assert "exec_interactive" not in fake.ops()


@pytest.mark.fr("FR-006")
def test_an_unacknowledged_host_change_refuses_the_re_attach_at_enter_without_a_wait(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
    host: Path,
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    start_realm(env, fake, RealmName("lab"), session=user, hooks=lifecycle_hooks)
    _change_hosts(host)
    (home / "Downloads").mkdir()
    fake.calls.clear()
    fake.exec_calls.clear()
    time_ = FakeTime()

    with pytest.raises(GinnError) as info:
        enter(env, fake, "lab", user, make_hooks(lifecycle_hooks, time_=time_), argv=["true"])

    assert info.value.code == "GINN-E-HOST-UNACKED"
    assert time_.sleeps == []
    assert "unshare" not in fake.ops()
    assert "exec_interactive" not in fake.ops()
    assert stored(env, "lab").state is RealmState.RUNNING
    assert audit_of(env, "reattach") == []


@pytest.mark.fr("DR-032")
@pytest.mark.fr("DR-033")
def test_a_lock_that_finds_no_work_records_a_noop_step_and_attaches_nothing(
    env: OpEnv,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
    user: HostUser,
    home: Path,
) -> None:
    """A concurrent enter attached the folder between this enter's look and its lock. The check
    under the lock finds nothing to do, so the audit entry says `noop` and no helper runs."""
    fake = AtTheLockedListing(root=str(tmp_path))
    _pending_realm(env, fake, user, lifecycle_hooks, home)
    mountinfo = lifecycle_hooks.mountinfo
    downloads = str(home / "Downloads")

    def the_other_enter_attached_it() -> None:
        store = RealmStore(env.paths, env.clock)
        record = store.load(RealmName("lab"))
        identity = attach.read_identity(downloads, mountinfo=mountinfo)
        grants = [
            replace(g, identity=identity) if g.realm == downloads else g for g in record.grants
        ]
        store.save(replace(record, grants=grants))

    fake.listings = 0
    fake.at_lock = the_other_enter_attached_it

    assert enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"]) == 0

    assert "unshare" not in fake.ops()
    (entry,) = audit_of(env, "reattach")
    assert entry.outcome == "ok"
    assert entry.params["noop"] == "no grant changed since the pre-check"
    assert env.journal.pending() == []
    assert exec_call(fake).argv == ("true",)


@pytest.mark.fr("DR-020")
@pytest.mark.fr("IR-011")
@pytest.mark.parametrize("status", [PODMAN_ERROR_EXIT, 255])
def test_a_125_or_255_with_the_container_still_running_is_returned_unchanged(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    status: int,
) -> None:
    """The command's own status and podman's failure look the same. With the realm still running,
    enter returns the status and does not guess (IR-011)."""
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    fake.exec_results[("false",)] = ExecResult(status, "", "")

    assert enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["false"]) == status
    assert fake.ops().count("exec_interactive") == 1


@pytest.mark.fr("DR-020")
@pytest.mark.fr("IR-011")
@pytest.mark.parametrize("status", [PODMAN_ERROR_EXIT, 255])
def test_a_125_or_255_after_the_container_stopped_is_a_realm_state_error_and_is_not_retried(
    env: OpEnv,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
    user: HostUser,
    status: int,
) -> None:
    fake = StopsDuringExec(root=str(tmp_path))
    fake.exec_status = status
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)

    with pytest.raises(GinnError) as info:
        enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert info.value.code == "GINN-E-REALM-STATE"
    assert info.value.details["state"] == "stopped"
    assert info.value.details["action"] == "exec"
    assert fake.ops().count("exec_interactive") == 1


@pytest.mark.fr("FR-017")
def test_a_record_that_says_running_while_its_container_exited_is_started_again(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    fake.stop(container_id(env, "lab"), 0)
    fake.calls.clear()
    fake.exec_calls.clear()

    assert enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"]) == 0

    assert "start" in fake.ops()
    assert stored(env, "lab").state is RealmState.RUNNING
    assert exec_call(fake).container_id == container_id(env, "lab")


def _launched_apps(paths: OwnedPaths, name: str) -> list[dict[str, Any]]:
    """The apps that the realm's proxy state holds now (FR-068). No state file means no app."""
    path = proxy_state_path(paths, RealmName(name))
    if not path.exists():
        return []
    apps: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["apps"]
    return apps


class LaunchedDuringExec(EnterPodman):
    """Records the launched apps of realm `lab` as each command starts, then runs `during`.

    `during` stands for the command: it returns, raises, or sends a signal to this process.
    """

    def __init__(self, *, root: str, paths: OwnedPaths, during: Callable[[], None]) -> None:
        super().__init__(root=root)
        self.paths = paths
        self.during = during
        self.apps_seen: list[list[dict[str, Any]]] = []

    def exec_interactive(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> int:
        self.apps_seen.append(_launched_apps(self.paths, "lab"))
        self.during()
        return super().exec_interactive(container_id, argv, **kwargs)


def _send_to_self(signum: int) -> None:
    """Sends `signum` to this process, as the end of a command would. Without the enter handler the
    signal would end the test run, so that case raises instead."""
    if signal.getsignal(signum) is signal.SIG_DFL:
        raise RuntimeError("no enter handler is installed for the signal")
    os.kill(os.getpid(), signum)


def _command_fails() -> None:
    raise RuntimeError("the command failed")


@pytest.fixture
def release_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The release of a launched app fails, as when the proxy state cannot be written."""

    def gone(env: OpEnv, name: RealmName, proc: object) -> None:
        raise OSError("the proxy state is gone")

    monkeypatch.setattr("ginn.runtime.enter.finish_app", gone)


@pytest.mark.fr("FR-068")
def test_the_launched_app_runs_during_the_command_and_is_released_after_a_normal_exit(
    env: OpEnv,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
) -> None:
    fake = LaunchedDuringExec(root=str(tmp_path), paths=env.paths, during=lambda: None)
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)

    assert enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"]) == 0

    assert [len(apps) for apps in fake.apps_seen] == [1]
    assert _launched_apps(env.paths, "lab") == []


@pytest.mark.fr("FR-068")
def test_the_launched_app_is_released_when_the_command_raises(
    env: OpEnv,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
) -> None:
    fake = LaunchedDuringExec(root=str(tmp_path), paths=env.paths, during=_command_fails)
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)

    with pytest.raises(RuntimeError, match="the command failed"):
        enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert _launched_apps(env.paths, "lab") == []


@pytest.mark.fr("FR-068")
def test_a_look_that_fails_before_the_command_releases_the_app(
    env: OpEnv,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the session read failed")

    monkeypatch.setattr("ginn.runtime.enter.session_env", unreadable)
    fake = LaunchedDuringExec(root=str(tmp_path), paths=env.paths, during=lambda: None)
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)

    with pytest.raises(RuntimeError, match="the session read failed"):
        enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert fake.apps_seen == []
    assert _launched_apps(env.paths, "lab") == []


@pytest.mark.fr("FR-068")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_ends_the_command_through_its_exit_path_and_the_app_is_released(
    env: OpEnv,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
    signum: int,
) -> None:
    fake = LaunchedDuringExec(
        root=str(tmp_path), paths=env.paths, during=lambda: _send_to_self(signum)
    )
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    before = signal.getsignal(signum)

    with pytest.raises(SystemExit) as info:
        enter(env, fake, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    assert info.value.code == 128 + signum
    assert _launched_apps(env.paths, "lab") == []
    assert signal.getsignal(signum) is before


class _CommandRuns(LaunchedDuringExec):
    """A command that podman exec runs as pid 4242. `during` stands for the command's own signal,
    and the exec then returns 143, as podman does when the command ends on SIGTERM."""

    def exec_interactive(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> int:
        kwargs["on_spawn"](4242)
        self.during()
        return 143


class _CommandExitsThenIsSignalled(LaunchedDuringExec):
    """A command that podman exec runs as pid 4242, and reports its exit through `on_exit` before
    `during` sends the signal. The signal comes after the client is reaped (FR-068)."""

    def exec_interactive(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> int:
        kwargs["on_spawn"](4242)
        kwargs["on_exit"]()
        self.during()
        return 143


@pytest.mark.fr("FR-068")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_after_the_client_is_reaped_is_not_forwarded_to_its_pid(
    env: OpEnv,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
    signum: int,
) -> None:
    """Once `on_exit` has run, the forwarder has no pid, so a signal ends ginn through its exit
    path and never reaches the pid, which the kernel may have given to another process (FR-068)."""
    fake = _CommandExitsThenIsSignalled(
        root=str(tmp_path), paths=env.paths, during=lambda: _send_to_self(signum)
    )
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    sent: list[tuple[int, int]] = []
    hooks = replace(make_hooks(lifecycle_hooks), kill=lambda pid, sig: sent.append((pid, sig)))

    with pytest.raises(SystemExit) as info:
        enter(env, fake, "lab", user, hooks, argv=["true"])

    assert info.value.code == 128 + signum
    assert sent == []
    assert _launched_apps(env.paths, "lab") == []


@pytest.mark.fr("FR-068")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_during_the_command_is_forwarded_to_it_and_its_status_comes_back(
    env: OpEnv,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
    signum: int,
) -> None:
    """The signal goes to the command, not to ginn. No exception ends the wait, so the command's
    status comes back, and the app is released after the command is gone (FR-068)."""
    fake = _CommandRuns(root=str(tmp_path), paths=env.paths, during=lambda: _send_to_self(signum))
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    sent: list[tuple[int, int]] = []
    hooks = replace(make_hooks(lifecycle_hooks), kill=lambda pid, sig: sent.append((pid, sig)))

    assert enter(env, fake, "lab", user, hooks, argv=["true"]) == 143

    assert sent == [(4242, signum)]
    assert _launched_apps(env.paths, "lab") == []


@pytest.mark.fr("FR-068")
@pytest.mark.fr("IR-011")
@pytest.mark.parametrize("status", [0, 7])
def test_a_failed_release_is_a_warning_and_the_exit_status_is_unchanged(
    env: OpEnv,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
    release_fails: None,
    status: int,
) -> None:
    fake = LaunchedDuringExec(root=str(tmp_path), paths=env.paths, during=lambda: None)
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    fake.exec_results[("true",)] = ExecResult(status, "", "")
    notices: list[GinnWarning] = []
    hooks = replace(make_hooks(lifecycle_hooks), notify=notices.append)

    assert enter(env, fake, "lab", user, hooks, argv=["true"]) == status

    assert [notice.code for notice in notices] == ["GINN-W-PROXY-RELEASE"]
    assert "lab" in notices[0].message
    assert "the proxy state is gone" in notices[0].message


@pytest.mark.fr("FR-068")
@pytest.mark.fr("IR-011")
def test_a_failed_release_never_replaces_the_error_of_the_command(
    env: OpEnv,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
    release_fails: None,
) -> None:
    fake = LaunchedDuringExec(root=str(tmp_path), paths=env.paths, during=_command_fails)
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    notices: list[GinnWarning] = []
    hooks = replace(make_hooks(lifecycle_hooks), notify=notices.append)

    with pytest.raises(RuntimeError, match="the command failed"):
        enter(env, fake, "lab", user, hooks, argv=["true"])

    assert [notice.code for notice in notices] == ["GINN-W-PROXY-RELEASE"]


@pytest.mark.fr("FR-068")
@pytest.mark.fr("FR-042")
def test_a_root_session_that_waits_for_a_lock_holder_registers_no_launched_app(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    home: Path,
    tmp_path: Path,
) -> None:
    """A `--root` enter is not a launched app, also while it waits (runtime/enter.py)."""
    recorder = LaunchedDuringExec(root=str(tmp_path), paths=env.paths, during=lambda: None)
    _pending_realm(env, recorder, user, lifecycle_hooks, home)
    time_ = FakeTime()
    held = ExitStack()
    held.enter_context(realm_lock(env.paths, RealmName("lab"), "reattach"))

    def the_other_operation_finishes(seconds: float) -> None:
        time_.sleep(seconds)
        held.close()
        enter(env, recorder, "lab", user, make_hooks(lifecycle_hooks), argv=["true"])

    hooks = make_hooks(lifecycle_hooks, time_=time_, sleep=the_other_operation_finishes)

    assert enter(env, recorder, "lab", user, hooks, argv=["true"], root=True) == 0

    assert recorder.apps_seen[-1] == []
    assert _launched_apps(env.paths, "lab") == []


@pytest.mark.fr("FR-068")
def test_a_proxy_that_fails_to_start_warns_and_the_command_still_runs(
    env: OpEnv,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
) -> None:
    """The enter is not refused when the proxy does not start: the app is registered with no bus,
    the warning reaches the caller, and the command runs (ADR-010, FR-068)."""
    fake = LaunchedDuringExec(root=str(tmp_path), paths=env.paths, during=lambda: None)
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    runtime = user.host_env["XDG_RUNTIME_DIR"]
    with_bus = replace(
        user, host_env={**user.host_env, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}
    )

    def refused(argv: Sequence[str]) -> object:
        raise GinnError("GINN-E-DBUS-PROXY", "proxy failed", "", 1, details={})

    security = replace(
        lifecycle_hooks.security, dbus_binary="/usr/bin/xdg-dbus-proxy", proxy_launch=refused
    )
    notices: list[GinnWarning] = []
    hooks = replace(make_hooks(replace(lifecycle_hooks, security=security)), notify=notices.append)

    assert enter(env, fake, "lab", with_bus, hooks, argv=["true"]) == 0

    assert [notice.code for notice in notices] == ["GINN-W-DBUS-PROXY"]
    assert _launched_apps(env.paths, "lab") == []


@pytest.mark.fr("FR-018")
def test_an_enter_that_auto_starts_a_services_off_realm_arms_its_idle_watcher(
    env: OpEnv,
    fake: EnterPodman,
    user: HostUser,
    lifecycle_hooks: LifecycleHooks,
    tmp_path: Path,
) -> None:
    """The auto-start of an enter is a services-off start, so the watcher is armed (FR-018)."""
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    fake.global_argv = lambda: ["/usr/bin/podman", "--root", "/data/podman"]  # type: ignore[attr-defined]
    helper = tmp_path / "ginn-idle"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    spawns: list[list[str]] = []

    def spawn(argv: Sequence[str], log: Path) -> int:
        spawns.append(list(argv))
        return 7777

    hooks = replace(
        lifecycle_hooks,
        idle=IdleHooks(
            helper=str(helper), grace_s=60, spawn=spawn, start_ticks=lambda pid: 500 + pid
        ),
    )

    status = enter(env, fake, "lab", user, make_hooks(hooks), argv=["true"])

    assert status == 0
    assert len(spawns) == 1
    assert spawns[0][-2:] == ["--", container_id(env, "lab")]
    assert spawns[0][2] == "60"
