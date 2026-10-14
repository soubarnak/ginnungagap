# SPDX-License-Identifier: Apache-2.0
"""Fault injection on every rebuild step (NFR-031, DR-034, FR-021 AC-2, T8.3a).

Each journal step of a rebuild is a fault point, for each of three kinds of fault and for a stopped
and a running realm. The matrix is fixed, so the cases are the same on every run:

- error: the step raises a GinnError. The rollback runs in the process, and the record returns to
  stopped (the rebuild's own failure path, ops/rebuild.py).
- interrupt: the step raises KeyboardInterrupt. It is a cancel (AS-018): the same rollback, then
  GINN-E-INTERRUPTED.
- kill: the step raises SimulatedCrash, which stands for a SIGKILL. The journal entry is kept, its
  process is made dead (`kill_journal_owner`), and `recover()` settles it.

After each fault the same checks hold. The old root gN is still there, with its record fields
unchanged, and no gN+1 container is left. The realm starts again. A running realm that failed in
process is running again on gN, as before the rebuild. A crash leaves a record that recovery returns
to stopped, and that is then usable too. The helpers, the fixtures and the create setup are those of
test_ops_rebuild.py.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import RealmState, Tier
from ginn.errors import GinnError, make
from ginn.ops import rebuild as rebuild_ops
from ginn.ops._mutating import OpEnv
from ginn.ops.lifecycle import LifecycleHooks, start_realm
from ginn.ops.permissions import set_tier
from ginn.ops.recovery import OUTCOME_RECOVERED, recover
from ginn.ops.service import set_services_mode
from ginn.podman.models import ContainerState
from ginn.runtime.spec import LABEL_GENERATION
from ginn.services.runit_user import DOWN, service_dir
from ginn.store.records import RealmRecord, RealmStore
from tests.unit import test_ops_rebuild as base
from tests.unit.test_ops_create import ScriptedPodman
from tests.unit.test_ops_delete import kill_journal_owner
from tests.unit.test_ops_rebuild import (
    NAME,
    _lazy,
    _nested_hooks,
    _nested_lazy,
    _runit,
    _services_realm,
    _supervised_lifecycle,
    generation_of,
    lifecycle_hooks,
    make_realm,
    realm_home_tree,
    rebuild,
    root_fields,
    session,
    stored,
)

# The fixtures are those of test_ops_rebuild, bound here so that pytest finds them in this module.
env = base.env
fake = base.fake
home = base.home
host = base.host
mountinfo = base.mountinfo
paths = base.paths
sleeps = base.sleeps
host_identity_is_read_only_under_tmp = base.host_identity_is_read_only_under_tmp

STEPS: tuple[str, ...] = (
    "rebuilding",
    "space-checked",
    "pulled",
    "created",
    "started",
    "attached",
    "user",
    "integrated",
    "replayed",
    "stopped",
    "swapping",
)
"""Every journal step of a rebuild, in order, that `_mark` or `_Rebuild._step` records."""
KINDS: tuple[str, ...] = ("error", "interrupt", "kill")
PREVIOUS: tuple[str, ...] = ("stopped", "running")
SERVICES: tuple[bool, ...] = (False, True)
"""The services mode of the realm: a services-on rebuild starts and stops gN+1 through runit."""
CASES: tuple[tuple[str, str, str, bool], ...] = tuple(
    (step, kind, previous, services)
    for step in STEPS
    for kind in KINDS
    for previous in PREVIOUS
    for services in SERVICES
)
"""The fault matrix: 11 steps by 3 kinds by 2 previous states by 2 services modes, 132 cases."""


class SimulatedCrash(Exception):
    """Stands for a SIGKILL inside a rebuild step. It is not a GinnError, so the journal stays."""


def _inject(step: str, kind: str) -> Any:
    """A hook that faults at `step`, once, in the way `kind` names."""

    def hook(name: str) -> None:
        if name != step:
            return
        if kind == "error":
            raise make("GINN-E-INTERNAL")
        if kind == "interrupt":
            raise KeyboardInterrupt
        raise SimulatedCrash(name)

    return hook


def _expected(kind: str) -> type[BaseException]:
    return SimulatedCrash if kind == "kill" else GinnError


def _check_old_root(env: OpEnv, fake: ScriptedPodman, before: Any) -> None:
    """The old root and its record fields are as they were, and no new root is left behind."""
    assert root_fields(stored(env)) == root_fields(before)
    assert generation_of(fake, 2) == []
    assert fake.containers[before.container_id].labels[LABEL_GENERATION] == "1"


def _check_usable(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    mountinfo: Path,
    lifecycle: LifecycleHooks | None = None,
) -> None:
    """The realm starts on gN, so the failed rebuild left a realm the user can run.

    A services-on realm starts through its fake runit (`lifecycle`), never through the real sv.
    """
    if stored(env).state is not RealmState.RUNNING:
        hooks = lifecycle_hooks(tmp_path, mountinfo) if lifecycle is None else lifecycle
        started = start_realm(env, fake, NAME, session=session(home), hooks=hooks)
        assert started.state is RealmState.RUNNING
    record = stored(env)
    assert fake.containers[record.container_id].state is ContainerState.RUNNING
    assert record.generation == 1


@pytest.mark.fr("NFR-031")
def test_the_fault_matrix_has_at_least_fifty_cases() -> None:
    assert len(CASES) == len(STEPS) * len(KINDS) * len(PREVIOUS) * len(SERVICES) == 132
    assert len(set(CASES)) == len(CASES)


@pytest.mark.fr("NFR-031")
@pytest.mark.parametrize(
    ("step", "kind", "previous", "services"),
    CASES,
    ids=[f"{s}-{k}-{p}-{'on' if v else 'off'}" for s, k, p, v in CASES],
)
def test_a_fault_at_every_rebuild_step_leaves_the_old_root_usable(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    step: str,
    kind: str,
    previous: str,
    services: bool,
) -> None:
    lifecycle: LifecycleHooks | None = None
    if services:
        runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
        lifecycle = _supervised_lifecycle(tmp_path, mountinfo, runit)
        before = _services_realm(
            env, fake, home, tmp_path, sleeps, mountinfo, runit, running=previous == "running"
        )
    else:
        before = make_realm(
            env, fake, home, tmp_path, sleeps, mountinfo, running=previous == "running"
        )
    marker = env.paths.homes / "debian" / "marker.txt"
    marker.write_text("kept\n", encoding="utf-8")
    extra: dict[str, Any] = {} if lifecycle is None else {"lifecycle": lifecycle}
    with pytest.raises(_expected(kind)):
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo, on_step=_inject(step, kind), **extra)
    # A services-on realm that was never started has its container created, not exited (ADR-003).
    stopped = (
        ContainerState.CREATED if services and previous == "stopped" else ContainerState.EXITED
    )
    if kind == "kill":
        assert env.journal.pending(), "a killed rebuild keeps its journal entry"
        kill_journal_owner(env, NAME.value)
        outcomes = recover(env, fake)
        assert [o.outcome for o in outcomes] == [OUTCOME_RECOVERED]
        assert env.journal.pending() == []
        _check_old_root(env, fake, before)
        # The rebuild stopped gN before its first step, so the recovered record reads stopped.
        assert stored(env).state is RealmState.STOPPED
        assert fake.containers[before.container_id].state is stopped
    else:
        assert env.journal.pending() == []
        assert [e.outcome for e in env.audit.read() if e.op == "rebuild"] == ["error"]
        _check_old_root(env, fake, before)
        expected = RealmState.RUNNING if previous == "running" else RealmState.STOPPED
        assert stored(env).state is expected
        assert fake.containers[before.container_id].state is (
            ContainerState.RUNNING if previous == "running" else stopped
        )
    assert marker.read_text(encoding="utf-8") == "kept\n", "the Realm Home is not touched"
    _check_usable(env, fake, home, tmp_path, mountinfo, lifecycle)


@pytest.mark.fr("DR-034")
def test_a_crash_after_the_swap_is_finished_by_recovery_and_the_old_root_is_removed(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)

    def crash(*_args: Any, **_kwargs: Any) -> int:
        raise SimulatedCrash("retired")

    monkeypatch.setattr(rebuild_ops, "remove_below", crash)
    with pytest.raises(SimulatedCrash):
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    swapped = stored(env)
    assert swapped.generation == 2
    assert fake.containers[before.container_id].labels[LABEL_GENERATION] == "1"
    kill_journal_owner(env, NAME.value)
    outcomes = recover(env, fake)
    assert [o.outcome for o in outcomes] == [OUTCOME_RECOVERED]
    assert "interrupted rebuild" in outcomes[0].reason
    assert env.journal.pending() == []
    assert generation_of(fake, 1) == []
    assert stored(env).generation == 2
    assert stored(env).state is RealmState.STOPPED


@pytest.mark.fr("DR-034")
def test_a_crash_before_the_swap_removes_the_orphan_new_root(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    with pytest.raises(SimulatedCrash):
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo, on_step=_inject("stopped", "kill"))
    assert generation_of(fake, 2), "the new root exists when the crash comes"
    kill_journal_owner(env, NAME.value)
    recover(env, fake)
    assert generation_of(fake, 2) == []
    assert root_fields(stored(env)) == root_fields(before)
    assert stored(env).state is RealmState.STOPPED


# The services-mode changes, the record saves and the lazy tier rebuild (T8.3 review, FR-083).
#
# The matrix above keeps the services mode, so these cases cover what it does not: a rebuild that
# turns services on (the runit directory is written and removed), one that turns them off (the
# directory is removed after the swap), the two record saves of a rebuild, and a tier change that a
# start rebuilds lazily. The state checked is the full one: the root fields, the grants, the runit
# directory and its `down` file, and every entry of the Realm Home.

TRANSITIONS: tuple[str, ...] = ("to-on", "to-off")
"""A rebuild that turns services on, and one that turns them off. The keep cases are CASES."""
TRANSITION_STEPS: dict[str, tuple[str, ...]] = {
    "to-on": (
        "rebuilding",
        "space-checked",
        "pulled",
        "created",
        "service",
        "started",
        "attached",
        "user",
        "integrated",
        "replayed",
        "stopped",
        "swapping",
    ),
    "to-off": STEPS,
}
"""The journal steps of each transition. `service` is written only when services turn on."""
TRANSITION_CASES: tuple[tuple[str, str, str, str], ...] = tuple(
    (transition, step, kind, previous)
    for transition in TRANSITIONS
    for step in TRANSITION_STEPS[transition]
    for kind in KINDS
    for previous in PREVIOUS
)
SAVE_POINTS: tuple[str, ...] = ("building", "swap")
"""The two record saves of a rebuild: the one that records the building container, and the swap."""
SAVE_CASES: tuple[tuple[str, str, str, str], ...] = tuple(
    (transition, point, kind, previous)
    for transition in TRANSITIONS
    for point in SAVE_POINTS
    for kind in KINDS
    for previous in PREVIOUS
)
LAZY_CASES: tuple[tuple[str, str], ...] = tuple((step, kind) for step in STEPS for kind in KINDS)
"""Every journal step of a lazy tier rebuild at a start, with each kind of fault."""


def _runit_state(env: OpEnv) -> tuple[bool, bool]:
    """Whether the realm's runit directory exists, and whether its `down` file does."""
    directory = service_dir(env.paths, NAME)
    if not directory.is_dir():
        return (False, False)
    return (True, (directory / DOWN).exists())


def _home_tree(env: OpEnv) -> dict[str, bytes | None]:
    return realm_home_tree(env.paths.homes / NAME.value)


def _transition_realm(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    transition: str,
    *,
    running: bool,
) -> tuple[RealmRecord, LifecycleHooks]:
    """A realm whose services mode `transition` changes, stopped or running before the rebuild.

    The record names the new mode and its container still has the old one, so the rebuild is the
    change. The lifecycle seams are the fake runit's, because either end of the change may be
    services-on.
    """
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    if transition == "to-on":
        make_realm(env, fake, home, tmp_path, sleeps, mountinfo, running=running)
    else:
        _services_realm(env, fake, home, tmp_path, sleeps, mountinfo, runit, running=running)
    set_services_mode(env, NAME, on=transition == "to-on")
    return stored(env), _supervised_lifecycle(tmp_path, mountinfo, runit)


def _is_save_point(record: RealmRecord, point: str) -> bool:
    if point == "building":
        return record.state is RealmState.REBUILDING and record.building_container_id is not None
    return (
        record.state is RealmState.STOPPED
        and record.generation == 2
        and (record.building_container_id is None)
    )


def _fault_save(monkeypatch: pytest.MonkeyPatch, point: str, kind: str) -> None:
    """Fault the first record save of `point`, the way `kind` names. Later saves are real."""
    real_save = RealmStore.save
    fired: list[str] = []

    def save(self: RealmStore, record: RealmRecord) -> None:
        if not fired and _is_save_point(record, point):
            fired.append(point)
            if kind == "error":
                raise make("GINN-E-INTERNAL")
            if kind == "interrupt":
                raise KeyboardInterrupt
            raise SimulatedCrash(point)
        real_save(self, record)

    monkeypatch.setattr(RealmStore, "save", save)


def _check_settled(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    mountinfo: Path,
    lifecycle: LifecycleHooks,
    *,
    before: RealmRecord,
    kind: str,
    previous: str,
    runit_before: tuple[bool, bool],
    home_before: dict[str, bytes | None],
) -> None:
    """After a fault in a services-mode change: recovery settles a kill, the state is the old one,
    and the old root starts and runs (NFR-031). A failed rebuild gives back the same root fields,
    the same grants, the same runit directory and the same Realm Home."""
    if kind == "kill":
        assert env.journal.pending(), "a killed rebuild keeps its journal entry"
        kill_journal_owner(env, NAME.value)
        outcomes = recover(env, fake)
        assert [o.outcome for o in outcomes] == [OUTCOME_RECOVERED]
        assert env.journal.pending() == []
        assert stored(env).state is RealmState.STOPPED
        # Recovery does not restart the old root, so only the directory itself is compared.
        assert _runit_state(env)[0] is runit_before[0], "the runit directory is the old one's"
        assert not any(c.labels.get(LABEL_GENERATION) == "2" for c in fake.containers.values())
    else:
        assert env.journal.pending() == []
        assert [e.outcome for e in env.audit.read() if e.op == "rebuild"] == ["error"]
        record = stored(env)
        assert root_fields(record) == root_fields(before)
        assert record.building_container_id is None and record.building_generation is None
        assert generation_of(fake, 2) == []
        assert _runit_state(env) == runit_before
        assert _home_tree(env) == home_before
        running = previous == "running"
        assert record.state is (RealmState.RUNNING if running else RealmState.STOPPED)
    assert _home_tree(env) == home_before
    if stored(env).state is not RealmState.RUNNING:
        started = start_realm(
            env,
            fake,
            NAME,
            session=session(home),
            hooks=lifecycle,
            rebuild=False,
        )
        assert started.state is RealmState.RUNNING
    record = stored(env)
    assert record.generation == 1, "the old root is the one that runs"
    assert fake.containers[record.container_id].state is ContainerState.RUNNING


@pytest.mark.fr("NFR-031")
@pytest.mark.fr("FR-083")
@pytest.mark.parametrize(
    ("transition", "step", "kind", "previous"),
    TRANSITION_CASES,
    ids=[f"{t}-{s}-{k}-{p}" for t, s, k, p in TRANSITION_CASES],
)
def test_a_fault_at_every_step_of_a_services_mode_change_leaves_the_old_root_usable(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    transition: str,
    step: str,
    kind: str,
    previous: str,
) -> None:
    before, lifecycle = _transition_realm(
        env, fake, home, tmp_path, sleeps, mountinfo, transition, running=previous == "running"
    )
    runit_before = _runit_state(env)
    home_before = _home_tree(env)
    with pytest.raises(_expected(kind)):
        rebuild(
            env,
            fake,
            home,
            tmp_path,
            sleeps,
            mountinfo,
            on_step=_inject(step, kind),
            lifecycle=lifecycle,
        )
    _check_settled(
        env,
        fake,
        home,
        tmp_path,
        mountinfo,
        lifecycle,
        before=before,
        kind=kind,
        previous=previous,
        runit_before=runit_before,
        home_before=home_before,
    )


@pytest.mark.fr("NFR-031")
@pytest.mark.fr("FR-083")
@pytest.mark.parametrize(
    ("transition", "point", "kind", "previous"),
    SAVE_CASES,
    ids=[f"{t}-save-{p}-{k}-{q}" for t, p, k, q in SAVE_CASES],
)
def test_a_fault_in_a_record_save_of_a_services_mode_change_leaves_the_old_root_usable(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    point: str,
    kind: str,
    previous: str,
) -> None:
    """The save that records the building container, and the swap, each fail once. The record
    keeps gN, or returns to stopped, and the old root runs (DR-034)."""
    before, lifecycle = _transition_realm(
        env, fake, home, tmp_path, sleeps, mountinfo, transition, running=previous == "running"
    )
    runit_before = _runit_state(env)
    home_before = _home_tree(env)
    _fault_save(monkeypatch, point, kind)
    with pytest.raises(_expected(kind)):
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo, lifecycle=lifecycle)
    _check_settled(
        env,
        fake,
        home,
        tmp_path,
        mountinfo,
        lifecycle,
        before=before,
        kind=kind,
        previous=previous,
        runit_before=runit_before,
        home_before=home_before,
    )


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-083")
def test_a_killed_rebuild_that_turned_services_on_removes_the_runit_directory_on_recovery(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The rebuild wrote the runit directory of a services-on gN+1 that gN does not have. A crash
    after the write, before the swap, leaves it, so recovery removes it (ADR-003)."""
    _, lifecycle = _transition_realm(
        env, fake, home, tmp_path, sleeps, mountinfo, "to-on", running=False
    )
    assert not _runit_state(env)[0]
    with pytest.raises(SimulatedCrash):
        rebuild(
            env,
            fake,
            home,
            tmp_path,
            sleeps,
            mountinfo,
            on_step=_inject("service", "kill"),
            lifecycle=lifecycle,
        )
    assert _runit_state(env)[0], "the crash comes after the directory is written"
    kill_journal_owner(env, NAME.value)
    recover(env, fake)
    assert not _runit_state(env)[0]
    assert stored(env).services_at_create is False


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-083")
def test_a_crash_after_the_swap_that_turned_services_off_removes_the_runit_directory(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The swap made the realm services-off, and the crash came before the directory of gN was
    removed (`_drop_service`). Recovery removes it, since the container of the record has no
    supervisor (ADR-003)."""
    _, lifecycle = _transition_realm(
        env, fake, home, tmp_path, sleeps, mountinfo, "to-off", running=False
    )
    assert _runit_state(env)[0]

    def crash(*_args: Any, **_kwargs: Any) -> int:
        raise SimulatedCrash("retired")

    monkeypatch.setattr(rebuild_ops, "remove_below", crash)
    with pytest.raises(SimulatedCrash):
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo, lifecycle=lifecycle)
    assert stored(env).services_at_create is False
    assert _runit_state(env)[0]
    kill_journal_owner(env, NAME.value)
    recover(env, fake)
    assert not _runit_state(env)[0]
    assert stored(env).state is RealmState.STOPPED


@pytest.mark.fr("FR-066")
@pytest.mark.fr("NFR-031")
@pytest.mark.parametrize(
    ("step", "kind"),
    LAZY_CASES,
    ids=[f"{s}-{k}" for s, k in LAZY_CASES],
)
def test_a_fault_in_a_lazy_tier_rebuild_at_start_keeps_the_old_root_and_the_change_pending(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    step: str,
    kind: str,
) -> None:
    """A start that rebuilds a stopped realm for a tier change (FR-066). An error at any step
    starts the old root with GINN-W-REBUILD-DEFERRED and the change pending. A cancel stops the
    start. A kill is recovered, and the next start rebuilds (FR-021 AC-2, AS-018)."""
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    set_tier(env, NAME, Tier.DEVELOPMENT)
    home_before = _home_tree(env)
    faulted = replace(_nested_hooks(tmp_path, sleeps, mountinfo), on_step=_inject(step, kind))
    lifecycle = _lazy(tmp_path, mountinfo, faulted)
    if kind == "error":
        notices: list[Any] = []
        started = start_realm(
            env, fake, NAME, session=session(home), hooks=lifecycle, warnings=notices
        )
        assert [w.code for w in notices] == ["GINN-W-REBUILD-DEFERRED"]
        assert started.state is RealmState.RUNNING
        assert started.generation == 1
        assert started.restart_required is True
        assert fake.containers[before.container_id].state is ContainerState.RUNNING
    elif kind == "interrupt":
        with pytest.raises(GinnError) as caught:
            start_realm(env, fake, NAME, session=session(home), hooks=lifecycle)
        assert caught.value.code == "GINN-E-INTERRUPTED"
        assert stored(env).state is RealmState.STOPPED
        assert fake.containers[before.container_id].state is not ContainerState.RUNNING
    else:
        with pytest.raises(SimulatedCrash):
            start_realm(env, fake, NAME, session=session(home), hooks=lifecycle)
        kill_journal_owner(env, NAME.value)
        outcomes = recover(env, fake)
        assert [o.outcome for o in outcomes] == [OUTCOME_RECOVERED]
        assert stored(env).state is RealmState.STOPPED
        restarted = start_realm(
            env, fake, NAME, session=session(home), hooks=_nested_lazy(tmp_path, sleeps, mountinfo)
        )
        assert restarted.generation == 2
        assert restarted.state is RealmState.RUNNING
    assert env.journal.pending() == []
    if kind != "kill":
        assert generation_of(fake, 2) == []
        assert _home_tree(env) == home_before, "the Realm Home is as the failed rebuild left it"
