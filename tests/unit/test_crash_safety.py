# SPDX-License-Identifier: Apache-2.0
"""SIGKILL at every durable effect of each mutating operation type (NFR-030, FR-122, FR-123).

NFR-030: after a SIGKILL at any point of any operation, no Realm Home loses data, and every realm
is in a DR-020 state within 5 s of the next ginn command.

Kill points. Each operation type runs each of its starting states (VARIANTS) from a fresh world. A
dry run lists the points of that run in order (crash_support.py): the journal begin, step and
finish, the audit append, the record writes, and each podman call that changes state, before and
after each one. Each point is then one kill run from a fresh world. Nothing is sampled, so the
points are the same on every run. The NFR asks for random points, and this covers every point of
each starting state, which includes any random sample of them. Each operation type must reach
MIN_POINTS, and the suite asserts the count.

Resume. `create --resume` (ops/resume.py) has its own sweep. Each starting state is a create killed
after one of its steps, then settled by recovery to broken, so the resume starts from a realm with
a stale container, or a runit directory, that it must clear. Its records are checked
like a create's.

Oracle. After each kill, recovery (ops/recovery.py) runs, and these must hold:
- The journal is empty, and every outcome is a finished one. A blocked, busy or corrupt outcome
  fails.
- The record reads a state that the operation's rule allows, from the state it had at the kill. A
  create killed while creating ends broken (FR-122). A create killed after its final stopped save
  stays stopped, as ops/recovery.py documents.
- A stopped or running record agrees with podman, at most one container runs, and every container
  of the realm is of the record's generation. No orphan generation is left (DR-034).
- The Realm Home marker is unchanged (NFR-030). Delete keeps the Realm Home, since it does not
  purge.
- A services-on realm is never started by podman (ADR-003), and no runit directory is left without
  a record.
- The realm is usable afterwards. A broken realm with a pending create is first finished by a second
  `create --resume`, which must reach stopped (FR-122). A stopped or running realm that the services
  mode does not run under runit starts and stops again. Then the realm is deleted, which leaves no
  record, no container, no runit directory and no journal entry. A services-on realm is started
  only by its runit entry (ADR-003), so its follow-up is the delete alone.

The rebuild points are split. The `after journal step` points of the steps in REBUILD_STEPS are
killed by test_ops_rebuild_faults, which runs those 44 kill-kind cases, so this file counts them
(REBUILD_FAULT_POINTS) and sweeps the others.

Fixed finding. A create or an update killed after its container starts, or after it marks the
record updating, used to leave a broken record whose container still ran. DR-020 has no move from
broken to running. ops/recovery.py now stops the container first (`_broken_verdict`), as it does for
an interrupted start, so the sweep checks those points with no exemption. Four in-process cases and
two real SIGKILL cases pin the fix at exact points.

Real SIGKILL. One forked child per operation type runs the middle point of its first starting
state. At that point the child saves its podman state, which is what a real podman keeps, and then
sends itself SIGKILL. The parent reads the saved state back and runs recovery on it.

Harness choices, all of which the crash model allows. fsync is skipped, since a SIGKILL keeps the
data each write(2) put in the page cache, and the sweep makes about a hundred writes per point.
The NVIDIA probe of the default security hooks, reached by a tier or permission change, reads no
host path. An in-process kill gives its journal entry a pid that no process has, so it reads as
dead.
"""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import pytest

from ginn.distros import Verb
from ginn.distros import load as load_distro
from ginn.domain import InstallMethod, InstallSource, ManifestEntry, RealmName, RealmState, Tier
from ginn.ops._mutating import OpEnv
from ginn.ops.create import create_realm
from ginn.ops.delete import delete_realm
from ginn.ops.lifecycle import HostUser, LifecycleHooks, start_realm, stop_realm
from ginn.ops.packages import PackageHooks, install_packages, remove_packages
from ginn.ops.permissions import set_permissions, set_tier
from ginn.ops.provision import (
    STEP_IMAGE_COMMITTED,
    STEP_INIT_INSTALLED,
    STEP_IPS_INSTALLED,
    STEP_PROVISION_CREATED,
    STEP_PROVISION_REMOVED,
    STEP_RESET_CREATED,
    STEP_RESET_REMOVED,
    STEP_STAGE_COMMITTED,
    STEP_STAGE_REMOVED,
)
from ginn.ops.recovery import OUTCOME_BROKEN, OUTCOME_FINISHED, OUTCOME_RECOVERED, recover
from ginn.ops.resume import resume_create
from ginn.ops.service import set_services_mode
from ginn.ops.update import update_realm
from ginn.podman.adapter import CreateSpec
from ginn.podman.models import ContainerState, ExecResult
from ginn.runtime.provision import (
    LABEL_BASE_DIGEST,
    LABEL_PROVISION,
    derived_image_name,
    derived_image_prefix,
    provision_name,
    provision_name_prefix,
    reset_name,
    stage_image_name,
)
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.security import nvidia
from ginn.security.caps import NESTED_DEVICES
from ginn.services.runit_user import service_dir, write_service_dir
from ginn.store.records import RealmRecord, RealmStore
from tests.fakes.clock import FakeClock
from tests.unit.crash_support import (
    LabelProbe,
    Probe,
    SimulatedCrash,
    load_podman_state,
    probing,
)
from tests.unit.realm_cli_support import DIGEST, World, build_world, record_of
from tests.unit.test_ops_create import GINN_PROGRAM, SECCOMP_SOURCE, ScriptedPodman, hooks_for
from tests.unit.test_ops_create import request as create_request
from tests.unit.test_ops_rebuild import NAME as REBUILD_NAME
from tests.unit.test_ops_rebuild import lifecycle_hooks, rebuild_hooks
from tests.unit.test_ops_rebuild import make_realm as rebuild_make_realm
from tests.unit.test_ops_rebuild import rebuild as rebuild_run
from tests.unit.test_ops_rebuild_faults import CASES as REBUILD_CASES
from tests.unit.test_ops_rebuild_faults import STEPS as REBUILD_STEPS

MIN_POINTS: Final = 50
"""Each operation type must reach this many kill points (NFR-030)."""
OPS: Final = (
    "create",
    "resume",
    "delete",
    "start",
    "stop",
    "install",
    "remove",
    "update",
    "tier",
    "perms",
    "services",
    "rebuild",
)
"""The operation types under test. tier and perms are separate journal ops (ops/permissions.py)."""
REBUILD_FAULT_POINTS: Final = sum(1 for case in REBUILD_CASES if case[1] == "kill")
"""The rebuild kill points that test_ops_rebuild_faults runs: one per journal step and case."""
USER: Final = "alice"
NAME: Final = RealmName("lab")
CREATE_NAME: Final = RealmName("debian")
MARKER: Final = "marker.txt"
MARKER_BYTES: Final = b"kept\n"
HTOP: Final = ManifestEntry(name="htop", source=InstallSource.REPO, method=InstallMethod.GINN)
_NOT_KILLED: Final = 70
"""The exit status of a forked child that was not killed, so the parent reports it."""
_NO_PROCESS: Final = 2**31 - 2
"""A pid that no process has, so an in-process kill's journal entry reads as dead (is_pid_alive)."""
_CLEAN_OUTCOMES: Final = frozenset({OUTCOME_RECOVERED, OUTCOME_BROKEN, OUTCOME_FINISHED})


@dataclass
class Bed:
    """One fresh world for one run: the stores, the fake podman and what the operation needs."""

    env: OpEnv
    fake: Any
    name: RealmName
    home: Path
    tmp: Path
    marker: Path | None
    session: HostUser
    hooks: LifecycleHooks
    run: Callable[[], object] | None = None
    done: Callable[[], list[str]] | None = None
    resume: Callable[[], object] | None = None
    """The `create --resume` of this realm's pending create, with the same request (FR-122)."""
    services: bool = False
    """True when the realm is services-on, so ADR-003 allows no podman start of it."""


@dataclass(frozen=True)
class Variant:
    """One starting state of one operation type. `build` makes a fresh Bed for each run."""

    op: str
    label: str
    build: Callable[[Path, pytest.MonkeyPatch], Bed]


class _NoHostGlob:
    """Stands in for the glob module of ginn.security.nvidia: the test host has no NVIDIA driver."""

    @staticmethod
    def glob(_pattern: str) -> list[str]:
        return []


def _world(tmp: Path, mp: pytest.MonkeyPatch) -> World:
    """A pinned world for one run, with two host reads that a SIGKILL sweep does not need.

    fsync is skipped. A SIGKILL leaves the data each write(2) put in the page cache, so the crash
    model needs no fsync, and the sweep makes about a hundred writes per point. The NVIDIA probe
    of the default security hooks, which a tier or permission change reaches, reads no host path.
    """
    mp.setattr(os, "fsync", lambda _fd: None)
    mp.setattr(nvidia, "glob", _NoHostGlob)
    return build_world(tmp, mp)


def _orphan(bed: Bed) -> None:
    """Make the journal entry of the realm look dead, as a SIGKILLed process leaves it."""
    path = bed.env.paths.journal / f"{bed.name.value}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pid"] = _NO_PROCESS
    data["pid_start"] = 0
    path.write_text(json.dumps(data), encoding="utf-8")


def _env(world: World) -> OpEnv:
    return OpEnv.for_paths(world.paths, USER, root_dir=world.host, clock=FakeClock())


def _lifecycle(world: World) -> LifecycleHooks:
    """The start seams of a bed. A pending change of a development realm is applied by a lazy
    rebuild at the next start, and that rebuild needs the nested devices and the containers seccomp
    profile of a nested realm (ADR-006). Both are answered here, not read from the host. A host
    without /dev/fuse or the profile (the CI container has neither) would defer the rebuild, and the
    old services-on root would then wait for a runsvdir.
    """
    mountinfo = world.tmp / "mountinfo"

    def exists(path: str) -> bool:
        return str(path).startswith(str(world.tmp)) or path in NESTED_DEVICES

    source = world.tmp / "containers-seccomp.json"
    source.write_text(json.dumps(SECCOMP_SOURCE), encoding="utf-8")
    hooks = lifecycle_hooks(world.tmp, mountinfo)
    rebuild = rebuild_hooks(world.tmp, [], mountinfo, exists=exists, seccomp_source=source)
    return replace(hooks, rebuild=rebuild)


def _bed(world: World, name: RealmName, fake: Any) -> Bed:
    return Bed(
        env=_env(world),
        fake=fake,
        name=name,
        home=world.home,
        tmp=world.tmp,
        marker=world.paths.homes / name.value / MARKER,
        session=HostUser(uid=os.getuid(), host_home=world.home, host_env={}),
        hooks=_lifecycle(world),
    )


def _seed(
    world: World,
    *,
    state: RealmState,
    running: bool,
    tier: Tier,
    services: bool,
    manifest: tuple[ManifestEntry, ...],
    restart: bool,
) -> None:
    """A stored realm with a fake container and its Realm Home, as make_realm does (T4.11)."""
    name = NAME.value
    labels = {LABEL_REALM: name, LABEL_DISTRO: "debian", LABEL_GENERATION: "1"}
    spec = CreateSpec(
        name=f"ginn-{name}-g1", image_digest_ref=DIGEST, network="host", labels=labels
    )
    cid = world.fake.create(spec)
    if running:
        world.fake.start(cid)
    home = world.paths.homes / name
    home.mkdir(parents=True, exist_ok=True)
    (home / MARKER).write_bytes(MARKER_BYTES)
    record = record_of(name, state, cid, tier=tier, services=services)
    if services:
        write_service_dir(world.paths, NAME, GINN_PROGRAM)
    RealmStore(world.paths).save(replace(record, manifest=list(manifest), restart_required=restart))
    world.fake.calls.clear()


def _realm(
    op_run: Callable[[Bed], object],
    *,
    state: RealmState,
    running: bool = False,
    tier: Tier = Tier.BASIC,
    services: bool = False,
    manifest: tuple[ManifestEntry, ...] = (),
    restart: bool = False,
    done: Callable[[Bed], list[str]],
    prepare: Callable[[World], None] | None = None,
) -> Callable[[Path, pytest.MonkeyPatch], Bed]:
    def build(tmp: Path, mp: pytest.MonkeyPatch) -> Bed:
        world = _world(tmp, mp)
        _seed(
            world,
            state=state,
            running=running,
            tier=tier,
            services=services,
            manifest=manifest,
            restart=restart,
        )
        if prepare is not None:
            prepare(world)
        bed = _bed(world, NAME, world.fake)
        bed.run = lambda: op_run(bed)
        bed.done = lambda: done(bed)
        bed.services = services
        return bed

    return build


def _remove_dry_run_lists_htop(world: World) -> None:
    """The apt simulate of a removal names htop, the shape ADR-011's pre-check reads."""
    argv = tuple(load_distro("debian").argv(Verb.REMOVE_CHECK, ["htop"]))
    world.fake.exec_results[argv] = ExecResult(0, "Remv htop [1.0]\n", "")


def _packages(bed: Bed) -> PackageHooks:
    return PackageHooks(lifecycle=bed.hooks, sleep=lambda _seconds: None)


def _store(bed: Bed) -> RealmStore:
    return RealmStore(bed.env.paths, bed.env.clock)


def _state_is(bed: Bed, *states: RealmState) -> list[str]:
    record = _store(bed).load(bed.name)
    if record.state in states:
        return []
    return [f"the record reads {record.state}, expected one of {sorted(states)}"]


def _gone(bed: Bed) -> list[str]:
    problems: list[str] = []
    if _store(bed).exists(bed.name):
        problems.append("the record is still there")
    if bed.fake.ps({LABEL_REALM: bed.name.value}):
        problems.append("a container of the realm is left")
    return problems


def _has_package(bed: Bed, present: bool) -> list[str]:
    manifest = _store(bed).load(bed.name).manifest
    if (HTOP in manifest) == present:
        return []
    return [f"htop is {'missing from' if present else 'still in'} the Install Manifest"]


def _tier_is(bed: Bed, tier: Tier) -> list[str]:
    record = _store(bed).load(bed.name)
    if record.settings.tier is tier:
        return []
    return [f"the tier reads {record.settings.tier}, expected {tier}"]


def _services_is(bed: Bed, on: bool) -> list[str]:
    record = _store(bed).load(bed.name)
    if record.settings.services is on:
        return []
    return [f"services reads {record.settings.services}, expected {on}"]


def _create(tmp: Path, mp: pytest.MonkeyPatch, **request_kwargs: Any) -> Bed:
    """A create of debian with the given request options, on the create test's scripted fake."""
    world = _world(tmp, mp)
    fake = ScriptedPodman(root=str(tmp))
    bed = _bed(world, CREATE_NAME, fake)
    bed.marker = None
    bed.services = bool(request_kwargs.get("services", False))
    hooks = hooks_for(tmp, [], world.tmp / "mountinfo", program=GINN_PROGRAM)
    request = create_request(world.home, **request_kwargs)
    bed.run = lambda: create_realm(bed.env, fake, request, hooks=hooks)
    bed.resume = lambda: resume_create(
        bed.env,
        fake,
        CREATE_NAME,
        uid=request.uid,
        gid=request.gid,
        host_home=request.host_home,
        host_env=request.host_env,
        hooks=hooks,
    )
    bed.done = lambda: _state_is(bed, RealmState.STOPPED)
    return bed


def _rebuild(tmp: Path, mp: pytest.MonkeyPatch, *, running: bool) -> Bed:
    """A debian realm with curl in its manifest, stopped or running, rebuilt by the rebuild test."""
    world = _world(tmp, mp)
    fake = ScriptedPodman(root=str(tmp))
    bed = _bed(world, REBUILD_NAME, fake)
    sleeps: list[float] = []
    mountinfo = world.tmp / "mountinfo"
    rebuild_make_realm(bed.env, fake, world.home, tmp, sleeps, mountinfo, running=running)
    assert bed.marker is not None
    bed.marker.write_bytes(MARKER_BYTES)
    bed.run = lambda: rebuild_run(bed.env, fake, world.home, tmp, sleeps, mountinfo)
    bed.done = lambda: _generation_is(bed, 2)
    return bed


def _resume(
    tmp: Path,
    mp: pytest.MonkeyPatch,
    *,
    killed_after: str,
    services: bool = False,
    with_packages: tuple[str, ...] = (),
) -> Bed:
    """A broken debian realm that `create --resume` finishes, killed after one create step.

    The create is killed in its own probe, and recovery settles it to broken, as a restart does. The
    Realm Home marker is written after that, and the resume must keep it.
    """
    world = _world(tmp, mp)
    fake = ScriptedPodman(root=str(tmp))
    bed = _bed(world, CREATE_NAME, fake)
    hooks = hooks_for(tmp, [], world.tmp / "mountinfo", program=GINN_PROGRAM)
    request = create_request(world.home, services=services, with_packages=with_packages)
    with probing(LabelProbe(f"after journal step {killed_after}"), fake):
        with pytest.raises(SimulatedCrash):
            create_realm(bed.env, fake, request, hooks=hooks)
    _orphan(bed)
    recover(bed.env, fake)
    assert _store(bed).load(bed.name).state is RealmState.BROKEN
    bed.marker = world.paths.homes / CREATE_NAME.value / MARKER
    bed.marker.write_bytes(MARKER_BYTES)
    fake.calls.clear()
    bed.run = lambda: resume_create(
        bed.env,
        fake,
        CREATE_NAME,
        uid=request.uid,
        gid=request.gid,
        host_home=request.host_home,
        host_env=request.host_env,
        hooks=hooks,
    )
    bed.resume = bed.run
    bed.services = services
    bed.done = lambda: _state_is(bed, RealmState.STOPPED)
    return bed


def _generation_is(bed: Bed, generation: int) -> list[str]:
    record = _store(bed).load(bed.name)
    if record.generation == generation:
        return []
    return [f"the generation reads {record.generation}, expected {generation}"]


BASE_SHA: Final = DIGEST.partition("@")[2]
"""The pinned base's manifest digest. A derived image of the fake labels it as its base."""
_FAULT_VARIANTS: Final = ("stopped", "running")
"""The rebuild variants whose REBUILD_STEPS points test_ops_rebuild_faults already runs."""


def _derived_image(fake: Any, name: str) -> str:
    """A derived image named `name`, committed from a probe container of the pinned base, labelled
    as the stage labels it (ADR-015 Decision 13). The probe is removed, so the image has no user."""
    probe = fake.create(
        CreateSpec(name=f"ginn-probe-{len(fake.images)}", image_digest_ref=DIGEST, network="host")
    )
    image = fake.commit(probe, name, changes=[f"LABEL {LABEL_BASE_DIGEST}={BASE_SHA}"])
    fake.rm(probe, force=True)
    return image


def _stage_leftovers(fake: Any, name: RealmName, generation: int) -> None:
    """The leftovers of an interrupted stage of `generation`, as ADR-015 Decisions 2f and 2h leave
    them: the provisioning container, the stage image committed from it, the reset container created
    from the stage, and the derived image committed from the reset."""
    labels = {LABEL_PROVISION: f"{name.value}:{generation}"}
    provision = fake.create(
        CreateSpec(
            name=provision_name(name, generation),
            image_digest_ref=DIGEST,
            network="host",
            labels=labels,
        )
    )
    stage = fake.commit(
        provision,
        stage_image_name(name, generation),
        changes=[f"LABEL {LABEL_BASE_DIGEST}={BASE_SHA}"],
    )
    reset = fake.create(
        CreateSpec(
            name=reset_name(name, generation),
            image_digest_ref=DIGEST,
            image_id=stage,
            network="host",
            labels=labels,
        )
    )
    fake.commit(
        reset,
        derived_image_name(name, generation),
        changes=[f"LABEL {LABEL_BASE_DIGEST}={BASE_SHA}"],
    )


def _run_from_derived(fake: Any, store: RealmStore, name: RealmName, init: str) -> None:
    """Record `init` as the realm's derived image, and make its container from that image.

    A realm with an init image always runs from it (ADR-015 Decisions 7 and 9), so the container
    that `_seed` or the rebuild fixture made from the pinned base is made again from `init`, with
    the same name and labels, and the record names the new container.
    """
    record = store.load(name)
    assert record.container_id is not None
    seeded = fake.inspect(record.container_id)
    fake.rm(record.container_id, force=True)
    cid = fake.create(
        CreateSpec(
            name=seeded.name,
            image_digest_ref=DIGEST,
            image_id=init,
            network="host",
            labels=seeded.labels,
        )
    )
    store.save(replace(record, container_id=cid, init_image_id=init))


def _seed_init_leftovers(world: World, name: RealmName) -> None:
    """A stored stopped realm that records its init image, with the leftovers of an interrupted
    stage of its next generation in podman. The delete must sweep the leftovers and the image it
    does not keep (ADR-015 Decision 13)."""
    if DIGEST not in world.fake.images:
        world.fake.pull(DIGEST)  # the pinned base, which the delete removes last (FR-019 AC-1)
    init = _derived_image(world.fake, derived_image_name(name, 1))
    _stage_leftovers(world.fake, name, 2)
    _run_from_derived(world.fake, RealmStore(world.paths), name, init)
    world.fake.calls.clear()


def _leftovers(bed: Bed, record: RealmRecord | None, *, in_use_ok: bool) -> list[str]:
    """No provisioning container of the realm stays, and every derived image of the realm is the one
    that the record names: `init_image_id`, or none when there is no record (ADR-015 Decisions 10
    and 13, FR-122).

    `in_use_ok` allows a derived image that a container still uses. A broken create keeps the
    container it made from its derived image, and podman does not remove an image in use, so
    recovery leaves that image and its note says so. A finished resume or delete allows none.
    """
    fake, name = bed.fake, bed.name
    problems: list[str] = []
    prefix = provision_name_prefix(name)
    left = sorted(c.name for c in fake.ps() if c.name.startswith(prefix))
    if left:
        problems.append(f"a provisioning container is left: {left}")
    expected = None if record is None else record.init_image_id
    held = {c.image_id for c in fake.ps()} if in_use_ok else set()
    derived = {info.id for info in fake.images_by_name(derived_image_prefix(name)).values()}
    stray = sorted(image for image in derived if image != expected and image not in held)
    if stray:
        problems.append(
            f"a derived image of the realm is left that its record does not name: {stray}"
        )
    return problems


def _gone_swept(bed: Bed) -> list[str]:
    """`_gone`, and the delete has also swept the realm's provisioning leftovers and derived images
    and the pinned base, which no other realm uses (FR-019 AC-1, ADR-015 Decision 13)."""
    problems = [*_gone(bed), *_leftovers(bed, None, in_use_ok=False)]
    if DIGEST in bed.fake.images:
        problems.append("the pinned base is left after the delete")
    return problems


def _rebuild_provisioned(tmp: Path, mp: pytest.MonkeyPatch) -> Bed:
    """A stopped debian realm that records a derived image, so `ginn rebuild` is a `provision` build
    (ADR-015 Decision 7). The rebuild provisions a new image and retires the recorded one."""
    bed = _rebuild(tmp, mp, running=False)
    store = _store(bed)
    init = _derived_image(bed.fake, derived_image_name(REBUILD_NAME, 1))
    _run_from_derived(bed.fake, store, REBUILD_NAME, init)
    bed.fake.calls.clear()
    bed.done = lambda: [
        *_generation_is(bed, 2),
        *_leftovers(bed, store.load(REBUILD_NAME), in_use_ok=False),
    ]
    return bed


def _killed_services_create(tmp: Path, mp: pytest.MonkeyPatch) -> Bed:
    """A services-on create killed after its derived image is committed, with its journal entry dead
    and no recovery run yet. Recovery is the operation under test (FR-122)."""
    world = _world(tmp, mp)
    fake = ScriptedPodman(root=str(tmp))
    bed = _bed(world, CREATE_NAME, fake)
    bed.marker = None
    bed.services = True
    hooks = hooks_for(tmp, [], world.tmp / "mountinfo", program=GINN_PROGRAM)
    request = create_request(world.home, services=True)
    with probing(LabelProbe("after journal step image-committed"), fake):
        with pytest.raises(SimulatedCrash):
            create_realm(bed.env, fake, request, hooks=hooks)
    _orphan(bed)
    fake.calls.clear()
    bed.run = lambda: recover(bed.env, fake)
    bed.resume = lambda: resume_create(
        bed.env,
        fake,
        CREATE_NAME,
        uid=request.uid,
        gid=request.gid,
        host_home=request.host_home,
        host_env=request.host_env,
        hooks=hooks,
    )
    bed.done = lambda: [
        *_state_is(bed, RealmState.BROKEN),
        *_leftovers(bed, _store(bed).load(CREATE_NAME), in_use_ok=False),
    ]
    return bed


def _variants() -> dict[str, tuple[Variant, ...]]:
    """The starting states of each operation type. Each state is a separate sweep."""
    deletes = {"stopped": (RealmState.STOPPED, False), "running": (RealmState.RUNNING, True)}
    tiers = (
        (Tier.BASIC, Tier.DEVELOPMENT, RealmState.STOPPED, False),
        (Tier.DEVELOPMENT, Tier.CUSTOM, RealmState.STOPPED, False),
        (Tier.CUSTOM, Tier.BASIC, RealmState.STOPPED, False),
        (Tier.BASIC, Tier.CUSTOM, RealmState.RUNNING, True),
        (Tier.DEVELOPMENT, Tier.BASIC, RealmState.RUNNING, True),
        (Tier.CUSTOM, Tier.DEVELOPMENT, RealmState.RUNNING, True),
    )

    def tier_case(start: Tier, target: Tier, state: RealmState, running: bool) -> Variant:
        build = _realm(
            lambda bed: set_tier(bed.env, bed.name, target),
            state=state,
            running=running,
            tier=start,
            done=lambda bed: _tier_is(bed, target),
        )
        return Variant("tier", f"{start}-to-{target}-{state}", build)

    def perms_case(
        label: str,
        prepare: Callable[[World], None],
        change: Callable[[Path], dict[str, Any]],
    ) -> Variant:
        build = _realm(
            lambda bed: set_permissions(bed.env, bed.name, bed.session, **change(bed.home)),
            state=RealmState.STOPPED,
            tier=Tier.CUSTOM,
            prepare=prepare,
            done=lambda bed: _state_is(bed, RealmState.STOPPED),
        )
        return Variant("perms", label, build)

    def make_dirs(*names: str) -> Callable[[World], None]:
        def prepare(world: World) -> None:
            for name in names:
                (world.home / name).mkdir(exist_ok=True)

        return prepare

    def none(_world: World) -> None:
        return None

    def services_case(start_on: bool, tier: Tier, state: RealmState, running: bool) -> Variant:
        build = _realm(
            lambda bed: set_services_mode(bed.env, bed.name, on=not start_on),
            state=state,
            running=running,
            tier=tier,
            services=start_on,
            done=lambda bed: _services_is(bed, not start_on),
        )
        direction = "on-to-off" if start_on else "off-to-on"
        return Variant("services", f"{direction}-{tier}-{state}", build)

    services_cases = tuple(
        services_case(start_on, tier, state, running)
        for start_on, state, running in (
            (False, RealmState.STOPPED, False),
            (False, RealmState.RUNNING, True),
            (True, RealmState.STOPPED, False),
        )
        for tier in (Tier.BASIC, Tier.DEVELOPMENT, Tier.CUSTOM)
    )
    variants: dict[str, tuple[Variant, ...]] = {
        "create": (
            Variant("create", "basic", lambda tmp, mp: _create(tmp, mp)),
            Variant("create", "services-on", lambda tmp, mp: _create(tmp, mp, services=True)),
            Variant(
                "create", "with-package", lambda tmp, mp: _create(tmp, mp, with_packages=("curl",))
            ),
        ),
        "resume": (
            Variant(
                "resume",
                "basic-after-pulled",
                lambda tmp, mp: _resume(tmp, mp, killed_after="pulled"),
            ),
            Variant(
                "resume",
                "basic-after-created",
                lambda tmp, mp: _resume(tmp, mp, killed_after="created"),
            ),
            Variant(
                "resume",
                "basic-after-started",
                lambda tmp, mp: _resume(tmp, mp, killed_after="started"),
            ),
            Variant(
                "resume",
                "with-package-after-created",
                lambda tmp, mp: _resume(tmp, mp, killed_after="created", with_packages=("curl",)),
            ),
            Variant(
                "resume",
                "services-after-service",
                lambda tmp, mp: _resume(tmp, mp, killed_after="service", services=True),
            ),
        ),
        "delete": tuple(
            Variant(
                "delete",
                label,
                _realm(
                    lambda bed: delete_realm(bed.env, bed.fake, bed.name),
                    state=state,
                    running=running,
                    done=_gone,
                ),
            )
            for label, (state, running) in deletes.items()
        )
        + (
            Variant(
                "delete",
                "broken",
                _realm(
                    lambda bed: delete_realm(bed.env, bed.fake, bed.name),
                    state=RealmState.BROKEN,
                    done=_gone,
                ),
            ),
        ),
        "start": tuple(
            Variant(
                "start",
                label,
                _realm(
                    lambda bed: start_realm(
                        bed.env, bed.fake, bed.name, session=bed.session, hooks=bed.hooks
                    ),
                    state=RealmState.STOPPED,
                    tier=tier,
                    done=lambda bed: _state_is(bed, RealmState.RUNNING),
                ),
            )
            for label, tier in (("basic", Tier.BASIC), ("development", Tier.DEVELOPMENT))
        ),
        "stop": tuple(
            Variant(
                "stop",
                label,
                _realm(
                    lambda bed: stop_realm(bed.env, bed.fake, bed.name, hooks=bed.hooks),
                    state=RealmState.RUNNING,
                    running=True,
                    tier=tier,
                    restart=restart,
                    done=lambda bed: _state_is(bed, RealmState.STOPPED),
                ),
            )
            for label, tier, restart in (
                ("basic", Tier.BASIC, False),
                ("development", Tier.DEVELOPMENT, False),
                ("custom", Tier.CUSTOM, False),
                ("basic-restart", Tier.BASIC, True),
                ("custom-restart", Tier.CUSTOM, True),
            )
        ),
        "install": tuple(
            Variant(
                "install",
                label,
                _realm(
                    lambda bed: install_packages(
                        bed.env,
                        bed.fake,
                        bed.name,
                        ["htop"],
                        aur=False,
                        session=bed.session,
                        hooks=_packages(bed),
                    ),
                    state=state,
                    running=running,
                    done=lambda bed: _has_package(bed, True),
                ),
            )
            for label, state, running in (
                ("running", RealmState.RUNNING, True),
                ("stopped", RealmState.STOPPED, False),
            )
        ),
        "remove": tuple(
            Variant(
                "remove",
                label,
                _realm(
                    lambda bed: remove_packages(
                        bed.env,
                        bed.fake,
                        bed.name,
                        ["htop"],
                        session=bed.session,
                        hooks=_packages(bed),
                    ),
                    state=state,
                    running=running,
                    manifest=(HTOP,),
                    prepare=_remove_dry_run_lists_htop,
                    done=lambda bed: _has_package(bed, False),
                ),
            )
            for label, state, running in (
                ("running", RealmState.RUNNING, True),
                ("stopped", RealmState.STOPPED, False),
            )
        ),
        "update": tuple(
            Variant(
                "update",
                label,
                _realm(
                    lambda bed: update_realm(
                        bed.env, bed.fake, bed.name, session=bed.session, hooks=_packages(bed)
                    ),
                    state=RealmState.RUNNING,
                    running=True,
                    tier=tier,
                    done=lambda bed: _state_is(bed, RealmState.RUNNING, RealmState.STOPPED),
                ),
            )
            for label, tier in (("basic", Tier.BASIC), ("development", Tier.DEVELOPMENT))
        ),
        "tier": tuple(tier_case(*case) for case in tiers),
        "perms": (
            perms_case("enable", none, lambda _home: {"enable": ["projects"]}),
            perms_case("disable", none, lambda _home: {"disable": ["downloads"]}),
            perms_case(
                "enable-and-disable",
                none,
                lambda _home: {"enable": ["projects"], "disable": ["downloads"]},
            ),
            perms_case(
                "share-ro",
                make_dirs("Music"),
                lambda home: {"share": [(str(home / "Music"), "ro")]},
            ),
            perms_case(
                "share-rw",
                make_dirs("Pictures"),
                lambda home: {"share": [(str(home / "Pictures"), "rw")]},
            ),
        ),
        "services": services_cases,
        "rebuild": (
            Variant("rebuild", "stopped", lambda tmp, mp: _rebuild(tmp, mp, running=False)),
            Variant("rebuild", "running", lambda tmp, mp: _rebuild(tmp, mp, running=True)),
        ),
    }
    variants["stop"] += (
        Variant(
            "stop",
            "stopped-record-running-container",
            _realm(
                lambda bed: stop_realm(bed.env, bed.fake, bed.name, hooks=bed.hooks),
                state=RealmState.STOPPED,
                running=True,
                done=lambda bed: _state_is(bed, RealmState.STOPPED),
            ),
        ),
    )
    variants["start"] += (
        Variant(
            "start",
            "from-failed",
            _realm(
                lambda bed: start_realm(
                    bed.env, bed.fake, bed.name, session=bed.session, hooks=bed.hooks
                ),
                state=RealmState.FAILED,
                done=lambda bed: _state_is(bed, RealmState.RUNNING),
            ),
        ),
    )
    variants["delete"] += (
        Variant(
            "delete",
            "init-leftovers",
            _realm(
                lambda bed: delete_realm(bed.env, bed.fake, bed.name),
                state=RealmState.STOPPED,
                prepare=lambda world: _seed_init_leftovers(world, NAME),
                done=_gone_swept,
            ),
        ),
    )
    variants["rebuild"] += (
        Variant("rebuild", "provision", lambda tmp, mp: _rebuild_provisioned(tmp, mp)),
    )
    return variants


VARIANTS: Final = _variants()
"""Every starting state of every operation type, in the order the sweep runs them."""


def _allowed(op: str, killed: RealmState | None) -> frozenset[RealmState | None]:
    """The record states that recovery may leave, given the state at the kill (DR-020, FR-122)."""
    if op in ("create", "resume"):
        if killed is None:
            return frozenset({None})
        if killed is RealmState.STOPPED:
            return frozenset({RealmState.STOPPED})
        return frozenset({RealmState.BROKEN})
    if op == "delete":
        if killed is None:
            return frozenset({None})
        if killed is RealmState.DELETING:
            return frozenset({RealmState.DELETING, None})
        if killed is RealmState.BROKEN:
            return frozenset({RealmState.BROKEN, None})
        return frozenset({RealmState.STOPPED, RealmState.RUNNING, RealmState.DELETING, None})
    if op == "update":
        if killed in (RealmState.UPDATING, RealmState.BROKEN):
            return frozenset({RealmState.BROKEN})
        return frozenset({RealmState.STOPPED, RealmState.RUNNING})
    if op in ("tier", "perms", "services"):
        return frozenset({killed})
    # A kill before the journal begin leaves the record as it was, which may be failed (DR-020).
    return frozenset({RealmState.STOPPED, RealmState.RUNNING, killed})


def _agree(record: Any, containers: list[Any]) -> list[str]:
    """A stopped or running record names a container that podman lists, in the same state."""
    match = [c for c in containers if c.id == record.container_id]
    if not match:
        return [f"the record names container {record.container_id}, which podman does not list"]
    running = match[0].state is ContainerState.RUNNING
    if record.state is RealmState.RUNNING and not running:
        return [f"the record reads running, but its container is {match[0].state}"]
    if record.state is RealmState.STOPPED and running:
        return ["the record reads stopped, but its container runs"]
    return []


def _started_a_realm(fake: Any) -> bool:
    """True when podman started a services-on realm's own container (ADR-003).

    A services-on create also starts its provisioning container, which carries no realm label, and
    that start is allowed (ADR-015 Decision 2). A fake that records the realm starts at the call
    answers from them, since a removed container leaves no label to read afterwards. Any other fake
    answers with every start.
    """
    if hasattr(fake, "realm_starts"):
        return bool(fake.realm_starts)
    return "start" in fake.ops()


def _verify(
    op: str,
    bed: Bed,
    killed: RealmState | None,
    journal_existed: bool,
    outcomes: list[Any],
) -> list[str]:
    problems: list[str] = []
    env, fake, name = bed.env, bed.fake, bed.name
    if env.journal.pending():
        problems.append("a journal entry is still pending after recovery")
    if bed.services and _started_a_realm(fake):
        problems.append("a services-on realm was started by podman start (ADR-003)")
    if journal_existed and len(outcomes) != 1:
        problems.append(f"recovery reported {len(outcomes)} outcomes for one entry")
    for outcome in outcomes:
        if outcome.outcome not in _CLEAN_OUTCOMES:
            problems.append(f"recovery reported {outcome.outcome}: {outcome.reason}")
    store = _store(bed)
    record = store.load(name) if store.exists(name) else None
    state = None if record is None else record.state
    if state not in _allowed(op, killed):
        problems.append(f"after recovery the record reads {state}, from {killed} at the kill")
    if op != "delete" or record is None:
        # A killed delete keeps its record `deleting` and leaves the sweep to the next delete
        # (FR-122), so its leftovers are checked after that delete, in _follow_up.
        problems.extend(_leftovers(bed, record, in_use_ok=True))
    containers = fake.ps({LABEL_REALM: name.value})
    if record is None:
        if containers:
            problems.append("containers are left without a record")
        if op == "create" and (env.paths.homes / name.value).exists():
            problems.append("a Realm Home is left without a record")
        if service_dir(env.paths, name).exists():
            problems.append("a runit directory is left without a record")
        return problems
    if sum(1 for c in containers if c.state is ContainerState.RUNNING) > 1:
        problems.append("two containers of the realm run")
    for container in containers:
        if container.labels.get(LABEL_GENERATION) != str(record.generation):
            problems.append(f"container {container.id} is an orphan generation")
    if state in (RealmState.STOPPED, RealmState.RUNNING) and journal_existed:
        # Recovery settles a record to podman's state. With no journal entry, no recovery ran, so a
        # seeded state such as a stopped record with a running container stays as it was found.
        problems.extend(_agree(record, containers))
    if state is RealmState.BROKEN and any(c.state is ContainerState.RUNNING for c in containers):
        problems.append("a broken realm still runs a container (DR-020 allows no move to running)")
    if bed.marker is not None and (
        not bed.marker.exists() or bed.marker.read_bytes() != MARKER_BYTES
    ):
        problems.append("the Realm Home marker was changed or lost")
    return problems


def _runs(fake: Any, record: RealmRecord) -> bool:
    """True when podman lists the record's container as running."""
    return any(
        c.id == record.container_id and c.state is ContainerState.RUNNING
        for c in fake.ps({LABEL_REALM: record.settings.name.value})
    )


def _second_resume(bed: Bed) -> list[str]:
    """Run `create --resume` again on a broken realm with a pending create, with no kill (FR-122).

    The first resume was killed, or the create was. Whatever the kill left, the second resume must
    reach stopped, including after a kill inside the first resume's `_clear` (ops/resume.py).
    """
    if bed.resume is None:
        return ["a broken realm with a pending create has no resume to run"]
    bed.resume()
    state = _store(bed).load(bed.name).state
    if state is RealmState.STOPPED:
        return []
    return [f"a second resume left the realm {state}"]


def _follow_up(bed: Bed) -> list[str]:
    """The realm is usable afterwards: it starts and stops, and it can be deleted (FR-122).

    A broken realm with a pending create is finished first, by a second resume (`_second_resume`).
    """
    env, fake, name, store = bed.env, bed.fake, bed.name, _store(bed)
    problems: list[str] = []
    try:
        if store.exists(name) and store.load(name).create_pending:
            problems.extend(_second_resume(bed))
            resumed = store.load(name) if store.exists(name) else None
            problems.extend(_leftovers(bed, resumed, in_use_ok=False))
        if store.exists(name):
            record = store.load(name)
            state = record.state
            if state in (RealmState.STOPPED, RealmState.RUNNING) and not record.settings.services:
                if _runs(fake, record):
                    # A running container is stopped first, as stop's contract says for a stopped
                    # record whose container runs (ops/lifecycle.py stop_realm).
                    stop_realm(env, fake, name, hooks=bed.hooks)
                started = start_realm(env, fake, name, session=bed.session, hooks=bed.hooks)
                if started.state is not RealmState.RUNNING:
                    problems.append(f"start left the realm {started.state}")
                stopped = stop_realm(env, fake, name, hooks=bed.hooks)
                if stopped.state is not RealmState.STOPPED:
                    problems.append(f"stop left the realm {stopped.state}")
            delete_realm(env, fake, name)
        if store.exists(name):
            problems.append("the record is still there after delete")
        problems.extend(_leftovers(bed, None, in_use_ok=False))
        if DIGEST in fake.images:
            problems.append("the pinned base is left after delete")
        if fake.ps({LABEL_REALM: name.value}):
            problems.append("a container of the realm is left after delete")
        if service_dir(env.paths, name).exists():
            problems.append("a runit directory is left after delete")
        if env.journal.pending():
            problems.append("a journal entry is left after delete")
    except Exception as exc:  # noqa: BLE001 - reported as a problem of this kill point
        problems.append(f"the follow-up raised {type(exc).__name__}: {exc}")
    return problems


def _after_crash(
    op: str,
    bed: Bed,
    *,
    in_process: bool,
    probe: Probe | None = None,
) -> list[str]:
    """Recover after a kill and check the result. The point's own problems are returned."""
    problems: list[str] = []
    if probe is not None:
        podman_writes = [label for label in probe.dead_attempts if label.startswith("podman")]
        if podman_writes:
            problems.append(f"a podman call ran after the death: {podman_writes}")
    store = _store(bed)
    journal_existed = bed.env.journal.load(bed.name) is not None
    if in_process and journal_existed:
        _orphan(bed)
    killed = store.load(bed.name).state if store.exists(bed.name) else None
    outcomes = recover(bed.env, bed.fake)
    problems.extend(_verify(op, bed, killed, journal_existed, outcomes))
    problems.extend(_follow_up(bed))
    return problems


def _dry_run(variant: Variant, run_dir: Path) -> list[str]:
    """Run the operation once with no kill. Its points are the kill points of the variant."""
    with pytest.MonkeyPatch.context() as mp:
        bed = variant.build(run_dir, mp)
        assert bed.run is not None and bed.done is not None
        probe = Probe()
        with probing(probe, bed.fake):
            bed.run()
        problems = bed.done()
        assert not problems, f"{variant.label}: the operation did not finish: {problems}"
        return probe.points


def _kill_and_check(variant: Variant, run_dir: Path, k: int, expected: str) -> list[str]:
    """Kill the operation at point `k`, then recover and check. In-process: an exception."""
    with pytest.MonkeyPatch.context() as mp:
        bed = variant.build(run_dir, mp)
        assert bed.run is not None
        probe = Probe(kill_at=k)
        with probing(probe, bed.fake):
            try:
                bed.run()
            except SimulatedCrash as crash:
                if str(crash) != expected:
                    return [f"the run diverged from its dry run: {crash} instead of {expected}"]
            except Exception as exc:  # noqa: BLE001 - an unexpected error is a finding
                return [f"the run raised {type(exc).__name__} before the kill: {exc}"]
            else:
                return [f"the run finished before point {k} ({expected})"]
        return _after_crash(variant.op, bed, in_process=True, probe=probe)


def _real_kill_and_check(variant: Variant, run_dir: Path, k: int, expected: str) -> list[str]:
    """Kill a forked child with SIGKILL at point `k`, then recover and check in the parent."""
    with pytest.MonkeyPatch.context() as mp:
        bed = variant.build(run_dir, mp)
        assert bed.run is not None
        snapshot = run_dir / "podman.pickle"
        probe = Probe(kill_at=k, real=True, snapshot=snapshot)
        pid = os.fork()
        if pid == 0:
            try:
                with probing(probe, bed.fake):
                    bed.run()
            finally:
                os._exit(_NOT_KILLED)
        _, status = os.waitpid(pid, 0)
        if not (os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL):
            return [f"the child was not killed at point {k} ({expected}): status {status}"]
        load_podman_state(bed.fake, snapshot)
        return _after_crash(variant.op, bed, in_process=False)


def _sweep(op: str, root: Path) -> tuple[int, list[str]]:
    """Every point of every variant of `op`, each from a fresh world.

    Returns the count of points and the problems found at them.
    """
    points = 0
    failures: list[str] = []
    for number, variant in enumerate(VARIANTS[op]):
        labels = _dry_run(variant, root / f"dry-{number}")
        for k, label in enumerate(labels):
            if (
                op == "rebuild"
                and variant.label in _FAULT_VARIANTS
                and label in {f"after journal step {x}" for x in REBUILD_STEPS}
            ):
                continue  # counted from the fault matrix (REBUILD_FAULT_POINTS)
            points += 1
            for problem in _kill_and_check(variant, root / f"{number}-{k}", k, label):
                failures.append(f"{variant.label} point {k} [{label}]: {problem}")
    return points, failures


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-122")
@pytest.mark.parametrize("op", OPS)
def test_a_sigkill_at_every_durable_effect_settles_to_a_documented_state(
    op: str, tmp_path: Path
) -> None:
    points, failures = _sweep(op, tmp_path)
    if op == "rebuild":
        points += REBUILD_FAULT_POINTS
    print(f"\n{op}: {points} kill points")
    assert points >= MIN_POINTS, f"{op} has {points} kill points, fewer than {MIN_POINTS}"
    assert not failures, f"{len(failures)} kill points failed:\n" + "\n".join(failures)


def _point_problems(op: str, variant_label: str, point: str, run_dir: Path) -> list[str]:
    """The problems of one named kill point (by its label, so an index shift cannot move it)."""
    variant = next(v for v in VARIANTS[op] if v.label == variant_label)
    labels = _dry_run(variant, run_dir / "dry")
    assert labels.count(point) == 1, f"{point!r} must name one point of {op}/{variant_label}"
    return _kill_and_check(variant, run_dir / "kill", labels.index(point), point)


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    ("variant_label", "point"),
    [("basic", "after podman start"), ("with-package", "after podman start")],
)
def test_a_create_killed_after_its_container_starts_leaves_no_broken_container_running(
    variant_label: str, point: str, tmp_path: Path
) -> None:
    problems = _point_problems("create", variant_label, point, tmp_path)
    assert not problems, f"{variant_label} killed at {point!r}: {problems}"


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    ("variant_label", "point"),
    [
        ("basic", "after record state updating"),
        ("development", "after record state updating"),
    ],
)
def test_an_update_killed_after_it_marks_the_realm_updating_leaves_no_broken_container_running(
    variant_label: str, point: str, tmp_path: Path
) -> None:
    problems = _point_problems("update", variant_label, point, tmp_path)
    assert not problems, f"{variant_label} killed at {point!r}: {problems}"


@pytest.mark.fr("NFR-030")
@pytest.mark.parametrize("op", OPS)
def test_a_real_sigkill_settles_to_a_documented_state(op: str, tmp_path: Path) -> None:
    variant = VARIANTS[op][0]
    labels = _dry_run(variant, tmp_path / "dry")
    k = len(labels) // 2
    problems = _real_kill_and_check(variant, tmp_path / "real", k, labels[k])
    assert not problems, f"SIGKILL at {labels[k]}: {problems}"


def _real_point_problems(op: str, variant_label: str, point: str, run_dir: Path) -> list[str]:
    """The problems of one named point, killed with a real SIGKILL in a forked child."""
    variant = next(v for v in VARIANTS[op] if v.label == variant_label)
    labels = _dry_run(variant, run_dir / "dry")
    assert labels.count(point) == 1, f"{point!r} must name one point of {op}/{variant_label}"
    return _real_kill_and_check(variant, run_dir / "real", labels.index(point), point)


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    ("op", "variant_label", "point"),
    [
        ("create", "basic", "after podman start"),
        ("resume", "basic-after-started", "after podman start"),
        ("update", "basic", "after record state updating"),
    ],
)
def test_a_real_sigkill_leaves_no_broken_container_running(
    op: str, variant_label: str, point: str, tmp_path: Path
) -> None:
    problems = _real_point_problems(op, variant_label, point, tmp_path)
    assert not problems, f"SIGKILL at {point!r}: {problems}"


PROVISION_STEPS: Final = (
    STEP_PROVISION_CREATED,
    STEP_INIT_INSTALLED,
    STEP_IPS_INSTALLED,
    STEP_STAGE_COMMITTED,
    STEP_RESET_CREATED,
    STEP_IMAGE_COMMITTED,
    STEP_RESET_REMOVED,
    STEP_STAGE_REMOVED,
    STEP_PROVISION_REMOVED,
)
"""The journal steps of the provisioning stage of a services-on create (ADR-015 Decision 13)."""


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-080")
def test_the_services_on_create_has_a_kill_point_at_each_provisioning_step(tmp_path: Path) -> None:
    """Each provisioning step is a journal write of the create, so a kill before and after it is a
    point of the sweep (NFR-030). The points come from a dry run of the services-on create."""
    (variant,) = [v for v in VARIANTS["create"] if v.label == "services-on"]
    labels = _dry_run(variant, tmp_path / "dry")
    for step in PROVISION_STEPS:
        assert f"before journal step {step}" in labels, step
        assert f"after journal step {step}" in labels, step
    assert labels.index(f"after journal step {STEP_PROVISION_CREATED}") < labels.index(
        f"after journal step {STEP_STAGE_COMMITTED}"
    )


def _variant(op: str, label: str) -> Variant:
    return next(v for v in VARIANTS[op] if v.label == label)


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-019")
def test_a_delete_killed_after_the_provisioning_sweep_is_finished_by_the_next_delete(
    tmp_path: Path,
) -> None:
    """The kill lands after the last provisioning container is removed and before the first derived
    image is removed (ADR-015 Decision 13). The record stays deleting, and the next delete sweeps
    the images and the base."""
    variant = _variant("delete", "init-leftovers")
    labels = _dry_run(variant, tmp_path / "dry")
    k = labels.index("before podman rmi") - 1
    assert labels[k] == "after podman rm", labels[k]
    problems = _kill_and_check(variant, tmp_path / "kill", k, labels[k])
    assert not problems, f"killed at {labels[k]!r}: {problems}"


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-019")
def test_a_delete_killed_between_its_init_image_step_and_its_image_step_is_finished_by_a_rerun(
    tmp_path: Path,
) -> None:
    """The kill lands after the derived images are gone and before the pinned base is removed, so
    the base is still there. The re-run removes it, and nothing else is left (FR-019 AC-1)."""
    variant = _variant("delete", "init-leftovers")
    labels = _dry_run(variant, tmp_path / "dry")
    k = labels.index("after journal step init-image-removed")
    assert labels[k + 1] == "before podman rmi", labels[k + 1]
    problems = _kill_and_check(variant, tmp_path / "kill", k, labels[k])
    assert not problems, f"killed at {labels[k]!r}: {problems}"


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-122")
def test_a_services_on_create_killed_after_its_image_is_committed_loses_the_image_on_recovery(
    tmp_path: Path,
) -> None:
    """The derived image exists, and the record does not name it yet, so recovery removes it by ID
    (ADR-015 Decisions 12 and 13). The redo is the resume's, checked after it."""
    variant = _variant("create", "services-on")
    labels = _dry_run(variant, tmp_path / "dry")
    k = labels.index("after journal step image-committed")
    problems = _kill_and_check(variant, tmp_path / "kill", k, labels[k])
    assert not problems, f"killed at {labels[k]!r}: {problems}"


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-122")
def test_a_services_on_create_killed_after_its_realm_container_is_on_the_derived_image_is_redone(
    tmp_path: Path,
) -> None:
    """The kill lands after the realm container is created from the derived image, and before the
    create records its image. Recovery keeps the image that this container uses, and the resume
    clears the container and the image and redoes the provisioning (ADR-015 Decision 12)."""
    variant = _variant("create", "services-on")
    labels = _dry_run(variant, tmp_path / "dry")
    after_stage = labels.index("after journal step provision-removed")
    k = labels.index("after podman create", after_stage)
    assert labels[k - 1] == "before podman create", labels[k - 1]
    problems = _kill_and_check(variant, tmp_path / "kill", k, labels[k])
    assert not problems, f"killed at {labels[k]!r}: {problems}"


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("DR-034")
@pytest.mark.fr("FR-021")
def test_a_provision_rebuild_killed_after_it_records_its_building_image_is_settled(
    tmp_path: Path,
) -> None:
    """The kill lands after `_mark_building` wrote `building_init_image_id`, before the swap. The
    record returns to its previous state, the building image and its stage go, and the recorded
    image stays (ADR-015 Decisions 8 and 13)."""
    variant = _variant("rebuild", "provision")
    labels = _dry_run(variant, tmp_path / "dry")
    created = labels.index("after journal step created")
    started = labels.index("before journal step started")
    (k,) = [i for i in range(created, started) if labels[i] == "after record save"]
    problems = _kill_and_check(variant, tmp_path / "kill", k, labels[k])
    assert not problems, f"killed at {labels[k]!r}: {problems}"


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("DR-034")
@pytest.mark.fr("FR-021")
def test_a_provision_rebuild_killed_after_retire_before_its_image_removal_is_settled(
    tmp_path: Path,
) -> None:
    """The kill lands after `_retire` removed gN and before `_retire_images` removes the derived
    image that the swap no longer keeps (ADR-015 Decision 13)."""
    variant = _variant("rebuild", "provision")
    labels = _dry_run(variant, tmp_path / "dry")
    k = labels.index("before podman rmi", labels.index("after journal step swapping")) - 1
    assert labels[k] == "after podman rm", labels[k]
    problems = _kill_and_check(variant, tmp_path / "kill", k, labels[k])
    assert not problems, f"killed at {labels[k]!r}: {problems}"


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-122")
def test_recovery_killed_inside_its_provisioning_sweep_is_settled_when_it_runs_again(
    tmp_path: Path,
) -> None:
    """Recovery of a services-on create killed after its image is committed. The kill lands on the
    first podman removal of recovery, which is the sweep of the provisioning containers (ADR-015
    Decision 13). The next recovery finds the same journal entry and settles it."""
    variant = Variant("create", "services-on-image-committed", _killed_services_create)
    labels = _dry_run(variant, tmp_path / "dry")
    k = next(i for i, label in enumerate(labels) if label.startswith("before podman"))
    assert labels[k] == "before podman rm", labels[k]
    problems = _kill_and_check(variant, tmp_path / "kill", k, labels[k])
    assert not problems, f"recovery killed at {labels[k]!r}: {problems}"
