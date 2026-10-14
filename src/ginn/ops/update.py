# SPDX-License-Identifier: Apache-2.0
"""`ginn update <realm>|--all` and `ginn update --check` (FR-023, FR-024, FR-120, IR-010, T8.4).

An update runs the realm's native full upgrade, the `upgrade` verb, as container root under the
locale (ADR-011). Realms are never updated by ginn on its own (FR-023 AC-1). A check runs
check-updates and reports whether updates exist (FR-024). It changes no installed package.

Order of an update of one realm:
1. The record is read (FR-025). A Kali realm is refused with GINN-E-NOT-SUPPORTED-YET (M10), as
   install refuses it. A state other than stopped, failed or running is GINN-E-REALM-STATE.
2. A stopped or failed realm starts first, as install starts one (FR-017). The start takes the
   realm lock itself, so it runs before `mutating`.
3. Inside `mutating("update")`, the container must still run. The record moves to `updating`
   (DR-020), so an interrupted update is marked broken by recovery (ops/recovery.py). `refresh`
   runs first only when the data lists `upgrade` in refresh_before (Debian and Kali). Then
   `upgrade` runs. A transient network failure is retried with the FR-120 delays (ops/retry.py).
   A signature failure is never retried.
4. On success the record returns to the state it had (DR-020) and the host menu is synced (FR-044),
   still under the lock.
5. On failure the record returns to the state it had, so the realm stays usable (FR-120 AC-2).
   It is marked `broken` only when `db_check` reports an inconsistent package database. No
   distribution defines that check yet (see the report of T8.4), so production passes None and never
   marks broken.
6. A realm that was stopped before the update stops again, through `stop_realm`, which takes its own
   lock. A realm that was failed stays running, as install leaves it (ADR-008).

The disk precheck (FR-121) is skipped for an update. The estimate verbs take named packages, so none
of them sizes an upgrade. The result names the skip (DISK_PRECHECK_SKIPPED).

A check (`--check`) takes no realm lock and no journal, because it writes nothing ginn records. A
stopped realm is not started for it, so its result is skipped, not checked. The lock question of
ADR-011 is open, so a check can run beside an update of the same realm.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from ginn.distros import DistroDef, Verb
from ginn.distros import load as load_distro
from ginn.domain import RealmName, RealmState
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.export.sync import SyncHooks, sync_in_lock
from ginn.ops._mutating import Mutation, OpEnv, mutating
from ginn.ops.lifecycle import HostUser, start_realm, stop_realm
from ginn.ops.packages import PackageHooks
from ginn.ops.retry import network_retry_hook
from ginn.packages.manager import CheckResult, run_check, run_step
from ginn.podman.adapter import Podman
from ginn.podman.models import ContainerState
from ginn.runtime.spec import KALI, LABEL_REALM
from ginn.store.records import RealmRecord, RealmStore

OP_UPDATE: Final = "update"
"""The audit operation name of `ginn update` (FR-124)."""
KALI_MILESTONE: Final = "M10"
"""The milestone that adds Kali realms (docs/roadmap.md, T10.1)."""
DISK_PRECHECK_SKIPPED: Final = "skipped: no estimate verb sizes an upgrade (FR-121)"
"""The disk precheck note of an update. The estimate verbs take named packages, not an upgrade."""
_ENTERABLE: Final = frozenset({RealmState.RUNNING, RealmState.STOPPED, RealmState.FAILED})
"""The record states that an update accepts. A stopped or failed realm starts first."""
_HALTED: Final = frozenset({ContainerState.CREATED, ContainerState.EXITED})
"""Container states that mean the process is not running (as enter reads them)."""

DbCheck = Callable[[Podman, str, DistroDef], bool]
"""Reports whether the package database of a realm is inconsistent (FR-120 AC-2).

True marks the realm `broken`. A check that fails is not a finding: the realm is restored, and the
failure is a note on the update's error.
"""


@dataclass(frozen=True)
class UpdateResult:
    """One realm of `update_realms`: the record after its update, or the error that ended it."""

    realm: str
    record: RealmRecord | None
    error: GinnError | None
    warnings: tuple[GinnWarning, ...] = ()


@dataclass(frozen=True)
class RealmCheck:
    """One realm of `check_realms`: what check-updates found, why it was skipped, or its error.

    `state` is None when the record cannot be read. Exactly one of `result`, `skipped` and `error`
    is set.
    """

    realm: str
    state: str | None
    result: CheckResult | None = None
    skipped: str | None = None
    error: GinnError | None = None


def _refuse_kali(record: RealmRecord) -> None:
    if record.settings.distro.value == KALI:
        raise make(
            "GINN-E-NOT-SUPPORTED-YET",
            realm=record.name.value,
            feature="an update of a Kali realm",
            milestone=KALI_MILESTONE,
        )


def _running(podman: Podman, record: RealmRecord) -> bool:
    """True when podman lists the realm's container and it is not created or exited."""
    cid = record.container_id
    if record.state is not RealmState.RUNNING or cid is None:
        return False
    listed = podman.ps({LABEL_REALM: record.name.value})
    return any(info.id == cid and info.state not in _HALTED for info in listed)


def _load_running(store: RealmStore, podman: Podman, name: RealmName) -> tuple[RealmRecord, str]:
    """The record under the lock, with its container id. The container must run (FR-123)."""
    current = store.load(name)
    cid = current.container_id
    if current.state not in _ENTERABLE or cid is None or not _running(podman, current):
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state=current.state.value,
            action=OP_UPDATE,
            allowed="running",
        )
    return current, cid


def _upgrade(
    podman: Podman,
    cid: str,
    definition: DistroDef,
    *,
    realm: str,
    hooks: PackageHooks,
    mutation: Mutation,
) -> None:
    """The native full upgrade (FR-023 AC-2), with the refresh the data asks for first.

    Each step retries a network failure with the FR-120 delays. A failed step raises the error of
    `run_step`, which is GINN-E-PKG-SIGNATURE or GINN-E-PKG-MANAGER, or GINN-E-NETWORK when the
    retries run out.
    """
    retry = network_retry_hook(hooks.sleep)
    if Verb.UPGRADE in definition.refresh_before:
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
    run_step(
        podman,
        cid,
        definition,
        definition.argv(Verb.UPGRADE),
        realm=realm,
        step="upgrade",
        retry_hook=retry,
    )
    mutation.step("upgraded")


def _inconsistent(
    podman: Podman,
    cid: str,
    definition: DistroDef,
    db_check: DbCheck | None,
    err: GinnError,
) -> bool:
    """True when `db_check` reports an inconsistent package database (FR-120 AC-2).

    None means no check is defined, so the answer is False. A check that fails adds a note to `err`.
    """
    if db_check is None:
        return False
    try:
        return db_check(podman, cid, definition)
    except GinnError as exc:
        err.add_note(f"the package database check failed: {exc.code}: {exc.message}")
        return False


def _sync_menu(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    mutation: Mutation,
    hooks: PackageHooks,
) -> None:
    """Sync the host menu after an upgrade, under the lock (FR-044). A failed sync is a warning."""
    try:
        result = sync_in_lock(
            env, podman, name, mutation=mutation, hooks=SyncHooks(program=hooks.program)
        )
    except GinnError as err:
        mutation.warnings.append(warn("GINN-W-EXPORT-SYNC", realm=name.value, reason=err.message))
        return
    mutation.warnings.extend(result.warnings)


def _update_locked(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    definition: DistroDef,
    *,
    hooks: PackageHooks,
    db_check: DbCheck | None,
    notices: list[GinnWarning],
) -> None:
    """The steps of an update, under the realm lock, inside the choke point (DR-032, FR-123)."""
    store = RealmStore(env.paths, env.clock)
    with mutating(env, OP_UPDATE, name, {"distro": definition.id}) as mutation:
        current, cid = _load_running(store, podman, name)
        previous = current.state
        store.set_state(name, RealmState.UPDATING)
        mutation.step("updating")
        try:
            _upgrade(podman, cid, definition, realm=name.value, hooks=hooks, mutation=mutation)
        except GinnError as err:
            # A GinnError ends the operation as finished (see _mutating). The record must be settled
            # here, because nothing later moves a realm that is left at `updating`.
            if _inconsistent(podman, cid, definition, db_check, err):
                store.set_state(name, RealmState.BROKEN)
            else:
                store.set_state(name, previous)
            raise
        store.set_state(name, previous)
        mutation.step("restored")
        _sync_menu(env, podman, name, mutation, hooks)
    notices.extend(mutation.warnings)


def _stop_after(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    hooks: PackageHooks,
    was_stopped: bool,
    notices: list[GinnWarning],
    failure: GinnError | None,
) -> None:
    """Stop a realm that was stopped before the update (DR-020, FR-120 AC-2).

    Only a realm the update left running is stopped. On a failed update a failed stop is a note on
    that error. On a successful update it is raised, because the realm is not in its prior state.
    """
    if not was_stopped:
        return
    if RealmStore(env.paths, env.clock).load(name).state is not RealmState.RUNNING:
        return
    try:
        stop_realm(env, podman, name, hooks=hooks.lifecycle, warnings=notices)
    except GinnError as err:
        if failure is None:
            raise
        failure.add_note(f"the realm was left running: {err.code}: {err.message}")


def update_realm(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    session: HostUser,
    hooks: PackageHooks | None = None,
    db_check: DbCheck | None = None,
    warnings: list[GinnWarning] | None = None,
) -> RealmRecord:
    """Run the native full upgrade of realm `name` and return its record (FR-023, FR-120).

    The FR-006 warnings and the warnings of the start and the stop are appended to `warnings` when
    it is given, after the operation has finished, also when it failed.
    """
    hooks = PackageHooks() if hooks is None else hooks
    store = RealmStore(env.paths, env.clock)
    record = store.load(name)
    _refuse_kali(record)
    if record.state not in _ENTERABLE:
        raise make(
            "GINN-E-REALM-STATE",
            realm=name.value,
            state=record.state.value,
            action=OP_UPDATE,
            allowed="stopped, failed or running",
        )
    definition = load_distro(record.settings.distro.value)
    notices: list[GinnWarning] = []
    was_stopped = record.state is RealmState.STOPPED
    try:
        if not _running(podman, record):
            start_realm(env, podman, name, session=session, hooks=hooks.lifecycle, warnings=notices)
        _update_locked(
            env, podman, name, definition, hooks=hooks, db_check=db_check, notices=notices
        )
    except GinnError as err:
        _stop_after(
            env,
            podman,
            name,
            hooks=hooks,
            was_stopped=was_stopped,
            notices=notices,
            failure=err,
        )
        if warnings is not None:
            warnings.extend(notices)
        raise
    _stop_after(
        env, podman, name, hooks=hooks, was_stopped=was_stopped, notices=notices, failure=None
    )
    if warnings is not None:
        warnings.extend(notices)
    return store.load(name)


def update_realms(
    env: OpEnv,
    podman: Podman,
    names: Sequence[RealmName] | None,
    *,
    session: HostUser,
    hooks: PackageHooks | None = None,
    db_check: DbCheck | None = None,
) -> list[UpdateResult]:
    """Update each realm in `names`, or every realm of this user when None (FR-023, `--all`).

    A realm that fails does not stop the others. Each realm's outcome is one UpdateResult, in name
    order. The caller decides the exit status from them.
    """
    targets = RealmStore(env.paths, env.clock).list_names() if names is None else list(names)
    results: list[UpdateResult] = []
    for name in targets:
        notices: list[GinnWarning] = []
        try:
            record = update_realm(
                env,
                podman,
                name,
                session=session,
                hooks=hooks,
                db_check=db_check,
                warnings=notices,
            )
        except GinnError as err:
            results.append(UpdateResult(name.value, None, err, tuple(notices)))
        else:
            results.append(UpdateResult(name.value, record, None, tuple(notices)))
    return results


def check_realm(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    *,
    hooks: PackageHooks | None = None,
    tmpdir: str | None = None,
) -> RealmCheck:
    """Check realm `name` for updates (FR-024). Nothing is started, stopped or written.

    A realm that does not run is skipped, not started: a check changes nothing (see the docstring).
    `tmpdir` is the Arch scratch directory, for a test. None makes a fresh one in the realm.
    """
    hooks = PackageHooks() if hooks is None else hooks
    record = RealmStore(env.paths, env.clock).load(name)
    _refuse_kali(record)
    state = record.state.value
    if record.state is not RealmState.RUNNING:
        return RealmCheck(name.value, state, skipped=f"the realm is {state}; start it to check")
    if not _running(podman, record) or record.container_id is None:
        return RealmCheck(name.value, state, skipped="the realm's container does not run")
    definition = load_distro(record.settings.distro.value)
    result = run_check(
        podman,
        record.container_id,
        definition,
        realm=name.value,
        retry_hook=network_retry_hook(hooks.sleep),
        tmpdir=tmpdir,
    )
    return RealmCheck(name.value, state, result=result)


def check_realms(
    env: OpEnv,
    podman: Podman,
    names: Sequence[RealmName] | None,
    *,
    hooks: PackageHooks | None = None,
) -> list[RealmCheck]:
    """Check each realm in `names`, or every realm of this user when None (FR-024).

    A realm whose check fails is one RealmCheck with its error. The others are still checked.
    """
    targets = RealmStore(env.paths, env.clock).list_names() if names is None else list(names)
    rows: list[RealmCheck] = []
    for name in targets:
        try:
            rows.append(check_realm(env, podman, name, hooks=hooks))
        except GinnError as err:
            rows.append(RealmCheck(name.value, None, error=err))
    return rows


__all__ = [
    "DISK_PRECHECK_SKIPPED",
    "OP_UPDATE",
    "DbCheck",
    "RealmCheck",
    "UpdateResult",
    "check_realm",
    "check_realms",
    "update_realm",
    "update_realms",
]
