# SPDX-License-Identifier: Apache-2.0
"""Create a realm (FR-010, FR-011, FR-012, FR-013, FR-015, FR-120, FR-121, FR-122).

`create_realm` checks the request before anything is written, then runs the body inside
`mutating()` (AGENTS.md, DR-032, FR-123, FR-124). The body runs these phases, and each one is a
journal step. `CreateHooks.on_step` runs after each step is recorded.

- checked: the realm name is free. Neither a record nor a Realm Home exists (FR-010 AC-3).
- space-checked: free space in Owned Paths covers the base estimate times 1.10 (FR-121).
- recorded: the record is saved as `creating` (DR-020), with the pinned reference and digest. It is
  saved before the pull, so an interrupted create leaves a record that recovery can find (FR-122).
- homed: the Realm Home is made, mode 0700 (Owned Paths, DR-010, FR-048). A realm with nested
  containers also gets its seccomp profile under the Owned Path `seccomp` and its inner podman
  config in the Realm Home (ADR-006, security/seccomp.py, security/nested.py).
- pulled: the image is pulled by digest, with the FR-120 retries. Its digest must equal the pin
  (FR-011). A mismatch removes the image, and nothing is kept.
- created: `podman create` runs from the realm's spec, Basic with services off (FR-010 AC-1), or
  with services on (ADR-003, runtime/spec.py). The record is saved again with the container ID, so a
  crash cannot leave an unnamed container.
- services on: the create stops after `created`. Its container is made and never started: a
  services-on realm starts only through its runit entry (FR-081, ADR-003), which arrives in T7.3
  and T7.4. So this create runs no `podman start`, `exec` or `stop`, attaches no grant, and runs no
  user check, integration step or package step. The final record is saved as `stopped`, and
  GINN-W-SERVICES-PENDING says what this create left out (FR-010).
- started: the container starts, and its init process is read.
- attached: the variable grants are attached (ADR-008 amendment). A failed attach fails the create.
- user: the realm user is the passwd entry that podman wrote at create from `--passwd-entry`. No
  `useradd` runs. `getent passwd <uid>` must return that entry exactly, or the create fails with
  GINN-E-REALM-USER (FR-048, ADR-006 amendment of 2026-10-13, T4.9b).
- integrated: `refresh` runs once, then the Integration Package Set and its steps (FR-013,
  ADR-011, CL-9). The refresh is not conditional on `refresh_before`, because creation always
  refreshes (ADR-011).
- packages: the `--with` packages are installed (FR-010 AC-2). Signature checks stay on (FR-012).
- stopped: the container stops. Then the final record is saved as `stopped` (FR-010 AC-1).

Failure. A GINN-E error rolls back every artifact the create made, newest first (FR-120 AC-1,
FR-121, DR-018), and the same error is re-raised with each rollback note added. The record is
deleted last. If a rollback step fails, the record stays and moves to `broken` (FR-122), so the
realm can be recovered. KeyboardInterrupt is a cancel (AS-018). It rolls back the same way, and
then the body raises GINN-E-INTERRUPTED from it. That lets `mutating()` finish the journal entry.
A bare re-raise would keep the entry, and it would block the next create of the same name until
recovery (T2.8) exists. Any other exception rolls nothing back and keeps the journal entry, so
recovery can mark the realm `broken`. The rollback notes are attached to the error in both branches.

Undo. Each artifact is registered before the call that makes it, so an interrupt inside the call
still leaves something to undo. Each undo is idempotent. The container is found by the labels in
its spec, not by a kept id. The image is removed only when this create pulled it, and only while
no container uses it. The Realm Home is removed by the path the name check proved absent. The
record is removed only when this create claimed it, before its write.

Image lock. Every create of one distribution holds `image_lock` from before the body starts until
after its rollback, so the image rmi guard is never raced by a second create. A second create
of that distribution is refused at once with GINN-E-REALM-BUSY. Any later operation that pulls or
removes an image must take the same lock first.

Retries. `podman pull` retries a GINN-E-NETWORK itself (adapter `with_network_retry`). A package
step is retried the same way, but only when `podman exec` raises GINN-E-NETWORK, which is podman's
own failure (exit 125). A package manager's own download failure (apt exit 100, pacman and dnf
exit 1) is GINN-E-PKG-MANAGER and is not retried. Classifying those is T8.1. Each retry waits 5,
15 and 45 s (FR-120, AS-023).

Not in this module: `--resume` (T2.8), the network classifier for package managers (T8.1), the
disk module (T8.2), the `--with` closure and size estimate (the install verb resolves the closure
natively, and the output formats are not verified until M4), the Install Manifest (M5), and a CLI
command, since T4.4 has no CLI task.
"""

import contextlib
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from ginn.distros import Verb
from ginn.domain import (
    InstallMethod,
    InstallSource,
    RealmState,
    transition,
)
from ginn.domain.permissions import Toggle
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.ops._create_plan import (
    CreateHooks,
    CreateRequest,
    _Plan,
    _validate,
    estimate_bytes,
    required_bytes,
)
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops._rollback import Rollback
from ginn.ops.security_ops import write_nvidia
from ginn.packages.manager import ROOT_USER, run_step
from ginn.packages.manifest import installed
from ginn.podman.adapter import Podman
from ginn.podman.models import ContainerState
from ginn.runtime.attach import (
    AttachResult,
    admission,
    attach_at_start,
    records_from_plan,
    share_skipped,
)
from ginn.runtime.mounts import attach_plan
from ginn.runtime.spec import create_spec
from ginn.security.nested import write_config
from ginn.security.nvidia import Status
from ginn.security.seccomp import write as write_profile
from ginn.services.runit_user import remove_service, write_journald_dropin, write_service_dir
from ginn.store._time import utc_from_epoch
from ginn.store.locks import image_lock
from ginn.store.paths import OWNED_DIR_MODE
from ginn.store.records import RealmRecord, RealmStore

RETRY_DELAYS: Final[tuple[float, ...]] = (5.0, 15.0, 45.0)
"""Seconds before retries 1 to 3 of a network failure (FR-120, AS-023)."""
STOP_TIMEOUT_S: Final = 30
"""Seconds `podman stop` waits before it kills the realm (FR-016, AS-023)."""
ROLLBACK_STOP_TIMEOUT_S: Final = 0
"""A half-made realm has no state to save, so a rollback stop does not wait."""


@dataclass(frozen=True)
class CreateResult:
    """A created realm and the notices the create produced.

    `warnings` holds GINN-W-HOST-CHANGED from the after-check (FR-006). `locale_note` is set when
    the host LANG cannot be used, so the realm gets C.UTF-8 instead (FR-013, distros/loader.py).
    """

    record: RealmRecord
    warnings: list[GinnWarning]
    locale_note: str | None
    gpu_note: str | None = None


def _remove_tree(path: Path) -> None:
    """Remove a path this create made: a directory tree, or a file or link. Absent is no-op."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


class _Create:
    """The state of one create, from its first step to its last. `create_realm` drives it."""

    def __init__(
        self,
        env: OpEnv,
        podman: Podman,
        request: CreateRequest,
        hooks: CreateHooks,
        plan: _Plan,
        mutation: Mutation,
    ) -> None:
        self._env = env
        self._podman = podman
        self._request = request
        self._hooks = hooks
        self._plan = plan
        self._mutation = mutation
        self._store = RealmStore(env.paths, env.clock)
        self._home = env.paths.homes / request.name.value
        self._rollback = Rollback()
        self._record: RealmRecord | None = None
        self._owns_record = False
        self._cid: str | None = None
        self._pid = 0
        self._attached: AttachResult | None = None
        self._skips: list[str] = []
        self._locale_note: str | None = None

    @property
    def locale_note(self) -> str | None:
        return self._locale_note

    @property
    def gpu_note(self) -> str | None:
        """An informational note on the GPU grant, for musl or no driver (FR-069 AC-2)."""
        if self._plan.gpu.status is Status.READY:
            return None
        return f"GPU grant not offered: {self._plan.gpu.reason}"

    def run(self) -> RealmRecord:
        """Run every phase, in order. Returns the stopped record. Raises on the first failure."""
        self._check_name()
        self._check_space()
        self._save_creating()
        self._make_home()
        self._pull()
        self._create_container()
        if self._plan.spec.services:
            return self._finish_services()
        self._start()
        self._attach()
        self._check_user()
        self._integrate()
        self._install_with()
        return self._finish()

    def undo(self) -> list[str]:
        """Roll back what this create made. Return the notes of the undo steps that failed.

        The artifacts go newest first, and the record goes last. Each undo is idempotent, so an
        artifact that was never made is a no-op. A record that cannot be removed, or an artifact
        that cannot be undone, leaves the record in `broken` (FR-122, DR-020).
        """
        notes = self._rollback.run()
        if not self._owns_record or not self._store.exists(self._request.name):
            return notes
        if not notes:
            try:
                self._store.delete(self._request.name)
                return notes
            except Exception as exc:  # noqa: BLE001 - recorded, so the error still reaches the caller
                notes.append(f"record: {type(exc).__name__}: {exc}")
        try:
            self._store.set_state(self._request.name, RealmState.BROKEN)
        except Exception as exc:  # noqa: BLE001 - recorded, so the error still reaches the caller
            notes.append(f"record broken: {type(exc).__name__}: {exc}")
        return notes

    # Phases.

    def _check_name(self) -> None:
        name = self._request.name
        if self._store.exists(name) or os.path.lexists(self._home):
            raise make("GINN-E-NAME-TAKEN", name=name.value)
        self._step("checked")

    def _check_space(self) -> None:
        needed = required_bytes(self._plan.entry)
        available = self._hooks.disk_free(self._env.paths.data)
        if available < needed:
            raise make(
                "GINN-E-DISK-SPACE", needed=f"{needed} bytes", available=f"{available} bytes"
            )
        self._step("space-checked")

    def _save_creating(self) -> None:
        plan = self._plan
        now = utc_from_epoch(self._env.clock())
        record = RealmRecord(
            settings=plan.settings,
            state=RealmState.CREATING,
            previous_state=None,
            image_ref=plan.spec.image,
            image_digest=plan.entry.digest,
            container_id=None,
            generation=plan.spec.generation,
            created_at=now,
            updated_at=now,
            permissions=plan.permissions,
            created_options=list(plan.created),
            kvm_at_create=plan.kvm_present,
            gpu_cdi=plan.gpu_cdi,
            gpu_at_create=Toggle.GPU in plan.toggles,
            fonts_at_create=Toggle.FONTS_TIMEZONE in plan.toggles,
        )
        # Claimed before the write: an interrupt inside the write must still find the record.
        self._owns_record = True
        self._store.save(record)
        self._record = record
        self._step("recorded")

    def _make_home(self) -> None:
        self._home.parent.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
        # Registered before the mkdir. The name check has shown that the path is absent, under the
        # realm lock, so the undo removes only what this create made.
        self._rollback.add("realm home", lambda: _remove_tree(self._home))
        self._home.mkdir(mode=OWNED_DIR_MODE)
        self._write_tier_files()
        self._step("homed")

    def _write_tier_files(self) -> None:
        """Write the NVIDIA spec, and the seccomp profile and inner podman config of a nested realm.

        The NVIDIA spec and the ld drop-in are host files under the ginn data directory, written
        once per digest (ADR-012). They are shared, so the undo leaves them. The profile is a shared
        file under `data/seccomp`, named by the hash of its generated bytes, and the undo leaves it
        too. The config is in the Realm Home, which the undo removes.
        """
        if self._plan.gpu_cdi:
            write_nvidia(self._env.paths, self._plan.gpu)
        profile = self._plan.seccomp
        if profile is None:
            return
        write_profile(profile, self._env.paths.seccomp)
        write_config(self._home)

    def _pull(self) -> None:
        ref = self._plan.spec.image
        pulled_here = not self._podman.image_exists(ref)
        if pulled_here:
            # Registered before the pull, so an interrupt inside the pull still finds the image.
            self._rollback.add("image", self._remove_image)
        image = self._podman.pull(ref, retry_hook=self._retry_hook)
        if image.digest != self._plan.entry.digest:
            # The image goes whatever other users it has: a digest that does not match its pin is
            # not trusted. A non-forced rmi still refuses while a container uses it. That is noted.
            # The rollback's image undo is the guarded one: it skips an image in use, and skips an
            # image that is already gone.
            err = make("GINN-E-IMAGE-UNVERIFIED", distro=self._plan.definition.id)
            try:
                self._podman.rmi(ref)
            except GinnError as rm_err:
                err.add_note(f"image not removed: {rm_err.code}")
            raise err
        self._step("pulled")

    def _create_container(self) -> None:
        # Registered before the call. An interrupt inside the call can leave a container whose id
        # was never stored, and the undo finds it by its labels.
        self._rollback.add("container", self._remove_container)
        cid = self._podman.create(create_spec(self._plan.spec))
        self._cid = cid
        self._update(container_id=cid)
        self._step("created")

    def _write_service(self) -> None:
        """Write the realm's runit service directory and, when set, the journald drop-in.

        The undo is registered before the write, and it removes the directory only for this realm
        name, which the name check proved free. The drop-in is shared by all realms, so its undo
        leaves it in place (as the NVIDIA spec does).
        """
        program = self._hooks.program
        if program is None or not os.path.isabs(program):
            err = make("GINN-E-INTERNAL")
            err.details.update({"detail": "no absolute ginn command for the service scripts"})
            raise err
        paths = self._env.paths
        name = self._request.name
        self._rollback.add("service directory", lambda: remove_service(paths, name))
        try:
            if self._plan.spec.services_mitigations:
                write_journald_dropin(paths)
            write_service_dir(paths, name, program)
        except OSError as exc:
            # A GinnError rolls the create back, as any other failed step does (FR-120 AC-1).
            err = make("GINN-E-INTERNAL")
            err.details.update({"detail": f"runit service directory: {exc}"})
            raise err from exc
        self._step("service")

    def _start(self) -> None:
        cid = self._container_id()
        self._podman.start(cid)
        info = self._podman.inspect(cid)
        if info.state is not ContainerState.RUNNING or info.pid < 2:
            err = make("GINN-E-INTERNAL")
            err.details.update(
                {"detail": "the realm is not running after start", "state": info.state.value}
            )
            raise err
        self._pid = info.pid
        self._step("started")

    def _attach(self) -> None:
        plan = self._plan
        env = self._request.host_env
        grants = attach_plan(
            plan.settings.tier,
            host_home=self._request.host_home,
            uid=self._request.uid,
            user=plan.user,
            owned_paths=plan.owned,
            runtime_root=self._env.paths.runtime,
            session_binds=plan.session.binds,
            env=env,
            exists=self._hooks.exists,
            permissions=plan.permissions,
        )
        result = attach_at_start(
            self._podman,
            self._container_id(),
            pid=self._pid,
            grants=records_from_plan(grants, plan.session),
            session=plan.session,
            realm_home=self._home,
            host_home=self._request.host_home,
            admit=admission(
                home=self._request.host_home,
                owned_paths=plan.owned,
                runtime_root=self._env.paths.runtime,
                env=env,
            ),
            helper=self._hooks.helper,
            mountinfo=self._hooks.mountinfo,
        )
        self._attached = result
        self._skips = share_skipped(grants, plan.session, result)
        self._step("attached")

    def _check_user(self) -> None:
        """Check that the realm user is the passwd entry ginn sent (FR-048, ADR-006 amendment).

        Podman writes the entry at create, from `--passwd-entry` (T4.9b), and ginn runs no
        `useradd`. The lookup must return that entry exactly. A missing entry (exit 2), a missing
        getent (exit 127) or a different entry fails the create, and the rollback removes the
        realm. The uid is the host user's, so it is the lookup key.
        """
        expected = self._plan.spec.passwd_entry
        result = self._podman.exec(
            self._container_id(), ["getent", "passwd", str(self._request.uid)], user=ROOT_USER
        )
        found = result.stdout.strip()
        if result.exit_code != 0 or found != expected:
            if result.exit_code != 0:
                detail = f"getent passwd exited {result.exit_code}"
            else:
                detail = f"found {found!r}, expected {expected!r}"
            err = make("GINN-E-REALM-USER", realm=self._request.name.value, detail=detail)
            err.details.update({"expected": expected, "found": found, "exit": result.exit_code})
            raise err
        self._step("user")

    def _integrate(self) -> None:
        definition = self._plan.definition
        integration = definition.integration_set(self._request.host_env.get("LANG"))
        if not self._hooks.integrate:
            # Test seam (CreateHooks.integrate, T4.9): no refresh, no package and no step runs.
            self._step("integrated")
            return
        self._locale_note = integration.fallback_reason
        self._exec(definition.argv(Verb.REFRESH), step="refresh")
        if integration.packages:
            argv = definition.argv(Verb.INSTALL, integration.packages)
            self._exec(argv, step="integration")
        for step in integration.steps:
            self._exec(list(step), step="locale")
        self._step("integrated")

    def _install_with(self) -> None:
        packages = self._plan.packages
        if packages:
            argv = self._plan.definition.argv(Verb.INSTALL, packages)
            self._exec(argv, step="with")
        self._step("packages")

    def _finish_services(self) -> RealmRecord:
        """Finish a services-on create with no start (ADR-003, FR-081).

        The container is made and left stopped. Its start goes through the runit entry, so nothing
        runs in it here. The runit service directory is written with `down` present, so runit does
        not start the realm until it is turned on (DR-012). The Install Manifest is left as it is,
        because no package is installed. The notice names what this create did not do.
        """
        self._write_service()
        notice = warn("GINN-W-SERVICES-PENDING", realm=self._request.name.value)
        self._mutation.warnings.append(notice)
        self._step("stopped")
        record = self._current()
        transition(record.state, RealmState.STOPPED, record.previous_state)
        self._update(state=RealmState.STOPPED)
        return self._current()

    def _finish(self) -> RealmRecord:
        self._podman.stop(self._container_id(), STOP_TIMEOUT_S)
        self._step("stopped")
        record = self._current()
        transition(record.state, RealmState.STOPPED, record.previous_state)
        attached = self._attached
        if attached is None:
            raise RuntimeError("the grants are attached before the create is finished")
        self._update(
            state=RealmState.STOPPED,
            grants=list(attached.records),
            share_skipped=list(self._skips),
            restart_required=False,
            manifest=installed(
                record.manifest,
                list(self._plan.packages),
                source=InstallSource.REPO,
                method=InstallMethod.GINN,
            ),
        )
        return self._current()

    # Rollback callables. Each one undoes exactly one artifact.

    def _remove_image(self) -> None:
        """Remove the image this create pulled. Absent, or in use by a container, is a no-op."""
        ref = self._plan.spec.image
        if not self._podman.image_exists(ref):
            return
        if any(info.image == ref for info in self._podman.ps()):
            return  # another container still uses the image, so it stays
        self._podman.rmi(ref)

    def _remove_container(self) -> None:
        """Stop and force-remove this realm's container. Absent is a no-op.

        Containers are found by the labels the spec gives every ginn container (T4.1), not by a
        stored id, so this works even when an interrupt came before the id was kept.
        """
        for info in self._podman.ps(self._plan.spec.labels):
            with contextlib.suppress(GinnError):
                self._podman.stop(
                    info.id, ROLLBACK_STOP_TIMEOUT_S
                )  # never started: nothing to stop
            self._podman.rm(info.id, force=True)

    # Helpers.

    def _step(self, name: str) -> None:
        self._mutation.step(name)
        if self._hooks.on_step is not None:
            self._hooks.on_step(name)

    def _retry_hook(self, attempt: int, err: GinnError) -> bool:
        """Wait the FR-120 delay for failed attempt `attempt`, then retry (AS-023)."""
        if attempt > len(RETRY_DELAYS):
            return False
        self._hooks.sleep(RETRY_DELAYS[attempt - 1])
        return True

    def _container_id(self) -> str:
        if self._cid is None:
            raise RuntimeError("the container exists before any step that runs in it")
        return self._cid

    def _current(self) -> RealmRecord:
        if self._record is None:
            raise RuntimeError("the record is saved before any step that updates it")
        return self._record

    def _update(self, **changes: Any) -> None:
        now = utc_from_epoch(self._env.clock())
        record = replace(self._current(), updated_at=now, **changes)
        self._store.save(record)
        self._record = record

    def _exec(self, argv: list[str], *, step: str) -> None:
        """Run a package step as container root, with its failure mapped (FR-012, FR-040).

        The call and its mapping are `packages/manager.py` `run_step`, shared with install and
        remove, so a create step and an install step fail the same way.
        """
        run_step(
            self._podman,
            self._container_id(),
            self._plan.definition,
            argv,
            realm=self._request.name.value,
            step=step,
            retry_hook=self._retry_hook,
        )


def create_realm(
    env: OpEnv,
    podman: Podman,
    request: CreateRequest,
    *,
    hooks: CreateHooks | None = None,
) -> CreateResult:
    """Create realm `request.name` as a stopped Basic realm with services off (FR-010).

    Validation runs first, and it writes nothing. The body runs inside `mutating()`. A GINN-E error
    from the body rolls back the body's artifacts and is re-raised. See the module docstring.
    """
    hooks = CreateHooks() if hooks is None else hooks
    plan = _validate(env, request, hooks)
    params: dict[str, object] = {
        "distro": plan.definition.id,
        "tier": request.tier.value,
        "services": request.services,
        "with": list(plan.packages),
    }
    image = f"create of {plan.definition.id} image"
    with (
        image_lock(env.paths, plan.definition.id, image),
        mutating(env, "create", request.name, params) as mutation,
    ):
        create = _Create(env, podman, request, hooks, plan, mutation)
        try:
            record = create.run()
        except GinnError as err:
            for note in create.undo():
                err.add_note(f"rollback: {note}")
            raise
        except KeyboardInterrupt as exc:
            cancelled = make("GINN-E-INTERRUPTED")
            for note in create.undo():
                cancelled.add_note(f"rollback: {note}")
            raise cancelled from exc
    return CreateResult(
        record=record,
        warnings=list(mutation.warnings),
        locale_note=create.locale_note,
        gpu_note=create.gpu_note,
    )


__all__ = [
    "CreateHooks",
    "CreateRequest",
    "CreateResult",
    "create_realm",
    "estimate_bytes",
    "required_bytes",
]
