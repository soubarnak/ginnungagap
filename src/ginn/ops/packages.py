# SPDX-License-Identifier: Apache-2.0
"""Install and remove packages in a realm (FR-040, FR-041, FR-043, DR-007, FR-124).

`install_packages` and `remove_packages` run the realm's own package manager as container root,
through the verbs of packages/manager.py. Each one is a mutating operation. Its body runs inside
`mutating("install")` or `mutating("remove")` (AGENTS.md, DR-032, FR-123, FR-124), so it takes the
realm lock, journals its steps and writes one audit entry with the packages and the `aur` flag.

Order of an install, and why:
1. The record is read (FR-025). A Kali realm is refused with GINN-E-NOT-SUPPORTED-YET (M10), as
   enter refuses it.
2. Each name is checked against DR-003 (PackageName) before any side effect (FR-040 AC-2,
   NFR-SEC-005). No names is GINN-E-USAGE.
3. `--aur` is checked. On a distribution other than Arch it is GINN-E-OPTION-INVALID. On Arch the
   packages are built from the AUR inside the realm (packages/aur.py, FR-043, IR-023).
4. A stopped or failed realm is started, as enter starts one (FR-017 for enter; install follows
   it). `start_realm` takes the realm lock itself, so it runs before `mutating`, never inside it.
5. Inside the lock, the container must still run, or GINN-E-REALM-STATE is raised. `refresh` runs
   first only when `install` is in the distribution's `refresh_before` (Debian and Kali, ADR-011).
   Then `install` runs. Signature checks stay on (FR-012).
6. Each name is recorded in the Install Manifest as `ginn` from the `repo` source (DR-007), and
   the record is saved atomically (DR-035).
7. The host menu is synced with the realm (`_sync_menu`, FR-044), still under the same lock. A
   sync that cannot finish is a warning, never a failure of the install.

Order of a remove. Steps 1, 2 and 4 are the same. Inside the lock, a distribution that defines
`remove-check` (Debian, Kali and Fedora, ADR-011) runs the dry run first. A removal that would take
a package the user did not name is refused with GINN-E-PKG-DEPENDENTS before anything is removed.
Then `remove` runs with no cascade flag (FR-041), and the names leave the manifest. The menu
sync follows, as step 7 of an install does.

Init entries (ADR-015 Decision 14). An init entry is a package that the provisioning stage
installed for a services-on realm, and its manifest entry carries `init`. An install that names one
is a manifest no-op: that name is not passed to the package manager, and `notes` gets a line that
says so. An install of init entries only changes nothing and starts nothing. A remove that names one
is refused as a whole with GINN-E-PKG-PROTECTED, before any step, because the init set is what lets
the realm boot. A start can rebuild the realm and change its init set, so the remove checks it again
under the lock, before its first step.

The realm stays running after an install or a remove, as enter leaves it (ADR-008).

Failure. A GinnError from the body is final: the realm's package manager has already refused or
finished the change, and the manifest is written only after `install` or `remove` succeeds. The
journal and the audit entry then record the error code (FR-122). Before the error is
raised, a failed install records the requested names that the realm now lists as explicit,
and a failed remove drops the names it no longer lists, so the manifest matches what the
failed transaction left (FR-040, FR-041). A network failure of a package
step is retried by `PackageHooks.sleep` with the FR-120 delays (AS-023), and a failed retry stops
the operation.

The AUR build of an install is journaled in the same operation: its steps are `git`,
`dependencies`, `toolchain`, `built` and `removed` (packages/aur.py). Recovery replays the AUR
manifest write from `removed` (ops/recovery.py).

The export and removal of desktop entries (FR-044, FR-045) are in export/sync.py. This module only
calls the sync, under its own lock, after a successful change.
"""

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from ginn.distros import DistroDef, Verb
from ginn.distros import load as load_distro
from ginn.domain import (
    DistroId,
    InstallMethod,
    InstallSource,
    PackageName,
    RealmName,
    RealmState,
)
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.export.sync import SyncHooks, sync_in_lock
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops.lifecycle import HostUser, LifecycleHooks, start_realm
from ginn.ops.retry import network_retry_hook
from ginn.packages.aur import install_aur
from ginn.packages.manager import (
    explicit_manifest,
    explicit_packages,
    name_batches,
    printable,
    removal_dependents,
    remove_passes,
    run_step,
    shown,
)
from ginn.packages.manifest import installed, removed
from ginn.podman.adapter import ExecTarget, Podman, RetryHook
from ginn.podman.models import ContainerState
from ginn.runtime.spec import KALI, LABEL_REALM
from ginn.store._time import utc_from_epoch
from ginn.store.records import RealmRecord, RealmStore

OP_INSTALL: Final = "install"
"""The audit operation name of `ginn install` (FR-124)."""
OP_REMOVE: Final = "remove"
"""The audit operation name of `ginn remove` (FR-124)."""
AUR_DISTRO: Final = "arch"
"""The one distribution whose AUR packages `--aur` names (FR-043)."""
KALI_MILESTONE: Final = "M10"
"""The milestone that adds Kali realms (docs/roadmap.md, T10.1)."""
_ENTERABLE: Final = frozenset({RealmState.RUNNING, RealmState.STOPPED, RealmState.FAILED})
"""The record states that an install or a remove accepts. Any other state is GINN-E-REALM-STATE."""
_HALTED: Final = frozenset({ContainerState.CREATED, ContainerState.EXITED})
"""Container states that mean the process is not running (as enter reads them)."""


@dataclass(frozen=True)
class PackageHooks:
    """The seams of an install or a remove.

    `lifecycle` is passed to `start_realm` when a stopped realm starts first. `sleep` is the wait
    before each retry of a network failure (FR-120), so a test runs without waiting. `program` is
    the ginn command that a menu entry runs (FR-044). The CLI passes the absolute path, as `ginn
    _sync` does, so an entry written by a change and one written by a sync are the same.
    """

    lifecycle: LifecycleHooks = field(default_factory=LifecycleHooks)
    sleep: Callable[[float], None] = time.sleep
    program: str = "ginn"


def _retry(hooks: PackageHooks) -> RetryHook:
    """The FR-120 retry rule of ops/retry.py, waiting with the hook's sleep (AS-023)."""
    return network_retry_hook(hooks.sleep)


def _checked(record: RealmRecord, packages: Sequence[str]) -> list[str]:
    """The names to act on: non-empty, each valid under DR-003, duplicates dropped (FR-040 AC-2).

    Raises before any podman call or write, so a bad name changes nothing.
    """
    if not packages:
        raise make("GINN-E-USAGE", detail="a package name is needed after REALM")
    for raw in packages:
        PackageName(raw, record.settings.distro)
    return list(dict.fromkeys(packages))


def _refuse_kali(record: RealmRecord) -> None:
    if record.settings.distro.value == KALI:
        raise make(
            "GINN-E-NOT-SUPPORTED-YET",
            realm=record.name.value,
            feature="a package change in a Kali realm",
            milestone=KALI_MILESTONE,
        )


def _refuse_non_arch_aur(distro: DistroId) -> None:
    """`--aur` is Arch-only (FR-043). On any other distribution it is an option error."""
    if distro.value != AUR_DISTRO:
        raise make("GINN-E-OPTION-INVALID", option="--aur", distro=distro.value)


def _init_names(record: RealmRecord) -> frozenset[str]:
    """The names of the realm's init entries (ADR-015 Decision 14). Install and remove read them."""
    return frozenset(entry.name for entry in record.manifest if entry.init)


def _init_note(realm: str, name: str) -> str:
    """The printed note of an install that names an init entry (ADR-015 Decision 14)."""
    return (
        f"'{name}' is an init package of realm '{realm}': it is already installed, "
        "so ginn install changed nothing for it"
    )


def _refuse_protected(record: RealmRecord, names: Sequence[str]) -> None:
    """Refuse a removal that names init entries, as a whole (GINN-E-PKG-PROTECTED, FR-041).

    Every protected name is listed. The check runs before a start, and again under the lock in
    `_remove_body`, because a start can rebuild the realm and change its init set (ADR-015).
    """
    init = _init_names(record)
    protected = [raw for raw in names if raw in init]
    if protected:
        raise make("GINN-E-PKG-PROTECTED", realm=record.name.value, name=shown(protected))


def _running(podman: Podman, record: RealmRecord) -> bool:
    """True when podman lists the realm's container and it is not created or exited."""
    cid = record.container_id
    if record.state is not RealmState.RUNNING or cid is None:
        return False
    listed = podman.ps({LABEL_REALM: record.name.value})
    return any(info.id == cid and info.state not in _HALTED for info in listed)


def _refuse_state(record: RealmRecord, *, action: str) -> None:
    """Refuse a state the verb does not allow (GINN-E-REALM-STATE)."""
    if record.state not in _ENTERABLE:
        raise make(
            "GINN-E-REALM-STATE",
            realm=record.name.value,
            state=record.state.value,
            action=action,
            allowed="stopped, failed or running",
        )


def _ensure_started(
    env: OpEnv,
    podman: Podman,
    record: RealmRecord,
    *,
    action: str,
    session: HostUser,
    hooks: PackageHooks,
    notices: list[GinnWarning],
) -> None:
    """Refuse a state the verb does not allow, then start a stopped realm (FR-017, enter's rule)."""
    _refuse_state(record, action=action)
    if not _running(podman, record):
        start_realm(
            env,
            podman,
            record.name,
            session=session,
            hooks=hooks.lifecycle,
            warnings=notices,
        )


def _load_running(
    store: RealmStore, podman: Podman, name: RealmName, *, action: str
) -> tuple[RealmRecord, str]:
    """Read the record under the lock and return it with its container id. The container must
    run: a realm that stopped between the start and the lock is GINN-E-REALM-STATE."""
    current = store.load(name)
    cid = current.container_id
    if current.state not in _ENTERABLE or cid is None or not _running(podman, current):
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state=current.state.value,
            action=action,
            allowed="running",
        )
    return current, cid


def _acked(mutation: Mutation) -> Callable[[int], None]:
    """The `on_ack` of a mutating exec: the journal step `exec-acked`, once the exec has started.

    The step is recorded after the worker or the direct route has the client's pid, and before the
    exec ends, so recovery can see that an exec of this operation was running (FR-122, ADR-014).
    """

    def acked(_pid: int) -> None:
        mutation.step("exec-acked")

    return acked


def _reconcile_failed(
    store: RealmStore,
    podman: Podman,
    name: RealmName,
    definition: DistroDef,
    cid: str,
    current: RealmRecord,
    names: list[str],
    *,
    installed_now: bool,
    clock: float,
) -> None:
    """After a failed install or remove, make the manifest match what the realm now lists.

    A failed transaction can leave some of the requested packages changed (FR-040, FR-041). The
    explicit-package list tells which. After an install, each requested name the realm lists as
    explicit is recorded as `ginn`. After a remove, each requested name it no longer lists is
    dropped. A list that cannot be read, or is over the caps, changes nothing, and the caller
    re-raises the operation's own error either way.
    """
    manifest = explicit_manifest(
        podman,
        cid,
        definition,
        realm=name.value,
        services=bool(current.services_at_create),
        manifest=current.manifest,
        names=names,
        installed_now=installed_now,
    )
    if manifest is None or manifest == current.manifest:
        return
    store.save(replace(current, manifest=manifest, updated_at=utc_from_epoch(clock)))


def _sync_menu(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    mutation: Mutation,
    hooks: PackageHooks,
) -> None:
    """Sync the host menu with the realm after a successful change, under the lock (FR-044).

    The change is already made, so the sync never fails it. A sync that lists nothing is the
    GINN-W-EXPORT-SYNC warning, and the next sync retries. The host write errors come back from the
    sync as warnings too (export/sync.py), and so do the entries it skips. A realm that does not
    run is skipped without a warning. Only a GinnError is caught here: a journal write error is
    not a sync failure, so it propagates and the change ends with it (FR-122).
    """
    try:
        result = sync_in_lock(
            env, podman, name, mutation=mutation, hooks=SyncHooks(program=hooks.program)
        )
    except GinnError as err:
        mutation.warnings.append(warn("GINN-W-EXPORT-SYNC", realm=name.value, reason=err.message))
        return
    mutation.warnings.extend(result.warnings)


def _install_aur_body(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    definition: DistroDef,
    names: list[str],
    uid: int,
    gid: int,
    hooks: PackageHooks,
    mutation: Mutation,
) -> None:
    """The AUR build of an install, under the realm lock (FR-043, IR-023).

    The build and its restore are in `install_aur`. The manifest is written only after it returns,
    so a failed build leaves the manifest as it was. Its entries are `aur` from `ginn` (DR-007).
    """
    store = RealmStore(env.paths, env.clock)
    current, cid = _load_running(store, podman, name, action=OP_INSTALL)
    outcome = install_aur(
        podman,
        cid,
        definition,
        names,
        realm=name.value,
        uid=uid,
        gid=gid,
        retry_hook=_retry(hooks),
        target=ExecTarget(
            realm=name.value, services=bool(current.services_at_create), mutating=True
        ),
        step=mutation.step,
    )
    if outcome.residual:
        # The packages stay installed, so the operation goes on, and the result names them (FR-043).
        reason = printable(outcome.reason or "pacman refused the removal", 400)
        packages = shown(list(outcome.residual))
        notice = warn("GINN-W-BUILD-RESIDUAL", realm=name.value, packages=packages, reason=reason)
        mutation.warnings.append(notice)
    updated = replace(
        current,
        manifest=installed(
            current.manifest,
            names,
            source=InstallSource.AUR,
            method=InstallMethod.GINN,
        ),
        updated_at=utc_from_epoch(env.clock()),
    )
    store.save(updated)
    mutation.step("recorded")


def _install_body(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    definition: DistroDef,
    names: list[str],
    hooks: PackageHooks,
    mutation: Mutation,
) -> None:
    """The steps of an install, under the realm lock (FR-040, FR-123)."""
    store = RealmStore(env.paths, env.clock)
    current, cid = _load_running(store, podman, name, action=OP_INSTALL)
    retry = _retry(hooks)
    realm = name.value
    target = ExecTarget(realm=realm, services=bool(current.services_at_create), mutating=True)
    if Verb.INSTALL in definition.refresh_before:
        run_step(
            podman,
            cid,
            definition,
            definition.argv(Verb.REFRESH),
            realm=realm,
            step="refresh",
            retry_hook=retry,
            target=target,
            on_ack=_acked(mutation),
        )
        mutation.step("refreshed")
    try:
        # A relay route installs in calls of its own size. A failed call leaves the earlier ones
        # installed, and the reconcile below reads the realm's list for all of `names` (FR-040).
        for batch in name_batches(names, target):
            run_step(
                podman,
                cid,
                definition,
                definition.argv(Verb.INSTALL, batch),
                realm=realm,
                step="install",
                retry_hook=retry,
                target=target,
                on_ack=_acked(mutation),
            )
    except GinnError:
        _reconcile_failed(
            store,
            podman,
            name,
            definition,
            cid,
            current,
            names,
            installed_now=True,
            clock=env.clock(),
        )
        raise
    mutation.step("installed")
    updated = replace(
        current,
        manifest=installed(
            current.manifest,
            names,
            source=InstallSource.REPO,
            method=InstallMethod.GINN,
        ),
        updated_at=utc_from_epoch(env.clock()),
    )
    store.save(updated)
    mutation.step("recorded")


def _remove_body(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    definition: DistroDef,
    names: list[str],
    hooks: PackageHooks,
    mutation: Mutation,
) -> None:
    """The steps of a remove, under the realm lock. The dry run comes first where it is defined."""
    store = RealmStore(env.paths, env.clock)
    current, cid = _load_running(store, podman, name, action=OP_REMOVE)
    # The init set can change during the start (a rebuild), so the refusal is checked again here,
    # under the lock, before any step (ADR-015 Decision 14).
    _refuse_protected(current, names)
    retry = _retry(hooks)
    realm = name.value
    services = bool(current.services_at_create)
    dry_target = ExecTarget(realm=realm, services=services, mutating=False)
    remove_target = ExecTarget(realm=realm, services=services, mutating=True)
    if definition.has_remove_check:
        # Every batch is checked before anything is removed, so a refusal changes nothing (FR-041).
        dependents: set[str] = set()
        for batch in name_batches(names, dry_target):
            dependents |= removal_dependents(
                podman,
                cid,
                definition,
                batch,
                realm=realm,
                retry_hook=retry,
                target=dry_target,
            )
        dependents -= set(names)
        if dependents:
            err = make(
                "GINN-E-PKG-DEPENDENTS",
                realm=realm,
                packages=shown(names),
                dependents=shown(sorted(dependents)),
            )
            raise err
        mutation.step("checked")
    batches = name_batches(names, remove_target)

    def remove_batch(batch: Sequence[str]) -> None:
        run_step(
            podman,
            cid,
            definition,
            definition.argv(Verb.REMOVE, batch),
            realm=realm,
            step="remove",
            retry_hook=retry,
            target=remove_target,
            on_ack=_acked(mutation),
        )

    def still_present() -> frozenset[str]:
        return explicit_packages(podman, cid, definition, realm=realm, target=dry_target)

    try:
        # A batch removed as a dependent of an earlier one is not passed again, and a batch refused
        # because it needs a later one removed first is retried after the pass (ADR-014, T7.10).
        remove_passes(
            batches,
            remove_batch,
            still_present=still_present if len(batches) > 1 else None,
        )
    except GinnError:
        _reconcile_failed(
            store,
            podman,
            name,
            definition,
            cid,
            current,
            names,
            installed_now=False,
            clock=env.clock(),
        )
        raise
    mutation.step("removed")
    updated = replace(
        current,
        manifest=removed(current.manifest, names),
        updated_at=utc_from_epoch(env.clock()),
    )
    store.save(updated)
    mutation.step("recorded")


def install_packages(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    packages: Sequence[str],
    *,
    aur: bool,
    session: HostUser,
    gid: int | None = None,
    hooks: PackageHooks | None = None,
    warnings: list[GinnWarning] | None = None,
    notes: list[str] | None = None,
    sent: list[str] | None = None,
) -> RealmRecord:
    """Install `packages` in realm `name` and record them as `ginn` installs (FR-040).

    With `aur`, each name is an AUR package, built inside the realm as the realm user (FR-043).
    `gid` is the host group of the realm user; it defaults to this process's group, as enter does.
    The checks of the module docstring run before any side effect. The FR-006 warnings of a start
    and of the install are appended to `warnings` when it is given, after the operation finished.
    A name that is an init entry of the realm is a manifest no-op (ADR-015 Decision 14): it is not
    passed on. Its note is appended to `notes` when it is given, before the install runs, so the
    note is still there when the install fails. `sent` is filled, when it is given, with the names
    that were passed to the package manager or the AUR build, once the install finished. A request
    of init entries only changes nothing and starts nothing.
    """
    hooks = PackageHooks() if hooks is None else hooks
    store = RealmStore(env.paths, env.clock)
    record = store.load(name)
    _refuse_kali(record)
    names = _checked(record, packages)
    if aur:
        _refuse_non_arch_aur(record.settings.distro)
    init = _init_names(record)
    skipped = [raw for raw in names if raw in init]
    names = [raw for raw in names if raw not in init]
    init_notes = [_init_note(record.name.value, raw) for raw in skipped]
    if not names:
        _refuse_state(record, action=OP_INSTALL)
        if notes is not None:
            notes.extend(init_notes)
        return record
    definition = load_distro(record.settings.distro.value)
    notices: list[GinnWarning] = []
    _ensure_started(
        env,
        podman,
        record,
        action=OP_INSTALL,
        session=session,
        hooks=hooks,
        notices=notices,
    )
    if notes is not None:
        notes.extend(init_notes)
    with mutating(env, OP_INSTALL, name, {"packages": names, "aur": aur}) as mutation:
        if aur:
            group = os.getgid() if gid is None else gid
            _install_aur_body(
                env, podman, name, definition, names, session.uid, group, hooks, mutation
            )
        else:
            _install_body(env, podman, name, definition, names, hooks, mutation)
        _sync_menu(env, podman, name, mutation, hooks)
    notices.extend(mutation.warnings)
    if warnings is not None:
        warnings.extend(notices)
    if sent is not None:
        sent.extend(names)
    return store.load(name)


def remove_packages(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    packages: Sequence[str],
    *,
    session: HostUser,
    hooks: PackageHooks | None = None,
    warnings: list[GinnWarning] | None = None,
) -> RealmRecord:
    """Remove `packages` from realm `name` and drop them from the manifest (FR-041).

    A removal that would take another package is refused before it runs (ADR-011), so the
    realm's packages are never changed by a removal the user did not ask for. A request that names
    an init entry is refused as a whole with GINN-E-PKG-PROTECTED, before any step (ADR-015
    Decision 14), so none of its packages is removed.
    """
    hooks = PackageHooks() if hooks is None else hooks
    store = RealmStore(env.paths, env.clock)
    record = store.load(name)
    _refuse_kali(record)
    names = _checked(record, packages)
    _refuse_protected(record, names)
    definition = load_distro(record.settings.distro.value)
    notices: list[GinnWarning] = []
    _ensure_started(
        env,
        podman,
        record,
        action=OP_REMOVE,
        session=session,
        hooks=hooks,
        notices=notices,
    )
    with mutating(env, OP_REMOVE, name, {"packages": names}) as mutation:
        _remove_body(env, podman, name, definition, names, hooks, mutation)
        _sync_menu(env, podman, name, mutation, hooks)
    notices.extend(mutation.warnings)
    if warnings is not None:
        warnings.extend(notices)
    return store.load(name)


__all__ = [
    "OP_INSTALL",
    "OP_REMOVE",
    "PackageHooks",
    "install_packages",
    "remove_packages",
]
