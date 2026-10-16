# SPDX-License-Identifier: Apache-2.0
"""The provisioning stage of a services-on realm: its init set and its derived image (FR-080).

A services-on realm does not run from its pinned image, because the official minimal images have
no systemd (ADR-015 Context). `provision_init` builds the root that it runs from, in the order of
ADR-015 Decision 2:

- the pinned base is checked by digest (FR-011). Nothing is made before the check passes;
- a provisioning container from the base runs the distribution's refresh, its init set, its
  Integration Package Set and locale steps, then its clean steps. Each step is an argv list with
  signature checks on (FR-012, NFR-SEC-005), and each exec carries the services-off target
  (ADR-014);
- `check_init` refuses an image with no `/sbin/init` (GINN-E-NO-INIT) before the commit;
- podman's keep-id passwd line of the realm uid is removed from the provisioning container, so
  the derived image carries no line for the uid, and getent proves it (T7.30, ADR-015 Decision 2i);
- a stage commit, then a reset container and its commit, give the derived image. It keeps the
  pinned user and environment, has a null entrypoint and command, and carries no realm label
  (CR-007). Its ID is checked (DR-040);
- the reset container, the stage image and the provisioning container are removed, in that order.

A GinnError or a KeyboardInterrupt rolls back what this call made, newest first, as ops/create.py
does (FR-120, DR-018). A SIGKILL cannot roll back. `sweep_provisions` and `remove_derived` find what
it leaves, and recovery and resume call them (FR-122, ADR-015 Decision 13).

Disk (FR-121). `storage_required_bytes` and `tmpdir_required_bytes` are the formulas of ADR-015
Decision 11. The commit stages its image copy in `paths.image_tmp`, so that directory is checked
with its own formula before the stage commit. The storage formula belongs to the caller's precheck,
before the pull, because this stage runs after the base is already in storage.

The caller holds the realm lock (FR-123) and the image lock of the distribution. This module takes
neither, and it does not import ops/create.py.
"""

import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Final, Protocol

from ginn.distros import DistroDef, IntegrationSet, Verb
from ginn.domain import InstallMethod, InstallSource, ManifestEntry, RealmName
from ginn.errors import CATALOGUE, GinnError, make
from ginn.ops._rollback import Rollback
from ginn.ops.disk import StatVfs, check_free, estimate_required
from ginn.ops.images import remove_image
from ginn.ops.retry import network_retry_hook
from ginn.packages.manager import run_step
from ginn.podman.adapter import (
    PATH_NOT_FOUND_REASON,
    ExecTarget,
    Podman,
    require_image_id_ref,
)
from ginn.podman.models import ExecResult, ImageInfo
from ginn.runtime.provision import (
    GETENT_NOT_FOUND,
    LABEL_PROVISION,
    ROOT_USER,
    clean_argvs,
    derived_image_name,
    derived_image_prefix,
    final_commit_changes,
    init_set_hash,
    install_argv,
    integration_step_argvs,
    keep_id_entry_check_argv,
    keep_id_entry_drop_argv,
    manifest_digest,
    provision_create_spec,
    provision_name,
    provision_name_prefix,
    refresh_argv,
    removal_steps,
    reset_create_spec,
    reset_name,
    stage_commit_changes,
    stage_image_name,
)
from ginn.runtime.spec import INIT_PATH
from ginn.store.paths import OwnedPaths
from ginn.store.scratch import scratch_dir

STOP_TIMEOUT_S: Final = 10
"""Seconds the provisioning container gets to stop before the stage commit (ADR-015 Decision 2f)."""
STEP_PROVISION_CREATED: Final = "provision-created"
STEP_INIT_INSTALLED: Final = "init-installed"
STEP_IPS_INSTALLED: Final = "ips-installed"
STEP_STAGE_COMMITTED: Final = "stage-committed"
STEP_RESET_CREATED: Final = "reset-created"
STEP_IMAGE_COMMITTED: Final = "image-committed"
STEP_RESET_REMOVED: Final = "reset-removed"
STEP_STAGE_REMOVED: Final = "stage-removed"
STEP_PROVISION_REMOVED: Final = "provision-removed"
"""The journal steps of the stage, in order (ADR-015 Decision 13). Each is a kill point (NFR-030).
The derived image is recorded on the realm by the caller, which is its own step there."""
_REMOVAL_STEPS: Final = (STEP_RESET_REMOVED, STEP_STAGE_REMOVED, STEP_PROVISION_REMOVED)
_GENERATION: Final = re.compile(r"[1-9][0-9]*")
"""The generation part of an `io.ginn.provision` value, `<realm>:<gen>` (ADR-015 Decision 10)."""


class StepJournal(Protocol):
    """Where the stage records a durable step (FR-122). `Mutation.step` of ops/_mutating.py fits."""

    def step(self, name: str, params: Mapping[str, Any] | None = None) -> None: ...


@dataclass(frozen=True)
class ProvisionEnv:
    """What the stage calls: the adapter, the Owned Paths, and two seams for the tests.

    `sleep` is the FR-120 wait of a network retry. `statvfs` reads the free space of the image
    staging directory (FR-121). Both default to the real calls.
    """

    podman: Podman
    paths: OwnedPaths
    sleep: Callable[[float], None] = time.sleep
    statvfs: StatVfs = os.statvfs


@dataclass(frozen=True)
class ProvisionPlan:
    """One provisioning: a realm's generation, its distribution, and its pinned base."""

    realm: RealmName
    generation: int
    distro: DistroDef
    image_digest_ref: str
    """The pinned base as `repo@sha256:<hex>` (FR-011). The caller checked it against the trust
    manifest before it gets here."""
    lang: str | None
    """The host LANG for the locale steps (FR-013). The fallback reason of its locale is returned
    as the locale note."""
    ips_installed_bytes: int
    """The installed size of the Integration Package Set, for the staging check (ADR-015 Decision
    11). No distribution data holds it yet, so the caller passes it in."""
    keep_ids: frozenset[str]
    """The derived image IDs that the realm's records hold: `init_image_id` and
    `building_init_image_id` (ADR-015 Decision 13). A leftover of any generation with one of these
    IDs stays. A rebuild passes its live image here, so the sweep before the build cannot take it.
    """
    uid: int
    """The host uid of the realm user. The keep-id passwd line of this uid is removed from the
    provisioning container before the stage commit (ADR-015 Decision 2i, T7.30)."""


@dataclass(frozen=True)
class ProvisionResult:
    """A finished stage: the derived image's ID, its init entries and the locale note."""

    image_id: str
    """The derived image's ID, 64 lowercase hex digits (ADR-015 Decision 4)."""
    init_entries: tuple[ManifestEntry, ...]
    """The init set as Install Manifest entries, each marked `init` (ADR-015 Decision 6)."""
    locale_note: str | None
    notes: tuple[str, ...] = ()
    """A removal after the commit that failed. The image stays, and the sweep removes the rest."""


@dataclass(frozen=True)
class DerivedRemoval:
    """What `remove_derived` did: the provisioning containers it swept, the image IDs it removed and
    kept, and why each image was kept."""

    swept: tuple[str, ...]
    removed: tuple[str, ...]
    kept: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class _Argvs:
    """The argv of every step of the stage, built before the first effect (NFR-SEC-005)."""

    refresh: list[str]
    init: list[str] | None
    integration: list[str] | None
    locale: list[list[str]]
    clean: list[list[str]]


def check_init(
    podman: Podman, paths: OwnedPaths, container_id: str, *, image: str, realm: str
) -> None:
    """Refuse an image with no `/sbin/init`, with GINN-E-NO-INIT (FR-080, F3, ADR-015 Decision 2d).

    The check copies `/sbin/init` out of the container into a scratch directory under the runtime
    root. podman follows a link in the source, so a link to systemd passes. A missing path fails
    with podman's not-found reason, the only reason that means an absent init. Any other copy
    failure stays GINN-E-INTERNAL. Only the container's filesystem is read, through the copy, and
    no process runs in it, so the check works on a container that is running or stopped. The
    scratch copy is removed on exit. The caller holds the image lock, since the scratch sweep
    removes scratch directories only while no create runs.
    """
    try:
        with scratch_dir(paths) as scratch:
            try:
                podman.cp_from(container_id, INIT_PATH, scratch / "init")
            except GinnError as err:
                if err.details.get("reason") != PATH_NOT_FOUND_REASON:
                    raise
                raise make("GINN-E-NO-INIT", image=image, realm=realm) from err
    except OSError as exc:
        raise _scratch_failure(exc) from exc


def verify_derived_id(podman: Podman, realm: str, name: str, expected_id: str) -> None:
    """Check that the image `name` holds the ID recorded for it (DR-040, ADR-015 Decision 9).

    A name that holds no image, or a different ID, raises GINN-E-IMAGE-DERIVED. The create's
    pre-start check and the rebuild's pre-create check (ADR-015 Decision 9) call the same function.
    """
    require_image_id_ref(expected_id)
    try:
        actual = podman.image_id(name)
    except GinnError as err:
        raise make("GINN-E-IMAGE-DERIVED", realm=realm, detail=f"{name} is not present") from err
    if actual != expected_id:
        detail = f"{name} has ID {actual}, not the recorded {expected_id}"
        raise make("GINN-E-IMAGE-DERIVED", realm=realm, detail=detail)


def storage_required_bytes(
    base_bytes: int, init_bytes: int, ips_bytes: int, cache_peak_bytes: int
) -> int:
    """The podman storage that a services-on create needs (FR-121, ADR-015 Decision 11).

    It is `base + 2 x (init + ips) + cache_peak`, times the 1.10 margin, rounded up. The base is
    counted once, because it is shared. The factor 2 covers the provisioning container's layer and
    the committed layer, which exist together during the commit. `cache_peak` is the package cache
    during the installs, before the clean. There is no keep-id term (S-11).
    """
    for value in (base_bytes, init_bytes, ips_bytes, cache_peak_bytes):
        if value < 0:
            raise ValueError(f"FR-121: a size estimate is not negative, got {value}")
    total = base_bytes + 2 * (init_bytes + ips_bytes) + cache_peak_bytes
    return estimate_required(total)


def tmpdir_required_bytes(init_bytes: int, ips_bytes: int) -> int:
    """The image staging space of a commit (FR-121, ADR-015 Decision 11): init and ips, with margin.

    It is checked on `paths.image_tmp`, apart from the storage formula, before the stage commit.
    """
    for value in (init_bytes, ips_bytes):
        if value < 0:
            raise ValueError(f"FR-121: a size estimate is not negative, got {value}")
    return estimate_required(init_bytes + ips_bytes)


def remove_derived(podman: Podman, realm: RealmName, keep: Iterable[str | None]) -> DerivedRemoval:
    """Remove the realm's provisioning containers, then its derived and stage images (Decision 13).

    The containers go first, by `sweep_provisions`. The images are listed by the name prefix
    `localhost/ginn-init-<realm>:`, and each distinct ID is one candidate. A candidate whose ID is
    in `keep` stays. So does one that a container still uses, judged by the container's image ID
    alone, so `g1` never matches `g10` or `g1-stage`. A container that uses it is the caller's to
    remove, and the image is kept with a note rather than taken from under it. A failed rmi keeps
    its image and adds a note, so the rest still goes. Each image goes by its ID, not by a name, so
    no removal depends on podman refusing it (Decision 13).
    """
    swept = sweep_provisions(podman, realm)
    keep_ids = {image_id for image_id in keep if image_id is not None}
    images = podman.images_by_name(derived_image_prefix(realm))
    candidates = sorted({info.id for info in images.values()})
    containers = podman.ps()
    removed: list[str] = []
    kept: list[str] = []
    notes: list[str] = []
    for image_id in candidates:
        short = image_id[:12]
        if image_id in keep_ids:
            kept.append(image_id)
            notes.append(f"kept {short}: it is recorded on the realm")
            continue
        users = [c for c in containers if c.image_id == image_id]
        if users:
            kept.append(image_id)
            notes.append(f"kept {short}: container {users[0].id[:12]} still uses it")
            continue
        try:
            podman.rmi(image_id)
        except GinnError as err:
            kept.append(image_id)
            notes.append(f"kept {short}: rmi failed with {err.code}")
            continue
        removed.append(image_id)
    return DerivedRemoval(swept, tuple(removed), tuple(kept), tuple(notes))


def sweep_provisions(podman: Podman, realm: RealmName) -> tuple[str, ...]:
    """Remove the realm's provisioning containers, and return their names (ADR-015 Decision 10).

    A container is one of them when its name starts with `ginn_provision_<realm>_` and its
    `io.ginn.provision` value is `<realm>:<gen>`. The value is checked, not only the name, so a
    bare label key never selects a container. A realm name has no `_`, so the prefix is one
    realm's alone.
    """
    prefix = provision_name_prefix(realm)
    removed: list[str] = []
    for info in podman.ps():
        if not info.name.startswith(prefix):
            continue
        if not _is_provision_value(info.labels.get(LABEL_PROVISION, ""), realm):
            continue
        podman.rm(info.id, force=True)
        removed.append(info.name)
    return tuple(removed)


def provision_init(env: ProvisionEnv, plan: ProvisionPlan, journal: StepJournal) -> ProvisionResult:
    """Build the derived image of one services-on generation (ADR-015 Decision 2).

    The caller holds the realm lock and the distribution's image lock, and it records the result:
    the image ID, the init entries and the locale note. Each durable step goes to `journal`. A
    GinnError or a KeyboardInterrupt rolls back what this call made, and re-raises. The GinnError
    carries one note for each rollback step that failed.
    """
    return _Stage(env, plan, journal).run()


def _is_provision_value(value: str, realm: RealmName) -> bool:
    head, sep, generation = value.partition(":")
    return head == realm.value and sep == ":" and _GENERATION.fullmatch(generation) is not None


class _Stage:
    """One provisioning, from its first effect to its last (ADR-015 Decision 2)."""

    def __init__(self, env: ProvisionEnv, plan: ProvisionPlan, journal: StepJournal) -> None:
        self._env = env
        self._podman = env.podman
        self._plan = plan
        self._journal = journal
        self._realm = plan.realm.value
        self._distro = plan.distro
        self._integration: IntegrationSet = plan.distro.integration_set(plan.lang)
        self._target = ExecTarget(realm=self._realm, services=False, mutating=True)
        self._retry = network_retry_hook(env.sleep)
        self._rollback = Rollback()
        self._notes: list[str] = []
        self._provision_cid: str | None = None
        # Each builder checks the generation or the reference, so a bad plan refuses before any
        # effect, with the same rules the create uses.
        self._digest = manifest_digest(plan.image_digest_ref)
        self._argvs = _argvs(plan.distro, self._integration)
        provision_name(plan.realm, plan.generation)
        derived_image_name(plan.realm, plan.generation)

    def run(self) -> ProvisionResult:
        try:
            return self._run()
        except (GinnError, KeyboardInterrupt) as exc:
            notes = self._rollback.run()
            if isinstance(exc, GinnError):
                for note in notes:
                    exc.add_note(note)
            raise

    def _run(self) -> ProvisionResult:
        info = self._check_pin()
        self._clear_leftovers()
        changes = self._stage_changes(info)
        self._create_provisioning()
        self._install()
        self._require_init()
        self._drop_keep_id_entry()
        self._clean()
        self._check_staging()
        stage_id = self._stage(changes)
        final_id = self._derive(stage_id)
        self._remove_stages(stage_id)
        return ProvisionResult(
            final_id,
            self._init_entries(),
            self._integration.fallback_reason,
            tuple(self._notes),
        )

    def _check_pin(self) -> ImageInfo:
        """Check the pinned base by digest, pulling it when it is not here (FR-011, ADR-015 2a).

        An image this stage pulled is removed by the rollback when the check fails, so no layers
        stay. An image the caller pulled is left to the caller's rollback.
        """
        ref = self._plan.image_digest_ref
        if not self._podman.image_exists(ref):
            self._rollback.add("pinned image", partial(_undo_image, self._podman, ref))
            self._podman.pull(ref, retry_hook=self._retry)
        info = self._podman.image_inspect(ref)
        if info.digest != self._digest:
            raise make("GINN-E-IMAGE-UNVERIFIED", distro=self._distro.id)
        return info

    def _clear_leftovers(self) -> None:
        """Remove what an interrupted provisioning of this realm left, then refuse what stays.

        The realm lock is held, so no other provisioning of the realm runs. The provisioning
        containers go first, then every derived image that the records do not keep (ADR-015
        Decisions 12 and 13). The generation's own names must then be free: a commit onto a taken
        name moves its tag and leaves the old image untagged (the adapter's `commit`). A name that
        stays is refused, and the reasons of the removals that failed go with it.
        """
        plan = self._plan
        realm, gen = plan.realm, plan.generation
        removal = remove_derived(self._podman, realm, plan.keep_ids)
        stays = [
            name
            for name in (stage_image_name(realm, gen), derived_image_name(realm, gen))
            if self._podman.image_exists(name)
        ]
        taken = {provision_name(realm, gen), reset_name(realm, gen)}
        stays += sorted(info.name for info in self._podman.ps() if info.name in taken)
        if stays:
            reason = f"a leftover of this generation stays: {', '.join(stays)}"
            if removal.notes:
                reason += f" ({'; '.join(removal.notes)})"
            raise _internal(reason)

    def _stage_changes(self, info: ImageInfo) -> list[str]:
        """The `--change` list of the stage commit, from the pinned image's config (ADR-015 2f)."""
        try:
            return stage_commit_changes(
                image_user=info.user or None,
                image_env=info.env,
                init_set=init_set_hash(self._distro.services_packages),
                base_digest=self._digest,
            )
        except ValueError as exc:
            raise _internal(str(exc)) from exc

    def _create_provisioning(self) -> None:
        plan = self._plan
        spec = provision_create_spec(plan.realm, plan.generation, plan.image_digest_ref)
        self._rollback.add(
            "provisioning container", partial(_remove_container, self._podman, spec.name)
        )
        cid = self._podman.create(spec)
        self._provision_cid = cid
        self._podman.start(cid)
        self._journal.step(STEP_PROVISION_CREATED)

    def _install(self) -> None:
        """The refresh, the init set, the Integration Package Set and its locale steps (2c)."""
        self._exec(self._argvs.refresh, "refresh")
        if self._argvs.init is not None:
            self._exec(self._argvs.init, "install")
        self._journal.step(STEP_INIT_INSTALLED)
        if self._argvs.integration is not None:
            self._exec(self._argvs.integration, "integration")
        for argv in self._argvs.locale:
            self._exec(argv, "locale")
        self._journal.step(STEP_IPS_INSTALLED)

    def _require_init(self) -> None:
        check_init(
            self._podman,
            self._env.paths,
            self._container(),
            image=self._plan.image_digest_ref,
            realm=self._realm,
        )

    def _drop_keep_id_entry(self) -> None:
        """Remove podman's keep-id line of the realm uid, then prove it is gone (ADR-015 2i).

        Podman writes that line into the provisioning container's /etc/passwd, because the container
        is keep-id with no `--passwd-entry`. The stage commit would bake it into the derived image,
        and the realm's create would then keep it (GINN-E-REALM-USER). Both steps run as root, and
        neither is a package step, so neither goes through `run_step`. A failed drop is an internal
        error. So is a getent that does not exit GETENT_NOT_FOUND: an account at the uid is still
        there, from podman or from the pinned base, and the stage is refused before its commit.
        Nothing is retried.
        """
        dropped = self._root_exec(keep_id_entry_drop_argv(self._plan.uid))
        if dropped.exit_code != 0:
            raise _internal(
                f"the keep-id passwd line was not removed (exit {dropped.exit_code}): "
                f"{dropped.stderr.strip()}"
            )
        found = self._root_exec(keep_id_entry_check_argv(self._plan.uid))
        if found.exit_code != GETENT_NOT_FOUND:
            raise _internal(
                f"an account still has uid {self._plan.uid} in the provisioning container "
                f"(getent exit {found.exit_code}): {found.stdout.strip()}"
            )

    def _root_exec(self, argv: list[str]) -> ExecResult:
        return self._podman.exec(self._container(), argv, target=self._target, user=ROOT_USER)

    def _clean(self) -> None:
        for argv in self._argvs.clean:
            self._exec(argv, "clean")

    def _check_staging(self) -> None:
        """The commit stages its image copy in `paths.image_tmp` (FR-121, ADR-015 Decision 11)."""
        required = tmpdir_required_bytes(
            self._distro.services_estimated_installed_bytes,
            self._plan.ips_installed_bytes,
        )
        check_free(self._env.paths.image_tmp, required, statvfs=self._env.statvfs)

    def _stage(self, changes: list[str]) -> str:
        """Stop the provisioning container and commit it to the stage image (ADR-015 2f)."""
        cid = self._container()
        self._podman.stop(cid, STOP_TIMEOUT_S)
        name = stage_image_name(self._plan.realm, self._plan.generation)
        self._rollback.add("stage image", partial(_undo_image, self._podman, name))
        stage_id = _commit(self._podman, self._realm, cid, name, changes)
        self._journal.step(STEP_STAGE_COMMITTED, {"image_id": stage_id})
        return stage_id

    def _derive(self, stage_id: str) -> str:
        """The reset container from the stage image, and the final commit (ADR-015 2g)."""
        plan = self._plan
        spec = reset_create_spec(plan.realm, plan.generation, plan.image_digest_ref, stage_id)
        self._rollback.add("reset container", partial(_remove_container, self._podman, spec.name))
        reset_cid = self._podman.create(spec)
        self._journal.step(STEP_RESET_CREATED)
        name = derived_image_name(plan.realm, plan.generation)
        self._rollback.add("derived image", partial(_undo_image, self._podman, name))
        changes = final_commit_changes(
            init_set=init_set_hash(self._distro.services_packages), base_digest=self._digest
        )
        final_id = _commit(self._podman, self._realm, reset_cid, name, changes)
        self._journal.step(STEP_IMAGE_COMMITTED, {"image_id": final_id})
        verify_derived_id(self._podman, self._realm, name, final_id)
        # Every undo stays armed until run() returns. The caller has no ID until then, so an
        # interrupt or a failing journal step in the removals still leaves nothing behind. A removal
        # that fails is a note on the result, and it never reaches the undo (ADR-015 Decision 2h).
        return final_id

    def _remove_stages(self, stage_id: str) -> None:
        """Remove the reset container, the stage image and the provisioning container (2h).

        It runs after the derived image is committed and checked. A removal that fails is noted on
        the result and does not raise, so it does not roll the build back. The step is journaled
        only when its removal succeeds. A failing journal write raises and rolls back everything,
        the derived image included, because the caller never got the ID.
        """
        plan = self._plan
        stage_name = stage_image_name(plan.realm, plan.generation)
        steps = removal_steps(plan.realm, plan.generation, stage_id)
        for (kind, ref), journal_name in zip(steps, _REMOVAL_STEPS, strict=True):
            try:
                if kind == "container":
                    _remove_container(self._podman, ref)
                elif self._podman.image_exists(stage_name):
                    self._podman.rmi(ref)
            except GinnError as err:
                self._notes.append(f"{journal_name} failed with {err.code}")
                continue
            self._journal.step(journal_name)

    def _exec(self, argv: list[str], step: str) -> None:
        """Run one package-manager step as container root, with the FR-120 retries (FR-012)."""
        run_step(
            self._podman,
            self._container(),
            self._distro,
            argv,
            realm=self._realm,
            step=step,
            retry_hook=self._retry,
            target=self._target,
        )

    def _container(self) -> str:
        if self._provision_cid is None:
            raise RuntimeError("the provisioning container exists before any step that runs in it")
        return self._provision_cid

    def _init_entries(self) -> tuple[ManifestEntry, ...]:
        return tuple(
            ManifestEntry(
                name=name,
                source=InstallSource.REPO,
                method=InstallMethod.GINN,
                init=True,
            )
            for name in self._distro.services_packages
        )


def _argvs(distro: DistroDef, integration: IntegrationSet) -> _Argvs:
    """Every argv of the stage, from the distribution data (ADR-015 Decision 3). No effect."""
    return _Argvs(
        refresh=refresh_argv(distro),
        init=install_argv(distro, distro.services_packages),
        integration=(
            distro.argv(Verb.INSTALL, integration.packages) if integration.packages else None
        ),
        locale=integration_step_argvs(integration),
        clean=clean_argvs(distro),
    )


def _commit(
    podman: Podman,
    realm: str,
    container_id: str,
    name: str,
    changes: Sequence[str],
) -> str:
    """Commit a container to `name`. A failed commit is GINN-E-IMAGE-DERIVED (Decision 9)."""
    try:
        return podman.commit(container_id, name, changes=changes)
    except GinnError as err:
        failure = make(
            "GINN-E-IMAGE-DERIVED",
            realm=realm,
            detail=f"the commit to {name} failed with {err.code}",
        )
        raise failure from err


def _remove_container(podman: Podman, name: str) -> None:
    """Force-remove the container named `name`. Absent is a no-op, so an undo may run twice."""
    for info in podman.ps():
        if info.name == name:
            podman.rm(info.id, force=True)


def _undo_image(podman: Podman, name: str) -> None:
    """The undo of an image this stage made: removed by name, unless a container still uses it.

    An image that stays is a failed undo. It raises with the reasons, so the rollback notes them on
    the error, and the sweep finds the image (ADR-015 Decision 13).
    """
    outcome, _, notes = remove_image(podman, name)
    if outcome == "kept":
        raise _internal(f"{name} was not removed: {'; '.join(notes)}")


def _internal(reason: str) -> GinnError:
    """A GINN-E-INTERNAL whose message is `reason`, so a note or a CLI line shows it."""
    spec = CATALOGUE["GINN-E-INTERNAL"]
    err = GinnError("GINN-E-INTERNAL", reason, spec.remedy, spec.exit_code)
    err.details["reason"] = reason
    return err


def _scratch_failure(exc: OSError) -> GinnError:
    err = make("GINN-E-INTERNAL")
    err.details.update({"detail": f"init check scratch directory: {exc.strerror or exc}"})
    return err


__all__ = [
    "STEP_IMAGE_COMMITTED",
    "STEP_INIT_INSTALLED",
    "STEP_IPS_INSTALLED",
    "STEP_PROVISION_CREATED",
    "STEP_PROVISION_REMOVED",
    "STEP_RESET_CREATED",
    "STEP_RESET_REMOVED",
    "STEP_STAGE_COMMITTED",
    "STEP_STAGE_REMOVED",
    "STOP_TIMEOUT_S",
    "DerivedRemoval",
    "ProvisionEnv",
    "ProvisionPlan",
    "ProvisionResult",
    "StepJournal",
    "check_init",
    "provision_init",
    "remove_derived",
    "storage_required_bytes",
    "sweep_provisions",
    "tmpdir_required_bytes",
    "verify_derived_id",
]
