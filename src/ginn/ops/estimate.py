# SPDX-License-Identifier: Apache-2.0
"""The size of the Kali everything set, read with no lasting ginn state (FR-014 AC-1, FR-121).

`ginn --json create kali --everything --estimate` and the Control Plane's first step run
`estimate_everything`. It makes a throwaway container from the pinned image, refreshes the package
lists inside it, reads the closure of the everything metapackage (`resolve`, then `estimate`), and
removes the container. It writes no realm record, no Realm Home, no Install Manifest, no journal
entry and no audit entry. The set itself is not downloaded: only its sizes are read.

Why it does not go through `mutating()` (DR-032, FR-123, FR-124). The choke point is keyed on a
realm: it takes that realm's lock, and it journals and audits the operation under that name. An
estimate names no realm, and FR-124 lists the realm operations that the Audit Log records, which
an estimate is not. Its podman writes all go to ginn's own storage (`paths.podman_root`, an Owned
Path; DR-010, CR-006). It keeps the two things a create keeps for the image. It holds the image
lock of the distribution for its whole run (store/locks.py), so a create, delete, rebuild, resume
or lazy rebuild of that distribution is refused at once with GINN-E-REALM-BUSY, and none of them
removes the image under it (CL-22). Before it starts, it takes the host integrity gate that a
mutating operation takes before its body (FR-006, `IntegrityState.before_op`). It writes no host
file, so it takes no after check. Every podman artifact it made is undone on success, on a
GinnError and on an interrupt.

What a host keeps. Only a SIGKILL between the start of an estimate and its undo leaves anything:
the labelled container, and the base image when this estimate pulled it. The next create, resume,
estimate, delete, rebuild or update sweeps both (`sweep_estimates` in ops/recovery.py; a read-only
command does not sweep), and a delete of the distribution removes the container before its image
check (ops/images.py). A failed undo is
not silent: the estimate raises GINN-E-INTERNAL with each failed undo as a note. The base image is
pulled before the closure can be read, because the closure is only readable inside a realm (CL-16),
so the create downloads it a second time, after the confirmation (CL-22). The gate's first call on
a host with no baseline writes the baseline and one integrity audit entry, as any mutating
operation's first call does (FR-006). That is the only ginn state an estimate can write.

The space checks are FR-121's. The base image is checked before its pull, with the services-off
storage figure of ops/provision.py (the pull, the keep-id copy and the stage, T7.35). The set is
checked against its estimate times 1.10 before the result is returned, so a short disk is refused
before anyone is asked to confirm. The result's `required_bytes` is the whole create's figure: the
base's storage peak and the set, times 1.10 (`EverythingEstimate`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ginn.distros import DistroDef, Verb
from ginn.errors import GinnError, make
from ginn.ops._create_plan import (
    EVERYTHING_CHUNK,
    EVERYTHING_CHUNK_MAX,
    EVERYTHING_LISTING_MAX,
    CreateHooks,
    CreateRequest,
    SizeEstimate,
    SpaceCheck,
    _Plan,
    _validate,
    check_space,
    closure_names,
    size_totals,
)
from ginn.ops._mutating import OpEnv
from ginn.ops._rollback import Rollback
from ginn.ops.disk import check_free_paths, estimate_required
from ginn.ops.images import image_names, remove_estimate_containers, uses_image
from ginn.ops.provision import storage_peak_for
from ginn.ops.retry import network_retry_hook
from ginn.packages.manager import run_step
from ginn.podman.adapter import CreateSpec, ExecTarget, Podman, RetryHook
from ginn.runtime.spec import (
    KEEP_ALIVE_COMMAND,
    KEEP_ALIVE_ENTRYPOINT,
    LABEL_ESTIMATE,
    network_of,
)
from ginn.store.locks import image_lock

_NAME_PREFIX: Final = "ginn-estimate-"


def _throwaway(realm: str) -> ExecTarget:
    """The exec target of an estimate. Its container is created and removed with services off, so
    every exec it makes is direct (ADR-014, Decision 1)."""
    return ExecTarget(realm=realm, services=False, mutating=False)


@dataclass(frozen=True)
class EverythingEstimate:
    """The size of the everything set of one distribution, as FR-014 AC-1 shows it before a yes.

    The create checks two figures, at two points. Before its pull it checks the storage figure of
    its base (`base_required_bytes`, the services-off formula with the keep-id copy, T7.35). After
    the refresh it checks the set (`everything_required_bytes`, the set's estimate times 1.10). The
    set's bytes land on top of the base's, so the free space the whole create needs is the base's
    peak and the set's estimate together, times 1.10 (`required_bytes`). With that much free, both
    of the create's checks pass. A create with less may pass the first and be refused at the set.
    """

    distro: str
    everything: tuple[str, ...]
    size: SizeEstimate
    base_peak_bytes: int
    """The raw storage peak of the base, before the margin (`storage_peak_for`, FR-121)."""

    @property
    def base_required_bytes(self) -> int:
        return estimate_required(self.base_peak_bytes)

    @property
    def everything_required_bytes(self) -> int:
        return estimate_required(self.size.total_bytes)

    @property
    def required_bytes(self) -> int:
        return estimate_required(self.base_peak_bytes + self.size.total_bytes)


def measure_everything(
    podman: Podman,
    cid: str,
    definition: DistroDef,
    names: Sequence[str],
    *,
    realm: str,
    retry_hook: RetryHook | None,
    target: ExecTarget,
) -> SizeEstimate:
    """The download and installed size of the closure of `names`, read in running container `cid`.

    `resolve` lists the closure, and `estimate` reads each package's sizes in chunks, so no call
    prints more than EVERYTHING_CHUNK_MAX bytes. Both run as container root, after the refresh. The
    create (ops/create.py) and the estimate read the sizes this way, so their figures agree.
    """
    listing = run_step(
        podman,
        cid,
        definition,
        definition.argv(Verb.RESOLVE, names),
        realm=realm,
        step="estimate",
        retry_hook=retry_hook,
        target=target,
        max_output=EVERYTHING_LISTING_MAX,
    )
    closure = closure_names(listing.stdout, definition.name_pattern, realm=realm, step="estimate")
    download = installed = 0
    for start in range(0, len(closure), EVERYTHING_CHUNK):
        chunk = closure[start : start + EVERYTHING_CHUNK]
        result = run_step(
            podman,
            cid,
            definition,
            definition.argv(Verb.ESTIMATE, chunk),
            realm=realm,
            step="estimate",
            retry_hook=retry_hook,
            target=target,
            max_output=EVERYTHING_CHUNK_MAX,
        )
        part = size_totals(result.stdout, chunk, realm=realm, step="estimate")
        download += part.download_bytes
        installed += part.installed_bytes
    return SizeEstimate(packages=len(closure), download_bytes=download, installed_bytes=installed)


def estimate_everything(
    env: OpEnv,
    podman: Podman,
    request: CreateRequest,
    *,
    hooks: CreateHooks | None = None,
) -> EverythingEstimate:
    """Size the everything set of `request.distro`, and leave nothing that ginn keeps (FR-014 AC-1).

    The checks are the create's own (`_validate`), so a request that a create would refuse, such as
    a distribution without an everything set, is refused here too, before any podman call. A host
    with unacknowledged changes is refused with GINN-E-HOST-UNACKED before any podman call (FR-006),
    as a mutating operation is. A concurrent create of the same distribution, or another estimate of
    it, is refused at once with GINN-E-REALM-BUSY (store/locks.py). GINN-E-DISK-SPACE is raised when
    the set does not fit.
    """
    hooks = CreateHooks() if hooks is None else hooks
    if not request.everything:
        raise make("GINN-E-USAGE", detail="--estimate sizes the --everything set, so it needs it")
    plan = _validate(env, request, hooks)
    env.integrity.before_op(env.audit, env.user)
    with image_lock(env.paths, plan.definition.id, f"estimate of {plan.definition.id} image"):
        return _Estimate(env, podman, plan, hooks).run()


class _Estimate:
    """The state of one estimate. `estimate_everything` runs it under the image lock."""

    def __init__(self, env: OpEnv, podman: Podman, plan: _Plan, hooks: CreateHooks) -> None:
        self._env = env
        self._podman = podman
        self._plan = plan
        self._hooks = hooks
        self._rollback = Rollback()
        self._retry = network_retry_hook(hooks.sleep)
        self._name = f"{_NAME_PREFIX}{uuid.uuid4().hex[:16]}"

    @property
    def _distro(self) -> str:
        return self._plan.definition.id

    @property
    def _image(self) -> str:
        return self._plan.spec.image

    @property
    def _label(self) -> str:
        """The name that error details carry for this estimate. It names no realm (FR-014)."""
        return f"estimate:{self._distro}"

    def _step(self, name: str, detail: Mapping[str, object] | None = None) -> None:
        """Report one finished step to the hooks: one progress line in the CLI (FR-107).

        A step's `detail` reaches the hook before the step, as the create's does (FR-121).
        """
        if detail is not None and self._hooks.on_step_detail is not None:
            self._hooks.on_step_detail(name, detail)
        if self._hooks.on_step is not None:
            self._hooks.on_step(name)

    def _space_checked(self, check: SpaceCheck) -> None:
        """Report `space-checked` with the figures of its check (FR-121)."""
        self._step("space-checked", check.fields())

    def run(self) -> EverythingEstimate:
        """Measure, then undo. Every exit runs the undo, and a failed undo is never silent."""
        try:
            result = self._measure()
        except GinnError as err:
            for note in self._rollback.run():
                err.add_note(f"rollback: {note}")
            raise
        except KeyboardInterrupt as exc:
            cancelled = make("GINN-E-INTERRUPTED")
            cancelled.add_note("the estimate was cancelled, and what it made was removed")
            for note in self._rollback.run():
                cancelled.add_note(f"rollback: {note}")
            raise cancelled from exc
        except BaseException:
            self._rollback.run()
            raise
        notes = self._rollback.run()
        if notes:
            # The sizes are known, but the estimate left something behind, so it is not reported
            # as a clean run (FR-122, DR-018).
            leftover = make("GINN-E-INTERNAL")
            leftover.details["detail"] = "the estimate could not remove what it made"
            for note in notes:
                leftover.add_note(f"rollback: {note}")
            raise leftover
        return result

    def _measure(self) -> EverythingEstimate:
        paths = self._env.paths
        plan = self._plan
        # The base is checked with its pull, as the create checks it before its own pull (T7.35).
        base_peak = storage_peak_for(
            plan.definition,
            services=plan.spec.services,
            download_bytes=plan.entry.compressed_size_bytes,
            base_pulled=True,
        )
        check = check_space(
            [paths.podman_root, paths.homes],
            estimate_required(base_peak),
            statvfs=self._hooks.statvfs,
        )
        self._space_checked(check)
        self._remove_containers()
        self._pull()
        self._step("pulled")
        cid = self._create()
        self._step("created")
        self._podman.start(cid)
        run_step(
            self._podman,
            cid,
            self._plan.definition,
            self._plan.definition.argv(Verb.REFRESH),
            realm=self._label,
            step="refresh",
            retry_hook=self._retry,
            target=_throwaway(self._label),
        )
        self._step("refreshed")
        size = measure_everything(
            self._podman,
            cid,
            self._plan.definition,
            self._plan.everything,
            realm=self._label,
            retry_hook=self._retry,
            target=_throwaway(self._label),
        )
        self._step("estimated")
        check_free_paths(
            [self._env.paths.podman_root, self._env.paths.homes],
            estimate_required(size.total_bytes),
            statvfs=self._hooks.statvfs,
        )
        return EverythingEstimate(
            distro=self._distro,
            everything=tuple(self._plan.everything),
            size=size,
            base_peak_bytes=base_peak,
        )

    def _pull(self) -> None:
        """Pull the pinned image by digest and check its digest, as the create does (FR-011)."""
        pulled_here = not self._podman.image_exists(self._image)
        if pulled_here:
            # Registered before the pull, so an interrupt inside the pull still finds the image.
            self._rollback.add("image", self._remove_image)
        image = self._podman.pull(self._image, retry_hook=self._retry)
        if image.digest != self._plan.entry.digest:
            err = make("GINN-E-IMAGE-UNVERIFIED", distro=self._distro)
            try:
                self._podman.rmi(self._image)
            except GinnError as rm_err:
                err.add_note(f"image not removed: {rm_err.code}")
            raise err

    def _create(self) -> str:
        """Make the throwaway container: the image's keep-alive, on the distribution's network.

        It has no mounts, no passwd entry and no realm user, because nothing runs as the realm user.
        It has the same proxy setting as a realm, so the refresh sees the network a realm would.
        """
        spec = CreateSpec(
            name=self._name,
            image_digest_ref=self._image,
            network=network_of(self._plan.spec.distro),
            init=True,
            http_proxy=False,
            entrypoint_argv=list(KEEP_ALIVE_ENTRYPOINT),
            command=list(KEEP_ALIVE_COMMAND),
            labels={LABEL_ESTIMATE: self._distro},
        )
        # Registered before the call. An interrupt inside the call leaves a container with no id
        # here, and the undo finds it by its label.
        self._rollback.add("container", self._remove_containers)
        return self._podman.create(spec)

    def _remove_containers(self) -> None:
        """Stop and force-remove this distribution's estimate containers. Absent is a no-op.

        The image lock is held, so the containers with this label are this estimate's, or the
        leftovers of an earlier estimate that was killed. Both are removed.
        """
        remove_estimate_containers(self._podman, self._distro)

    def _remove_image(self) -> None:
        """Remove the image this estimate pulled. Absent, or in use by a container, is a no-op.

        A container uses the image by the alias-aware match that delete and resume use
        (ops/images.py `uses_image`), since podman may show the image by name, digest or id. A
        container with no image string is not compared. A failed rmi is raised, so the undo reports
        it.
        """
        if not self._podman.image_exists(self._image):
            return
        names = image_names(self._podman, self._image)
        for info in self._podman.ps():
            if info.image != "" and uses_image(info.image, names):
                return  # a realm or a create still uses the image, so it stays
        self._podman.rmi(self._image)


__all__ = [
    "LABEL_ESTIMATE",
    "EverythingEstimate",
    "estimate_everything",
    "measure_everything",
]
