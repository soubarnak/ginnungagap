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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.errors import GinnError, make
from ginn.ops import lifecycle
from ginn.ops._mutating import OpEnv
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
from ginn.podman.adapter import CreateSpec
from ginn.podman.models import ContainerInfo, ContainerState, ExecResult
from ginn.runtime import attach
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
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
    "distro",
    "tier",
    "services",
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
    """The probes answer only for paths under tmp_path that exist, as in test_ops_create."""
    return LifecycleHooks(
        exists=lambda p: p.startswith(str(tmp_path)) and os.path.exists(p),
        mountinfo=mountinfo,
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

    def inspect_fails_second_time(container_id: str) -> ContainerInfo:
        seen["calls"] += 1
        if seen["calls"] == 2:  # the first call is the check before the operation
            raise make("GINN-E-INTERNAL")
        return real_inspect(container_id)

    monkeypatch.setattr(fake, "inspect", inspect_fails_second_time)

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


@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-081")
def test_services_on_start_and_stop_are_refused_until_m7(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False, services=True)

    with pytest.raises(GinnError, match="M7") as started:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)
    assert started.value.code == "GINN-E-NOT-SUPPORTED-YET"
    with pytest.raises(GinnError, match="M7") as stopped:
        stop_realm(env, fake, RealmName("lab"), hooks=hooks)
    assert stopped.value.code == "GINN-E-NOT-SUPPORTED-YET"

    assert fake.calls == []
    assert list(env.audit.read()) == []
    assert env.journal.pending() == []


@pytest.mark.fr("FR-016")
def test_a_tier_other_than_basic_is_refused_until_m6(
    env: OpEnv, fake: LifecyclePodman, user: HostUser, hooks: LifecycleHooks
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False, tier=Tier.DEVELOPMENT)

    with pytest.raises(GinnError, match="M6") as caught:
        start_realm(env, fake, RealmName("lab"), session=user, hooks=hooks)

    assert caught.value.code == "GINN-E-NOT-SUPPORTED-YET"
    assert fake.calls == []


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
    assert data["exported_apps"] is None
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
