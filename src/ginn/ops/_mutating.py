# SPDX-License-Identifier: Apache-2.0
"""The single choke point for mutating operations (DR-032, DR-033, FR-006, FR-123, FR-124).

Every mutating operation runs inside `mutating()`. Nothing else takes the realm lock for an
operation, runs the host integrity check around it, or writes its journal and audit entries, with
one exception: recovery (ops/recovery.py, T2.8) is the second lock holder and audit writer. It takes
the realm lock for an interrupted operation, writes that operation's audit entry (or a `recover`
entry that names it), and removes the journal file. It skips the host integrity check, because it
changes only ginn state, and an unacknowledged host change must not stop it. The steps, in order:

1. Realm lock (FR-123, DR-033). A second operation on the same realm is refused at once with
   GINN-E-REALM-BUSY, naming the running operation. It is never queued.
2. Host integrity before check (FR-006), through `IntegrityState.before_op`. It refuses with
   GINN-E-HOST-UNACKED while a change is unacknowledged. The changed paths are in the error's
   `details["paths"]`. This module takes no global lock around before_op or after_op. The
   integrity state takes its own short blocking lock inside them, so a concurrent
   `ginn integrity` makes this step wait rather than refuse.
3. Journal begin (FR-122, NFR-030). The entry's params carry `op_id`, a fresh uuid4 hex. An entry
   left by an earlier interrupted operation is never replaced: GINN-E-RECOVERY-PENDING is raised.
4. The body, which the caller runs inside the `with` block.
5. Host integrity after check (FR-006), through `IntegrityState.after_op`. A change found here
   becomes a GINN-W-HOST-CHANGED warning in `Mutation.warnings`, and the integrity state records
   it as pending.
6. One audit entry (FR-124, NFR-043). Its `ts` is the start of the operation, not its end (DR-032).
   The start is read once, at entry, and the journal entry carries the same instant. The params
   carry `op_id`, and the outcome is "ok", "error" or "interrupted".
7. Journal finish, but only when the outcome is "ok" or the body raised a GinnError. Otherwise the
   journal entry stays for recovery (see below).
8. Realm lock released.

Steps 1 to 3 are entry. A refusal there is raised at once. The operation never began, so it writes
no journal entry and no operation audit entry. A host change that causes a refusal is still
audited by the integrity state as a host-integrity event (FR-124). Steps 5 to 8 run on every exit
from the body: success, GinnError, KeyboardInterrupt or any other exception.

Crash evidence for recovery (T8.5, FR-122, NFR-030). The audit entry is appended before the
journal is finished. Recovery reads a journal entry together with the audit entries that have the
same `op_id`. `is_finished` decides whether such an entry means the operation completed. An outcome
of "ok", or an error with an error code, counts as finished. So do "recovered" and "broken", which
recovery writes. Recovery then only removes the leftover journal file. Any other journal entry is an
interrupted operation: no audit entry, outcome "interrupted", or outcome "error" without a code.
Recovery moves the realm to `broken` or reverts it (FR-122). The journal is finished only for the
finished outcomes, so an interrupt or any other exception keeps its entry. When the audit append
itself fails, the journal entry is kept too, because it is then the only record of the operation.

Body contract. When the body raises GinnError, it must already have rolled back its own changes.
That is why such an operation counts as finished. Any other exception, and KeyboardInterrupt, means
the body may have stopped part way, so its journal entry is kept for recovery.

Exit failures. After_op runs and the audit entry is written even when the body failed. A failure in
after_op is recorded as `integrity_after` in the audit params, and the audit entry is still
written. If the audit append fails, the journal is not finished. No ordinary exception in an exit
step stops the remaining steps. A KeyboardInterrupt does: it leaves the journal entry and no audit
line, which recovery reads as an interrupted operation. When the body raised, every exit failure is
added to the body's exception as a note, and the body's exception is re-raised unchanged. When the
body raised and the after check found a host change, its GINN-W-HOST-CHANGED text is added to that
exception as a note too. When the body succeeded, the first exit failure is raised after all steps
have run and the realm lock is released.

Ordering (DR-032). Each audit entry is appended when its operation finishes, with `ts` set to the
start time. `AuditLog.read` sorts by `ts`, so entries come back in start order, across realms too.
Entries with equal `ts` keep their file order. A reader still cannot see an operation that is in
flight, so it can show a later-started operation that finished before an earlier one still running.

This module makes no subprocess call and touches no podman.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ginn.domain import RealmName
from ginn.errors import GinnError
from ginn.errors import Warning as GinnWarning
from ginn.integrity.fingerprint import HOST_ROOT, Fingerprint
from ginn.integrity.state import IntegrityState
from ginn.store._time import utc_from_epoch, utc_iso
from ginn.store.audit import AuditEntry, AuditLog
from ginn.store.journal import Journal, JournalEntry
from ginn.store.locks import realm_lock
from ginn.store.paths import OwnedPaths


@dataclass(frozen=True)
class OpEnv:
    """What one mutating operation needs: the stores, the user name and the clock.

    `clock` returns epoch seconds. Tests pass a fake clock and the same clock to every store.
    """

    paths: OwnedPaths
    audit: AuditLog
    integrity: IntegrityState
    journal: Journal
    user: str
    clock: Callable[[], float] = time.time

    @classmethod
    def for_paths(
        cls,
        paths: OwnedPaths,
        user: str,
        *,
        root_dir: Path = HOST_ROOT,
        clock: Callable[[], float] = time.time,
    ) -> OpEnv:
        """Build the stores for `paths`, with the host checked under `root_dir`."""
        return cls(
            paths=paths,
            audit=AuditLog(paths, clock),
            integrity=IntegrityState(paths, root_dir, clock),
            journal=Journal(paths),
            user=user,
            clock=clock,
        )


class Mutation:
    """What the body of one mutating operation may record about itself. `mutating()` yields it."""

    def __init__(
        self,
        op: str,
        realm: RealmName,
        op_id: str,
        started: str,
        params: Mapping[str, Any],
        journal: Journal,
    ) -> None:
        self.op = op
        self.realm = realm
        self.op_id = op_id
        self.started = started
        self.params: dict[str, Any] = dict(params)
        self.before_permissions: dict[str, Any] | None = None
        self.after_permissions: dict[str, Any] | None = None
        # GINN-W-HOST-CHANGED from the after check. The caller emits these once the body ends.
        self.warnings: list[GinnWarning] = []
        self._journal = journal
        self._entry: JournalEntry | None = None

    def step(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        drop: Sequence[str] = (),
    ) -> None:
        """Record in the journal that the body reached step `name` (FR-122).

        `params` are journal-only: they are merged into the journal entry and never written to the
        audit entry, and `drop` removes journal params. An AUR build records its package sets this
        way (FR-043).
        """
        if self._entry is None:
            raise RuntimeError("step() is only valid inside the journaled body")
        self._entry = self._journal.step(self._entry, name, params, drop=drop)

    def set_permissions(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
        """Record the permission diff of this operation for the audit entry (FR-124 AC-1)."""
        self.before_permissions = dict(before)
        self.after_permissions = dict(after)

    def add_params(self, **values: Any) -> None:
        """Add or replace audit parameters, such as the resolved paths of the operation."""
        self.params.update(values)

    def _recorded_params(self) -> dict[str, Any]:
        """The params stored in the audit entry and the journal entry: the caller's, plus op_id."""
        return {**self.params, "op_id": self.op_id}


@contextmanager
def mutating(
    env: OpEnv,
    op: str,
    realm: RealmName,
    params: Mapping[str, Any] | None = None,
) -> Iterator[Mutation]:
    """Run one mutating operation on `realm` through the choke point (see the module docstring).

    Entry refusals (GINN-E-REALM-BUSY, GINN-E-HOST-UNACKED, GINN-E-RECOVERY-PENDING) propagate
    before the body runs, and write no operation entry. The body's own exceptions propagate
    unchanged after the exit steps.
    """
    started_at = env.clock()  # the one reading for this operation: audit ts and journal started
    started = utc_iso(utc_from_epoch(started_at))
    mutation = Mutation(op, realm, uuid.uuid4().hex, started, params or {}, env.journal)
    with realm_lock(env.paths, realm, op):
        before, entry = _enter(env, mutation, started_at)
        try:
            yield mutation
        except BaseException as exc:
            _settle(env, mutation, before, entry, exc)
            raise
        _settle(env, mutation, before, entry, None)


def _enter(env: OpEnv, mutation: Mutation, started_at: float) -> tuple[Fingerprint, JournalEntry]:
    """Steps 2 and 3: the host check before the body, then the journal entry. Raises on refusal."""
    before = env.integrity.before_op(env.audit, env.user)
    entry = env.journal.begin(
        mutation.op, mutation.realm, mutation._recorded_params(), clock=lambda: started_at
    )
    mutation._entry = entry
    return before, entry


def _settle(
    env: OpEnv,
    mutation: Mutation,
    before: Fingerprint,
    entry: JournalEntry,
    failure: BaseException | None,
) -> None:
    """Steps 5 to 8: the host check after the body, the audit entry, the journal finish.

    Never raises while `failure` is set: its exit failures become notes on `failure`, which the
    caller re-raises. With no `failure`, the first exit failure is raised after every step ran.
    """
    problems: list[Exception] = []
    params = mutation._recorded_params()

    try:
        warning = env.integrity.after_op(before, env.audit, env.user)
    except Exception as exc:
        problems.append(exc)
        params["integrity_after"] = _label(exc)
    else:
        if warning is not None:
            mutation.warnings.append(warning)
            if failure is not None:
                failure.add_note(f"{warning.code}: {warning.message}. {warning.remedy}")

    outcome, error_code = _outcome(failure)
    audited = False
    try:
        env.audit.append(
            AuditEntry(
                user=env.user,
                realm=mutation.realm.value,
                op=mutation.op,
                outcome=outcome,
                params=params,
                before=mutation.before_permissions,
                after=mutation.after_permissions,
                error_code=error_code,
                ts=mutation.started,
            )
        )
        audited = True
    except Exception as exc:
        problems.append(exc)

    if audited and is_finished(outcome, error_code):
        try:
            env.journal.finish(entry)
        except Exception as exc:
            problems.append(exc)

    _report(problems, failure)


_RECOVERY_OUTCOMES: Final = frozenset({"recovered", "broken"})


def is_finished(outcome: str, error_code: str | None) -> bool:
    """True when an audit entry with this outcome and error code means its operation completed.

    "ok" and any error with a code are finished (FR-122). So are "recovered" and "broken", the two
    outcomes that recovery writes (ops/recovery.py). "interrupted", and an error without a code, are
    not finished. The operation's own journal finish and recovery both use this predicate.
    """
    return outcome == "ok" or outcome in _RECOVERY_OUTCOMES or error_code is not None


def _outcome(failure: BaseException | None) -> tuple[str, str | None]:
    """The audit outcome and error code for how the body ended (DR-050)."""
    if failure is None:
        return "ok", None
    if isinstance(failure, GinnError):
        return "error", failure.code
    if isinstance(failure, KeyboardInterrupt):
        return "interrupted", None
    return "error", None


def _label(exc: Exception) -> str:
    """A short name for an exit failure: its catalogue code when it has one, else its type."""
    if isinstance(exc, GinnError):
        return exc.code
    return type(exc).__name__


def _report(problems: list[Exception], failure: BaseException | None) -> None:
    """Attach exit failures to `failure`, or raise the first one when the body succeeded."""
    if not problems:
        return
    if failure is not None:
        for problem in problems:
            failure.add_note(f"exit step failed: {_label(problem)}: {problem}")
        return
    first, *rest = problems
    for problem in rest:
        first.add_note(f"further exit failure: {_label(problem)}: {problem}")
    raise first
