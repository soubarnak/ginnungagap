# SPDX-License-Identifier: Apache-2.0
"""Resume an interrupted create: `ginn create --resume <realm>` (FR-122, DR-020).

A create that is killed, or whose rollback fails, leaves its realm `broken`. The record keeps the
request that made it (`create_pending`, `with_at_create` and `everything_confirmed`, in
store/records.py). The create's `recorded` save sets them, and the save that writes `stopped` clears
them. Recovery removes the journal entry, but the record keeps the request, so `resume_create` can
finish the create.

Order of a resume:

1. Before any write (`resume_create`). The record must read `broken` with `create_pending`, or the
   refusal is GINN-E-REALM-STATE. A missing realm is GINN-E-NO-SUCH-REALM. A Kali everything set
   that the create did not confirm needs a prompt, so with no `hooks.confirm` it is GINN-E-USAGE.
   The plan is checked by `_validate`, with the record's tier, permissions, services mode,
   everything set and packages, and with the current host, pinned image and Trust Manifest.
2. Inside `mutating("create")`, under the realm lock (`_Resume._check_name`). The record is checked
   again. A Realm Home that is a link or a file is refused with GINN-E-INTERNAL.
3. The create phases run, with the step names that create records:
   - `space-checked`: the free-space check runs again. When the pinned image is already in storage,
     nothing is downloaded, so a services-off create's base estimate is not checked (FR-121). A
     services-on create checks the disk formula of ADR-015 Decision 11, without its base term then.
   - `cleared` (`_Resume._clear`): a services-on realm is told `d` first, so runsv does not restart
     a container that is removed (ADR-003). Every container of the generation is stopped and removed
     by its labels, and the runit directory is removed, since the create writes it again. A
     services-on resume then removes the provisioning leftovers by ID: the provisioning containers,
     then the derived and stage images, with the record's keep set (ADR-015 Decisions 12 and 13).
     The image of an older pin is removed when no container uses it, best effort (FR-011). The
     record still reads `broken` here.
   - `recorded` (`_Resume._save_creating`): the record moves `broken` to `creating` (DR-020), with
     the current pin and the create-time fields of the new plan. The pending request is kept.
   - `homed` (`_Resume._make_home`): the Realm Home is kept, or made when the crash came before it.
     Its tier files are written again, and each writer repairs what it finds.
   - `pulled`, `created`, `service`, `started` and the rest: as create. The image is pulled by its
     digest, and the container is made fresh. A services-on realm is never started (ADR-003).
   - `estimated`: a Kali everything set that was confirmed is not asked again.
   - `packages`: the whole `--with` list is installed. The Install Manifest is written only when the
     create finishes, so no listed package is known to be installed. Installing one that is present
     changes nothing.
4. Failure. The rollback removes the container of this generation, and the runit directory that this
   resume wrote. The image is removed only when this resume pulled it, as create does. The record
   moves back to `broken`, with its pending request, and it is never deleted. The Realm Home stays.
   A resume that is killed is settled by recovery as an interrupted create (ops/recovery.py).

Why redo from `pulled`. No step between `created` and `stopped` writes the record, so after a crash
the progress of the container and of its packages is not known. The resume rebuilds the container
from the pinned image. It keeps only what is safe to keep: the Realm Home, which is user data and
whose partial artifacts stay (FR-122), the pending request, and the Kali consent.

The journal op is `create`, with `resume: True` in its params. So an interrupted resume is settled
by the create verdicts of ops/recovery.py, and needs no verdict of its own.
"""

import contextlib
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from ginn.domain import RealmName, RealmState, transition
from ginn.domain.permissions import Toggle
from ginn.errors import GinnError, make
from ginn.ops._create_plan import CreateHooks, CreateRequest, _validate
from ginn.ops._mutating import OpEnv
from ginn.ops.create import CreateResult, _Create, run_journaled
from ginn.ops.delete import remove_image
from ginn.ops.provision import remove_derived, require_storage_layers
from ginn.podman.adapter import Podman
from ginn.services.runit_user import remove_service, service_dir
from ginn.services.supervise import signal_runsv_down
from ginn.store._time import utc_from_epoch
from ginn.store.paths import OWNED_DIR_MODE
from ginn.store.records import RealmRecord, RealmStore

_CONSENT_NEEDED: str = (
    "--everything needs a confirmation: answer the prompt on a terminal, or pass --yes"
)


def resume_create(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    uid: int,
    gid: int,
    host_home: Path,
    host_env: Mapping[str, str],
    hooks: CreateHooks | None = None,
) -> CreateResult:
    """Finish the interrupted create of realm `name` from the request its record keeps (FR-122).

    `uid`, `gid`, `host_home` and `host_env` are the host user's, as a create's request has them.
    Every other input comes from the record. The refusals of step 1 are raised before any write.
    """
    hooks = CreateHooks() if hooks is None else hooks
    record = _resumable(RealmStore(env.paths, env.clock), name)
    _home_is_a_directory(env.paths.homes / name.value)
    if record.settings.everything and not record.everything_confirmed and hooks.confirm is None:
        raise make("GINN-E-USAGE", detail=_CONSENT_NEEDED)
    request = CreateRequest(
        name=name,
        distro=record.settings.distro.value,
        uid=uid,
        gid=gid,
        host_home=host_home,
        host_env=host_env,
        tier=record.settings.tier,
        services=record.settings.services,
        everything=record.settings.everything,
        with_packages=tuple(record.with_at_create),
    )
    plan = _validate(env, request, hooks, permissions=record.permissions)
    # The generation is the one that the broken record names, so the container removed is the one
    # of that generation (ops/create.py `_remove_container`).
    plan = replace(plan, spec=replace(plan.spec, generation=record.generation))
    params: dict[str, Any] = {
        "distro": plan.definition.id,
        "tier": request.tier.value,
        "services": request.services,
        "with": list(plan.packages),
        "everything": list(plan.everything),
        "resume": True,
    }
    return run_journaled(
        env,
        plan,
        name,
        params,
        lambda mutation: _Resume(env, podman, request, hooks, plan, mutation),
        resumed=True,
    )


def _home_is_a_directory(home: Path) -> None:
    """Refuse a Realm Home that is a link or a file, with GINN-E-INTERNAL (DR-021, FR-122).

    It is checked before the plan, since the plan resolves the Realm Home and refuses a link that
    leaves Owned Paths with a plain error. Nothing is written.
    """
    if os.path.lexists(home) and (home.is_symlink() or not home.is_dir()):
        err = make("GINN-E-INTERNAL")
        err.details.update({"detail": "the realm home is not a directory; no create resumes"})
        raise err


def _resumable(store: RealmStore, name: RealmName) -> RealmRecord:
    """The record of a realm that `create --resume` can finish, or the refusal (FR-122).

    A record that reads broken with a pending create is resumable. Any other record is refused with
    GINN-E-REALM-STATE. A broken record without a pending create has no request to resume from. An
    update may have left it broken, or an interrupted create from before `create_pending` existed
    (CL-21). Its remedy is `ginn delete`, which the note says. Nothing is written.
    """
    record = store.load(name)
    if record.state is RealmState.BROKEN and record.create_pending:
        return record
    err = make(
        "GINN-E-REALM-STATE",
        realm=name.value,
        state=record.state.value,
        action="create --resume",
        allowed="broken from an interrupted create",
    )
    if record.state is RealmState.BROKEN:
        err.add_note("no resumable create is recorded for this realm; 'ginn delete' removes it")
    raise err


class _Resume(_Create):
    """The phases of one resume, on top of the create phases (ops/create.py).

    The methods below replace the create phase of the same job. The rest of the create run is
    inherited, so a resume records the same steps as a create. Recovery and the progress lines see
    no difference.
    """

    _moved: bool = False
    """True once the record has been saved as creating. Only then does the undo write `broken`."""

    def _check_name(self) -> None:
        """`checked`: the record is still a pending broken create, under the realm lock (FR-122)."""
        self._record = _resumable(self._store, self._request.name)
        _home_is_a_directory(self._home)
        self._step("checked")

    def _clear(self) -> None:
        """`cleared`: remove what the interrupted create left, which the resume writes again.

        A services-on realm is told `d` first, so runsv does not restart a container that is
        removed (ADR-003). A runsv that is not running is not an error (services/supervise.py).
        The containers of the generation are then stopped and removed by their labels, which also
        finds a container whose id was never stored. The runit directory is removed too, since
        `service` writes it again. The provisioning leftovers go next, by ID
        (`_remove_leftover_images`). The image of an older pin goes last
        (`_remove_superseded_image`).
        """
        paths = self._env.paths
        name = self._request.name
        services = self._plan.spec.services
        try:
            if services:
                signal_runsv_down(service_dir(paths, name))
            self._remove_container()
            if services:
                remove_service(paths, name)
        except OSError as exc:
            err = make("GINN-E-INTERNAL")
            err.details.update({"detail": f"runit service directory: {exc}"})
            raise err from exc
        if services:
            self._remove_leftover_images()
        self._remove_superseded_image()
        self._step("cleared")

    def _remove_leftover_images(self) -> None:
        """Remove the provisioning leftovers of the killed create, by ID, before the redo (FR-122).

        The provisioning containers go first, then every derived and stage image of the realm
        (ADR-015 Decision 12). The keep set is the record's `init_image_id` and
        `building_init_image_id`, which a create that did not finish leaves empty. An image that
        stays for any other reason is refused with GINN-E-INTERNAL, before the stage is redone.
        """
        record = self._current()
        keep = (record.init_image_id, record.building_init_image_id)
        removal = remove_derived(self._podman, self._request.name, keep)
        stays = [image_id for image_id in removal.kept if image_id not in keep]
        if stays:
            err = make("GINN-E-INTERNAL")
            err.details.update(
                {"detail": f"a leftover of the killed create stays: {'; '.join(removal.notes)}"}
            )
            raise err

    def _remove_superseded_image(self) -> None:
        """Remove the image that the killed create pulled, when the pin has changed since (FR-011).

        The record names the image that its create pulled (`image_ref`). When the Trust Manifest now
        pins another digest, no container of this realm uses that image. It is removed as delete
        removes an image: not while a container uses it, and best effort, so a failed removal does
        not fail the resume. An image that is already gone is a no-op.
        """
        old = self._current().image_ref
        if not old or old == self._plan.spec.image:
            return
        with contextlib.suppress(GinnError):
            remove_image(self._podman, old)

    def _check_space(self) -> None:
        """`space-checked`: a services-off resume with its image in storage checks nothing.

        A services-off resume whose pinned image is already in storage downloads nothing: its layers
        are local, and the requirement is the base estimate alone (FR-121). The step is recorded as
        create records it. A missing image is downloaded, and then the full check runs.

        A services-on resume provisions again, so the stage's terms are always checked (ADR-015
        Decision 11). Only the pull is left out when the image is in storage. The keep-id copy of
        the base and the flattened copy of the derived image still count. A create has no reuse
        branch, since it always provisions, so no reuse term applies here. The measured layers are
        checked before the first podman call (`require_storage_layers`).
        """
        if self._plan.spec.services:
            require_storage_layers(self._plan.definition)
        local = self._podman.image_exists(self._plan.spec.image)
        if local and not self._plan.spec.services:
            self._step("space-checked")
            return
        self._check_space_for(base_in_storage=local)

    def _save_creating(self) -> None:
        """`recorded`: the record moves from broken to creating, with the current pin (DR-020).

        The move is checked by `transition` before anything is removed. The pending request and
        the Kali consent are kept. The create-time fields are the plan's, since the new container is
        made from it. `_moved` is set before the save, so a failed save still reads as a move.
        """
        record = self._current()
        transition(record.state, RealmState.CREATING, record.previous_state)
        self._clear()
        plan = self._plan
        now = utc_from_epoch(self._env.clock())
        creating = replace(
            record,
            state=RealmState.CREATING,
            previous_state=None,
            image_ref=plan.spec.image,
            image_digest=plan.entry.digest,
            container_id=None,
            generation=plan.spec.generation,
            updated_at=now,
            permissions=plan.permissions,
            created_options=list(plan.created),
            kvm_at_create=plan.kvm_present,
            gpu_cdi=plan.gpu_cdi,
            gpu_at_create=Toggle.GPU in plan.toggles,
            fonts_at_create=Toggle.FONTS_TIMEZONE in plan.toggles,
        )
        self._moved = True
        self._store.save(creating)
        self._record = creating
        self._step("recorded")

    def _make_home(self) -> None:
        """`homed`: the Realm Home is kept, or made when the crash came before it (FR-122).

        No rollback removes it, so a failed resume leaves the Realm Home as it was. Its tier files
        are written again, and each writer repairs what it finds (security/nested.py,
        security/seccomp.py, ops/security_ops.py).
        """
        self._home.parent.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
        self._home.mkdir(mode=OWNED_DIR_MODE, exist_ok=True)
        self._write_tier_files()
        self._step("homed")

    def _confirm_everything(self, prompt: str) -> bool:
        """A Kali everything set that the create confirmed is not asked again (FR-014, FR-122)."""
        if self._current().everything_confirmed:
            return True
        return super()._confirm_everything(prompt)

    def undo(self) -> list[str]:
        """Roll back what this resume made. Return the notes of the undo steps that failed.

        The record goes back to `broken` with its pending request, and it is never deleted. A record
        that was never moved is left as it is. A failed write of the record is a note, so the error
        still reaches the caller.
        """
        notes = self._rollback.run()
        if not self._moved:
            return notes
        try:
            current = self._store.load(self._request.name)
            if current.state is RealmState.CREATING:
                self._store.set_state(self._request.name, RealmState.BROKEN)
        except Exception as exc:  # noqa: BLE001 - recorded, so the error still reaches the caller
            notes.append(f"record broken: {type(exc).__name__}: {exc}")
        return notes


__all__ = ["resume_create"]
