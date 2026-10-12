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
- stop, install, remove, tier, perms, services, export and any other op with no state of its own:
  the record takes the state that podman reports, `running` or `stopped`, when the record is
  `stopped`, `running` or `failed`.
- rebuild: DR-034 keeps the old root until the atomic swap, so a `rebuilding` record returns to its
  previous state (DR-020). Other records are matched to podman, as above.
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

import uuid
from dataclasses import dataclass, replace
from typing import Any, Final

from ginn.domain import RealmName, RealmState, can_transition
from ginn.errors import GinnError
from ginn.ops._mutating import OpEnv, is_finished
from ginn.podman.adapter import Podman
from ginn.podman.models import ContainerState
from ginn.store.audit import AuditEntry, AuditIndex
from ginn.store.journal import JournalEntry, JournalFile, damaged_reason
from ginn.store.locks import realm_lock
from ginn.store.records import RealmRecord, RealmStore

OP_CREATE: Final = "create"
OP_START: Final = "start"
OP_STOP: Final = "stop"
OP_REBUILD: Final = "rebuild"
OP_UPDATE: Final = "update"
OP_DELETE: Final = "delete"
OP_RECOVER: Final = "recover"
"""The op of the entry that recovery writes for an operation that already had an audit entry."""

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
    """

    outcome: str
    target: RealmState | None
    reason: str
    stop_container: str | None = None


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
        decision = _decide(podman, store, name, entry)
    except GinnError as err:
        return _outcome(current, OUTCOME_BLOCKED, f"{err.code}: {err.message}", warnings=notes)
    if decision.outcome == OUTCOME_BLOCKED:
        return _outcome(current, OUTCOME_BLOCKED, decision.reason, warnings=notes)

    # NFR-030 order: a podman stop, the record, the audit entry, then the journal file.
    if decision.stop_container is not None:
        podman.stop(decision.stop_container, STOP_NOW_S)
    if decision.target is not None:
        store.set_state(name, decision.target)
    _write_settlement(
        env, entry, op_id, own=bool(own), outcome=decision.outcome, reason=decision.reason
    )
    env.journal.finish(entry)
    return _outcome(current, decision.outcome, decision.reason, warnings=notes)


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
    podman: Podman,
    store: RealmStore,
    name: RealmName,
    entry: JournalEntry,
) -> _Decision:
    """The verdict for one interrupted operation. GinnError: no verdict can be reached."""
    if not store.exists(name):
        reason = "no realm record exists, so no realm state needs a revert"
        return _Decision(OUTCOME_RECOVERED, None, reason)
    record = store.load(name)
    if entry.op == OP_CREATE:
        return _decide_create(record)
    if entry.op == OP_DELETE:
        return _decide_delete(podman, record)
    if entry.op == OP_REBUILD:
        return _decide_rebuild(podman, record)
    if entry.op == OP_UPDATE:
        return _decide_update(podman, record)
    if entry.op == OP_START:
        return _decide_start(podman, record, entry)
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


def _decide_rebuild(podman: Podman, record: RealmRecord) -> _Decision:
    if record.state is RealmState.REBUILDING:
        previous = record.previous_state
        if previous is None:
            return _Decision(OUTCOME_BLOCKED, None, "a rebuilding record has no previous state")
        reason = (
            f"interrupted rebuild: returned to {previous.value}; the old root stays intact until "
            "the atomic swap (DR-034)"
        )
        return _move(record, previous, OUTCOME_RECOVERED, reason)
    if record.state in _BY_PODMAN_FROM:
        return _decide_by_podman(podman, record, OP_REBUILD)
    return _impossible(record, OP_REBUILD)


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
