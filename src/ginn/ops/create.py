# SPDX-License-Identifier: Apache-2.0
"""Create a realm (FR-010, FR-011, FR-012, FR-013, FR-015, FR-120, FR-121, FR-122).

`create_realm` checks the request before anything is written, then runs the body inside
`mutating()` (AGENTS.md, DR-032, FR-123, FR-124). The body runs these phases, and each one is a
journal step. `CreateHooks.on_step` runs after each step is recorded.

- checked: the realm name is free. Neither a record nor a Realm Home exists (FR-010 AC-3).
- space-checked: free space in the podman root and the Realm Home filesystems covers the base
  estimate times 1.10, rounded up (FR-121, ops/disk.py).
- recorded: the record is saved as `creating` (DR-020), with the pinned reference and digest. It is
  saved before the pull, so an interrupted create leaves a record that recovery can find (FR-122).
  It also keeps the request that `create --resume` needs: `create_pending`, the `--with` packages
  and, once the Kali prompt is answered yes, `everything_confirmed` (store/records.py).
- homed: the Realm Home is made, mode 0700 (Owned Paths, DR-010, FR-048). A realm with nested
  containers also gets its seccomp profile under the Owned Path `seccomp` and its inner podman
  config in the Realm Home (ADR-006, security/seccomp.py, security/nested.py).
- pulled: the image is pulled by digest, with the FR-120 retries. Its digest must equal the pin
  (FR-011). A mismatch removes the image, and nothing is kept.
- created: `podman create` runs from the realm's spec, Basic with services off (FR-010 AC-1), or
  with services on (ADR-003, runtime/spec.py). The record is saved again with the container ID, so a
  crash cannot leave an unnamed container. A services-on container then gets two checks before its
  first start: the image must hold `/sbin/init` (GINN-E-NO-INIT, F3), and the realm's machine-id is
  copied into `/etc/machine-id` with `podman cp` (ADR-007, F6). The id is kept on the record, so a
  rebuild writes the same one.
- services on: the create stops after `created`. Its container is made and never started: a
  services-on realm starts only through its runit entry (FR-081, ADR-003), and the CLI never starts
  it. So this create runs no `podman start`, `exec` or `stop`, attaches no grant, and runs no user
  check, integration step or package step. The final record is saved as `stopped`, and
  GINN-W-SERVICES-PENDING says what this create left out (FR-010).
- started: the container starts, and its init process is read.
- attached: the variable grants are attached (ADR-008 amendment). A failed attach fails the create.
- user: the realm user is the passwd entry that podman wrote at create from `--passwd-entry`. No
  `useradd` runs. `getent passwd <uid>` must return that entry exactly, or the create fails with
  GINN-E-REALM-USER (FR-048, ADR-006 amendment of 2026-10-13, T4.9b).
- integrated: `refresh` runs once, then the Integration Package Set and its steps (FR-013,
  ADR-011, CL-9). The refresh is not conditional on `refresh_before`, because creation always
  refreshes (ADR-011).
- everything: only with `--everything` (Kali, FR-014). `resolve` and `estimate` read the closure
  of the everything metapackage from the refreshed lists. Then the space check runs, with the
  estimate times 1.10. Then `confirm` answers the prompt, and only a yes installs the set. A short
  disk or a no raises, and the rollback removes the realm. The step is `estimated` before the
  install and `everything` after it.
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
exit 1) is GINN-E-PKG-MANAGER and is not retried. Each retry waits 5, 15 and 45 s (FR-120,
AS-023). The waits and the hook are ops/retry.py's (`network_retry_hook`, T8.1).

`create --resume` is ops/resume.py (T8.5). It is a subclass of `_Create` that runs the same phases
from the request kept in the record, and `run_journaled` below is the journaled body both share.

Not in this module: the classifier of package-manager output (ops/retry.py and packages/manager.py),
and the Install Manifest (M5).
"""

import contextlib
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final

from ginn.distros import Verb
from ginn.domain import (
    InstallMethod,
    InstallSource,
    RealmName,
    RealmState,
    transition,
)
from ginn.domain.permissions import Toggle
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.ops._create_plan import (
    CreateHooks,
    CreateRequest,
    SizeEstimate,
    _Plan,
    _validate,
    estimate_bytes,
    human_bytes,
    required_bytes,
)
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops._rollback import Rollback
from ginn.ops.disk import check_free_paths, estimate_required
from ginn.ops.estimate import measure_everything
from ginn.ops.images import image_names, uses_image
from ginn.ops.retry import RETRY_DELAYS, network_retry_hook
from ginn.ops.security_ops import write_nvidia
from ginn.packages.manager import ROOT_USER, run_step
from ginn.packages.manifest import installed
from ginn.podman.adapter import PATH_NOT_FOUND_REASON, ExecTarget, Podman
from ginn.podman.models import ContainerState
from ginn.runtime.attach import (
    AttachResult,
    admission,
    attach_at_start,
    records_from_plan,
    share_skipped,
)
from ginn.runtime.mounts import attach_plan
from ginn.runtime.spec import (
    INIT_PATH,
    MACHINE_ID_PATH,
    RUN_USER_TMPFILES_PATH,
    create_spec,
    new_machine_id,
    run_user_tmpfiles,
)
from ginn.security.nested import write_config
from ginn.security.nvidia import Status
from ginn.security.seccomp import write as write_profile
from ginn.services.exec_relay import relay_failure, sync_exec_service
from ginn.services.runit_user import remove_service, write_journald_dropin, write_service_dir
from ginn.store._time import utc_from_epoch
from ginn.store.locks import image_lock
from ginn.store.paths import OWNED_DIR_MODE
from ginn.store.records import RealmRecord, RealmStore
from ginn.store.scratch import scratch_dir

STOP_TIMEOUT_S: Final = 30
"""Seconds `podman stop` waits before it kills the realm (FR-016, AS-023)."""
ROLLBACK_STOP_TIMEOUT_S: Final = 0
"""A half-made realm has no state to save, so a rollback stop does not wait."""


@dataclass(frozen=True)
class CreateResult:
    """A created realm and the notices the create produced.

    `warnings` holds GINN-W-HOST-CHANGED from the after-check (FR-006). `locale_note` is set when
    the host LANG cannot be used, so the realm gets C.UTF-8 instead (FR-013, distros/loader.py).
    `estimate` is the size of the everything set that was confirmed, or None (FR-014). `resumed` is
    True when the result comes from `create --resume` (ops/resume.py).
    """

    record: RealmRecord
    warnings: list[GinnWarning]
    locale_note: str | None
    gpu_note: str | None = None
    estimate: SizeEstimate | None = None
    resumed: bool = False


def _remove_tree(path: Path) -> None:
    """Remove a path this create made: a directory tree, or a file or link. Absent is no-op."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _scratch_failure(exc: OSError) -> GinnError:
    """The GINN-E-INTERNAL of a scratch directory that cannot be made, written or read (F3, F6)."""
    err = make("GINN-E-INTERNAL")
    err.details.update({"detail": f"create scratch directory: {exc.strerror or exc}"})
    return err


def _drop_in_failure(cause: OSError | GinnError) -> GinnError:
    """The GINN-E-INTERNAL of the tmpfiles.d drop-in step, with its cause named (T7.28)."""
    if isinstance(cause, OSError):
        reason = cause.strerror or str(cause)
    else:
        reason = f"{cause.code} {cause.message}"
    err = make("GINN-E-INTERNAL")
    err.details.update({"detail": f"tmpfiles.d drop-in: {reason}"})
    return err


def _cleared_request() -> dict[str, Any]:
    """The fields that the save writing `stopped` clears, as the create is no longer pending."""
    return {"create_pending": False, "with_at_create": [], "everything_confirmed": False}


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
        self._estimate: SizeEstimate | None = None
        self._retry = network_retry_hook(hooks.sleep)

    @property
    def locale_note(self) -> str | None:
        return self._locale_note

    @property
    def estimate(self) -> SizeEstimate | None:
        return self._estimate

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
        self._everything()
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
            except Exception as exc:  # noqa: BLE001 - recorded, so the error still reaches the caller
                notes.append(f"record: {type(exc).__name__}: {exc}")
            else:
                self._sync_relay_after_undo(notes)
                return notes
        try:
            self._store.set_state(self._request.name, RealmState.BROKEN)
        except Exception as exc:  # noqa: BLE001 - recorded, so the error still reaches the caller
            notes.append(f"record broken: {type(exc).__name__}: {exc}")
        self._sync_relay_after_undo(notes)
        return notes

    def _sync_relay_after_undo(self, notes: list[str]) -> None:
        """Let the relay service follow the records after the undo (ADR-014). A failure is noted."""
        try:
            sync_exec_service(self._env.paths, self._podman)
        except Exception as exc:  # noqa: BLE001 - recorded, so the error still reaches the caller
            notes.append(f"relay service: {type(exc).__name__}: {exc}")

    # Phases.

    def _check_name(self) -> None:
        name = self._request.name
        if self._store.exists(name) or os.path.lexists(self._home):
            raise make("GINN-E-NAME-TAKEN", name=name.value)
        self._step("checked")

    def _check_space(self) -> None:
        """FR-121: the base estimate times 1.10 must fit on each filesystem the create fills."""
        paths = self._env.paths
        check_free_paths(
            [paths.podman_root, paths.homes],
            required_bytes(self._plan.entry),
            statvfs=self._hooks.statvfs,
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
            create_pending=True,
            with_at_create=list(plan.packages),
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
        image = self._podman.pull(ref, retry_hook=self._retry)
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
        # Podman reads a bind source when the container is created, so the journald drop-in (ADR-007
        # a) must exist before the call. The drop-in is shared by all realms, so no undo removes it.
        if self._plan.spec.services_mitigations:
            self._write_journald_dropin()
        # Registered before the call. An interrupt inside the call can leave a container whose id
        # was never stored, and the undo finds it by its labels.
        self._rollback.add("container", self._remove_container)
        cid = self._podman.create(create_spec(self._plan.spec))
        self._cid = cid
        self._update(container_id=cid)
        if self._plan.spec.services:
            self._prepare_services(cid)
        self._step("created")

    def _prepare_services(self, cid: str) -> None:
        """Check that the image has systemd, give the container its machine-id (F3, F6), and its
        runtime-dir drop-in for the realm user (T7.28, T716-1).

        All run before the first start, which for a services-on realm is its runit start
        (ADR-003). The rebuild inherits this, so a rebuilt generation gets the same checks. A
        failure is a GinnError, so the create or the rebuild is rolled back.
        """
        self._check_init(cid)
        machine_id = self._current().machine_id or new_machine_id()
        self._copy_machine_id(cid, machine_id)
        self._copy_run_user_tmpfiles(cid)
        self._update(machine_id=machine_id)

    def _check_init(self, cid: str) -> None:
        """Refuse a services-on image that has no /sbin/init, with GINN-E-NO-INIT (F3, FR-080).

        The check copies `/sbin/init` out of the created container into a scratch directory under
        the runtime root. podman follows a link in the source, so a link to systemd passes, and a
        missing path fails with podman's not-found reason. That reason is the only one this check
        takes for an absent init. Any other copy failure stays GINN-E-INTERNAL. A created container
        is read here, not started, so nothing runs in it. The scratch copy is removed on exit.
        """
        try:
            with scratch_dir(self._env.paths) as scratch:
                try:
                    self._podman.cp_from(cid, INIT_PATH, scratch / "init")
                except GinnError as err:
                    if err.details.get("reason") != PATH_NOT_FOUND_REASON:
                        raise
                    raise make(
                        "GINN-E-NO-INIT",
                        image=self._plan.spec.image,
                        realm=self._request.name.value,
                    ) from err
        except OSError as exc:
            # A failed scratch directory is a GinnError, so run_journaled rolls the create back.
            raise _scratch_failure(exc) from exc

    def _copy_machine_id(self, cid: str, machine_id: str) -> None:
        """Write the realm's machine-id into the container with `podman cp` (ADR-007, F6).

        The file goes in as a copy, not as a bind mount: a bind of /etc/machine-id makes
        systemd-machine-id-commit.service fail (spikes/results/F6-F1-probe.md, section A.2). The
        scratch file holds the id and a newline, with mode 0644, and it is removed on exit. An
        OSError while the file is staged is a GinnError, so the create is rolled back.
        """
        try:
            with scratch_dir(self._env.paths) as scratch:
                src = scratch / "machine-id"
                src.write_text(f"{machine_id}\n", encoding="ascii")
                src.chmod(0o644)
                self._podman.cp_into(cid, src, MACHINE_ID_PATH)
        except OSError as exc:
            raise _scratch_failure(exc) from exc

    def _copy_run_user_tmpfiles(self, cid: str) -> None:
        """Copy the realm user's tmpfiles.d drop-in into the container with `podman cp` (T7.28).

        The drop-in directory is staged, and the staged directory is copied into `/etc/`. podman
        makes `/etc/tmpfiles.d` when the image lacks it, and merges the drop-in into it when it
        exists, so the other files there stay. The line makes systemd-tmpfiles-setup give
        `/run/user/<uid>` to the realm user at each boot (T716-1). A failure of any step is a
        GINN-E-INTERNAL that names the drop-in step, so the create is rolled back.
        """
        spec = self._plan.spec
        drop_in = PurePosixPath(RUN_USER_TMPFILES_PATH)
        try:
            with scratch_dir(self._env.paths) as scratch:
                staged = scratch / drop_in.parent.name
                staged.mkdir(mode=0o755)
                # The umask masks mkdir's mode, and podman cp applies the staged mode to
                # /etc/tmpfiles.d, so the mode is set again after the mkdir.
                staged.chmod(0o755)
                src = staged / drop_in.name
                src.write_text(run_user_tmpfiles(spec.uid, spec.gid), encoding="ascii")
                src.chmod(0o644)
                self._podman.cp_into(cid, staged, f"{drop_in.parent.parent}/")
        except (OSError, GinnError) as exc:
            raise _drop_in_failure(exc) from exc

    def _write_journald_dropin(self) -> None:
        """Write the shared journald drop-in that the create argv binds (ADR-007 a, CR-007)."""
        try:
            write_journald_dropin(self._env.paths)
        except OSError as exc:
            err = make("GINN-E-INTERNAL")
            err.details.update({"detail": f"journald drop-in: {exc}"})
            raise err from exc

    def _write_service(self) -> None:
        """Write the realm's runit service directory. The journald drop-in is written before podman.

        The undo is registered before the write, and it removes the directory only for this realm
        name, which the name check proved free.
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
            target=self._target(mutating=False),
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
            self._container_id(),
            ["getent", "passwd", str(self._request.uid)],
            target=self._target(mutating=False),
            user=ROOT_USER,
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

    def _everything(self) -> None:
        """Estimate, check, confirm and install the Kali everything set (FR-014, FR-121).

        Only a request with `--everything` runs this. The estimate reads the realm's package lists,
        so it comes after the refresh, and the base image is downloaded before it. Nothing is
        installed until the space check and the confirmation both pass. A short disk, or no answer
        to the prompt, raises, and the rollback removes the realm.
        """
        names = self._plan.everything
        if not names:
            return
        estimate = self._estimate_everything()
        self._estimate = estimate
        paths = self._env.paths
        check_free_paths(
            [paths.podman_root, paths.homes],
            estimate_required(estimate.total_bytes),
            statvfs=self._hooks.statvfs,
        )
        prompt = (
            f"Install {', '.join(names)} ({estimate.packages} packages): download "
            f"{human_bytes(estimate.download_bytes)}, installed "
            f"{human_bytes(estimate.installed_bytes)}. Continue?"
        )
        if not self._confirm_everything(prompt):
            cancelled = make("GINN-E-INTERRUPTED")
            cancelled.add_note("the everything install was not confirmed, so nothing was installed")
            raise cancelled
        self._step("estimated")
        self._install_everything(names)
        self._step("everything")

    def _install_everything(self, names: tuple[str, ...]) -> None:
        """Install the everything names, as container root (FR-014). A rebuild overrides this."""
        self._exec(self._plan.definition.argv(Verb.INSTALL, names), step="everything")

    def _confirm_everything(self, prompt: str) -> bool:
        """Answer the everything prompt. A create asks `hooks.confirm`; None declines (FR-014).

        A yes is saved in the record before the install, so `create --resume` does not ask again
        (FR-122). `--yes` answers through the same hook, so it is recorded too.
        """
        if self._hooks.confirm is None or not self._hooks.confirm(prompt):
            return False
        self._update(everything_confirmed=True)
        return True

    def _estimate_everything(self) -> SizeEstimate:
        """The size of the everything closure, read from the realm's package lists (FR-014 AC-1).

        The reading is ops/estimate.py's `measure_everything`, shared with the estimate that runs
        before any create (FR-014 AC-1). It runs after the refresh, inside the realm.
        """
        return measure_everything(
            self._podman,
            self._container_id(),
            self._plan.definition,
            self._plan.everything,
            realm=self._request.name.value,
            retry_hook=self._retry,
            target=self._target(mutating=False),
        )

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
        try:
            sync_exec_service(self._env.paths, self._podman)
        except Exception as exc:  # noqa: BLE001 - the realm is made, so a relay failure is a warning
            self._mutation.warnings.append(relay_failure(exc))
        notice = warn("GINN-W-SERVICES-PENDING", realm=self._request.name.value)
        self._mutation.warnings.append(notice)
        self._step("stopped")
        record = self._current()
        transition(record.state, RealmState.STOPPED, record.previous_state)
        self._update(state=RealmState.STOPPED, **_cleared_request())
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
                [*self._plan.everything, *self._plan.packages],
                source=InstallSource.REPO,
                method=InstallMethod.GINN,
            ),
            **_cleared_request(),
        )
        return self._current()

    # Rollback callables. Each one undoes exactly one artifact.

    def _remove_image(self) -> None:
        """Remove the image this create pulled. Absent, or in use by a container, is a no-op.

        A container uses the image by the alias-aware match of ops/images.py (`uses_image`), since
        podman may show the image by name, digest or id. A container with no image string is not
        compared, as delete and the estimate do.
        """
        ref = self._plan.spec.image
        if not self._podman.image_exists(ref):
            return
        names = image_names(self._podman, ref)
        for info in self._podman.ps():
            if info.image != "" and uses_image(info.image, names):
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

    def _target(self, *, mutating: bool) -> ExecTarget:
        """The exec target of this operation, by the services mode of the container it builds.

        A services-on create stops after `created` and runs no exec, so its execs are all services
        off. A rebuild overrides nothing here: its plan carries the mode of the new generation
        (ADR-014, Decision 1).
        """
        return ExecTarget(
            realm=self._request.name.value, services=self._plan.spec.services, mutating=mutating
        )

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
            retry_hook=self._retry,
            target=self._target(mutating=True),
        )


def run_journaled(
    env: OpEnv,
    plan: _Plan,
    name: RealmName,
    params: dict[str, Any],
    body: Callable[[Mutation], _Create],
    *,
    resumed: bool = False,
) -> CreateResult:
    """Run one create or resume inside the image lock and `mutating("create")` (FR-122).

    `body` builds the phases over the mutation that the journal gives it, so the rollback can reach
    them. A GinnError from the phases rolls them back and is re-raised with the rollback notes. A
    KeyboardInterrupt rolls back the same way and becomes GINN-E-INTERRUPTED. Any other exception
    keeps the journal entry, as `mutating()` does (FR-122). `resumed` is copied to the result.
    """
    image = f"create of {plan.definition.id} image"
    with (
        image_lock(env.paths, plan.definition.id, image),
        mutating(env, "create", name, params) as mutation,
    ):
        create = body(mutation)
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
        estimate=create.estimate,
        resumed=resumed,
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
    params: dict[str, Any] = {
        "distro": plan.definition.id,
        "tier": request.tier.value,
        "services": request.services,
        "with": list(plan.packages),
        "everything": list(plan.everything),
    }
    return run_journaled(
        env,
        plan,
        request.name,
        params,
        lambda mutation: _Create(env, podman, request, hooks, plan, mutation),
    )


__all__ = [
    "RETRY_DELAYS",
    "CreateHooks",
    "CreateRequest",
    "CreateResult",
    "SizeEstimate",
    "create_realm",
    "estimate_bytes",
    "human_bytes",
    "required_bytes",
]
