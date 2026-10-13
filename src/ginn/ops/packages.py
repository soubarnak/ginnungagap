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
from ginn.ops.create import RETRY_DELAYS
from ginn.ops.lifecycle import HostUser, LifecycleHooks, start_realm
from ginn.packages.aur import install_aur
from ginn.packages.manager import (
    explicit_packages,
    printable,
    removal_dependents,
    run_step,
    shown,
)
from ginn.packages.manifest import installed, removed
from ginn.podman.adapter import Podman, RetryHook
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
    """The FR-120 retry rule: wait the delay of the failed attempt, then retry (AS-023)."""

    def retry(attempt: int, err: GinnError) -> bool:
        if attempt > len(RETRY_DELAYS):
            return False
        hooks.sleep(RETRY_DELAYS[attempt - 1])
        return True

    return retry


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


def _running(podman: Podman, record: RealmRecord) -> bool:
    """True when podman lists the realm's container and it is not created or exited."""
    cid = record.container_id
    if record.state is not RealmState.RUNNING or cid is None:
        return False
    listed = podman.ps({LABEL_REALM: record.name.value})
    return any(info.id == cid and info.state not in _HALTED for info in listed)


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
    if record.state not in _ENTERABLE:
        raise make(
            "GINN-E-REALM-STATE",
            realm=record.name.value,
            state=record.state.value,
            action=action,
            allowed="stopped, failed or running",
        )
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
    try:
        explicit = explicit_packages(podman, cid, definition, realm=name.value)
    except Exception:  # noqa: BLE001 - the operation's own error is the one the caller raises
        return
    if installed_now:
        present = [raw for raw in names if raw in explicit]
        if not present:
            return
        manifest = installed(
            current.manifest, present, source=InstallSource.REPO, method=InstallMethod.GINN
        )
    else:
        gone = [raw for raw in names if raw not in explicit]
        if not gone:
            return
        manifest = removed(current.manifest, gone)
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
    if Verb.INSTALL in definition.refresh_before:
        run_step(
            podman,
            cid,
            definition,
            definition.argv(Verb.REFRESH),
            realm=realm,
            step="refresh",
            retry_hook=retry,
        )
        mutation.step("refreshed")
    try:
        run_step(
            podman,
            cid,
            definition,
            definition.argv(Verb.INSTALL, names),
            realm=realm,
            step="install",
            retry_hook=retry,
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
    retry = _retry(hooks)
    realm = name.value
    if definition.has_remove_check:
        dependents = removal_dependents(
            podman, cid, definition, names, realm=realm, retry_hook=retry
        )
        if dependents:
            err = make(
                "GINN-E-PKG-DEPENDENTS",
                realm=realm,
                packages=shown(names),
                dependents=shown(sorted(dependents)),
            )
            raise err
        mutation.step("checked")
    try:
        run_step(
            podman,
            cid,
            definition,
            definition.argv(Verb.REMOVE, names),
            realm=realm,
            step="remove",
            retry_hook=retry,
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
) -> RealmRecord:
    """Install `packages` in realm `name` and record them as `ginn` installs (FR-040).

    With `aur`, each name is an AUR package, built inside the realm as the realm user (FR-043).
    `gid` is the host group of the realm user; it defaults to this process's group, as enter does.
    The checks of the module docstring run before any side effect. The FR-006 warnings of a start
    and of the install are appended to `warnings` when it is given, after the operation finished.
    """
    hooks = PackageHooks() if hooks is None else hooks
    store = RealmStore(env.paths, env.clock)
    record = store.load(name)
    _refuse_kali(record)
    names = _checked(record, packages)
    if aur:
        _refuse_non_arch_aur(record.settings.distro)
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
    realm's packages are never changed by a removal the user did not ask for.
    """
    hooks = PackageHooks() if hooks is None else hooks
    store = RealmStore(env.paths, env.clock)
    record = store.load(name)
    _refuse_kali(record)
    names = _checked(record, packages)
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
