# SPDX-License-Identifier: Apache-2.0
"""Realm start, stop, status and list (T4.5): ops/lifecycle.py against FakePodman (FR-016, FR-020).

Covers the start that attaches grants and records them, the journal steps of a start, the failed
starts and the stops that follow them, the idempotent no-ops, the refusals (unknown name, DR-020
state, services-on, another tier), a podman error that is never read as "stopped", the status
reconciliation with podman, the stable JSON keys, the list order and unreadable records, the disk
walk, and the read-only rule for status and list.

The fake runs no podman. Its helper rows come from the host, as ginn-rebind reports them, and only
paths under tmp_path answer, so the attach runs for real. No test runs podman, sudo or the helper
(AGENTS.md). Every request pins XDG_RUNTIME_DIR to an empty directory, because the test host has a
live session under /run/user/1000 (a PipeWire socket), and a real socket would add a grant.
"""

import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.domain.permissions import BASIC_TOGGLES, Permissions, Toggle
from ginn.errors import GinnError, make
from ginn.errors import Warning as GinnWarning
from ginn.ops import lifecycle
from ginn.ops import rebuild as rebuild_module
from ginn.ops._mutating import OpEnv, mutating
from ginn.ops.lifecycle import (
    HostUser,
    LifecycleHooks,
    list_realms,
    realm_status,
    render_status_text,
    start_realm,
    stop_realm,
)
from ginn.ops.lifecycle import _tree_usage as tree_usage
from ginn.ops.security_ops import agent_state_path, read_agent_record
from ginn.podman.adapter import CreateSpec, ExecTarget
from ginn.podman.models import ContainerInfo, ContainerState, ExecResult
from ginn.runtime import attach
from ginn.runtime.idle import IdleHooks, log_path, record_path
from ginn.runtime.realm_security import SecurityHooks
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.security.agent import AgentRecord
from ginn.services import supervisor
from ginn.services.runit_user import service_dir, write_service_dir
from ginn.services.supervisor import Result, SupervisorHooks
from ginn.store.locks import realm_lock
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from tests.fakes.clock import FakeClock
from tests.fakes.fake_podman import FakePodman

UID = 1000
USER = "alice"
DIGEST = "registry.example/debian@sha256:" + "ab" * 32
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
BASE_MOUNTINFO = (
    "36 35 98:0 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
    "37 36 98:1 / /opt rw - ext4 /dev/root rw\n"
)
SCOPE = ("etc", "var/db/xbps", "var/service", "usr/local", "opt")
SKIP_DOWNLOADS = "share skipped: ~/Downloads (missing)"
STATUS_KEYS = (
    "name",
    "state",
    "container_state",
    "state_mismatch",
    "state_reason",
    "supervision",
    "distro",
    "tier",
    "services",
    "autostart",
    "generation",
    "image_digest",
    "created_at",
    "updated_at",
    "restart_required",
    "share_skipped",
    "disk",
    "exported_apps",
    "error",
)


def _helper_rows(argv: Sequence[str], root: str) -> str:
    """The helper's stdout for `argv`: one row per attach group, read from the host.

    Only paths under `root` exist for this test, so the real host never answers a row.
    """
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


class LifecyclePodman(FakePodman):
    """The fake plus the helper's answers from the host (test seam only).

    A queued `unshare_results` entry wins. Otherwise the helper rows come from the host, under
    `root`. `ps_error` makes every `ps` call fail, to test a podman that cannot answer.
    """

    def __init__(self, *, root: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.root = root
        self.ps_error = False

    def ps(self, label_filter: Mapping[str, str] | None = None) -> list[ContainerInfo]:
        if self.ps_error:
            self._record("ps", ())
            raise make("GINN-E-INTERNAL")
        return super().ps(label_filter)

    def unshare(self, argv: Sequence[str]) -> ExecResult:
        if self.unshare_results:
            return super().unshare(argv)
        self._record("unshare", tuple(argv))
        return ExecResult(0, _helper_rows(argv, self.root), "")


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    """Owned Paths under tmp_path, with every XDG variable pinned (as in test_ops_create)."""
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
def fake(tmp_path: Path) -> LifecyclePodman:
    return LifecyclePodman(root=str(tmp_path))


@pytest.fixture
def mountinfo(tmp_path: Path) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(BASE_MOUNTINFO, encoding="utf-8")
    return path


@pytest.fixture
def user(tmp_path: Path, home: Path) -> HostUser:
    """The host user. The runtime variable points at an empty directory (see the docstring)."""
    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700, exist_ok=True)
    return HostUser(uid=UID, host_home=home, host_env={"XDG_RUNTIME_DIR": str(runtime)})


@pytest.fixture(autouse=True)
def host_identity_is_read_only_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`attach_at_start` reads source identities with os.stat, not through a hook. Only paths
    under tmp_path answer, so the host's /media and /mnt never attach (as in test_ops_create)."""
    real = attach.read_identity

    def under_tmp(host: str, *, mountinfo: Path = attach.MOUNTINFO) -> attach.SourceIdentity | None:
        if not host.startswith(str(tmp_path)):
            return None
        return real(host, mountinfo=mountinfo)

    monkeypatch.setattr(attach, "read_identity", under_tmp)


@pytest.fixture
def hooks(tmp_path: Path, mountinfo: Path) -> LifecycleHooks:
    """The probes answer only for paths under tmp_path that exist, as in test_ops_create. The gpg
    hook is pinned, so no start runs the host's gpgconf (NFR-SEC-007)."""
    return LifecycleHooks(
        exists=lambda p: p.startswith(str(tmp_path)) and os.path.exists(p),
        mountinfo=mountinfo,
        security=SecurityHooks(gpg_extra=lambda: None),
    )


def _record(
    name: str,
    state: RealmState,
    container_id: str | None,
    *,
    previous: RealmState | None = None,
    services: bool = False,
    tier: Tier = Tier.BASIC,
    restart: bool = False,
    share: list[str] | None = None,
    permissions: Permissions | None = None,
    created_options: list[str] | None = None,
    kvm_at_create: bool = False,
    gpu_at_create: bool = True,
    fonts_at_create: bool = True,
) -> RealmRecord:
    settings = RealmSettings(
        name=RealmName(name),
        distro=DistroId("debian"),
        tier=tier,
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
        restart_required=restart,
        share_skipped=list(share or []),
        permissions=Permissions() if permissions is None else permissions,
        created_options=list(created_options or []),
        kvm_at_create=kvm_at_create,
        gpu_at_create=gpu_at_create,
        fonts_at_create=fonts_at_create,
    )


def make_realm(
    env: OpEnv,
    fake: LifecyclePodman,
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
    return record


def stored(env: OpEnv, name: str) -> RealmRecord:
    return RealmStore(env.paths, env.clock).load(RealmName(name))


def audit_of(env: OpEnv, op: str) -> list[Any]:
    return [entry for entry in env.audit.read() if entry.op == op]


def record_bytes(env: OpEnv, name: str) -> bytes:
    return (env.paths.realms / f"{name}.json").read_bytes()


def container_id(env: OpEnv, name: str) -> str:
    cid = stored(env, name).container_id
    assert cid is not None
    return cid


@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-066")
def test_start_attaches_the_grants_and_saves_them_with_a_running_record(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, home: Path
) -> None:
    (home / "Downloads").mkdir()
    make_realm(
        env,
        fake,
        "lab",
        RealmState.STOPPED,
        running=False,
        restart=True,
        share=["share skipped: ~/Projects (missing)"],
    )

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert record.state is RealmState.RUNNING
    assert record.restart_required is False
    assert "share skipped: ~/Projects (missing)" not in record.share_skipped
    assert all("Downloads" not in s for s in record.share_skipped)
    downloads = [g for g in record.grants if g.realm == str(home / "Downloads")]
    assert len(downloads) == 1
    assert downloads[0].identity is not None
    assert stored(env, "lab") == record
    assert fake.containers[container_id(env, "lab")].state.value == "running"
    assert env.journal.pending() == []
    (entry,) = audit_of(env, "start")
    assert entry.outcome == "ok"
    assert entry.params["generation"] == 1


@pytest.mark.fr("FR-064")
@pytest.mark.fr("FR-016")
def test_a_missing_source_is_recorded_as_skipped_and_the_start_goes_on(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, home: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert record.state is RealmState.RUNNING
    assert SKIP_DOWNLOADS in record.share_skipped
    missing = [g for g in record.grants if g.realm == str(home / "Downloads")]
    assert len(missing) == 1
    assert missing[0].identity is None
    assert "unshare" not in fake.ops()


@pytest.mark.fr("FR-016")
def test_start_of_a_realm_that_runs_already_is_a_no_op(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    before = make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    raw = record_bytes(env, "lab")

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert record == before
    assert record_bytes(env, "lab") == raw
    assert "start" not in fake.ops()
    assert list(env.audit.read()) == []
    assert env.journal.pending() == []


@pytest.mark.fr("FR-066")
@pytest.mark.fr("FR-016")
def test_a_start_of_a_running_realm_returns_the_pending_restart_flag(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True, restart=True)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert record.restart_required is True
    assert "start" not in fake.ops()


@pytest.mark.fr("FR-016")
def test_a_podman_error_while_inspecting_a_running_realm_is_not_read_as_stopped(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    fake.fail_ops = {"inspect": "GINN-E-INTERNAL"}

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-INTERNAL"
    assert stored(env, "lab").state is RealmState.RUNNING
    assert "start" not in fake.ops()
    assert "stop" not in fake.ops()
    assert list(env.audit.read()) == []


@pytest.mark.fr("FR-016")
def test_a_podman_error_inside_the_body_leaves_the_record_running_and_no_stop_runs(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    real_inspect = fake.inspect
    seen = {"calls": 0}

    def inspect_fails_in_the_body(container_id: str) -> ContainerInfo:
        seen["calls"] += 1
        # Call 1 is the check before the operation, call 2 the idle-stop check (which treats a
        # podman error as no confirmation), and call 3 the body's own check. The body must fail.
        if seen["calls"] == 3:
            raise make("GINN-E-INTERNAL")
        return real_inspect(container_id)

    monkeypatch.setattr(fake, "inspect", inspect_fails_in_the_body)

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-INTERNAL"
    assert stored(env, "lab").state is RealmState.RUNNING
    assert "start" not in fake.ops()
    assert "stop" not in fake.ops()
    (entry,) = audit_of(env, "start")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-INTERNAL")
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
def test_a_record_that_says_running_with_a_dead_container_is_restarted_as_a_crash(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert record.state is RealmState.RUNNING
    assert fake.containers[container_id(env, "lab")].state.value == "running"
    (entry,) = audit_of(env, "start")
    assert entry.outcome == "ok"


@pytest.mark.fr("FR-016")
def test_a_failed_realm_starts_again(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.FAILED, running=False)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert record.state is RealmState.RUNNING
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-064")
def test_a_failed_attach_stops_the_realm_again_and_leaves_it_stopped(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, home: Path
) -> None:
    (home / "Downloads").mkdir()
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    fake.unshare_results.append(ExecResult(1, "", "ginn-rebind: mount refused"))

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    assert fake.containers[container_id(env, "lab")].state.value == "exited"
    record = stored(env, "lab")
    assert record.state is RealmState.STOPPED
    assert record.grants and all(g.identity is None for g in record.grants)
    (entry,) = audit_of(env, "start")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-ATTACH-FAILED")
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
def test_a_failed_attach_after_the_crash_path_leaves_the_realm_stopped(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, home: Path
) -> None:
    (home / "Downloads").mkdir()
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    fake.unshare_results.append(ExecResult(1, "", "ginn-rebind: mount refused"))

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    assert stored(env, "lab").state is RealmState.STOPPED
    assert fake.containers[container_id(env, "lab")].state.value == "exited"
    (entry,) = audit_of(env, "start")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-ATTACH-FAILED")
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
def test_a_stop_that_fails_after_a_failed_attach_leaves_the_state_as_found(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, home: Path
) -> None:
    (home / "Downloads").mkdir()
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    fake.unshare_results.append(ExecResult(1, "", "ginn-rebind: mount refused"))
    fake.fail_ops = {"stop": "GINN-E-INTERNAL"}

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    assert any("stop after failed start" in note for note in caught.value.__notes__)
    assert stored(env, "lab").state is RealmState.FAILED
    assert fake.containers[container_id(env, "lab")].state.value == "running"
    (entry,) = audit_of(env, "start")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-ATTACH-FAILED")


@pytest.mark.fr("FR-016")
def test_a_failed_podman_start_leaves_the_realm_stopped_with_its_grants_unidentified(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, home: Path
) -> None:
    (home / "Downloads").mkdir()
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    fake.fail_ops = {"start": "GINN-E-INTERNAL"}

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-INTERNAL"
    record = stored(env, "lab")
    assert record.state is RealmState.STOPPED
    assert record.grants and all(g.identity is None for g in record.grants)
    assert "unshare" not in fake.ops()
    (entry,) = audit_of(env, "start")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-INTERNAL")
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-066")
def test_a_crash_between_started_and_attached_leaves_no_identity_and_names_the_step(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (home / "Downloads").mkdir()
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)

    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(lifecycle, "attach_at_start", interrupted)

    with pytest.raises(KeyboardInterrupt):
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    record = stored(env, "lab")
    assert record.state is RealmState.STOPPED
    assert record.grants and all(g.identity is None for g in record.grants)
    (pending,) = env.journal.pending()
    assert pending.step == "started"


@pytest.mark.fr("FR-025")
@pytest.mark.fr("FR-016")
def test_start_of_an_unknown_realm_is_refused_before_any_write(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("ghost"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-NO-SUCH-REALM"
    assert fake.calls == []
    assert list(env.audit.read()) == []
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    ("state", "previous"),
    [
        (RealmState.BROKEN, None),
        (RealmState.CREATING, None),
        (RealmState.DELETING, None),
        (RealmState.UPDATING, RealmState.STOPPED),
    ],
)
def test_start_from_a_state_dr020_does_not_allow_is_refused_and_audited(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    state: RealmState,
    previous: RealmState | None,
) -> None:
    make_realm(env, fake, "lab", state, running=False, previous=previous)

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-REALM-STATE"
    assert caught.value.exit_code == 4
    assert "start" not in fake.ops()
    assert stored(env, "lab").state is state
    (entry,) = audit_of(env, "start")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-REALM-STATE")
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    ("state", "previous"),
    [
        (RealmState.BROKEN, None),
        (RealmState.CREATING, None),
        (RealmState.DELETING, None),
        (RealmState.UPDATING, RealmState.STOPPED),
    ],
)
def test_stop_from_a_state_dr020_does_not_allow_is_refused_and_audited(
    env: OpEnv,
    fake: LifecyclePodman,
    hooks: LifecycleHooks,
    state: RealmState,
    previous: RealmState | None,
) -> None:
    make_realm(env, fake, "lab", state, running=True, previous=previous)

    with pytest.raises(GinnError) as caught:
        stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert caught.value.code == "GINN-E-REALM-STATE"
    assert caught.value.exit_code == 4
    assert "stop" not in fake.ops()
    assert stored(env, "lab").state is state
    (entry,) = audit_of(env, "stop")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-REALM-STATE")


SV = "/usr/bin/sv"
"""The sv path the hooks name. No test runs a real sv: RunitFake answers in its place."""


class RunitFake:
    """Stands in for sv in the lifecycle tests. It records each argv, answers `sv status` from
    `state`, and runs the action of the supervised process on `up` and `down` (`on_up`, `on_down`).
    `on_sleep` runs once, at the first wait of the start's poll, so a test can take the realm lock
    there to prove that the wait holds none."""

    def __init__(self, service: Path, clock: FakeClock) -> None:
        self.service = service
        self.clock = clock
        self.argvs: list[list[str]] = []
        self.state = "down"
        self.on_up: Callable[[], None] | None = None
        self.on_down: Callable[[], None] | None = None
        self.on_sleep: Callable[[], None] | None = None
        self.status_reply: Result | None = None
        self.down_reply: Result | None = None
        self.want_up = False
        self.normally_up = True

    def run(self, argv: Sequence[str], timeout: float) -> Result:
        self.argvs.append(list(argv))
        if argv[1] == "status":
            if self.status_reply is not None:
                return self.status_reply
            return Result(0, self._status_line() + "\n", "")
        if argv[3] == "up":
            self.state = "run"
            if self.on_up is not None:
                self.on_up()
            return Result(0, f"ok: run: {self.service}: (pid 4321) 0s\n", "")
        if self.down_reply is not None:
            return self.down_reply
        self.state = "down"
        if self.on_down is not None:
            self.on_down()
        return Result(0, f"ok: down: {self.service}: 0s, normally up\n", "")

    def sleep(self, seconds: float) -> None:
        self.clock.advance(seconds)
        if self.on_sleep is not None:
            hook, self.on_sleep = self.on_sleep, None
            hook()

    def _status_line(self) -> str:
        if self.state == "run":
            return f"run: {self.service}: (pid 4321) 0s"
        want = ", want up" if self.want_up else ""
        normal = ", normally up" if self.normally_up else ""
        return f"down: {self.service}: 0s{normal}{want}"


def _services_hooks(
    hooks: LifecycleHooks, runit: RunitFake, clock: FakeClock, **overrides: float
) -> LifecycleHooks:
    """The hooks with the fake sv and a short start wait, so no wait is real."""
    supervisor_hooks = SupervisorHooks(sv=SV, run=runit.run, clock=clock, sleep=runit.sleep)
    return replace(
        hooks,
        supervisor=supervisor_hooks,
        start_wait_s=float(overrides.get("start_wait_s", 60.0)),
        start_poll_s=1.0,
    )


@pytest.fixture
def clock() -> FakeClock:
    """The clock of the supervisor's waits. The record and the audit use the env clock."""
    return FakeClock(1_800_000_000.0)


def _supervised_realm(
    env: OpEnv, fake: LifecyclePodman, name: str, *, state: RealmState, running: bool
) -> RealmRecord:
    """A services-on realm with its runit service, and the supervise/ok FIFO that runsv creates.
    An updating or rebuilding realm records the state it moved from, as DR-020 needs."""
    previous = RealmState.RUNNING if state in (RealmState.UPDATING, RealmState.REBUILDING) else None
    record = make_realm(env, fake, name, state, running=running, services=True, previous=previous)
    write_service_dir(env.paths, RealmName(name), "/usr/bin/ginn")
    supervise = service_dir(env.paths, RealmName(name)) / "supervise"
    supervise.mkdir(exist_ok=True)
    os.mkfifo(supervise / "ok")
    return record


def audited_ops(env: OpEnv) -> list[str]:
    """The operations of the audit log, without the host integrity baseline entries."""
    return [entry.op for entry in env.audit.read() if entry.op != "integrity"]


def _try_lock(env: OpEnv) -> str:
    """Take the realm lock as an operation of its own, the way a supervised start does (DR-032).

    Returns "free" when the lock was taken, and "busy" when another operation holds it.
    """
    try:
        with mutating(env, "probe", RealmName("lab"), {}):
            return "free"
    except GinnError:
        return "busy"


@pytest.mark.fr("FR-081")
def test_a_services_on_start_goes_through_its_runit_service_and_writes_nothing_itself(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    cid = container_id(env, "lab")

    def supervised_start() -> None:
        fake.start(cid)
        RealmStore(env.paths, env.clock).set_state(RealmName("lab"), RealmState.RUNNING)

    runit.on_up = supervised_start
    record = start_realm(
        env,
        fake,
        RealmName("lab"),
        session=user,
        hooks=_services_hooks(hooks, runit, clock),
    )

    assert record.state is RealmState.RUNNING
    assert runit.argvs == [[SV, "-w", str(supervisor.UP_WAIT_S), "up", str(runit.service)]]
    assert audited_ops(env) == [], "the CLI process writes no operation entry for the start"
    assert env.journal.pending() == []


@pytest.mark.fr("FR-081")
def test_the_wait_for_a_services_on_start_holds_no_realm_lock(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    """The supervised start needs the lock to record `running`, so the wait must not hold it."""
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    cid = container_id(env, "lab")
    probed: list[str] = []

    def first_poll_then_start() -> None:
        # The first poll finds the realm stopped. The lock is free then, and the supervised start
        # records running after it.
        probed.append(_try_lock(env))
        fake.start(cid)
        RealmStore(env.paths, env.clock).set_state(RealmName("lab"), RealmState.RUNNING)

    runit.on_sleep = first_poll_then_start
    start_realm(
        env,
        fake,
        RealmName("lab"),
        session=user,
        hooks=_services_hooks(hooks, runit, clock),
    )

    assert probed == ["free"], "the realm lock is free during the wait (DR-032)"
    assert audited_ops(env) == ["probe"]


@pytest.mark.fr("FR-081")
def test_a_services_on_start_times_out_when_the_realm_never_runs(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    started_at = clock()

    with pytest.raises(GinnError) as caught:
        start_realm(
            env,
            fake,
            RealmName("lab"),
            session=user,
            hooks=_services_hooks(hooks, runit, clock, start_wait_s=5.0),
        )

    assert caught.value.code == "GINN-E-SERVICE-TIMEOUT"
    assert caught.value.details["action"] == "start"
    assert clock() - started_at >= 5.0
    assert stored(env, "lab").state is RealmState.STOPPED


@pytest.mark.fr("AS-028")
def test_a_services_on_start_stops_waiting_when_runit_has_given_the_realm_up(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.FAILED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.on_up = lambda: (setattr(runit, "state", "down"), setattr(runit, "normally_up", False))
    started_at = clock()

    with pytest.raises(GinnError) as caught:
        start_realm(
            env,
            fake,
            RealmName("lab"),
            session=user,
            hooks=_services_hooks(hooks, runit, clock, start_wait_s=60.0),
        )

    assert caught.value.code == "GINN-E-SERVICE-TIMEOUT"
    assert "AS-028" in caught.value.details["detail"]
    assert clock() - started_at < 60.0, "the wait ends at once, not at the time limit"


@pytest.mark.fr("FR-081")
def test_a_services_on_start_of_a_running_realm_runs_no_sv(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.RUNNING, running=True)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)

    record = start_realm(
        env, fake, RealmName("lab"), session=user, hooks=_services_hooks(hooks, runit, clock)
    )

    assert record.state is RealmState.RUNNING
    assert runit.argvs == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-081")
def test_stop_of_a_services_on_realm_holds_the_lock_across_sv_down_and_records_the_stop(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    """The stop holds the realm lock while sv runs, so the finish script that sv waits for sees it
    held by this stop and writes nothing. The stop records `stopped` itself, after sv returns."""
    _supervised_realm(env, fake, "lab", state=RealmState.RUNNING, running=True)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.state = "run"
    cid = container_id(env, "lab")
    lock_during_sv: list[str] = []

    def supervised_stop() -> None:
        lock_during_sv.append(_try_lock(env))  # busy: this stop holds it
        fake.stop(cid, 30)  # ginn-wait stops the container; the finish script writes nothing

    runit.on_down = supervised_stop
    record = stop_realm(env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock))

    assert record.state is RealmState.STOPPED
    assert lock_during_sv == ["busy"]
    assert runit.argvs == [[SV, "-w", str(supervisor.DOWN_WAIT_S), "down", str(runit.service)]]
    assert audited_ops(env) == ["stop"]
    assert stored(env, "lab").state is RealmState.STOPPED


@pytest.mark.fr("FR-016")
@pytest.mark.fr("AS-028")
def test_stop_of_a_failed_services_on_realm_stops_it_here_because_no_finish_will(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.FAILED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.state = "down"

    record = stop_realm(env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock))

    assert record.state is RealmState.STOPPED
    assert [argv[3] for argv in runit.argvs] == ["down"]
    assert audited_ops(env) == ["stop"]


@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-081")
def test_stop_of_an_unsupervised_services_on_realm_stops_it_with_podman(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True, services=True)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)

    record = stop_realm(env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock))

    assert record.state is RealmState.STOPPED
    assert runit.argvs == [], "no runsv watches the directory, so no sv runs"
    assert "stop" in fake.ops()


@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-062")
def test_a_development_realm_starts_with_the_container_it_was_made_with(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    made = [
        "device=/dev/fuse",
        "device=/dev/net/tun",
        "security-opt=seccomp",
        "security-opt=unmask",
    ]
    make_realm(
        env,
        fake,
        "lab",
        RealmState.STOPPED,
        running=False,
        tier=Tier.DEVELOPMENT,
        created_options=made,
    )

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert record.state is RealmState.RUNNING
    assert record.settings.tier is Tier.DEVELOPMENT


def _rebuilt_before_start(
    monkeypatch: pytest.MonkeyPatch, env: OpEnv, fake: LifecyclePodman
) -> list[tuple[str, list[Any]]]:
    """Stub the rebuild a stale start runs (ops/rebuild.py, FR-066). The stub saves the record it
    returns, as the swap does, and it records the podman calls that were made before it ran.

    The rebuild's own tests drive it on the create fake. Here only the start's order is checked.
    """
    store = RealmStore(env.paths)
    seen: list[tuple[str, list[Any]]] = []

    def rebuild(env_: Any, podman: Any, name: RealmName, **kwargs: Any) -> RealmRecord:
        seen.append((name.value, list(fake.calls)))
        fresh = replace(store.load(name), created_options=[], kvm_at_create=False)
        store.save(fresh)
        return fresh

    monkeypatch.setattr(rebuild_module, "rebuild_for_start", rebuild)
    return seen


@pytest.mark.fr("FR-066")
def test_a_start_whose_options_differ_from_the_container_rebuilds_before_it_starts(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Basic record that names nested options is not started with its old container. The
    rebuild comes first, before any podman call, and the start runs on the rebuilt generation."""
    make_realm(
        env,
        fake,
        "lab",
        RealmState.STOPPED,
        running=False,
        created_options=["device=/dev/fuse"],
    )
    fake.calls.clear()
    seen = _rebuilt_before_start(monkeypatch, env, fake)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert seen == [("lab", [])]
    assert record.state is RealmState.RUNNING
    assert record.created_options == []


@pytest.mark.fr("FR-066")
@pytest.mark.fr("DR-020")
def test_a_services_off_realm_running_with_no_container_is_settled_as_a_crash_first(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending change on a realm that reads running with no container is not refused with
    GINN-E-REALM-STATE. The crash is recorded first (running to failed, DR-020), the lazy rebuild
    then stops the failed realm and rebuilds it, and the start runs the new generation."""
    make_realm(
        env,
        fake,
        "lab",
        RealmState.RUNNING,
        running=False,
        created_options=["device=/dev/fuse"],
    )
    fake.calls.clear()
    seen = _rebuilt_before_start(monkeypatch, env, fake)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert len(seen) == 1, "the stale start rebuilds, and does not refuse the record"
    assert record.state is RealmState.RUNNING
    assert audited_ops(env) == ["crash", "stop", "start"]
    (crash,) = audit_of(env, "crash")
    assert crash.outcome == "ok"


@pytest.mark.fr("FR-063")
@pytest.mark.fr("NFR-SEC-002")
def test_a_start_with_a_kvm_toggle_the_container_lacks_rebuilds_first(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kvm device is fixed at create. A Custom realm whose toggle asks for it, on a host that
    had /dev/kvm at create, is rebuilt at its next start (ADR-006)."""
    make_realm(
        env,
        fake,
        "lab",
        RealmState.STOPPED,
        running=False,
        tier=Tier.CUSTOM,
        permissions=Permissions(toggles=BASIC_TOGGLES | {Toggle.KVM}),
        created_options=[],
        kvm_at_create=True,
    )
    fake.calls.clear()
    seen = _rebuilt_before_start(monkeypatch, env, fake)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert seen == [("lab", [])]
    assert record.state is RealmState.RUNNING


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
def test_a_start_with_a_gpu_toggle_the_container_was_made_without_rebuilds_first(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GPU device is fixed at create, so a stored toggle that differs from the flag makes the
    start rebuild the realm first (T8.3 is done, FR-066)."""
    make_realm(
        env,
        fake,
        "lab",
        RealmState.STOPPED,
        running=False,
        tier=Tier.CUSTOM,
        permissions=Permissions(toggles=BASIC_TOGGLES - {Toggle.GPU}),
        gpu_at_create=True,
    )
    fake.calls.clear()
    seen = _rebuilt_before_start(monkeypatch, env, fake)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert seen == [("lab", [])]
    assert record.state is RealmState.RUNNING


@pytest.mark.fr("FR-016")
def test_stop_of_a_running_realm_uses_the_timeout_and_keeps_its_restart_flag(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True, restart=True)
    cid = container_id(env, "lab")

    record = stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert record.state is RealmState.STOPPED
    assert record.restart_required is True
    assert ("stop", (cid, 30)) in fake.calls
    assert fake.containers[cid].state.value == "exited"
    (entry,) = audit_of(env, "stop")
    assert entry.outcome == "ok"
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
def test_stop_of_a_stopped_realm_whose_container_runs_stops_it_and_keeps_the_record(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=True)
    cid = container_id(env, "lab")
    raw = record_bytes(env, "lab")

    stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert ("stop", (cid, 30)) in fake.calls
    assert fake.containers[cid].state.value == "exited"
    assert stored(env, "lab").state is RealmState.STOPPED
    assert record_bytes(env, "lab") == raw
    (entry,) = audit_of(env, "stop")
    assert entry.outcome == "ok"


@pytest.mark.fr("FR-016")
def test_stop_of_a_stopped_realm_with_no_running_container_writes_nothing(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks
) -> None:
    before = make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    raw = record_bytes(env, "lab")

    record = stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert record == before
    assert record_bytes(env, "lab") == raw
    assert "stop" not in fake.ops()
    assert list(env.audit.read()) == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
def test_a_failed_realm_whose_stop_fails_is_stopped_when_podman_lists_no_running_container(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.FAILED, running=False)
    fake.fail_ops = {"stop": "GINN-E-INTERNAL"}

    record = stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert record.state is RealmState.STOPPED
    (entry,) = audit_of(env, "stop")
    assert entry.outcome == "ok"


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
def test_a_failed_realm_whose_stop_fails_with_a_running_container_stays_failed(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.FAILED, running=True)
    fake.fail_ops = {"stop": "GINN-E-INTERNAL"}

    with pytest.raises(GinnError) as caught:
        stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert caught.value.code == "GINN-E-INTERNAL"
    assert stored(env, "lab").state is RealmState.FAILED
    (entry,) = audit_of(env, "stop")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-INTERNAL")


@pytest.mark.fr("FR-025")
@pytest.mark.fr("FR-016")
def test_stop_of_an_unknown_realm_is_refused_before_any_write(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks
) -> None:
    with pytest.raises(GinnError) as caught:
        stop_realm(env, fake, RealmName("ghost"), hooks=hooks)

    assert caught.value.code == "GINN-E-NO-SUCH-REALM"
    assert fake.calls == []
    assert list(env.audit.read()) == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-124")
def test_start_then_stop_leave_one_ok_audit_entry_each_and_no_pending_journal(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)

    start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)
    stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    lifecycle_entries = [(e.op, e.outcome) for e in env.audit.read() if e.op in ("start", "stop")]
    assert lifecycle_entries == [("start", "ok"), ("stop", "ok")]
    assert env.journal.pending() == []


@pytest.mark.fr("FR-020")
@pytest.mark.fr("DR-020")
def test_status_reports_the_record_state_beside_podman_and_marks_a_mismatch(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "dead", RealmState.RUNNING, running=False)
    make_realm(env, fake, "live", RealmState.STOPPED, running=True)
    make_realm(env, fake, "gone", RealmState.RUNNING, running=True)
    del fake.containers[container_id(env, "gone")]

    dead = realm_status(env, fake, RealmName("dead"))
    live = realm_status(env, fake, RealmName("live"))
    gone = realm_status(env, fake, RealmName("gone"))

    assert (dead.state, dead.container_state, dead.state_mismatch) == ("running", "created", True)
    assert (live.state, live.container_state, live.state_mismatch) == ("stopped", "running", True)
    assert (gone.state, gone.container_state, gone.state_mismatch) == ("running", "absent", True)


@pytest.mark.fr("FR-020")
def test_a_stopped_record_with_a_container_that_is_not_running_has_no_mismatch(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)

    status = realm_status(env, fake, RealmName("lab"))

    assert (status.state, status.container_state, status.state_mismatch) == (
        "stopped",
        "created",
        False,
    )


@pytest.mark.fr("FR-020")
def test_status_says_unavailable_when_podman_cannot_answer_and_list_still_shows_the_realm(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    fake.ps_error = True

    (status,) = list_realms(env, fake, disk=False)

    assert (status.state, status.container_state, status.state_mismatch) == (
        "running",
        "unavailable",
        None,
    )


@pytest.mark.fr("FR-020")
@pytest.mark.fr("DR-020")
def test_podman_own_unknown_state_is_reported_as_unknown_not_as_unavailable(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    fake.containers[container_id(env, "lab")].state = ContainerState.UNKNOWN

    status = realm_status(env, fake, RealmName("lab"))

    # podman's own "unknown" is a state that ginn does not model. ginn cannot say whether that
    # container runs, so the mismatch is null, and the state keeps podman's name.
    assert (status.container_state, status.state_mismatch) == ("unknown", None)


@pytest.mark.fr("FR-020")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize("state", [RealmState.RUNNING, RealmState.STOPPED, RealmState.FAILED])
def test_podman_unknown_state_is_never_a_mismatch_for_any_record_state(
    env: OpEnv, fake: LifecyclePodman, state: RealmState
) -> None:
    make_realm(env, fake, "lab", state, running=False)
    fake.containers[container_id(env, "lab")].state = ContainerState.UNKNOWN

    status = realm_status(env, fake, RealmName("lab"))

    assert (status.container_state, status.state_mismatch) == ("unknown", None)


@pytest.mark.fr("FR-020")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    ("state", "container", "mismatch"),
    [
        (RealmState.RUNNING, ContainerState.PAUSED, False),
        (RealmState.RUNNING, ContainerState.STOPPING, False),
        (RealmState.STOPPED, ContainerState.PAUSED, True),
        (RealmState.FAILED, ContainerState.PAUSED, True),
        (RealmState.RUNNING, ContainerState.EXITED, True),
        (RealmState.RUNNING, ContainerState.CREATED, True),
        (RealmState.STOPPED, ContainerState.EXITED, False),
        (RealmState.STOPPED, ContainerState.CREATED, False),
    ],
)
def test_a_paused_container_counts_as_running_in_the_status_mismatch(
    env: OpEnv,
    fake: LifecyclePodman,
    state: RealmState,
    container: ContainerState,
    mismatch: bool,
) -> None:
    make_realm(env, fake, "lab", state, running=False)
    fake.containers[container_id(env, "lab")].state = container

    status = realm_status(env, fake, RealmName("lab"))

    assert (status.container_state, status.state_mismatch) == (container.value, mismatch)


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize("state", [RealmState.RUNNING, RealmState.FAILED])
def test_start_of_a_record_with_no_container_id_is_refused_before_any_state_write(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    state: RealmState,
) -> None:
    make_realm(env, fake, "lab", state, running=False)
    RealmStore(env.paths, env.clock).save(replace(stored(env, "lab"), container_id=None))
    before = record_bytes(env, "lab")
    fake.calls.clear()

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-INTERNAL"
    assert "ginn delete lab" in caught.value.remedy
    assert record_bytes(env, "lab") == before
    assert fake.ops() == []
    (entry,) = audit_of(env, "start")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-INTERNAL")


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize("state", [RealmState.RUNNING, RealmState.FAILED])
def test_stop_of_a_record_with_no_container_id_points_at_delete_and_writes_no_state(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, state: RealmState
) -> None:
    make_realm(env, fake, "lab", state, running=False)
    RealmStore(env.paths, env.clock).save(replace(stored(env, "lab"), container_id=None))
    before = record_bytes(env, "lab")
    fake.calls.clear()

    with pytest.raises(GinnError) as caught:
        stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert caught.value.code == "GINN-E-INTERNAL"
    assert "ginn delete lab" in caught.value.remedy
    assert record_bytes(env, "lab") == before
    assert fake.ops() == []


@pytest.mark.fr("FR-064")
@pytest.mark.fr("FR-066")
@pytest.mark.fr("FR-020")
def test_status_shows_the_recorded_share_skips_and_the_restart_required_flag(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(
        env,
        fake,
        "lab",
        RealmState.RUNNING,
        running=True,
        restart=True,
        share=[SKIP_DOWNLOADS],
    )

    status = realm_status(env, fake, RealmName("lab"), disk=False)

    assert status.share_skipped == [SKIP_DOWNLOADS]
    assert status.restart_required is True


@pytest.mark.fr("FR-064")
@pytest.mark.fr("FR-066")
def test_status_text_prints_each_skip_the_restart_line_and_a_corrupt_error(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(
        env, fake, "lab", RealmState.RUNNING, running=True, restart=True, share=[SKIP_DOWNLOADS]
    )

    text = render_status_text(realm_status(env, fake, RealmName("lab"), disk=False))

    assert SKIP_DOWNLOADS in text.splitlines()
    assert "restart required" in text.splitlines()
    assert "state: running" in text.splitlines()
    (env.paths.realms / "bad.json").write_text("{not json", encoding="utf-8")
    (bad,) = [entry for entry in list_realms(env, fake, disk=False) if entry.name == "bad"]
    assert render_status_text(bad).splitlines()[-1].startswith("error: GINN-E-INTERNAL")


@pytest.mark.fr("FR-020")
@pytest.mark.fr("IR-010")
def test_status_json_has_the_pinned_keys_and_the_disk_keys(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)

    data = realm_status(env, fake, RealmName("lab")).to_json()
    disk = data["disk"]

    assert tuple(data) == STATUS_KEYS
    assert isinstance(disk, dict)
    assert tuple(disk) == ("realm_home_bytes", "realm_home_partial", "container_bytes")
    assert data["exported_apps"] == 0  # no host entry of the realm exists
    assert data["error"] is None
    assert json.loads(json.dumps(data)) == data


@pytest.mark.fr("FR-020")
def test_list_is_sorted_by_name_and_reports_a_corrupt_record_without_failing(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "work", RealmState.STOPPED, running=False)
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    (env.paths.realms / "bad.json").write_text("{not json", encoding="utf-8")

    entries = list_realms(env, fake, disk=False)

    assert [entry.name for entry in entries] == ["bad", "lab", "work"]
    bad, lab, work = entries
    assert bad.error is not None and bad.error.startswith("GINN-E-INTERNAL")
    assert bad.state is None and bad.share_skipped is None
    assert lab.state == "stopped" and work.state == "stopped"


@pytest.mark.fr("FR-020")
def test_list_reports_an_unreadable_record_and_skips_one_removed_after_the_listing(
    env: OpEnv, fake: LifecyclePodman, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    (env.paths.realms / "locked.json").write_text("{}", encoding="utf-8")
    (env.paths.realms / "gone.json").write_text("{}", encoding="utf-8")
    real_load = RealmStore.load

    def load(self: RealmStore, name: RealmName) -> RealmRecord:
        if name.value == "locked":
            raise PermissionError(13, "Permission denied")
        if name.value == "gone":
            raise FileNotFoundError(2, "No such file or directory")
        return real_load(self, name)

    monkeypatch.setattr(RealmStore, "load", load)

    entries = list_realms(env, fake, disk=False)

    assert [entry.name for entry in entries] == ["lab", "locked"]
    assert entries[1].error == "GINN-E-INTERNAL: Permission denied"
    assert entries[0].state == "stopped"


@pytest.mark.fr("FR-025")
@pytest.mark.fr("FR-020")
def test_status_of_a_corrupt_record_raises_and_of_a_missing_one_names_it(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    (env.paths.realms / "bad.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(GinnError) as corrupt:
        realm_status(env, fake, RealmName("bad"))
    with pytest.raises(GinnError) as missing:
        realm_status(env, fake, RealmName("ghost"))

    assert corrupt.value.code == "GINN-E-INTERNAL"
    assert missing.value.code == "GINN-E-NO-SUCH-REALM"


@pytest.mark.fr("FR-020")
def test_disk_usage_counts_the_realm_home_and_does_not_follow_links(
    env: OpEnv, fake: LifecyclePodman, tmp_path: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    home = env.paths.homes / "lab"
    (home / "data").write_bytes(b"x" * 10_000)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"y" * 1_000_000)
    (home / "link").symlink_to(outside)

    status = realm_status(env, fake, RealmName("lab"))

    assert status.disk is not None
    assert status.disk.realm_home_partial is False
    assert 10_000 <= status.disk.realm_home_bytes < 1_000_000
    assert status.disk.container_bytes is None


@pytest.mark.fr("FR-020")
def test_the_disk_walk_stops_at_its_entry_budget_and_says_it_is_partial(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(5):
        (root / f"f{index}").write_bytes(b"z")

    _, partial = tree_usage(root, budget=2)
    total, full_walk_partial = tree_usage(root)

    assert partial is True
    assert full_walk_partial is False
    assert total > 0


@pytest.mark.fr("FR-020")
@pytest.mark.fr("FR-124")
def test_status_and_list_write_no_journal_or_audit_entry_and_run_no_mutation(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    raw = record_bytes(env, "lab")

    realm_status(env, fake, RealmName("lab"))
    list_realms(env, fake)

    assert env.journal.pending() == []
    assert list(env.audit.read()) == []
    assert record_bytes(env, "lab") == raw
    mutating_ops = {"start", "stop", "create", "rm", "rmi", "unshare", "exec"}
    assert mutating_ops.isdisjoint(fake.ops())


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
def test_a_running_record_whose_container_was_deleted_ends_stopped_when_started(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    del fake.containers[container_id(env, "lab")]

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-INTERNAL"
    assert stored(env, "lab").state is RealmState.STOPPED
    (entry,) = audit_of(env, "start")
    assert entry.outcome == "error"
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
def test_a_stopped_record_whose_container_was_deleted_fails_to_start_and_stays_stopped(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    del fake.containers[container_id(env, "lab")]

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-INTERNAL"
    assert stored(env, "lab").state is RealmState.STOPPED
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
def test_stop_of_a_running_record_whose_container_was_deleted_records_stopped(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    del fake.containers[container_id(env, "lab")]

    record = stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert record.state is RealmState.STOPPED
    (entry,) = audit_of(env, "stop")
    assert entry.outcome == "ok"
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
def test_stop_of_a_stopped_record_whose_container_was_deleted_does_nothing(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    raw = record_bytes(env, "lab")
    del fake.containers[container_id(env, "lab")]

    stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert "stop" not in fake.ops()
    assert record_bytes(env, "lab") == raw
    assert list(env.audit.read()) == []


@pytest.mark.fr("FR-016")
def test_a_listing_error_before_start_propagates_and_writes_nothing(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    fake.ps_error = True

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-INTERNAL"
    assert stored(env, "lab").state is RealmState.RUNNING
    assert "start" not in fake.ops()
    assert list(env.audit.read()) == []


@pytest.mark.fr("FR-016")
def test_a_paused_container_counts_as_running_for_start(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    fake.containers[container_id(env, "lab")].state = ContainerState.PAUSED

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert record.state is RealmState.RUNNING
    assert "start" not in fake.ops()


@pytest.mark.fr("FR-016")
@pytest.mark.fr("DR-020")
def test_a_paused_container_is_not_confirmed_stopped_when_a_stop_fails(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.FAILED, running=True)
    fake.containers[container_id(env, "lab")].state = ContainerState.PAUSED
    fake.fail_ops = {"stop": "GINN-E-INTERNAL"}

    with pytest.raises(GinnError):
        stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert stored(env, "lab").state is RealmState.FAILED


@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-066")
def test_a_record_write_failure_after_a_failed_attach_is_a_note_and_the_attach_error_is_raised(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (home / "Downloads").mkdir()
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    fake.unshare_results.append(ExecResult(1, "", "ginn-rebind: mount refused"))
    real_set_state = RealmStore.set_state

    def refuse_stopped(self: RealmStore, name: RealmName, new_state: RealmState) -> RealmRecord:
        if new_state is RealmState.STOPPED:
            raise make("GINN-E-INTERNAL")
        return real_set_state(self, name, new_state)

    monkeypatch.setattr(RealmStore, "set_state", refuse_stopped)

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    assert "record not set stopped: GINN-E-INTERNAL" in caught.value.__notes__
    assert stored(env, "lab").state is RealmState.FAILED


def _restat(
    st: os.stat_result, *, ino: int | None = None, dev: int | None = None
) -> os.stat_result:
    """A copy of `st` with a different inode or device, as a test stands for a swapped directory."""
    return os.stat_result(
        (
            st.st_mode,
            st.st_ino if ino is None else ino,
            st.st_dev if dev is None else dev,
            st.st_nlink,
            st.st_uid,
            st.st_gid,
            st.st_size,
            int(st.st_atime),
            int(st.st_mtime),
            int(st.st_ctime),
        )
    )


@pytest.mark.fr("FR-020")
def test_the_disk_walk_is_partial_when_a_directory_descriptor_is_not_the_listed_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f").write_bytes(b"x" * 100)
    expected_root = lifecycle._allocated(os.lstat(root))
    real_fstat = os.fstat

    def other_inode(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        return _restat(st, ino=st.st_ino + 1)

    monkeypatch.setattr(os, "fstat", other_inode)

    total, partial = tree_usage(root)

    assert partial is True
    assert total == expected_root


@pytest.mark.fr("FR-020")
def test_the_disk_walk_skips_an_entry_on_another_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f").write_bytes(b"x" * 100)
    (root / "other").mkdir()
    (root / "other" / "g").write_bytes(b"y" * 5000)
    real_lstat_entry = lifecycle._lstat_entry

    def other_device(entry: os.DirEntry[str]) -> os.stat_result:
        st = real_lstat_entry(entry)
        if entry.name == "other":
            return _restat(st, dev=st.st_dev + 1)
        return st

    monkeypatch.setattr(lifecycle, "_lstat_entry", other_device)

    total, partial = tree_usage(root)

    assert partial is False
    expected = lifecycle._allocated(os.lstat(root)) + lifecycle._allocated(os.lstat(root / "f"))
    assert total == expected


def _change_etc_hosts(host: Path) -> None:
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")


@pytest.mark.fr("FR-006")
def test_a_host_change_during_a_start_is_handed_to_the_caller_after_the_start(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_realm(env, fake, "arch", RealmState.STOPPED, running=False)
    real_start = fake.start

    def start_and_change(container_id: str) -> None:
        _change_etc_hosts(host)
        real_start(container_id)

    monkeypatch.setattr(fake, "start", start_and_change)
    notices: list[Any] = []

    record = start_realm(env, fake, RealmName("arch"), session=user, hooks=hooks, warnings=notices)

    assert record.state is RealmState.RUNNING
    assert [notice.code for notice in notices] == ["GINN-W-HOST-CHANGED"]
    assert "/etc/hosts" in notices[0].message


@pytest.mark.fr("FR-006")
def test_a_clean_start_hands_over_no_warning_and_no_warnings_list_is_needed(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "arch", RealmState.STOPPED, running=False)
    notices: list[Any] = []
    start_realm(env, fake, RealmName("arch"), session=user, hooks=hooks, warnings=notices)
    assert notices == []
    start_realm(env, fake, RealmName("arch"), session=user, hooks=hooks)  # the old call shape


@pytest.mark.fr("FR-006")
def test_a_host_change_during_a_stop_is_handed_to_the_caller_after_the_stop(
    env: OpEnv,
    fake: LifecyclePodman,
    hooks: LifecycleHooks,
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_realm(env, fake, "arch", RealmState.RUNNING, running=True)
    real_stop = fake.stop

    def stop_and_change(container_id: str, timeout_s: int) -> None:
        _change_etc_hosts(host)
        real_stop(container_id, timeout_s)

    monkeypatch.setattr(fake, "stop", stop_and_change)
    notices: list[Any] = []

    record = stop_realm(env, fake, RealmName("arch"), hooks=hooks, warnings=notices)

    assert record.state is RealmState.STOPPED
    assert [notice.code for notice in notices] == ["GINN-W-HOST-CHANGED"]


@pytest.mark.fr("FR-067")
def test_a_development_realm_starts_one_filter_before_its_grants_and_stop_ends_it(
    env: OpEnv,
    fake: LifecyclePodman,
    home: Path,
    tmp_path: Path,
    hooks: LifecycleHooks,
) -> None:
    from tests.unit.test_ops_security import _Launches

    launches = _Launches()
    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700, exist_ok=True)
    session = HostUser(
        uid=UID,
        host_home=home,
        host_env={"XDG_RUNTIME_DIR": str(runtime), "SSH_AUTH_SOCK": str(runtime / "ssh")},
    )
    security = SecurityHooks(
        agent_launch=launches,
        gpg_extra=lambda: None,
        file_type=lambda _path: stat.S_IFSOCK,
    )
    hooks_s = replace(hooks, security=security)
    made = [
        "device=/dev/fuse",
        "device=/dev/net/tun",
        "security-opt=seccomp",
        "security-opt=unmask",
    ]
    make_realm(
        env,
        fake,
        "lab",
        RealmState.STOPPED,
        running=False,
        tier=Tier.DEVELOPMENT,
        created_options=made,
    )

    try:
        record = start_realm(env, fake, RealmName("lab"), session=session, hooks=hooks_s)
        assert record.state is RealmState.RUNNING
        assert len(launches.argvs) == 1
        assert read_agent_record(env.paths).proc == launches.children[0].proc
        stop_realm(env, fake, RealmName("lab"), hooks=hooks_s)
        assert not launches.children[0].running()
        assert read_agent_record(env.paths) == AgentRecord()
    finally:
        for child in launches.children:
            if child.running():
                child.stop()


@pytest.mark.fr("NFR-004")
def test_a_development_start_resolves_the_gpg_socket_once_and_stores_it(
    env: OpEnv,
    fake: LifecyclePodman,
    home: Path,
    tmp_path: Path,
    hooks: LifecycleHooks,
) -> None:
    """gpgconf runs once per start, and the enter that follows reads the stored socket (NFR-004)."""
    from tests.unit.test_ops_security import _Launches

    launches = _Launches()
    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700, exist_ok=True)
    session = HostUser(
        uid=UID,
        host_home=home,
        host_env={"XDG_RUNTIME_DIR": str(runtime), "SSH_AUTH_SOCK": str(runtime / "ssh")},
    )
    lookups: list[str] = []
    socket = "/run/user/1000/gnupg/S.gpg-agent.extra"

    def gpg_extra() -> str:
        lookups.append(socket)
        return socket

    security = SecurityHooks(
        agent_launch=launches,
        gpg_extra=gpg_extra,
        file_type=lambda _path: stat.S_IFSOCK,
    )
    hooks_s = replace(hooks, security=security)
    made = [
        "device=/dev/fuse",
        "device=/dev/net/tun",
        "security-opt=seccomp",
        "security-opt=unmask",
    ]
    make_realm(
        env,
        fake,
        "lab",
        RealmState.STOPPED,
        running=False,
        tier=Tier.DEVELOPMENT,
        created_options=made,
    )
    try:
        start_realm(env, fake, RealmName("lab"), session=session, hooks=hooks_s)
        stored = RealmStore(env.paths, env.clock).load(RealmName("lab"))
        assert (stored.gpg_extra, stored.gpg_extra_known) == (socket, True)
        assert lookups == [socket]
        stop_realm(env, fake, RealmName("lab"), hooks=hooks_s)
        start_realm(env, fake, RealmName("lab"), session=session, hooks=hooks_s)
        assert lookups == [socket, socket]
    finally:
        for child in launches.children:
            if child.running():
                child.stop()


@pytest.mark.fr("FR-067")
def test_a_basic_realm_start_never_touches_the_filter_state(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)
    assert not agent_state_path(env.paths).exists()


@pytest.mark.fr("FR-067")
def test_a_missing_filter_helper_starts_the_development_realm_without_ssh_and_warns(
    env: OpEnv,
    fake: LifecyclePodman,
    home: Path,
    tmp_path: Path,
    hooks: LifecycleHooks,
) -> None:
    """A missing helper is a skip: the start goes on with no SSH grant, and it warns (ADR-008)."""
    from tests.unit.test_ops_security import _Launches

    launches = _Launches()
    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700, exist_ok=True)
    session = HostUser(
        uid=UID,
        host_home=home,
        host_env={"XDG_RUNTIME_DIR": str(runtime), "SSH_AUTH_SOCK": str(runtime / "ssh")},
    )
    helper = str(tmp_path / "no-such-ginn-agent-filter")
    security = SecurityHooks(
        agent_helper=helper,
        agent_launch=launches,
        gpg_extra=lambda: None,
        file_type=lambda path: None if path == helper else stat.S_IFSOCK,
    )
    hooks_s = replace(hooks, security=security)
    made = [
        "device=/dev/fuse",
        "device=/dev/net/tun",
        "security-opt=seccomp",
        "security-opt=unmask",
    ]
    make_realm(
        env,
        fake,
        "lab",
        RealmState.STOPPED,
        running=False,
        tier=Tier.DEVELOPMENT,
        created_options=made,
    )
    notices: list[GinnWarning] = []
    record = start_realm(
        env, fake, RealmName("lab"), session=session, hooks=hooks_s, warnings=notices
    )
    assert record.state is RealmState.RUNNING
    assert [notice.code for notice in notices] == ["GINN-W-AGENT-FILTER"]
    assert launches.argvs == []
    assert any(
        line == "agent skipped: SSH agent (the filter helper is not installed)"
        for line in record.share_skipped
    )
    stored = read_agent_record(env.paths)
    assert stored.members == ("lab",) and stored.proc is None


@pytest.mark.fr("FR-067")
def test_an_os_error_in_the_filter_record_rolls_a_development_start_back(
    env: OpEnv,
    fake: LifecyclePodman,
    home: Path,
    tmp_path: Path,
    hooks: LifecycleHooks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain OSError while the filter record is read is a GinnError, so the start rolls back."""
    from ginn.ops import security_ops

    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700, exist_ok=True)
    session = HostUser(
        uid=UID,
        host_home=home,
        host_env={"XDG_RUNTIME_DIR": str(runtime), "SSH_AUTH_SOCK": str(runtime / "ssh")},
    )

    def unreadable(paths: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(security_ops, "read_agent_record", unreadable)
    made = [
        "device=/dev/fuse",
        "device=/dev/net/tun",
        "security-opt=seccomp",
        "security-opt=unmask",
    ]
    make_realm(
        env,
        fake,
        "lab",
        RealmState.STOPPED,
        running=False,
        tier=Tier.DEVELOPMENT,
        created_options=made,
    )
    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=session, hooks=hooks)
    assert caught.value.code == "GINN-E-INTERNAL"
    assert RealmStore(env.paths, env.clock).load(RealmName("lab")).state is RealmState.STOPPED


@pytest.mark.fr("FR-068")
def test_an_abstract_session_bus_is_listed_among_the_share_skips_of_the_start(
    env: OpEnv, fake: LifecyclePodman, home: Path, tmp_path: Path, hooks: LifecycleHooks
) -> None:
    """AS-012: the refused bus is recorded like a skipped grant, so `ginn status` lists it."""
    runtime = tmp_path / "hostrun"
    runtime.mkdir(mode=0o700, exist_ok=True)
    session = HostUser(
        uid=UID,
        host_home=home,
        host_env={
            "XDG_RUNTIME_DIR": str(runtime),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus,abstract=/tmp/dbus-x",
        },
    )
    hooks_s = replace(hooks, security=SecurityHooks(dbus_binary="/usr/bin/xdg-dbus-proxy"))
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    record = start_realm(env, fake, RealmName("lab"), session=session, hooks=hooks_s)
    assert any(
        line.startswith("bus skipped: D-Bus session bus (") and "AS-012" in line
        for line in record.share_skipped
    )


@pytest.mark.fr("FR-081")
def test_status_shows_the_runit_state_of_a_services_on_realm_beside_its_record(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.state = "run"

    status = realm_status(
        env,
        fake,
        RealmName("lab"),
        hooks=_services_hooks(hooks, runit, clock),
        disk=False,
    )

    assert status.services is True
    assert status.autostart is False
    assert status.supervision == "run, pid 4321, 0 s"
    assert "supervision: run, pid 4321, 0 s" in render_status_text(status)


@pytest.mark.fr("FR-081")
def test_a_realm_given_up_after_crashes_shows_failed_and_runit_down(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.FAILED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.state = "down"

    status = realm_status(
        env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock), disk=False
    )

    assert status.state == "failed"
    assert status.supervision == "down, 0 s"


@pytest.mark.fr("FR-081")
def test_status_shows_the_record_when_runit_does_not_answer(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.status_reply = Result(
        1, "", "warning: x: unable to open supervise/ok: file does not exist\n"
    )

    status = realm_status(
        env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock), disk=False
    )

    assert status.state == "stopped"
    assert status.supervision is not None
    assert status.supervision.startswith("unavailable (GINN-E-SUPERVISOR-DOWN)")


@pytest.mark.fr("DR-012")
def test_a_services_off_realm_shows_no_runit_state_and_runs_no_sv(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)

    status = realm_status(
        env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock), disk=False
    )

    assert status.supervision is None
    assert status.autostart is False
    assert runit.argvs == []


@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-081")
def test_stop_of_a_services_on_realm_whose_runsv_is_gone_stops_it_with_podman(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    """A supervised directory whose runsv no longer answers has nothing to restart the realm, so
    the stop goes to podman, as delete does (GINN-E-SUPERVISOR-DOWN is passed over)."""
    _supervised_realm(env, fake, "lab", state=RealmState.RUNNING, running=True)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.down_reply = Result(1, "", "fail: x: runsv not running\n")

    record = stop_realm(env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock))

    assert record.state is RealmState.STOPPED
    assert [argv[3] for argv in runit.argvs] == ["down"]
    assert "stop" in fake.ops()


@pytest.mark.fr("FR-081")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    "state",
    [RealmState.UPDATING, RealmState.REBUILDING, RealmState.CREATING, RealmState.BROKEN],
)
def test_a_services_on_stop_of_a_realm_in_a_state_it_does_not_allow_sends_no_sv_command(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock, state: RealmState
) -> None:
    """An sv down would stop the container under an upgrade, so the state is refused first."""
    _supervised_realm(env, fake, "lab", state=state, running=True)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        stop_realm(env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock))

    assert caught.value.code == "GINN-E-REALM-STATE"
    assert runit.argvs == []
    assert fake.ops() == []
    assert (env.paths.realms / "lab.json").read_bytes() == before


@pytest.mark.fr("FR-081")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    "state",
    [RealmState.UPDATING, RealmState.REBUILDING, RealmState.CREATING, RealmState.BROKEN],
)
def test_a_services_on_start_of_a_realm_in_a_state_it_does_not_allow_sends_no_sv_command(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    clock: FakeClock,
    state: RealmState,
) -> None:
    _supervised_realm(env, fake, "lab", state=state, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)

    with pytest.raises(GinnError) as caught:
        start_realm(
            env, fake, RealmName("lab"), session=user, hooks=_services_hooks(hooks, runit, clock)
        )

    assert caught.value.code == "GINN-E-REALM-STATE"
    assert runit.argvs == []
    assert stored(env, "lab").state is state


@pytest.mark.fr("FR-122")
def test_a_services_on_stop_refused_by_an_unrecovered_journal_entry_stops_nothing(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.RUNNING, running=True)
    env.journal.begin("start", RealmName("lab"), {})
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)

    with pytest.raises(GinnError) as caught:
        stop_realm(env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock))

    assert caught.value.code == "GINN-E-RECOVERY-PENDING"
    assert runit.argvs == []
    assert "stop" not in fake.ops()
    assert stored(env, "lab").state is RealmState.RUNNING


@pytest.mark.fr("FR-006")
def test_a_services_on_stop_refused_by_a_host_change_stops_nothing(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock, host: Path
) -> None:
    """The host check is made before sv runs. A change that is not acknowledged refuses the stop,
    and the container keeps running (the locked check would otherwise refuse after the stop)."""
    (host / "etc").mkdir(exist_ok=True)
    (host / "etc" / "hosts").write_text("10.0.0.1 before\n", encoding="utf-8")
    _supervised_realm(env, fake, "lab", state=RealmState.RUNNING, running=True)
    env.integrity.before_op(env.audit, env.user)  # the baseline, as the first operation makes it
    (host / "etc" / "hosts").write_text("10.0.0.9 changed\n", encoding="utf-8")
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)

    with pytest.raises(GinnError) as caught:
        stop_realm(env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock))

    assert caught.value.code == "GINN-E-HOST-UNACKED"
    assert runit.argvs == []
    assert "stop" not in fake.ops()
    assert stored(env, "lab").state is RealmState.RUNNING


@pytest.mark.fr("FR-081")
def test_a_services_on_stop_while_another_operation_holds_the_lock_sends_no_sv_command(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.RUNNING, running=True)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)

    with realm_lock(env.paths, RealmName("lab"), "rebuild"):
        with pytest.raises(GinnError) as caught:
            stop_realm(env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock))

    assert caught.value.code == "GINN-E-REALM-BUSY"
    assert runit.argvs == []


@pytest.mark.fr("AS-028")
def test_a_crash_restart_that_reads_down_with_want_up_is_not_a_give_up(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    """runsv shows `down` while it waits to restart run after a crash, and it says `want up`. That
    is not runsv giving the realm up, so the wait goes on and the realm starts."""
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    cid = container_id(env, "lab")

    def restarting() -> None:
        runit.state = "down"
        runit.want_up = True

    def recovered() -> None:
        fake.start(cid)
        RealmStore(env.paths, env.clock).set_state(RealmName("lab"), RealmState.RUNNING)
        runit.state = "run"
        runit.want_up = False

    runit.on_up = restarting
    runit.on_sleep = recovered
    record = start_realm(
        env, fake, RealmName("lab"), session=user, hooks=_services_hooks(hooks, runit, clock)
    )

    assert record.state is RealmState.RUNNING
    verbs = [argv[3] for argv in runit.argvs if argv[1] != "status"]
    assert verbs == ["up"], "no second sv command during the restart"


@pytest.mark.fr("FR-081")
def test_a_failed_status_read_during_the_start_wait_counts_as_unknown_and_the_wait_goes_on(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.status_reply = Result(1, "", "fail: x: unable to read supervise/status\n")
    cid = container_id(env, "lab")

    def recovered() -> None:
        runit.status_reply = None
        fake.start(cid)
        RealmStore(env.paths, env.clock).set_state(RealmName("lab"), RealmState.RUNNING)

    runit.on_sleep = recovered
    record = start_realm(
        env, fake, RealmName("lab"), session=user, hooks=_services_hooks(hooks, runit, clock)
    )

    assert record.state is RealmState.RUNNING


@pytest.mark.fr("FR-081")
@pytest.mark.fr("FR-016")
def test_a_stop_whose_sv_control_fails_stops_nothing_and_names_the_control_error(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    """runsv may still be alive when its control FIFO cannot be opened, so no podman stop follows:
    runsv would restart the container after it."""
    _supervised_realm(env, fake, "lab", state=RealmState.RUNNING, running=True)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.down_reply = Result(
        1, "", "warning: x: unable to open supervise/control: file does not exist\n"
    )

    with pytest.raises(GinnError) as caught:
        stop_realm(env, fake, RealmName("lab"), hooks=_services_hooks(hooks, runit, clock))

    assert caught.value.code == "GINN-E-SERVICE-CONTROL"
    assert "stop" not in fake.ops()
    assert stored(env, "lab").state is RealmState.RUNNING


PODMAN_PREFIX = ["/usr/bin/podman", "--root", "/data/podman", "--runroot", "/run/ginn/podman"]
IDLE_PID = 7777


class IdleSpawns:
    """The spawn seam of the idle watcher: records each argv, starts nothing."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any, log: Path) -> int:
        self.calls.append(list(argv))
        return IDLE_PID


def _with_idle(hooks: LifecycleHooks, tmp_path: Path, spawns: IdleSpawns) -> LifecycleHooks:
    """The hooks with an executable ginn-idle that exists, and the recording spawn."""
    helper = tmp_path / "ginn-idle"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    idle = IdleHooks(
        helper=str(helper),
        grace_s=60,
        spawn=spawns,
        start_ticks=lambda pid: 500 + pid,
    )
    return replace(hooks, idle=idle)


@pytest.mark.fr("FR-018")
def test_a_services_off_start_arms_the_idle_watcher_for_the_containers_init(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, tmp_path: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    fake.global_argv = lambda: list(PODMAN_PREFIX)  # type: ignore[attr-defined]
    spawns = IdleSpawns()
    notices: list[GinnWarning] = []

    record = start_realm(
        env,
        fake,
        RealmName("lab"),
        session=user,
        hooks=_with_idle(hooks, tmp_path, spawns),
        warnings=notices,
    )

    cid = container_id(env, "lab")
    init = fake.inspect(cid).pid
    assert record.state is RealmState.RUNNING
    assert spawns.calls == [
        [str(tmp_path / "ginn-idle"), str(init), "60", *PODMAN_PREFIX, "--", cid]
    ]
    assert [notice.code for notice in notices] == []


@pytest.mark.fr("FR-018")
def test_a_services_on_start_arms_no_idle_watcher(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, tmp_path: Path
) -> None:
    """A services-on realm is stopped by its runit service, never by a watcher (FR-018, ADR-003)."""
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    fake.global_argv = lambda: list(PODMAN_PREFIX)  # type: ignore[attr-defined]
    spawns = IdleSpawns()
    start_realm(
        env,
        fake,
        RealmName("lab"),
        session=user,
        hooks=_with_idle(hooks, tmp_path, spawns),
        supervised=True,
    )
    assert spawns.calls == []


@pytest.mark.fr("FR-018")
def test_a_missing_helper_warns_and_the_services_off_start_still_succeeds(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, tmp_path: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    fake.global_argv = lambda: list(PODMAN_PREFIX)  # type: ignore[attr-defined]
    spawns = IdleSpawns()
    absent = replace(
        hooks,
        idle=IdleHooks(helper=str(tmp_path / "absent" / "ginn-idle"), spawn=spawns),
    )
    notices: list[GinnWarning] = []

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=absent, warnings=notices)

    assert record.state is RealmState.RUNNING
    assert spawns.calls == []
    assert [notice.code for notice in notices] == ["GINN-W-IDLE-STOP"]


@pytest.mark.fr("FR-018")
def test_a_failed_inspect_for_the_watcher_warns_and_the_start_still_succeeds(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The start's own inspect succeeds. The watcher's inspect, after the start, fails."""
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    fake.global_argv = lambda: list(PODMAN_PREFIX)  # type: ignore[attr-defined]
    armed = {"on": False}
    real_inspect = fake.inspect

    def inspect(container_id: str) -> ContainerInfo:
        if armed["on"]:
            raise make("GINN-E-INTERNAL")
        return real_inspect(container_id)

    real_start = lifecycle._start

    def start_then_arm(*args: Any, **kwargs: Any) -> RealmRecord:
        result = real_start(*args, **kwargs)
        armed["on"] = True
        return result

    monkeypatch.setattr(fake, "inspect", inspect)
    monkeypatch.setattr(lifecycle, "_start", start_then_arm)
    spawns = IdleSpawns()
    notices: list[GinnWarning] = []

    record = start_realm(
        env,
        fake,
        RealmName("lab"),
        session=user,
        hooks=_with_idle(hooks, tmp_path, spawns),
        warnings=notices,
    )

    assert record.state is RealmState.RUNNING
    assert spawns.calls == []
    assert [notice.code for notice in notices] == ["GINN-W-IDLE-STOP"]


@pytest.mark.fr("FR-018")
def test_a_second_start_of_a_running_realm_arms_nothing(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, tmp_path: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    fake.global_argv = lambda: list(PODMAN_PREFIX)  # type: ignore[attr-defined]
    spawns = IdleSpawns()

    start_realm(
        env, fake, RealmName("lab"), session=user, hooks=_with_idle(hooks, tmp_path, spawns)
    )

    assert spawns.calls == []


@pytest.mark.fr("FR-018")
def test_an_adapter_without_global_argv_arms_no_watcher(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, tmp_path: Path
) -> None:
    """A fake has no podman binary to name in the watcher's argv, so nothing is armed."""
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    spawns = IdleSpawns()

    start_realm(
        env, fake, RealmName("lab"), session=user, hooks=_with_idle(hooks, tmp_path, spawns)
    )

    assert spawns.calls == []


STOP_LINE_TAIL = "ginn-idle: no user process for 60 s; stopping {cid}\n"
FAIL_LINE = "ginn-idle: podman stop failed; init 4321 still runs\n"


def _idle_evidence(
    env: OpEnv,
    text: str | None,
    *,
    container: str | None = None,
    watcher_pid: int = 999_999,
    watcher_start: int = 1,
) -> str:
    """Write the watcher record and log of realm `lab`, as a services-off start would (FR-018).

    `text` is the log (None writes no log). The record names the realm's container unless
    `container` is given.
    """
    cid = container or container_id(env, "lab")
    (env.paths.runtime / "idle").mkdir(mode=0o700, exist_ok=True)
    record = {
        "watcher_pid": watcher_pid,
        "watcher_start": watcher_start,
        "container_id": cid,
        "init_pid": 4321,
        "init_start": 900,
    }
    record_path(env.paths, RealmName("lab")).write_text(json.dumps(record), encoding="utf-8")
    if text is not None:
        log_path(env.paths, RealmName("lab")).write_text(text, encoding="utf-8")
    return cid


def _stop_line(cid: str) -> str:
    return STOP_LINE_TAIL.format(cid=cid)


def _set_state_targets(monkeypatch: pytest.MonkeyPatch) -> list[RealmState]:
    """Record every state that set_state writes, in order (the crash path writes failed)."""
    targets: list[RealmState] = []
    real = RealmStore.set_state

    def spy(self: RealmStore, name: RealmName, new_state: Any) -> RealmRecord:
        targets.append(new_state)
        return real(self, name, new_state)

    monkeypatch.setattr(RealmStore, "set_state", spy)
    return targets


@pytest.mark.fr("FR-018")
def test_a_start_of_an_idle_stopped_realm_records_the_idle_stop_then_starts_plainly(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idle stop is a clean stop: running to stopped, audited as its own entry, and no crash
    (running to failed) before the start."""
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    fake.global_argv = lambda: list(PODMAN_PREFIX)  # type: ignore[attr-defined]
    _idle_evidence(env, _stop_line(container_id(env, "lab")))
    targets = _set_state_targets(monkeypatch)
    spawns = IdleSpawns()

    record = start_realm(
        env, fake, RealmName("lab"), session=user, hooks=_with_idle(hooks, tmp_path, spawns)
    )

    assert record.state is RealmState.RUNNING
    assert targets == [RealmState.STOPPED], "no failed state: the idle stop is not a crash"
    assert audited_ops(env) == ["idle-stop", "start"]
    (idle_entry,) = audit_of(env, "idle-stop")
    assert idle_entry.outcome == "ok"
    assert len(spawns.calls) == 1, "the new start arms a watcher for its own container"


@pytest.mark.fr("FR-018")
def test_a_failed_stop_line_is_a_crash_and_no_idle_stop_is_recorded(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    cid = container_id(env, "lab")
    _idle_evidence(env, _stop_line(cid) + FAIL_LINE)
    targets = _set_state_targets(monkeypatch)

    record = start_realm(
        env, fake, RealmName("lab"), session=user, hooks=_with_idle(hooks, tmp_path, IdleSpawns())
    )

    assert record.state is RealmState.RUNNING
    assert targets == [RealmState.FAILED], "a stop that podman failed is the crash path"
    assert audited_ops(env) == ["start"]


@pytest.mark.fr("FR-018")
def test_a_stop_of_an_idle_stopped_realm_is_already_stopped_and_runs_no_podman_stop(
    env: OpEnv, fake: LifecyclePodman, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    _idle_evidence(env, _stop_line(container_id(env, "lab")))

    record = stop_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert record.state is RealmState.STOPPED
    assert "stop" not in fake.ops()
    assert audited_ops(env) == ["idle-stop"]


@pytest.mark.fr("FR-018")
def test_status_of_an_idle_stopped_realm_reads_stopped_with_the_idle_reason_and_writes_nothing(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    _idle_evidence(env, _stop_line(container_id(env, "lab")))
    before = record_bytes(env, "lab")

    status = realm_status(env, fake, RealmName("lab"), disk=False)

    assert status.state == "stopped"
    assert status.state_reason == "idle"
    assert status.state_mismatch is False
    assert record_bytes(env, "lab") == before
    assert audited_ops(env) == []


@pytest.mark.fr("FR-018")
def test_status_keeps_the_mismatch_when_the_stop_line_is_followed_by_a_failure(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    _idle_evidence(env, _stop_line(container_id(env, "lab")) + FAIL_LINE)

    status = realm_status(env, fake, RealmName("lab"), disk=False)

    assert status.state == "running"
    assert status.state_reason is None
    assert status.state_mismatch is True


@pytest.mark.fr("FR-018")
def test_status_of_a_running_container_is_not_idle_even_with_the_stop_line(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    _idle_evidence(env, _stop_line(container_id(env, "lab")))

    status = realm_status(env, fake, RealmName("lab"), disk=False)

    assert status.state == "running"
    assert status.state_reason is None


@pytest.mark.fr("FR-018")
def test_a_watcher_record_for_another_container_does_not_make_the_stop_idle(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    _idle_evidence(env, _stop_line("some-other-container"), container="some-other-container")

    status = realm_status(env, fake, RealmName("lab"), disk=False)

    assert status.state == "running"
    assert status.state_mismatch is True


@pytest.mark.fr("FR-018")
def test_a_reused_watcher_pid_does_not_change_the_idle_answer(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    """The answer rests on the record and the log, which a new arm replaces. A reused pid is not
    read at all, so it cannot make a stop idle or stop being idle."""
    make_realm(env, fake, "lab", RealmState.RUNNING, running=False)
    _idle_evidence(
        env,
        _stop_line(container_id(env, "lab")),
        watcher_pid=os.getpid(),
        watcher_start=1,
    )

    status = realm_status(env, fake, RealmName("lab"), disk=False)

    assert status.state == "stopped"
    assert status.state_reason == "idle"


@pytest.mark.fr("FR-081")
@pytest.mark.fr("AS-028")
def test_a_stop_requested_during_a_start_is_not_read_as_a_give_up(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    """A `down` status with `normally up` is a stop request (the down file is absent), not runsv
    giving the realm up after crashes, which writes the down file (AS-028)."""
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.on_up = lambda: setattr(runit, "state", "down")  # normally_up stays True

    with pytest.raises(GinnError) as caught:
        start_realm(
            env,
            fake,
            RealmName("lab"),
            session=user,
            hooks=_services_hooks(hooks, runit, clock, start_wait_s=60.0),
        )

    assert caught.value.code == "GINN-E-SERVICE-TIMEOUT"
    assert "stop was requested" in caught.value.details["detail"]
    assert "AS-028" not in caught.value.details["detail"]


@pytest.mark.fr("FR-081")
def test_a_runsv_proven_gone_during_the_start_wait_is_supervisor_down(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    """Only a proven-gone runsv is an error that ends the wait. An unreadable status is unknown."""
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)
    runit.on_up = lambda: setattr(
        runit, "status_reply", Result(1, "", "fail: x: runsv not running\n")
    )

    with pytest.raises(GinnError) as caught:
        start_realm(
            env,
            fake,
            RealmName("lab"),
            session=user,
            hooks=_services_hooks(hooks, runit, clock, start_wait_s=60.0),
        )

    assert caught.value.code == "GINN-E-SUPERVISOR-DOWN"


@pytest.mark.fr("FR-081")
def test_a_services_on_start_refusal_names_the_states_a_user_can_start_from(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, clock: FakeClock
) -> None:
    _supervised_realm(env, fake, "lab", state=RealmState.BROKEN, running=False)
    runit = RunitFake(service_dir(env.paths, RealmName("lab")), clock)

    with pytest.raises(GinnError) as caught:
        start_realm(
            env, fake, RealmName("lab"), session=user, hooks=_services_hooks(hooks, runit, clock)
        )

    assert caught.value.code == "GINN-E-REALM-STATE"
    assert "stopped or failed" in caught.value.message
    assert "running" not in caught.value.message


@pytest.mark.fr("FR-044")
def test_exported_apps_counts_the_host_entries_of_this_realm_only(
    env: OpEnv, fake: LifecyclePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    make_realm(env, fake, "other", RealmState.STOPPED, running=False)
    env.paths.applications_dir.mkdir(parents=True, exist_ok=True)
    for realm, app in [("lab", "alpha"), ("lab", "beta"), ("other", "gamma")]:
        (env.paths.applications_dir / f"ginn-{realm}-{app}.desktop").write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={app}\n"
            "Exec=/bin/true\n"
            f"X-Ginn-Realm={realm}\n"
            f"X-Ginn-Source={app}\n",
            encoding="utf-8",
        )

    status = realm_status(env, fake, RealmName("lab"), disk=False)

    assert status.exported_apps == 2
    assert realm_status(env, fake, RealmName("other"), disk=False).exported_apps == 1


RUN_USER_STAT = ("stat", "-c", "%u", "--", f"/run/user/{UID}")
"""The argv that `run_user_owned` runs for the realm's runtime dir (T7.28)."""


def _polling(
    hooks: LifecycleHooks, clock: FakeClock, sleeps: list[float], *, wait_s: float, poll_s: float
) -> LifecycleHooks:
    """The hooks with a clock that a sleep moves, and the runtime-dir bounds of the test."""

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    supervisor_hooks = SupervisorHooks(clock=clock, sleep=sleep)
    return replace(
        hooks,
        supervisor=supervisor_hooks,
        run_user_wait_s=wait_s,
        run_user_poll_s=poll_s,
    )


@pytest.mark.fr("FR-083")
def test_the_run_user_wait_costs_no_sleep_when_the_dir_is_owned_already(clock: FakeClock) -> None:
    sleeps: list[float] = []
    waited = lifecycle.wait_for_run_user(
        lambda: True, clock=clock, sleep=sleeps.append, wait_s=10.0, poll_s=0.2
    )
    assert waited is True
    assert sleeps == []


@pytest.mark.fr("FR-083")
def test_the_run_user_wait_returns_once_the_owner_is_the_realm_user(clock: FakeClock) -> None:
    answers = iter([False, False, False, True])
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    waited = lifecycle.wait_for_run_user(
        lambda: next(answers), clock=clock, sleep=sleep, wait_s=10.0, poll_s=0.2
    )
    assert waited is True
    assert sleeps == [0.2, 0.2, 0.2]


@pytest.mark.fr("FR-083")
def test_the_run_user_wait_gives_up_at_its_bound_and_says_so(clock: FakeClock) -> None:
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    waited = lifecycle.wait_for_run_user(
        lambda: False, clock=clock, sleep=sleep, wait_s=1.0, poll_s=0.25
    )
    assert waited is False
    assert sleeps == [0.25] * 4


@pytest.mark.fr("FR-083")
def test_run_user_owned_is_true_only_for_the_realm_uid_and_a_failed_exec_is_not_owned(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks, home: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True, services=True)
    cid = container_id(env, "lab")
    target = ExecTarget(realm="lab", services=True, mutating=False)

    fake.exec_results[RUN_USER_STAT] = ExecResult(0, f"{UID}\n", "")
    assert lifecycle.run_user_owned(fake, cid, UID, target=target) is True
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, "0\n", "")
    assert lifecycle.run_user_owned(fake, cid, UID, target=target) is False
    fake.exec_results[RUN_USER_STAT] = ExecResult(1, "", "stat: cannot statx")
    assert lifecycle.run_user_owned(fake, cid, UID, target=target) is False

    make_realm(env, fake, "pen", RealmState.STOPPED, running=False, services=True)
    pen = container_id(env, "pen")
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, f"{UID}\n", "")
    assert lifecycle.run_user_owned(fake, pen, UID, target=target) is False


@pytest.mark.fr("FR-081")
@pytest.mark.fr("FR-083")
def test_a_supervised_services_on_start_waits_for_the_owner_before_it_attaches(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    clock: FakeClock,
) -> None:
    """T7.28, T716-1: the drop-in gives the runtime dir to the realm user at boot. The start polls
    the owner until it is the realm user, and only then attaches and records `running`."""
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    sleeps: list[float] = []
    fake.exec_sequences[RUN_USER_STAT] = [
        ExecResult(0, "0\n", ""),
        ExecResult(0, "0\n", ""),
        ExecResult(0, f"{UID}\n", ""),
    ]
    hooks = _polling(hooks, clock, sleeps, wait_s=10.0, poll_s=0.2)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks, supervised=True)

    assert record.state is RealmState.RUNNING
    assert sleeps == [0.2, 0.2]
    assert fake.exec_sequences[RUN_USER_STAT] == []


@pytest.mark.fr("FR-081")
@pytest.mark.fr("FR-083")
def test_a_supervised_start_whose_dir_stays_root_owned_goes_on_at_its_bound(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    clock: FakeClock,
) -> None:
    """On a timeout the start records `running` anyway, and the attach runs as it did before the
    wait (T7.28). Nothing retries the attach here."""
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    sleeps: list[float] = []
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, "0\n", "")
    hooks = _polling(hooks, clock, sleeps, wait_s=1.0, poll_s=0.25)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks, supervised=True)

    assert record.state is RealmState.RUNNING
    assert sleeps == [0.25] * 4


@pytest.mark.fr("FR-083")
def test_a_run_user_timeout_is_a_pending_warning_on_the_start_and_the_start_goes_on(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    clock: FakeClock,
) -> None:
    """T7.28: the owner wait gives up at its bound. The start still records `running`, and the
    timeout is GINN-W-RUN-USER-PENDING on the start's warnings (FR-083)."""
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    sleeps: list[float] = []
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, "0\n", "")
    hooks = _polling(hooks, clock, sleeps, wait_s=1.0, poll_s=0.25)
    warnings: list[GinnWarning] = []

    record = start_realm(
        env, fake, RealmName("lab"), session=user, hooks=hooks, warnings=warnings, supervised=True
    )

    assert record.state is RealmState.RUNNING
    pending = [w for w in warnings if w.code == "GINN-W-RUN-USER-PENDING"]
    assert [w.message for w in pending] == [
        "the runtime directory of realm 'lab' was not yet owned by the realm user when its wait "
        "ended; the attach went ahead and succeeded"
    ]


@pytest.mark.fr("FR-083")
def test_a_run_user_timeout_then_a_failed_attach_carries_the_pending_cause_as_a_note(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    clock: FakeClock,
    home: Path,
) -> None:
    """The owner wait timed out and the attach then failed. The start raises the attach error, and
    its warnings are not printed, so the timeout is a note on that error (T7.28, FR-083)."""
    (home / "Downloads").mkdir()
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    sleeps: list[float] = []
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, "0\n", "")
    fake.unshare_results.append(ExecResult(1, "", "ginn-rebind: mount refused"))
    hooks = _polling(hooks, clock, sleeps, wait_s=1.0, poll_s=0.25)
    warnings: list[GinnWarning] = []

    with pytest.raises(GinnError) as caught:
        start_realm(
            env,
            fake,
            RealmName("lab"),
            session=user,
            hooks=hooks,
            warnings=warnings,
            supervised=True,
        )

    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    assert sleeps == [0.25] * 4
    assert [note for note in caught.value.__notes__ if "GINN-W-RUN-USER-PENDING" in note] == [
        "GINN-W-RUN-USER-PENDING: the runtime directory of realm 'lab' was not yet owned by the "
        "realm user when its wait ended"
    ]
    assert warnings == []


@pytest.mark.fr("FR-083")
def test_a_run_user_owner_that_comes_in_time_then_a_failed_attach_adds_no_note(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    clock: FakeClock,
    home: Path,
) -> None:
    """The owner is ready in time, so a failed attach carries no pending note."""
    (home / "Downloads").mkdir()
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    sleeps: list[float] = []
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, f"{UID}\n", "")
    fake.unshare_results.append(ExecResult(1, "", "ginn-rebind: mount refused"))
    hooks = _polling(hooks, clock, sleeps, wait_s=1.0, poll_s=0.25)

    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks, supervised=True)

    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    notes = getattr(caught.value, "__notes__", [])
    assert not any("GINN-W-RUN-USER-PENDING" in note for note in notes)


@pytest.mark.fr("FR-083")
def test_a_run_user_owner_read_in_time_gives_no_pending_warning(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    clock: FakeClock,
) -> None:
    """The owner is the realm user on the second read, so the wait returns in time and the start
    has no pending warning."""
    _supervised_realm(env, fake, "lab", state=RealmState.STOPPED, running=False)
    sleeps: list[float] = []
    fake.exec_sequences[RUN_USER_STAT] = [ExecResult(0, "0\n", ""), ExecResult(0, f"{UID}\n", "")]
    hooks = _polling(hooks, clock, sleeps, wait_s=10.0, poll_s=0.2)
    warnings: list[GinnWarning] = []

    record = start_realm(
        env, fake, RealmName("lab"), session=user, hooks=hooks, warnings=warnings, supervised=True
    )

    assert record.state is RealmState.RUNNING
    assert sleeps == [0.2]
    assert "GINN-W-RUN-USER-PENDING" not in [w.code for w in warnings]


@pytest.mark.fr("FR-081")
def test_a_services_off_start_does_not_wait_for_a_run_dir_owner(
    env: OpEnv,
    fake: LifecyclePodman,
    user: HostUser,
    hooks: LifecycleHooks,
    clock: FakeClock,
) -> None:
    """A services-off tmpfs is chowned to the realm user at create (`U`), so there is no wait."""
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False)
    sleeps: list[float] = []
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, "0\n", "")
    hooks = _polling(hooks, clock, sleeps, wait_s=1.0, poll_s=0.25)

    record = start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert record.state is RealmState.RUNNING
    assert sleeps == []
