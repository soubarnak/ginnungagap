# SPDX-License-Identifier: Apache-2.0
"""Recovery of interrupted operations: the journal entry of a dead process (FR-122, NFR-030).

A mutating operation that is killed (SIGKILL, power loss) leaves its journal file behind
(store/journal.py). The next ginn command finds it and calls `recover()`. Recovery reads the
realm record, the audit log and podman, moves the record to a DR-020 state, records the
interrupted operation in the audit log, and removes the journal file.

`recover()` works in this order:

1. `find_pending` scans the journal directory. A file that cannot be read is reported as corrupt,
   with the remedy from `journal.damaged_reason`, and left alone. Its pid cannot be trusted, and it
   is never deleted.
2. An entry whose process is alive (same pid and start time, `JournalFile.is_live`) is reported as
   live and not touched. This is only the pre-lock filter.
3. The audit log is read once for the whole call (`AuditLog.index_by_op_id`), after the scan. Its
   damaged lines that carry an op_id become warnings in the outcomes. If the audit log cannot be
   read at all, every dead entry is blocked.
4. For each dead entry, under the realm lock (FR-123, DR-033), taken without waiting: the entry is
   read again, and its op_id is looked up in the audit log again with a filtered read. The
   pre-lock index can be stale. An earlier recovery may have written the operation's entry and
   died before it removed the journal file. A held lock means another operation runs on the realm,
   so the entry is reported as busy. Under the lock no operation is in flight, so the entry is
   treated as dead, and the pid check is not repeated. An entry that is gone was settled by someone
   else.
5. If the audit log holds a `recover` entry that names the op_id (params.recovers), the settlement
   was logged earlier, and only the journal file is removed.
6. If the operation's own audit entry is finished, the operation completed and its state is final.
   Nothing is moved and nothing is written. Only the journal file is removed.
7. Otherwise the verdict (`_decide`) gives the state to move to and the outcome. A move that DR-020
   does not allow is never written, and that entry is reported as blocked with both files kept.
8. The writes follow the NFR-030 order: any podman stop, then the record, then the audit entry, then
   the journal file. A crash after the audit entry is found by rule 5 or 6 next time.

The audit entry follows NFR-043 and FR-124. Each op_id gets exactly one entry from recovery, and
the original op_id is never written twice:

- No audit entry carries the operation's op_id. The entry is written under the original op and
  op_id, with ts set to the operation's start, and its outcome is "recovered" or "broken".
- An audit entry with that op_id exists and is finished ("ok", an error with a code, "recovered"
  or "broken"). Recovery writes nothing. The existing entry is the one entry, and only the journal
  file is removed.
- An audit entry with that op_id exists and is not finished ("interrupted", or an error without a
  code). The original is not written again. Recovery writes a separate entry with op "recover", a
  new op_id, ts set to now, and params.recovers set to the original op_id.

Revert rules, by the operation the journal names. They follow DR-020, the ADRs, FR-122 and FR-120:

- create: `creating` becomes `broken`, with no error code. Partial artifacts stay, for
  `ginn create --resume` or `ginn delete` (FR-122). A record already `stopped` is a complete create,
  because `stopped` is the last record write of a create, so it is recovered without a change. A
  record already `broken` is settled as broken, with no state write.
- start: a record that already reads `running` is a completed start, because the record is saved
  running only after the attach. So is a journal step of `running`. Both are matched to podman. Any
  other step means the start did not finish: a container that podman runs is stopped, and the
  record becomes `stopped`.
- stop, install, remove, tier, perms, services and any other op with no state of its own: the
  record takes the state that podman reports, `running` or `stopped`, when the record is `stopped`,
  `running` or `failed`.
- An interrupted start or stop that leaves the realm not running also leaves the Agent Proxy
  filter (FR-067), after the journal file is removed. A failure there is a note on the outcome, and
  the next filter sync drops a member that does not run. The entry is never blocked by it.
- install and remove also replay the Install Manifest write that the package step did not reach.
  The journal step `installed` (install) or `removed` (remove) is written after the package manager
  succeeds and before the manifest is saved, so an entry at that step gets the manifest written from
  its `packages` param, in the same record write (FR-040, FR-041, DR-007). An AUR install (param
  `aur`) writes its step `removed` after its build-only set is gone, and its entries are `aur`
  (FR-043). An entry at any earlier step leaves the manifest as it was. The native change may
  already have happened there, and no journal step says how far it got. The rebuild replays the
  manifest as it stands, so this residual stays open.
- sync, export and unexport change only host files, never a realm record (FR-044, FR-047). They are
  recovered with no state write, whatever the record reads, so a deleting or broken realm is not
  blocked by a launcher. The next run of the same operation reconciles the host files.
- rebuild (ops/rebuild.py, DR-034). Until the atomic swap the record names the old generation gN,
  and a `rebuilding` record returns to its previous state (DR-020). The new container gN+1 is an
  orphan then, so it is stopped and removed by its generation label. A services-on orphan runs under
  its runit service, so runsv is told to stop it first (ADR-003). After the swap the record names
  gN+1, so the settled state comes from podman, and gN is removed by its label, best effort.
  Other records are matched to podman, as above.
- update: `updating` becomes `broken` (DR-020, FR-120). A record already `broken` is settled as
  broken, with no state write. Other records are matched to podman, as above.
- delete: a `deleting` record is left deleting, and so is a `broken` record. Both states are
  starting states for `ginn delete` (T4.7). Other records are matched to podman.
- Any other pair, such as an install on a `creating` record, is impossible. It is reported as
  blocked and the journal file is kept.

A container podman cannot inspect is looked up in `podman.ps()`. Only a successful list without the
container's id means it is gone, and then it counts as stopped. If the list fails too, the entry is
blocked (FR-122).

Step names (caveat). Recovery reads the journal step of a start. It depends on the names that
ops/lifecycle.py records: `running` is the final step of a start, after the record is saved running.
If lifecycle renames it, `START_COMPLETE_STEP` and the test that uses it must change together.

Op names. The op names that ops/lifecycle.py journals (`start`, `stop`) are the ones matched here.

Locks and the choke point. Recovery does not go through `mutating()`. `mutating()` begins a journal
entry and refuses while one exists, and that entry is the evidence recovery reads. Recovery is the
second lock holder and audit writer for an interrupted operation (ops/_mutating.py documents it).
It takes the realm lock itself, and it skips the integrity check, because it changes only ginn
state. Recovery's finished rule is `is_finished` from ops/_mutating.py, which counts "recovered" and
"broken" as finished.

Idempotent. Once a file is removed, a later run finds nothing for that operation. A record move is
skipped when the record already has its target state, and a second podman stop is never needed,
because a container that is already stopped is not running.
"""

import contextlib
import uuid
from dataclasses import dataclass, replace
from typing import Any, Final

from ginn.distros import Verb
from ginn.distros import load as load_distro
from ginn.domain import (
    DistroId,
    InstallMethod,
    InstallSource,
    ManifestEntry,
    PackageName,
    RealmName,
    RealmState,
    can_transition,
)
from ginn.errors import GinnError
from ginn.ops._mutating import OpEnv, is_finished
from ginn.ops.security_ops import leave_agent
from ginn.packages.aur import query_argv
from ginn.packages.manager import MAX_OUTPUT_CHARS, parse_names, shown
from ginn.packages.manifest import installed, removed
from ginn.podman.adapter import Podman
from ginn.podman.models import ContainerState
from ginn.runtime.spec import LABEL_GENERATION, LABEL_REALM
from ginn.services.runit_user import remove_service, service_dir
from ginn.services.supervise import signal_runsv_down
from ginn.store._time import utc_from_epoch
from ginn.store.audit import AuditEntry, AuditIndex
from ginn.store.journal import PARAMS_OVERFLOW, JournalEntry, JournalFile, damaged_reason
from ginn.store.locks import realm_lock
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore

OP_CREATE: Final = "create"
OP_START: Final = "start"
OP_STOP: Final = "stop"
OP_REBUILD: Final = "rebuild"
OP_UPDATE: Final = "update"
OP_DELETE: Final = "delete"
OP_RECOVER: Final = "recover"
"""The op of the entry that recovery writes for an operation that already had an audit entry."""
OP_SYNC: Final = "sync"
OP_EXPORT: Final = "export"
OP_INSTALL: Final = "install"
OP_REMOVE: Final = "remove"
STEP_INSTALLED: Final = "installed"
"""The journal step that an install writes after its package step (ops/packages.py)."""
STEP_REMOVED: Final = "removed"
STEP_GIT: Final = "git"
"""The AUR build's journal step after its git install (ops/packages.py, packages/aur.py)."""
STEP_DEPENDENCIES: Final = "dependencies"
"""The AUR build's journal step after its runtime dependencies are installed."""
STEP_TOOLCHAIN: Final = "toolchain"
"""The AUR build's journal step after its toolchain is installed."""
STEP_BUILT: Final = "built"
STEP_UPGRADING: Final = "upgrading"
"""The AUR build's journal step before each package install (packages/aur.py)."""
_QUERY_TIMEOUT_S: Final = 300.0
_REMOVE_TIMEOUT_S: Final = 300.0
"""The AUR build's journal step after its packages are installed, before the build-only removal."""
_AUR_STEPS: Final = frozenset(
    {STEP_GIT, STEP_DEPENDENCIES, STEP_TOOLCHAIN, STEP_UPGRADING, STEP_BUILT, STEP_REMOVED}
)
_ROOT_ENV: Final = {"LC_ALL": "C.UTF-8"}
"""The journal step that a remove writes after its package step (ops/packages.py)."""
OP_UNEXPORT: Final = "unexport"
_HOST_ONLY_OPS: Final = frozenset({OP_SYNC, OP_EXPORT, OP_UNEXPORT})
"""Operations that write host files only, never a realm record (see the module docstring)."""

START_COMPLETE_STEP: Final = "running"
"""The journal step of a start after its record is saved running (ops/lifecycle.py). Earlier steps
mean the start did not finish: the container is stopped and the record is left stopped."""

STOP_NOW_S: Final = 0
"""Seconds before a container that a crashed start left running is killed. The same rollback
wait as lifecycle's rollback (ops/lifecycle.py ROLLBACK_STOP_TIMEOUT_S)."""

OUTCOME_RECOVERED: Final = "recovered"
OUTCOME_BROKEN: Final = "broken"
OUTCOME_FINISHED: Final = "finished"
OUTCOME_LIVE: Final = "live"
OUTCOME_BUSY: Final = "busy"
OUTCOME_BLOCKED: Final = "blocked"
OUTCOME_CORRUPT: Final = "corrupt"

_REALM_BUSY: Final = "GINN-E-REALM-BUSY"
_LIVE_REASON: Final = "its process is still running; the entry is left untouched"
_FINISHED_REASON: Final = "the operation had finished; only its leftover journal file is removed"
_LOGGED_REASON: Final = "a recovery entry already names this operation; the journal file is removed"
_BY_PODMAN_FROM: Final = frozenset({RealmState.STOPPED, RealmState.RUNNING, RealmState.FAILED})
"""The record states that an op with no state of its own can leave an entry in."""


@dataclass(frozen=True)
class RecoveryOutcome:
    """What recovery did with one journal file. `outcome` is one of the OUTCOME_* values.

    `reason` says why, in a sentence. `path` is the journal file. `warnings` lists the damaged
    audit lines that the lookup skipped, so an outcome says when its lookup was incomplete.
    """

    realm: str
    op: str | None
    op_id: str | None
    outcome: str
    reason: str
    path: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Decision:
    """The verdict on one interrupted operation. `target` None means the record is not written.

    `stop_container` is the id of a container to stop before the record is written, or None.
    `manifest`, when set, is the Install Manifest to write with the record (FR-040, FR-041).
    `remove_names` and `remove_argv` are packages to remove best effort (FR-043), and `remove_in`
    is the running container they are removed from. Without a container they are reported left.
    """

    outcome: str
    target: RealmState | None
    reason: str
    stop_container: str | None = None
    manifest: list[ManifestEntry] | None = None
    remove_names: tuple[str, ...] = ()
    remove_argv: tuple[str, ...] = ()
    remove_in: str | None = None


def find_pending(env: OpEnv) -> list[JournalFile]:
    """Every journal file, live or dead, readable or corrupt, sorted by name (FR-122).

    An empty or missing journal directory gives an empty list. Nothing is changed.
    """
    return env.journal.scan()


def recover(
    env: OpEnv,
    podman: Podman,
    *,
    realm: RealmName | None = None,
) -> list[RecoveryOutcome]:
    """Recover the interrupted operations of every realm, or only of `realm`. One outcome per file.

    Live entries and corrupt files are reported and not changed. Dead entries are settled by the
    rules in the module docstring. Calling it again after a successful run does nothing. An empty
    journal returns at once, without reading the audit log.
    """
    pending = [f for f in find_pending(env) if realm is None or f.realm == realm.value]
    if not pending:
        return []
    lookup = _lookup(env)
    outcomes: list[RecoveryOutcome] = []
    for file in pending:
        if file.entry is None:
            outcomes.append(_outcome(file, OUTCOME_CORRUPT, damaged_reason(file)))
        elif file.is_live():
            outcomes.append(_outcome(file, OUTCOME_LIVE, _LIVE_REASON))
        elif isinstance(lookup, OSError):
            reason = (
                f"the audit log cannot be read ({lookup.strerror or lookup}), so it cannot be "
                "known whether this operation finished; the journal file is kept"
            )
            outcomes.append(_outcome(file, OUTCOME_BLOCKED, reason))
        else:
            outcomes.append(_recover_realm(env, podman, RealmName(file.realm), file, lookup))
    return outcomes


def _lookup(env: OpEnv) -> AuditIndex | OSError:
    """The audit log keyed by op_id, read once per call. An unreadable log is returned."""
    try:
        return env.audit.index_by_op_id()
    except OSError as exc:
        return exc


def _outcome(
    file: JournalFile,
    outcome: str,
    reason: str,
    *,
    warnings: tuple[str, ...] = (),
) -> RecoveryOutcome:
    entry = file.entry
    op = None if entry is None else entry.op
    return RecoveryOutcome(
        realm=file.realm,
        op=op,
        op_id=None if entry is None else _op_id(entry),
        outcome=outcome,
        reason=reason,
        path=str(file.path),
        warnings=warnings,
    )


def _op_id(entry: JournalEntry) -> str | None:
    value = entry.params.get("op_id")
    return value if isinstance(value, str) else None


def _recover_realm(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    scanned: JournalFile,
    index: AuditIndex,
) -> RecoveryOutcome:
    """Settle one dead entry under the realm lock.

    A GinnError or an OSError reports the entry as blocked, and the journal file stays, so the next
    run settles it again. Every step of that run is idempotent (see the module docstring). Any
    other exception propagates, because it is a bug and must not be reported as a settled state.
    """
    try:
        with realm_lock(env.paths, name, "recover"):
            settled = _settle_locked(env, podman, name, index)
    except GinnError as err:
        if err.code == _REALM_BUSY:
            return _outcome(scanned, OUTCOME_BUSY, f"the realm is busy: {err.message}")
        return _outcome(scanned, OUTCOME_BLOCKED, f"{err.code}: {err.message}")
    except OSError as err:
        reason = f"{err.strerror or err}; the journal file is kept, so the next run tries again"
        return _outcome(scanned, OUTCOME_BLOCKED, reason)
    if settled is None:
        # Another recovery or a finished operation removed the file after the scan.
        return _outcome(scanned, OUTCOME_FINISHED, "the journal file was already removed")
    return settled


def _settle_locked(
    env: OpEnv,
    podman: Podman,
    name: RealmName,
    index: AuditIndex,
) -> RecoveryOutcome | None:
    """Re-read the entry and its audit lookup under the lock, then decide, write and finish.

    None when the journal file is gone. The op_id lookup is repeated here, because the index read
    before the lock can be stale (rule 4 of the module docstring).
    """
    current = env.journal.load(name)
    if current is None:
        return None
    if current.entry is None:
        return _outcome(current, OUTCOME_CORRUPT, damaged_reason(current))
    entry = current.entry
    op_id = _op_id(entry)
    damaged = list(index.damaged)
    own: tuple[AuditEntry, ...] = ()
    logged = False
    if op_id is not None:
        fresh = env.audit.index_by_op_id(op_id)
        own = fresh.by_op_id.get(op_id, ())
        logged = op_id in fresh.recovers
        damaged.extend(where for where in fresh.damaged if where not in damaged)
    notes = tuple(f"audit line skipped as damaged: {where}" for where in damaged)
    if logged:
        env.journal.finish(entry)
        return _outcome(current, OUTCOME_FINISHED, _LOGGED_REASON, warnings=notes)
    if any(is_finished(stored.outcome, stored.error_code) for stored in own):
        # The operation completed, so its state is final: nothing is moved or written.
        env.journal.finish(entry)
        return _outcome(current, OUTCOME_FINISHED, _FINISHED_REASON, warnings=notes)

    store = RealmStore(env.paths, env.clock)
    try:
        decision = _decide(env.paths, podman, store, name, entry)
    except GinnError as err:
        return _outcome(current, OUTCOME_BLOCKED, f"{err.code}: {err.message}", warnings=notes)
    if decision.outcome == OUTCOME_BLOCKED:
        return _outcome(current, OUTCOME_BLOCKED, decision.reason, warnings=notes)

    # NFR-030 order: a podman stop, the record, the audit entry, then the journal file.
    if decision.stop_container is not None:
        podman.stop(decision.stop_container, STOP_NOW_S)
    decision = _remove_left_over(podman, decision)
    if decision.target is not None:
        store.set_state(name, decision.target)
    if decision.manifest is not None:
        _write_manifest(env, store, name, decision.manifest)
    _write_settlement(
        env, entry, op_id, own=bool(own), outcome=decision.outcome, reason=decision.reason
    )
    env.journal.finish(entry)
    filter_notes = _leave_filter(env, name, store, entry)
    return _outcome(current, decision.outcome, decision.reason, warnings=(*notes, *filter_notes))


def _leave_filter(
    env: OpEnv, name: RealmName, store: RealmStore, entry: JournalEntry
) -> tuple[str, ...]:
    """Leave the filter after an interrupted start or stop that does not leave the realm running.

    The realm lock is held here, so the filter's global lock is taken inside it (the lock order of
    ops/security_ops.py). A failure is returned as a note, and never raised.
    """
    if entry.op not in (OP_START, OP_STOP):
        return ()
    try:
        if store.load(name).state is RealmState.RUNNING:
            return ()
        leave_agent(env, name)
    except (GinnError, OSError) as err:
        detail = err.message if isinstance(err, GinnError) else str(err)
        return (f"agent filter not released: {detail}",)
    return ()


def _write_manifest(
    env: OpEnv,
    store: RealmStore,
    name: RealmName,
    manifest: list[ManifestEntry],
) -> None:
    """Save the Install Manifest that an interrupted package step did not reach (DR-007).

    The record write is atomic (DR-035), and the replay is idempotent: a second run computes the
    same manifest from the same journal and the same record.
    """
    record = store.load(name)
    store.save(replace(record, manifest=manifest, updated_at=utc_from_epoch(env.clock())))


def _names_of(value: object, distro: DistroId) -> list[str] | None:
    """A list of package names that each pass DR-003 for the realm's distribution (item 9), or None.

    A name that fails is refused before it reaches an argv or the manifest: the journal file is
    written by ginn, but a damaged or edited file is not trusted (NFR-SEC-005).
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    try:
        for item in value:
            PackageName(item, distro)
    except GinnError:
        return None
    return list(value)


def _journal_names(entry: JournalEntry, distro: DistroId) -> list[str] | None:
    """The package names the journal entry carries, or None when they are not valid names."""
    return _names_of(entry.params.get("packages"), distro)


def _decide_package(podman: Podman, record: RealmRecord, entry: JournalEntry) -> _Decision:
    """An interrupted install or remove. The state is decided as for any op with no state of its
    own. The manifest is replayed only when the journal step shows the package step finished."""
    if entry.op == OP_INSTALL and entry.params.get("aur") is True:
        return _decide_aur(podman, record, entry)
    replays = entry.op == OP_REMOVE and entry.step == STEP_REMOVED
    replays = replays or (entry.op == OP_INSTALL and entry.step == STEP_INSTALLED)
    decision = _decide_by_podman(podman, record, entry.op)
    if not replays or decision.outcome == OUTCOME_BLOCKED:
        return decision
    names = _journal_names(entry, record.settings.distro)
    if names is None:
        reason = "the journal entry names no valid package list, so the manifest is left as it is"
        return _Decision(OUTCOME_BLOCKED, None, f"{reason}; the entry is kept")
    if entry.op == OP_INSTALL:
        manifest = installed(
            record.manifest, names, source=InstallSource.REPO, method=InstallMethod.GINN
        )
    else:
        manifest = removed(record.manifest, names)
    reason = f"{decision.reason}; the Install Manifest now matches the interrupted {entry.op}"
    return replace(decision, manifest=manifest, reason=reason)


def _decide_aur(podman: Podman, record: RealmRecord, entry: JournalEntry) -> _Decision:
    """An interrupted AUR install (FR-043). The state is decided as for any op with no state of its
    own. The journal step says what is settled:

    - `upgrading`: some requested packages may be installed. The realm is queried, and each one that
      is installed is recorded in the manifest as `aur` from `ginn`. A query that exits non-zero
      keeps the entry (BLOCKED), so the next run asks again. When the realm is not running, the
      requested names are recorded without a check, since the upgrade may have run, and the warning
      says so. Nothing is removed, because the build-only set is not known yet.
    - `built`: the requested packages are installed, so the manifest is written. The journal's
      `removable` set is removed best effort (a removal pacman refuses is reported left installed).
    - `removed`: the manifest is written, and nothing is removed.
    - `git`, `dependencies` or `toolchain`: the operation is restored, so every package in `added`
      is removed best effort, and the manifest is left alone (AC-2).
    - any other step: nothing is known to be changed, so nothing is removed.

    A journal whose package sets overflowed (PARAMS_OVERFLOW, journal.py) does not know its
    packages. Then nothing is removed and the reason says so. The requested names still come from
    `packages`.
    """
    decision = _decide_by_podman(podman, record, entry.op)
    if decision.outcome == OUTCOME_BLOCKED or entry.step not in _AUR_STEPS:
        return decision
    distro = record.settings.distro
    names = _journal_names(entry, distro)
    if names is None:
        reason = "the journal entry names no valid package list, so the build is left as it is"
        return _Decision(OUTCOME_BLOCKED, None, f"{reason}; the entry is kept")
    overflowed = entry.params.get(PARAMS_OVERFLOW) is True
    running = _podman_state(podman, record) is ContainerState.RUNNING
    container = record.container_id if running else None
    notes: list[str] = []
    manifest: list[ManifestEntry] | None = None
    removable: list[str] | None = None
    unverified = False
    if entry.step in (STEP_BUILT, STEP_REMOVED):
        manifest = installed(
            record.manifest, names, source=InstallSource.AUR, method=InstallMethod.GINN
        )
    if entry.step == STEP_UPGRADING:
        if container is not None:
            result = podman.exec(
                container,
                query_argv(),
                user="0:0",
                env=_ROOT_ENV,
                timeout_s=_QUERY_TIMEOUT_S,
                max_output=MAX_OUTPUT_CHARS,
            )
            if result.exit_code != 0:
                # An unread listing settles nothing. The entry stays, and the next run asks again.
                reason = (
                    f"the installed packages could not be read (the query exited with status "
                    f"{result.exit_code}), so the entry is kept"
                )
                return _Decision(OUTCOME_BLOCKED, None, reason)
            found = sorted(parse_names(result.stdout, realm=record.name.value, step="query"))
            done = [name for name in names if name in found]
            if done:
                manifest = installed(
                    record.manifest, done, source=InstallSource.AUR, method=InstallMethod.GINN
                )
        else:
            # The realm is stopped, which a reboot usually causes. The upgrade may have run, so the
            # requested names are recorded unchecked. A rebuild reinstalls any that are missing.
            manifest = installed(
                record.manifest, names, source=InstallSource.AUR, method=InstallMethod.GINN
            )
            unverified = True
            notes.append(
                f"recorded without a check, since the realm is not running: {shown(names)}"
            )
        if not overflowed and (added := _names_of(entry.params.get("added"), distro)) is None:
            return _Decision(OUTCOME_BLOCKED, None, "the journal's added set is not valid; kept")
        if not overflowed and added:
            left = sorted(set(added) - set(names))
            notes.append(f"the build-only packages are not removed: {shown(left)}")
    elif entry.step == STEP_BUILT:
        if overflowed:
            notes.append(
                "the build-only packages are not known (the journal overflowed), so none is removed"
            )
        else:
            removable = _names_of(entry.params.get("removable"), distro)
            if removable is None:
                return _Decision(
                    OUTCOME_BLOCKED, None, "the journal's removable set is not valid; kept"
                )
    elif entry.step in (STEP_GIT, STEP_DEPENDENCIES, STEP_TOOLCHAIN):
        if overflowed:
            notes.append("the packages added so far are not known (the journal overflowed)")
        else:
            removable = _names_of(entry.params.get("added"), distro)
            if removable is None:
                return _Decision(
                    OUTCOME_BLOCKED, None, "the journal's added set is not valid; kept"
                )
    reason = decision.reason
    if manifest is not None and not unverified:
        reason = f"{reason}; the Install Manifest now matches the interrupted install"
    remove_names: tuple[str, ...] = ()
    remove_argv: tuple[str, ...] = ()
    if removable:
        remove_names = tuple(sorted(set(removable)))
        remove_argv = tuple(load_distro(distro.value).argv(Verb.REMOVE, list(remove_names)))
    if notes:
        reason = f"{reason}; {'; '.join(notes)}"
    return replace(
        decision,
        manifest=manifest,
        reason=reason,
        remove_names=remove_names,
        remove_argv=remove_argv,
        remove_in=container if remove_names else None,
    )


def _remove_left_over(podman: Podman, decision: _Decision) -> _Decision:
    """Remove the packages of an interrupted AUR build, best effort (FR-043, FR-122).

    The removal is bounded in time and output, as every root step is. A removal that stops at a
    limit, or that pacman refuses, is named in the reason, so the user sees it in the
    GINN-W-RECOVERED warning. The record is settled either way: a left-over package is a residual,
    not a reason to keep the journal forever.
    """
    if not decision.remove_names:
        return decision
    names = shown(list(decision.remove_names))
    if decision.remove_in is None:
        note = f"left installed, since the realm is not running: {names}"
    else:
        try:
            result = podman.exec(
                decision.remove_in,
                list(decision.remove_argv),
                user="0:0",
                env=_ROOT_ENV,
                timeout_s=_REMOVE_TIMEOUT_S,
                max_output=MAX_OUTPUT_CHARS,
                stdout_tail=True,
            )
        except GinnError as exc:
            reason = exc.details.get("reason")
            if exc.code == "GINN-E-INTERNAL" and isinstance(reason, str):
                note = f"left installed, since the removal stopped at its limit: {names}"
            else:
                note = f"left installed, since the removal failed ({exc.code}): {names}"
        else:
            if result.exit_code == 0:
                return decision
            note = f"left installed, since pacman refused the removal: {names}"
    return replace(decision, reason=f"{decision.reason}; build-only packages {note}")


def _write_settlement(
    env: OpEnv,
    entry: JournalEntry,
    op_id: str | None,
    *,
    own: bool,
    outcome: str,
    reason: str,
) -> None:
    """Write the one audit entry that settles `entry`, as the module docstring describes.

    `own` is True when an audit entry with the operation's op_id already exists. Then the entry is
    a separate `recover` entry naming the operation. Otherwise it is the operation's own entry, with
    the operation's start as ts, so `ginn log` shows it at the place the operation started.
    """
    step_params = {"step": entry.step, "reason": reason}
    if not own:
        params = {**entry.params, **step_params}
        env.audit.append(
            AuditEntry(
                user=env.user,
                realm=entry.realm,
                op=entry.op,
                outcome=outcome,
                params=params,
                error_code=None,
                ts=entry.started,
            )
        )
        return
    recover_params: dict[str, Any] = {
        "op_id": uuid.uuid4().hex,
        "recovers": "" if op_id is None else op_id,
        "recovered_op": entry.op,
        **step_params,
    }
    env.audit.append(
        AuditEntry(
            user=env.user,
            realm=entry.realm,
            op=OP_RECOVER,
            outcome=outcome,
            params=recover_params,
            error_code=None,
            ts=None,
        )
    )


def _decide(
    paths: OwnedPaths,
    podman: Podman,
    store: RealmStore,
    name: RealmName,
    entry: JournalEntry,
) -> _Decision:
    """The verdict for one interrupted operation. GinnError: no verdict can be reached."""
    if entry.op in _HOST_ONLY_OPS:
        reason = f"interrupted {entry.op}: only host files were written, so no realm state moves"
        return _Decision(OUTCOME_RECOVERED, None, reason)
    if not store.exists(name):
        reason = "no realm record exists, so no realm state needs a revert"
        return _Decision(OUTCOME_RECOVERED, None, reason)
    record = store.load(name)
    if entry.op == OP_CREATE:
        return _decide_create(record)
    if entry.op == OP_DELETE:
        return _decide_delete(podman, record)
    if entry.op == OP_REBUILD:
        return _decide_rebuild(paths, podman, record, entry)
    if entry.op == OP_UPDATE:
        return _decide_update(podman, record)
    if entry.op == OP_START:
        return _decide_start(podman, record, entry)
    if entry.op in (OP_INSTALL, OP_REMOVE):
        return _decide_package(podman, record, entry)
    return _decide_by_podman(podman, record, entry.op)


def _impossible(record: RealmRecord, op: str) -> _Decision:
    reason = f"an interrupted {op} cannot leave a realm in state {record.state.value}"
    return _Decision(OUTCOME_BLOCKED, None, f"{reason}; left as it is")


def _decide_create(record: RealmRecord) -> _Decision:
    if record.state is RealmState.STOPPED:
        reason = "the record already reads stopped, the last record write of a create"
        return _Decision(OUTCOME_RECOVERED, None, reason)
    if record.state in (RealmState.CREATING, RealmState.BROKEN):
        reason = (
            "interrupted create: marked broken; partial artifacts are left for "
            "'ginn create --resume' or 'ginn delete' (FR-122)"
        )
        return _move(record, RealmState.BROKEN, OUTCOME_BROKEN, reason)
    return _impossible(record, OP_CREATE)


def _decide_update(podman: Podman, record: RealmRecord) -> _Decision:
    if record.state is RealmState.UPDATING:
        reason = "interrupted update: marked broken (FR-120, DR-020)"
        return _move(record, RealmState.BROKEN, OUTCOME_BROKEN, reason)
    if record.state is RealmState.BROKEN:
        # A crash between the record write and the audit append: already settled.
        reason = "interrupted update: the record already reads broken, so nothing is moved"
        return _Decision(OUTCOME_BROKEN, None, reason)
    if record.state in _BY_PODMAN_FROM:
        return _decide_by_podman(podman, record, OP_UPDATE)
    return _impossible(record, OP_UPDATE)


def _decide_rebuild(
    paths: OwnedPaths, podman: Podman, record: RealmRecord, entry: JournalEntry
) -> _Decision:
    """The verdict for an interrupted rebuild (ops/rebuild.py, DR-034).

    Before the swap the record names gN and reads `rebuilding`. Then gN+1 is an orphan, and it is
    removed before the record returns to its previous state. The rebuild stops gN before it starts,
    so its previous state is `stopped`. A services-on orphan runs under the realm's runit service,
    which restarts a container that exits while runsv still wants it up. So runsv is told to stop
    it first (`d`, which does not wait), and the removal follows. After the swap the record names
    gN+1, and the journal says so (`new_generation` is the record's generation). Then gN is the
    orphan, removed best effort.
    """
    if record.state is RealmState.REBUILDING:
        previous = record.previous_state
        if previous is None:
            return _Decision(OUTCOME_BLOCKED, None, "a rebuilding record has no previous state")
        try:
            if record.settings.services or not record.services_at_create:
                signal_runsv_down(service_dir(paths, record.name))
            remove_generation(podman, record.name, record.generation + 1, keep=record.container_id)
            if not record.services_at_create:
                # The rebuild wrote the runit directory of a services-on gN+1 that gN does not have.
                remove_service(paths, record.name)
        except OSError as exc:
            return _runit_blocked(exc)
        reason = (
            f"interrupted rebuild: returned to {previous.value}; the old root stays intact until "
            "the atomic swap (DR-034)"
        )
        return _move(record, previous, OUTCOME_RECOVERED, reason)
    if record.state in _BY_PODMAN_FROM:
        swapped = entry.params.get("new_generation") == record.generation
        if swapped:
            with contextlib.suppress(GinnError):
                remove_below(podman, record.name, record.generation, keep=record.container_id)
            if not record.services_at_create:
                # The swap made the realm services-off, and the crash came before the rebuild
                # removed the directory of gN (ADR-003). The container's mode is what counts here.
                try:
                    signal_runsv_down(service_dir(paths, record.name))
                    remove_service(paths, record.name)
                except OSError as exc:
                    return _runit_blocked(exc)
        return _decide_by_podman(podman, record, OP_REBUILD)
    return _impossible(record, OP_REBUILD)


def _runit_blocked(exc: OSError) -> _Decision:
    """The verdict when the runit service of an interrupted rebuild cannot be stopped or removed.

    The journal entry is kept and the record is not moved, so the next recovery tries again. An
    OSError is not a GinnError, so it is reported as a blocked outcome here, as the other failures
    of a recovery are (`_settle_locked`).
    """
    reason = f"interrupted rebuild: the runit service could not be stopped or removed ({exc})"
    return _Decision(OUTCOME_BLOCKED, None, f"{reason}; the entry is kept")


def remove_below(podman: Podman, name: RealmName, generation: int, *, keep: str | None) -> int:
    """Stop and remove every container of realm `name` whose generation is below `generation`.

    The containers are found by the realm label, and the generation is read from each one's
    generation label (runtime/spec.py). `keep` is never removed. A container without a generation
    label is not one of the realm's roots, so it is left. Return the count removed. A rebuild uses
    this after the swap, so a generation whose removal failed earlier is swept too (DR-034).
    """
    targets = []
    for info in podman.ps({LABEL_REALM: name.value}):
        raw = info.labels.get(LABEL_GENERATION)
        if info.id == keep or raw is None or not raw.isdecimal():
            continue
        if int(raw) < generation:
            targets.append(info)
    for info in targets:
        with contextlib.suppress(GinnError):
            podman.stop(info.id, STOP_NOW_S)
        podman.rm(info.id, force=True)
    return len(targets)


def remove_generation(podman: Podman, name: RealmName, generation: int, *, keep: str | None) -> int:
    """Stop and remove the containers of one generation of realm `name`, and return their count.

    The containers are found by the realm and generation labels (runtime/spec.py), not by an id.
    `keep` is the id of the container that the record names now, which is never removed, even if
    the label filter lists it. The caller passes the generation of an orphan, not the record's. A
    stop waits for no time, as recovery's own stop does.
    """
    labels = {LABEL_REALM: name.value, LABEL_GENERATION: str(generation)}
    targets = [info for info in podman.ps(labels) if info.id != keep]
    for info in targets:
        with contextlib.suppress(GinnError):
            podman.stop(info.id, STOP_NOW_S)
        podman.rm(info.id, force=True)
    return len(targets)


def _decide_delete(podman: Podman, record: RealmRecord) -> _Decision:
    if record.state is RealmState.DELETING:
        return _Decision(OUTCOME_RECOVERED, None, "left deleting; run 'ginn delete' to finish")
    if record.state is RealmState.BROKEN:
        return _Decision(OUTCOME_RECOVERED, None, "left broken; run 'ginn delete' to finish")
    if record.state in _BY_PODMAN_FROM:
        return _decide_by_podman(podman, record, OP_DELETE)
    return _impossible(record, OP_DELETE)


def _decide_start(podman: Podman, record: RealmRecord, entry: JournalEntry) -> _Decision:
    if record.state not in _BY_PODMAN_FROM:
        return _impossible(record, OP_START)
    state = _podman_state(podman, record)
    if entry.step == START_COMPLETE_STEP or record.state is RealmState.RUNNING:
        return _to_podman_state(
            record, state, "interrupted start after its record was saved running"
        )
    # The start did not reach its final record save. The realm is left stopped, and a container
    # that podman still runs is stopped, so no realm runs without its grants attached.
    reason = (
        f"interrupted start at step {entry.step!r}, before its record was saved running: the "
        "container is stopped if it runs, and the record is left stopped"
    )
    decision = _move(record, RealmState.STOPPED, OUTCOME_RECOVERED, reason)
    if decision.outcome == OUTCOME_BLOCKED or state is not ContainerState.RUNNING:
        return decision
    return replace(decision, stop_container=record.container_id)


def _decide_by_podman(podman: Podman, record: RealmRecord, op: str) -> _Decision:
    """An op with no state of its own: the record takes the state that podman reports."""
    if record.state not in _BY_PODMAN_FROM:
        return _impossible(record, op)
    state = _podman_state(podman, record)
    return _to_podman_state(record, state, f"interrupted {op}")


def _to_podman_state(
    record: RealmRecord,
    state: ContainerState | None,
    what: str,
) -> _Decision:
    """Move the record to the state podman reports. No container is taken as stopped."""
    target = RealmState.RUNNING if state is ContainerState.RUNNING else RealmState.STOPPED
    return _move(record, target, OUTCOME_RECOVERED, f"{what}: the record now reads {target.value}")


def _podman_state(podman: Podman, record: RealmRecord) -> ContainerState | None:
    """The container's state, or None when podman lists no such container.

    `inspect` is asked first. When it fails, `ps` decides: only a list that succeeds and does not
    contain the id means the container is gone. A failed list raises GinnError, which blocks the
    entry, because a missing answer is not proof that the container is gone.
    """
    cid = record.container_id
    if cid is None:
        return None
    try:
        return podman.inspect(cid).state
    except GinnError:
        pass
    for info in podman.ps():
        if info.id == cid:
            return info.state
    return None


def _move(
    record: RealmRecord,
    target: RealmState,
    outcome: str,
    reason: str,
) -> _Decision:
    """Decide a move to `target`. A move DR-020 does not allow is blocked, never written."""
    if target is record.state:
        return _Decision(outcome, None, reason)
    if not can_transition(record.state, target, record.previous_state):
        blocked = f"DR-020 allows no move from {record.state.value} to {target.value}"
        return _Decision(OUTCOME_BLOCKED, None, f"{blocked}; left as it is")
    return _Decision(outcome, target, reason)


__all__ = [
    "OUTCOME_BLOCKED",
    "OUTCOME_BROKEN",
    "OUTCOME_BUSY",
    "OUTCOME_CORRUPT",
    "OUTCOME_FINISHED",
    "OUTCOME_LIVE",
    "OUTCOME_RECOVERED",
    "RecoveryOutcome",
    "find_pending",
    "recover",
]
