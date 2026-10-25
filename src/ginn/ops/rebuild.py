# SPDX-License-Identifier: Apache-2.0
"""Rebuild a realm's root from a freshly verified image (FR-021, FR-022, NFR-031, DR-034).

`rebuild_realm` builds generation gN+1 beside gN, replays the Install Manifest into it, and swaps it
in with one record write that names gN+1. The Realm Home, the tier, the permissions and the services
setting are kept (FR-021 AC-1). The old root is removed only after the swap.

`rebuild_for_start` is the same work for a stopped realm whose record asks for a create-time state
that its container lacks (a tier, permission or services change, FR-066, FR-083). The start that
calls it starts the new generation. It stops and starts nothing itself, so it runs in front of a
start, under no realm lock.

Order of a rebuild, and why:
1. The record is read (FR-025), and the plan is checked (`_validate`), before anything is written.
   A state other than stopped or running is GINN-E-REALM-STATE. The plan takes the record's tier,
   permissions and services mode, so a realm with Custom permissions is rebuilt with them.
2. The image lock of the distribution is taken (create.py, `image_lock`), because the rebuild pulls
   the pinned image.
3. A running realm is stopped first, through `stop_realm`, which takes its own lock. Two roots never
   run on one Realm Home, and the swap needs gN stopped. A services-on realm is stopped through its
   runit service (ADR-003), so runit does not restart it under the rebuild.
4. Inside `mutating("rebuild")`, the record moves to `rebuilding`, with previous_state `stopped`
   (step `rebuilding`).
5. The phases of ops/create.py run for gN+1, through `_Rebuild`, a subclass of `_Create` (T8.3):
   space-checked, pulled and created. Then the record names gN+1 as the container being built
   (`building_container_id`). That is a second write while the record reads rebuilding, and it is
   not the swap: the record still names gN, and the building container is a field of its own. Then
   the runit directory is written when the realm becomes services-on (`service`, with `down`
   present). Then gN+1 starts (`started`). A services-on gN+1 starts through its runit service:
   `sv up`, then the wait that a `ginn start` uses. Its `ginn _service-run` starts the building
   container and writes nothing (ops/service.py, ADR-003), and the rebuild does not start it itself.
   A services-off gN+1 starts with podman. Then attached, user, integrated, the Kali everything set
   (estimated and installed), and replayed (`replayed`, FR-022 below). Then gN+1 is stopped
   (`stopped`): a services-on one by `sv down` and its wait, a services-off one by podman. None of
   these phases writes the record, apart from the building fields, and none removes the Realm Home.
6. The swap (`swapping`) is the one record save that names gN+1. It names the new digest, the
   post-replay manifest, the grants, and the create-time fields of the new plan (`created_options`,
   the kvm and GPU and fonts flags, and `services_at_create`), and the state is `stopped`, with the
   building fields cleared. `start_realm` refuses a record whose options differ from its container's
   (ADR-006), so the swap recomputes them.
7. After the swap, every generation below gN+1 is removed best effort (`retired`): gN, and any
   older root that an earlier removal left, found by its labels (`remove_below`). Its failure does
   not fail the rebuild: the audit entry records `old_removed`, and recovery or `ginn delete`
   removes it later.
   When the swap turns services off, the realm's runit service directory is removed the same way,
   after the swap (`_drop_service`), so a failed swap leaves the service that gN runs.
8. A realm that was running starts again through `start_realm`, on gN+1, and that start goes
   through runit for a services-on realm (`sv up`). Then its export entries are synced
   (`sync_realm`), since a sync reads only a running realm (FR-044). A realm that was stopped is
   started once, synced and stopped again (`_sync_stopped`), so its menu loses the entries of
   the packages that are gone (FR-021 AC-1). Each of these steps takes its own lock, outside the
   image lock. `rebuild_for_start` skips steps 3 and 8: its realm is stopped and the calling start
   does the start, with `rebuild=False`, so the start never rebuilds again.

Failure. A GinnError from steps 4 to 6 rolls back gN+1 (the `_Create` undo, newest first). A
services-on gN+1 is stopped through `sv down` first, so runsv does not restart a container that is
being removed. Then the container and the directory are removed, and the record returns to `stopped`
with its building fields cleared (`set_state`). The error is re-raised. The old root and the old
record are unchanged, so the old realm is usable (FR-021 AC-2, NFR-031). A realm that was running is
started again on gN, best effort. A KeyboardInterrupt is a cancel (AS-018), and it is handled the
same way. Any other exception keeps the journal entry, and recovery (ops/recovery.py,
`_decide_rebuild`) settles it.

Missing packages (FR-022 AC-1). The replay installs the manifest's packages in one transaction per
source. If the package manager refuses the set with GINN-E-PKG-MANAGER, and the refusal's output
matches the distribution's `missing_package_regex` (`_is_missing`), each package is installed on its
own. A package that is refused again with that evidence is no longer available upstream. It gets a
GINN-W-REPLAY-MISSING warning and leaves the new manifest, and the swap still completes. This holds
for every package, the last one included: a manifest whose packages are all gone completes with an
empty manifest and one warning each. A package manager's transaction is all or nothing, so the
refused set changed nothing. Only positive evidence counts: a refusal with no output (a time or
size limit), output that says the disk is full, a network failure (GINN-E-NETWORK) and a signature
failure are never a missing package. Each of them fails the rebuild with the old realm kept, and so
does a refusal of one package without that evidence, whatever the other packages did. The
per-package attempts are the only probe that works on every distribution: `resolve` exits non-zero
on dnf whether or not the package exists (fedora.toml). The everything set follows the same rule
(`_install_everything`), and so do AUR packages, each one on its own.

A Kali everything set is installed by `_everything`, so it is not replayed, and it is not confirmed
again. The create asked for the yes, and the Install Manifest records the set (FR-014 AC-2), so that
entry is the consent. `_fixed` keeps only the everything names that the manifest records, so a set
that `ginn remove` took out is not put back (FR-021). No prompt is asked, on a lazy rebuild at a
start or on `ginn rebuild`.

AUR packages (FR-043) go through `install_aur` with the same per-package rule. Its own steps are
journaled through this operation's `on_step`, so a test can stop it there.

Recovery and the image lock are in the modules named above. A rebuild takes the same locks as any
mutating operation: the image lock before the realm lock, and the realm lock inside `mutating`.
A rebuild that a start runs holds the image lock and the realm lock of its own, and the start takes
its locks after the rebuild has released them. Nothing nests. The `sv up` of a services-on rebuild
runs while the rebuild holds the realm lock, and its `_service-run` takes no lock, so nothing waits
on that lock. Its finish script writes no record while the record reads rebuilding (ops/service.py).
"""

import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from ginn.distros import DistroDef, Verb
from ginn.domain import InstallSource, ManifestEntry, RealmName, RealmState
from ginn.domain.permissions import Toggle
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.export.sync import SyncHooks, sync_realm
from ginn.ops._create_plan import CreateHooks, CreateRequest, _Plan, _validate
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops.create import ROLLBACK_STOP_TIMEOUT_S, STOP_TIMEOUT_S, _Create
from ginn.ops.disk import StatVfs
from ginn.ops.lifecycle import (
    HostUser,
    LifecycleHooks,
    RunUserState,
    _settle_run_user,
    run_user_gone_error,
    run_user_pending_note,
    start_realm,
    start_through_sv,
    stop_realm,
    stop_through_sv,
)
from ginn.ops.recovery import OP_REBUILD, remove_below
from ginn.ops.security_ops import write_nvidia
from ginn.packages.aur import install_aur
from ginn.packages.manager import name_batches, printable, run_step, shown
from ginn.packages.manifest import removed
from ginn.podman.adapter import Podman
from ginn.podman.models import ContainerState
from ginn.runtime.attach import MOUNTINFO
from ginn.runtime.mounts import Exists
from ginn.runtime.realm_security import SecurityHooks
from ginn.security import seccomp
from ginn.security.caps import TierExtras
from ginn.security.nested import write_missing_config
from ginn.security.seccomp import write as write_profile
from ginn.services import autostart
from ginn.services.exec_relay import relay_failure, sync_exec_service
from ginn.services.runit_user import remove_service, write_service_dir
from ginn.store._time import utc_from_epoch
from ginn.store.locks import image_lock
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from ginn.trust.manifest import TrustManifest

_REFUSED: Final = "GINN-E-PKG-MANAGER"
"""The code of a package manager's own refusal, which a per-package install may answer (FR-022)."""
_NO_SPACE: Final = re.compile(r"No space left on device|ENOSPC")
"""Output that says the disk is full. It is never a missing package, whatever else it says."""
_REBUILDABLE: Final = frozenset({RealmState.STOPPED, RealmState.RUNNING})
"""The record states that a rebuild accepts. A running realm is stopped first (step 3)."""


@dataclass(frozen=True)
class RebuildHooks:
    """The seams of a rebuild: those of a create, the lifecycle of a stop and a start, and the sync.

    The fields mirror CreateHooks (ops/_create_plan.py), so a test drives a rebuild the way it
    drives a create. `on_step` runs after each journal step of the rebuild, including `rebuilding`,
    `pulled` and `swapping`. `program` is the ginn command that a synced menu entry runs (FR-044).
    `lifecycle` is passed to the stop and the start of a running realm, and to the runit start and
    stop of a services-on gN+1 (ADR-003). There is no confirm seam: a rebuild asks no question, and
    a replayed everything set is consented at create (`_Rebuild._confirm_everything`).
    """

    statvfs: StatVfs = os.statvfs
    sleep: Callable[[float], None] = time.sleep
    exists: Exists = os.path.exists
    mountinfo: Path = MOUNTINFO
    helper: str | None = None
    manifest: TrustManifest | None = None
    on_step: Callable[[str], None] | None = None
    seccomp_source: Path = seccomp.DEFAULT_SOURCE
    extras: TierExtras | None = None
    security: SecurityHooks = field(default_factory=SecurityHooks)
    program: str | None = None
    lifecycle: LifecycleHooks = field(default_factory=LifecycleHooks)


@dataclass(frozen=True)
class RebuildResult:
    """A rebuilt realm and the notices it produced.

    `record` is the record after the rebuild, so `generation` is gN+1. `replay_missing` names the
    packages that are no longer available upstream (FR-022), and `warnings` holds their
    GINN-W-REPLAY-MISSING notices with the others. `old_removed` is False when gN could not be
    removed after the swap. gN is then left for recovery or `ginn delete`.
    """

    record: RealmRecord
    warnings: tuple[GinnWarning, ...]
    replay_missing: tuple[str, ...]
    old_removed: bool


@dataclass(frozen=True)
class _Outcome:
    """What the locked part of a rebuild returns: the swapped record and its notices."""

    record: RealmRecord
    replay_missing: tuple[str, ...]
    old_removed: bool
    warnings: tuple[GinnWarning, ...]


def _create_hooks(hooks: RebuildHooks) -> CreateHooks:
    """The create seams of a rebuild. `_validate` and `_Create` read them."""
    return CreateHooks(
        statvfs=hooks.statvfs,
        sleep=hooks.sleep,
        exists=hooks.exists,
        mountinfo=hooks.mountinfo,
        helper=hooks.helper,
        manifest=hooks.manifest,
        on_step=hooks.on_step,
        seccomp_source=hooks.seccomp_source,
        extras=hooks.extras,
        security=hooks.security,
        program=hooks.program,
    )


def _is_missing(definition: DistroDef, err: GinnError) -> bool:
    """True when a refusal is positive evidence that its package is gone upstream (FR-022 AC-1).

    The distribution's `missing_package_regex` must match the output of the refusal (fedora.toml).
    Output that says the disk is full is never a missing package. A refusal with no output, such as
    a time or size limit (packages/manager.py), carries no evidence, so it is not one either.
    """
    output = err.details.get("output")
    if definition.missing_package is None or not isinstance(output, str):
        return False
    if _NO_SPACE.search(output) is not None:
        return False
    return definition.missing_package.search(output) is not None


def _replay_group(
    names: list[str], install: Callable[[list[str]], None], definition: DistroDef
) -> list[str]:
    """Install `names` with `install`, and return the names that are missing upstream (FR-022).

    One call installs the whole group. A refusal of the group (GINN-E-PKG-MANAGER) is retried name
    by name, but only when the refusal is positive evidence of a missing package (`_is_missing`).
    A name refused again with that evidence is returned, and the others stay installed, even when
    every name is returned. Any other error, such as a limit, a full disk, a network or a signature
    failure, and a name refused without that evidence, is raised as it is, so the rebuild fails with
    the old realm kept (CL-17).

    On the relay route `install` runs the group in batches (ADR-014, T7.10 review). A refusal can
    then come after earlier batches installed, and those stay installed in gN+1. The name-by-name
    pass installs the names again, which does nothing to a package that is already there. gN+1 is
    discarded when the rebuild fails, so none of it is kept.
    """
    if not names:
        return []
    try:
        install(names)
    except GinnError as err:
        if err.code != _REFUSED or not _is_missing(definition, err):
            raise
    else:
        return []
    refused: list[str] = []
    for name in names:
        try:
            install([name])
        except GinnError as one:
            if one.code != _REFUSED or not _is_missing(definition, one):
                raise
            refused.append(name)
    return refused


class _Rebuild(_Create):
    """The phases of one rebuild, on top of the create phases (ops/create.py).

    `_Create` keeps its draft record in memory through `_update`, and its own `_update` saves each
    change. Here `_update` only changes the draft, so the one save is `_swap`. The `_Create`
    phases that would register a rollback of the Realm Home or the runit directory are not called
    (`_make_home`, `_write_service`, `_check_name`, `_save_creating`, `_finish`).
    """

    def __init__(
        self,
        env: OpEnv,
        podman: Podman,
        request: CreateRequest,
        hooks: CreateHooks,
        plan: _Plan,
        mutation: Mutation,
        record: RealmRecord,
        lifecycle: LifecycleHooks,
    ) -> None:
        super().__init__(env, podman, request, hooks, plan, mutation)
        self._record = record
        self._manifest: list[ManifestEntry] = list(record.manifest)
        self._lifecycle = lifecycle
        self.replay_missing: list[str] = []
        self._everything_missing: list[str] = []

    def run(self) -> RealmRecord:
        """Run the rebuild phases in order. Return the swapped record, or raise the first error.

        A services-on gN+1 is started and stopped through its runit service, and a services-off one
        with podman (ADR-003). Its runit directory is written before the start, which goes through
        it.
        """
        self._check_space()
        self._share_host_files()
        self._pull()
        self._create_container()
        self._mark_building()
        self._sync_relay()
        self._service_dir()
        if self._plan.spec.services:
            self._start_through_sv()
        else:
            self._start()
        self._attach()
        self._check_user()
        self._integrate()
        self._everything()
        self._replay()
        self._stop_new()
        return self._swap()

    def _update(self, **changes: Any) -> None:
        """Change the draft record in memory. Only `_swap` saves the draft (DR-034)."""
        now = utc_from_epoch(self._env.clock())
        self._record = replace(self._current(), updated_at=now, **changes)

    def _install_everything(self, names: tuple[str, ...]) -> None:
        """Install the everything set, and leave out the names that are missing upstream (FR-022).

        The rule of `_replay_group` applies to it too, so a name the upstream no longer offers is
        dropped from the new manifest and reported, and the others install.
        """
        self._everything_missing = _replay_group(
            list(names), self._install_everything_group, self._plan.definition
        )

    def _install_everything_group(self, names: list[str]) -> None:
        """One install of `names` of the everything set, as container root (FR-014).

        A relay route installs the set in calls of its own size (name_batches, ADR-014).
        """
        for batch in name_batches(names, self._target(mutating=True)):
            self._exec(self._plan.definition.argv(Verb.INSTALL, batch), step="everything")

    def _confirm_everything(self, prompt: str) -> bool:
        """A replayed everything set needs no second yes, so the answer is yes (FR-014 AC-2).

        `_fixed` keeps only the everything names that the Install Manifest records, and that record
        is the consent that the create gave. The prompt is not shown.
        """
        return True

    def _share_host_files(self) -> None:
        """Write the NVIDIA spec and the seccomp profile, which are host files outside the home.

        The inner podman config of a nested realm is in the Realm Home, which is kept. A realm that
        becomes nested by a rebuild (a Development tier change, FR-066) may have none, or only one
        of its two files. Only the files that are absent are written, and a file that is there is
        never overwritten, so an edited file stays as the user left it. What this writes is
        registered for the rollback, which removes it again when the rebuild fails, so a failed
        rebuild leaves the Realm Home as it was (NFR-031). The profile is shared and idempotent
        (ADR-012, ADR-006).
        """
        if self._plan.gpu_cdi:
            write_nvidia(self._env.paths, self._plan.gpu)
        if self._plan.seccomp is not None:
            write_profile(self._plan.seccomp, self._env.paths.seccomp)
            written = write_missing_config(self._home)
            self._rollback.add("inner podman config", written.undo)

    def _mark_building(self) -> None:
        """Record the container that this rebuild is building, before any start of it (ADR-003).

        This is a second write while the record reads rebuilding, and it is not the swap. The record
        still names gN and its container. Only the two building fields are new. The runit service
        starts the building container (ops/service.py, `_service-run`), so they are written before
        the runit directory and before `sv up`. `set_state` clears them when the record leaves
        rebuilding, and the swap clears them too.
        """
        cid = self._container_id()
        current = self._store.load(self._request.name)
        self._store.save(
            replace(
                current,
                building_container_id=cid,
                building_generation=self._plan.spec.generation,
                updated_at=utc_from_epoch(self._env.clock()),
            )
        )

    def _start_through_sv(self) -> None:
        """Start the services-on gN+1 through its runit service, and wait until it runs (ADR-003).

        `sv up` makes runsv start `ginn _service-run`, which starts the building container and
        writes nothing (ops/service.py). The wait is the one a `ginn start` uses
        (lifecycle.start_through_sv). The rollback is registered before the start and runs before
        the container and the directory are removed: it stops the container through `sv down`, so
        runsv does not restart it.
        """
        name = self._request.name
        cid = self._container_id()
        lifecycle = self._lifecycle
        self._rollback.add(
            "supervised container",
            lambda: stop_through_sv(
                self._env,
                self._podman,
                name,
                lifecycle,
                container_id=cid,
                timeout_s=ROLLBACK_STOP_TIMEOUT_S,
            ),
        )
        start_through_sv(self._env, self._podman, name, lifecycle, container_id=cid)
        info = self._podman.inspect(cid)
        if info.state is not ContainerState.RUNNING or info.pid < 2:
            err = make("GINN-E-INTERNAL")
            err.details.update(
                {
                    "detail": "the rebuilt realm is not running after start",
                    "state": info.state.value,
                }
            )
            raise err
        self._pid = info.pid
        self._step("started")

    def _attach(self) -> None:
        """Attach the grants of gN+1 once its runtime dir belongs to the realm user (T7.28).

        A services-on gN+1 is started by runit, and its tmpfiles.d drop-in may not have chowned
        `/run/user/<uid>` yet. The attach mkdirs under it as the realm user, so the wait comes
        first. It is the start's own wait (lifecycle._settle_run_user), bounded the same way, and
        a gN+1 that stops in the wait fails here as it does after start (FR-081). The record it is
        given is gN+1's view, since its exec target follows the services mode of gN+1. A
        services-off gN+1 has no wait, and its attach is the create's.
        """
        run_user_pending = False
        if self._plan.spec.services:
            new_view = replace(self._current(), services_at_create=True)
            cid = self._container_id()
            run_user = _settle_run_user(
                self._podman, cid, self._request.uid, new_view, self._lifecycle, self._pid
            )
            if run_user is RunUserState.GONE:
                raise run_user_gone_error(
                    self._podman,
                    cid,
                    self._request.name,
                    detail="the rebuilt realm is not running after start",
                )
            run_user_pending = run_user is RunUserState.NOT_YET
        try:
            super()._attach()
        except GinnError as err:
            if run_user_pending:
                err.add_note(run_user_pending_note(self._request.name))
            raise
        if run_user_pending:
            self._mutation.warnings.append(
                warn("GINN-W-RUN-USER-PENDING", realm=self._request.name.value)
            )

    def _replay(self) -> None:
        """Install the manifest's packages into gN+1, and drop the ones that are gone (FR-022).

        The everything set is left out, because `_everything` installed it. The AUR packages are
        built through install_aur, and the native and repository ones through one install verb.
        """
        held = set(self._plan.everything)
        manifest = self._current().manifest
        entries = [entry for entry in manifest if entry.name not in held]
        repo = [entry.name for entry in entries if entry.source is not InstallSource.AUR]
        aur = [entry.name for entry in entries if entry.source is InstallSource.AUR]
        definition = self._plan.definition
        missing = _replay_group(repo, self._install_repo, definition)
        missing += _replay_group(aur, self._build_aur, definition)
        self.replay_missing = self._everything_missing + missing
        self._manifest = removed(manifest, self.replay_missing)
        self._step("replayed")

    def _install_repo(self, names: list[str]) -> None:
        """One install of `names` with the distribution's install verb, as container root.

        A relay route replays in calls of its own size (name_batches, ADR-014). A failed call leaves
        the earlier ones in gN+1. gN+1 is discarded when the rebuild fails, so they are not kept.
        """
        definition = self._plan.definition
        target = self._target(mutating=True)
        for batch in name_batches(names, target):
            run_step(
                self._podman,
                self._container_id(),
                definition,
                definition.argv(Verb.INSTALL, batch),
                realm=self._request.name.value,
                step="replay",
                retry_hook=self._retry,
                target=target,
                on_ack=self._exec_acked,
            )

    def _exec_acked(self, pid: int) -> None:
        """The journal step that records a replay exec has started (FR-122, ADR-014).

        The pid is the podman client's, on either route. It is not stored, because recovery only
        needs to know that the exec began. The step goes through `_step`, so the hook sees it too.
        """
        self._step("exec-acked")

    def _sync_relay(self) -> None:
        """Sync the supervisor relay with the rebuild's REBUILDING state (ADR-014).

        A services-on rebuild runs its execs through the relay, so the relay must be up first. A
        failed sync is a warning: the rebuild goes on, and the next sync retries it.
        """
        try:
            sync_exec_service(self._env.paths, self._podman)
        except Exception as exc:  # noqa: BLE001 - the rebuild goes on, so this is a warning
            self._mutation.warnings.append(relay_failure(exc))

    def _build_aur(self, names: list[str]) -> None:
        """Build and install `names` from the AUR, as the install does (FR-043)."""
        outcome = install_aur(
            self._podman,
            self._container_id(),
            self._plan.definition,
            names,
            realm=self._request.name.value,
            uid=self._request.uid,
            gid=self._request.gid,
            retry_hook=self._retry,
            target=self._target(mutating=True),
            step=self._aur_step,
        )
        if outcome.residual:
            packages = shown(list(outcome.residual))
            reason = printable(outcome.reason or "pacman refused the removal", 400)
            notice = warn(
                "GINN-W-BUILD-RESIDUAL",
                realm=self._request.name.value,
                packages=packages,
                reason=reason,
            )
            self._mutation.warnings.append(notice)

    def _aur_step(
        self, name: str, params: Mapping[str, Any] | None = None, *, drop: Sequence[str] = ()
    ) -> None:
        """A journal step of the AUR build (FR-043), with its journal-only params and the hook."""
        self._mutation.step(name, params, drop=drop)
        if self._hooks.on_step is not None:
            self._hooks.on_step(name)

    def _service_dir(self) -> None:
        """Write the runit service directory of a realm that the rebuild makes services-on.

        It is written before the start of gN+1, with the rollback that create registers for its own
        directory, so a failed swap removes it again. `down` is in it, so runsvdir starts the runsv
        but not the realm. The rebuild's own `sv up` starts gN+1, and after the swap the record's
        start does. An existing directory, while the record says services off, is left by an
        interrupted rebuild, and it is replaced. A rebuild that turns services off removes the
        directory after the swap (`_drop_service`), so nothing happens here for it.
        """
        if not self._plan.spec.services or self._current().services_at_create:
            return
        name = self._request.name
        paths = self._env.paths
        program = self._hooks.program
        if program is None or not os.path.isabs(program):
            err = make("GINN-E-INTERNAL")
            err.details.update({"detail": "no absolute ginn command for the service scripts"})
            raise err
        remove_service(paths, name)
        self._rollback.add("service directory", lambda: remove_service(paths, name))
        try:
            write_service_dir(paths, name, program)
        except OSError as exc:
            err = make("GINN-E-INTERNAL")
            err.details.update({"detail": f"runit service directory: {exc}"})
            raise err from exc
        if self._current().settings.autostart:
            autostart.apply(paths, name, True)
        self._step("service")

    def _stop_new(self) -> None:
        """Stop gN+1 before the swap, so the record never names a running root it did not start.

        A services-on gN+1 is stopped through its runit service, as a stop of the realm is
        (ADR-003), and its wait ends when the container is down.
        """
        cid = self._container_id()
        if self._plan.spec.services:
            stop_through_sv(
                self._env,
                self._podman,
                self._request.name,
                self._lifecycle,
                container_id=cid,
                timeout_s=STOP_TIMEOUT_S,
            )
        else:
            self._podman.stop(cid, STOP_TIMEOUT_S)
        self._step("stopped")

    def _swap(self) -> RealmRecord:
        """The record write that names gN+1, and the one that drops gN's create fields (DR-034).

        The building fields written before the start are cleared here, and so is the autostart flag
        that a services-off change displaced (`autostart_before_off`), since the change is applied.
        `swapping` is journaled
        before the save, so a failure at that step, or a crash, leaves the record at gN, which the
        rollback or recovery restores. Nothing after the save raises a GinnError, so the rollback
        never runs once the record names gN+1.
        """
        self._step("swapping")
        plan = self._plan
        attached = self._attached
        if attached is None:
            raise RuntimeError("the grants are attached before the swap")
        swapped = replace(
            self._current(),
            generation=plan.spec.generation,
            container_id=self._container_id(),
            image_ref=plan.spec.image,
            image_digest=plan.entry.digest,
            state=RealmState.STOPPED,
            previous_state=None,
            manifest=self._manifest,
            grants=list(attached.records),
            share_skipped=list(self._skips),
            created_options=list(plan.created),
            kvm_at_create=plan.kvm_present,
            gpu_cdi=plan.gpu_cdi,
            gpu_at_create=Toggle.GPU in plan.toggles,
            fonts_at_create=Toggle.FONTS_TIMEZONE in plan.toggles,
            services_at_create=plan.spec.services,
            building_container_id=None,
            building_generation=None,
            autostart_before_off=None,
            restart_required=False,
            updated_at=utc_from_epoch(self._env.clock()),
        )
        self._store.save(swapped)
        self._record = swapped
        try:
            sync_exec_service(self._env.paths, self._podman)
        except Exception as exc:  # noqa: BLE001 - the swap is saved, so this is a warning
            self._mutation.warnings.append(relay_failure(exc))
        return swapped


@dataclass(frozen=True)
class _Request:
    """The fixed inputs of one rebuild, read once before any write."""

    plan: _Plan
    request: CreateRequest
    old: RealmRecord


def _load(store: RealmStore, name: RealmName) -> RealmRecord:
    try:
        return store.load(name)
    except FileNotFoundError:
        raise make("GINN-E-NO-SUCH-REALM", realm=name.value) from None


def _refuse(record: RealmRecord) -> None:
    """Refuse a state that cannot rebuild. A services-on realm is rebuilt too (ADR-003)."""
    if record.state not in _REBUILDABLE:
        raise make(
            "GINN-E-REALM-STATE",
            realm=record.name.value,
            state=record.state.value,
            action="rebuild",
            allowed="stopped or running",
        )


def _fixed(
    env: OpEnv, record: RealmRecord, user: HostUser, group: int, hooks: RebuildHooks
) -> _Request:
    """The plan of a rebuild of `record`, checked before any write (FR-021).

    The request takes the record's tier, services mode and everything set, so the new generation has
    the create-time state the record asks for. The permissions are the record's own. The plan keeps
    only the everything names that the Install Manifest records, so a set that `ginn remove` took
    out is not installed again (FR-021, FR-014 AC-2).
    """
    request = CreateRequest(
        name=record.name,
        distro=record.settings.distro.value,
        uid=user.uid,
        gid=group,
        host_home=user.host_home,
        host_env=user.host_env,
        tier=record.settings.tier,
        services=record.settings.services,
        everything=record.settings.everything,
    )
    plan = _validate(
        env, request, _create_hooks(hooks), permissions=record.permissions, replay=True
    )
    recorded = {entry.name for entry in record.manifest}
    everything = tuple(name for name in plan.everything if name in recorded)
    plan = replace(
        plan,
        spec=replace(plan.spec, generation=record.generation + 1),
        everything=everything,
    )
    return _Request(plan=plan, request=request, old=record)


def _host_user(env: OpEnv) -> HostUser:
    """The host user of this process, as lifecycle's start reads it when no session is given."""
    return HostUser(uid=env.paths.uid, host_home=Path.home(), host_env=os.environ)


def _mark(mutation: Mutation, hooks: RebuildHooks, name: str) -> None:
    """A journal step of the rebuild's own body, with the hook that a test uses to stop there."""
    mutation.step(name)
    if hooks.on_step is not None:
        hooks.on_step(name)


def _relay_follows_failure(paths: OwnedPaths, podman: Podman, err: GinnError) -> None:
    """Sync the relay after a failed rebuild has returned its record to stopped (ADR-014).

    The sync is best effort. A failure is a GINN-W-RELAY-SYNC note on the error, and the error is
    still the rebuild's own, so the caller re-raises it as it was.
    """
    try:
        sync_exec_service(paths, podman)
    except Exception as exc:  # noqa: BLE001 - the rebuild has failed already, so this is a note
        warning = relay_failure(exc)
        err.add_note(f"{warning.code}: {warning.message}")


def _back_to_stopped(store: RealmStore, name: RealmName, old: RealmRecord) -> None:
    """Return a failed rebuild's record to `stopped`, unless its swap was saved (DR-034).

    A record that names gN+1 is the swap, and it stays. If the write fails, the record would stay
    at `rebuilding` with no journal entry left to settle it, so the failure is raised as a
    non-GinnError. That keeps the journal entry, and recovery returns the record to `stopped`.
    """
    try:
        current = store.load(name)
        if current.generation != old.generation or current.state is not RealmState.REBUILDING:
            return
        store.set_state(name, RealmState.STOPPED)
    except (GinnError, OSError) as exc:
        raise RuntimeError(
            f"the rebuild of {name.value} could not return its record to stopped; "
            "recovery will do it"
        ) from exc


def _retire(podman: Podman, name: RealmName, swapped: RealmRecord, mutation: Mutation) -> bool:
    """Remove every generation below the swapped one after the swap (DR-034).

    That is gN, and any older root that an earlier removal left behind, found by its labels. A
    failure is an audit note, never an error (DR-034).
    """
    try:
        remove_below(podman, name, swapped.generation, keep=swapped.container_id)
    except GinnError as err:
        mutation.add_params(old_remove_error=err.code)
        return False
    return True


def _drop_service(
    paths: OwnedPaths, name: RealmName, old: RealmRecord, swapped: RealmRecord, mutation: Mutation
) -> None:
    """Remove the runit service directory of a realm that the swap made services-off (ADR-003).

    It runs after the swap, so a failed or interrupted rebuild keeps the service that gN runs. The
    directory's `down` file keeps runit from starting the realm meanwhile. A failure is an audit
    note, as the removal of gN is (DR-034), and `ginn delete` removes the directory later.
    """
    if not old.services_at_create or swapped.services_at_create:
        return
    try:
        remove_service(paths, name)
    except OSError:
        mutation.add_params(service_dir_kept=True)


def _locked(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    fixed: _Request,
    hooks: RebuildHooks,
) -> _Outcome:
    """The steps of a rebuild inside `mutating("rebuild")` (DR-032, FR-123).

    A GinnError before the swap rolls gN+1 back, returns the record to `stopped`, and is raised.
    The rollback is the `_Create` undo, which removes only what this rebuild made. Once the swap is
    saved, nothing here raises a GinnError (see `_swap`).
    """
    store = RealmStore(env.paths, env.clock)
    old = fixed.old
    params = {
        "distro": old.settings.distro.value,
        "generation": old.generation,
        "new_generation": fixed.plan.spec.generation,
    }
    with mutating(env, OP_REBUILD, name, params) as mutation:
        current = _load(store, name)
        if current.state is not RealmState.STOPPED or current.generation != old.generation:
            raise make(
                "GINN-E-REALM-STATE",
                realm=name.value,
                state=current.state.value,
                action="rebuild",
                allowed="stopped or running",
            )
        store.set_state(name, RealmState.REBUILDING)
        rebuild = _Rebuild(
            env,
            podman,
            fixed.request,
            _create_hooks(hooks),
            fixed.plan,
            mutation,
            store.load(name),
            hooks.lifecycle,
        )
        try:
            # Inside the try, so a fault at this step also returns the record to stopped.
            _mark(mutation, hooks, "rebuilding")
            swapped = rebuild.run()
        except GinnError as err:
            for note in rebuild.undo():
                err.add_note(f"rollback: {note}")
            _back_to_stopped(store, name, old)
            _relay_follows_failure(env.paths, podman, err)
            raise
        except KeyboardInterrupt as exc:
            cancelled = make("GINN-E-INTERRUPTED")
            for note in rebuild.undo():
                cancelled.add_note(f"rollback: {note}")
            _back_to_stopped(store, name, old)
            _relay_follows_failure(env.paths, podman, cancelled)
            raise cancelled from exc
        for pkg in rebuild.replay_missing:
            mutation.warnings.append(warn("GINN-W-REPLAY-MISSING", package=pkg))
        old_removed = _retire(podman, name, swapped, mutation)
        _drop_service(env.paths, name, old, swapped, mutation)
        mutation.step("retired")
        mutation.add_params(
            replay_missing=list(rebuild.replay_missing),
            old_removed=old_removed,
        )
    return _Outcome(
        record=swapped,
        replay_missing=tuple(rebuild.replay_missing),
        old_removed=old_removed,
        warnings=tuple(mutation.warnings),
    )


def _restart(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    user: HostUser,
    hooks: RebuildHooks,
    notices: list[GinnWarning],
) -> None:
    """Start the realm again on the root its record names (AC-2, FR-021 AC-1).

    `rebuild=False`: the record already names the root that this rebuild left, so a start never
    rebuilds again inside a rebuild's own restart, and a failed rebuild restarts the old root.
    """
    start_realm(
        env,
        podman,
        name,
        session=user,
        hooks=hooks.lifecycle,
        warnings=notices,
        rebuild=False,
    )


def _sync(env: OpEnv, podman: Podman, name: RealmName, hooks: RebuildHooks) -> list[GinnWarning]:
    """Regenerate the realm's exported entries from gN+1, best effort (FR-021 AC-1, FR-044).

    A failed sync is a GINN-W-EXPORT-SYNC warning, as update's is. A sync reads only a running
    realm, so the caller runs it after the start.
    """
    sync_hooks = SyncHooks() if hooks.program is None else SyncHooks(program=hooks.program)
    try:
        result = sync_realm(env, podman, name, hooks=sync_hooks)
    except GinnError as err:
        return [warn("GINN-W-EXPORT-SYNC", realm=name.value, reason=err.message)]
    return list(result.warnings)


def _sync_stopped(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    user: HostUser,
    hooks: RebuildHooks,
    generation: int,
) -> list[GinnWarning]:
    """Sync the menu of a rebuilt realm that was stopped: start it, sync it, stop it (FR-021 AC-1).

    A sync reads only a running realm (FR-044), and the swap has already named gN+1. So the realm
    is started on it, synced, and stopped again, which leaves it stopped as it was. Each step takes
    its own lock. A start that fails is a GINN-W-EXPORT-SYNC warning, and the rebuild stands: the
    entries of packages that are gone stay until the next sync. A stop that fails leaves the realm
    running, so that error is raised with a note that the rebuild finished.
    """
    notices: list[GinnWarning] = []
    try:
        start_realm(
            env,
            podman,
            name,
            session=user,
            hooks=hooks.lifecycle,
            warnings=notices,
            rebuild=False,
        )
    except GinnError as err:
        return [warn("GINN-W-EXPORT-SYNC", realm=name.value, reason=err.message)]
    notices.extend(_sync(env, podman, name, hooks))
    try:
        stop_realm(env, podman, name, hooks=hooks.lifecycle, warnings=notices)
    except GinnError as err:
        err.add_note(
            f"the rebuild finished: generation {generation} is in place, "
            "and the realm was left running"
        )
        raise
    return notices


def rebuild_realm(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    session: HostUser | None = None,
    gid: int | None = None,
    hooks: RebuildHooks | None = None,
) -> RebuildResult:
    """Rebuild realm `name` from its pinned image, and return the record of generation gN+1.

    `session` is the host user, read from the process when None (as lifecycle reads it). `gid` is
    the host user's group, which the realm's passwd entry carries (AS-007); None is `os.getgid()`,
    the value the CLI passes to create. The plan is checked before anything is written. See the
    module docstring for the order, the refusals, the failure rule and the recovery.

    The image lock covers the stop and the rebuild. It is released before the restart and the sync,
    because those start and stop the realm, and each takes the realm lock and the image lock is
    not reentrant (GINN-E-REALM-BUSY). A failed rebuild of a running realm restarts it on gN.
    """
    hooks = RebuildHooks() if hooks is None else hooks
    store = RealmStore(env.paths, env.clock)
    record = _load(store, name)
    _refuse(record)
    user = _host_user(env) if session is None else session
    group = os.getgid() if gid is None else gid
    fixed = _fixed(env, record, user, group, hooks)
    was_running = record.state is RealmState.RUNNING
    notices: list[GinnWarning] = []
    image = f"rebuild of {record.settings.distro.value} image"
    try:
        with image_lock(env.paths, record.settings.distro.value, image):
            if was_running:
                stop_realm(env, podman, name, hooks=hooks.lifecycle, warnings=notices)
            outcome = _locked(env, podman, name, fixed, hooks)
    except GinnError as err:
        if was_running:
            _restart_quietly(env, podman, name, user, hooks, notices, err)
        raise
    notices.extend(outcome.warnings)
    if was_running:
        try:
            _restart(env, podman, name, user, hooks, notices)
        except GinnError as err:
            err.add_note(
                f"the rebuild finished: generation {outcome.record.generation} is in place, "
                "and the realm was left stopped"
            )
            raise
        notices.extend(_sync(env, podman, name, hooks))
    else:
        notices.extend(_sync_stopped(env, podman, name, user, hooks, outcome.record.generation))
    return RebuildResult(
        record=store.load(name),
        warnings=tuple(notices),
        replay_missing=outcome.replay_missing,
        old_removed=outcome.old_removed,
    )


def rebuild_for_start(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    session: HostUser,
    gid: int | None = None,
    hooks: RebuildHooks | None = None,
    warnings: list[GinnWarning] | None = None,
) -> RealmRecord:
    """Rebuild stopped realm `name` into the container its record asks for (FR-066, FR-083).

    A start calls this when the record's create-time state differs from the container's (lifecycle,
    `stale_container`). The rebuild stops nothing and starts nothing: the realm is stopped, and the
    caller starts the new generation after this returns. The record and the plan are read here, and
    the swap is the one write. The FR-006 and rebuild warnings are appended to `warnings`, and a
    failed rebuild rolls back and raises, as `rebuild_realm` does, with the old root kept.
    """
    hooks = RebuildHooks() if hooks is None else hooks
    store = RealmStore(env.paths, env.clock)
    record = _load(store, name)
    if record.state is not RealmState.STOPPED:
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state=record.state.value,
            action="rebuild",
            allowed="stopped",
        )
    group = os.getgid() if gid is None else gid
    fixed = _fixed(env, record, session, group, hooks)
    image = f"rebuild of {record.settings.distro.value} image"
    with image_lock(env.paths, record.settings.distro.value, image):
        outcome = _locked(env, podman, name, fixed, hooks)
    if warnings is not None:
        warnings.extend(outcome.warnings)
    return outcome.record


def _restart_quietly(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    user: HostUser,
    hooks: RebuildHooks,
    notices: list[GinnWarning],
    failure: GinnError,
) -> None:
    """Restart a running realm after a failed rebuild. A failed start is a note on the failure."""
    try:
        _restart(env, podman, name, user, hooks, notices)
    except GinnError as err:
        failure.add_note(f"the realm was left stopped: {err.code}: {err.message}")


__all__ = ["RebuildHooks", "RebuildResult", "rebuild_for_start", "rebuild_realm"]
