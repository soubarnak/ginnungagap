# SPDX-License-Identifier: Apache-2.0
"""The single choke point for mutating operations (DR-032, DR-033, FR-006, FR-123, FR-124).

Every mutating operation runs inside `mutating()`. Nothing else takes the realm lock for an
operation, runs the host integrity check around it, or writes its journal and audit entries, with
one exception: recovery (ops/recovery.py, T2.8) is the second lock holder and audit writer. It takes
the realm lock for an interrupted operation, writes that operation's audit entry (or a `recover`
entry that names it), and removes the journal file. It skips the host integrity check, because it
changes only ginn state, and an unacknowledged host change must not stop it. The steps, in order:

1. Exec lock, then realm lock (FR-123, DR-033, ADR-014). A held exec lock refuses the operation
   with GINN-E-REALM-BUSY, naming the running exec (see below). A second operation on the same
   realm is refused at once with GINN-E-REALM-BUSY, naming the running operation. It is never
   queued.
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

Exec lock (FR-123, ADR-014). A mutating exec holds `<runtime root>/exec/<realm>.lock` while it
runs. The check opens that file read-only and never creates it, so a missing file is not busy. The
operations named in EXEC_EXEMPT_OPS are not refused by it. Under the realm lock, and only while no
exec holds the exec lock, the realm's orphan marker is audited once and removed (FR-124). That
happens before the journal begins. A marker is read only when it is a regular file owned by the
user, and a symlink is never followed. The audit entry of a marker is not an operation entry: it
has no op_id and no journal entry.

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

import errno
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ginn.domain import RealmName
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.integrity.fingerprint import HOST_ROOT, Fingerprint
from ginn.integrity.state import IntegrityState
from ginn.services._exec_lock import (
    exec_lock_busy,
    exec_lock_dir,
    exec_lock_path,
    exec_orphan_path,
)
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
    exempt = op in EXEC_EXEMPT_OPS
    _check_exec_lock(env.paths, realm, exempt)  # before the realm lock (FR-123, ADR-014)
    with realm_lock(env.paths, realm, op):
        # The check repeats under the realm lock. An exec of the realm can start under that lock,
        # outlive its CLI and be released between the first check and this lock, so the first
        # check alone could miss it.
        busy = _check_exec_lock(env.paths, realm, exempt)
        if not busy:
            _settle_orphan(env, mutation, realm)  # a free lock means the marker is complete
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


EXEC_EXEMPT_OPS: Final = frozenset({"start", "stop", "service-exit", "crash", "idle-stop"})
"""Operations that run without the exec lock check (ADR-014). None of them starts a user's exec.

`start` needs its reason: an exec runs only inside a running container, and a start that passes the
state checks has no live exec. A supervised restart happens only after the container died, which
killed its processes. `stop` is explicit user intent, and the exec ends with the container. `crash`,
`idle-stop` and `service-exit` are the supervisor's own operations."""
ORPHAN_OP: Final = "exec-orphan"
ORPHAN_OUTCOME: Final = "orphan"
ORPHAN_CODE: Final = "GINN-W-RELAY-ORPHAN"
ORPHAN_MAX_BYTES: Final = 64 * 1024
"""The most of a marker that is read. A record is at most 256 pid lines (ADR-014), so this holds
many records. A longer marker is read up to the bound, its last record is marked truncated, and it
is kept rather than removed, because its later records were not read."""
HOLDER_MAX_BYTES: Final = 256
_TOKEN: Final = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class OrphanMarker:
    """One record of an orphan marker (ADR-014). `read_orphans` and `mutating()` both parse it."""

    token: str | None
    when: int | None
    pids: tuple[int, ...]
    kill_ran: bool
    truncated: bool
    unknown: tuple[int, ...] = ()
    """Processes whose token the kill could not read, and which it left alone (FR-124)."""
    join_failed: bool = False
    """No namespace join, so the kill could not name the realm's unreadable processes (FR-124)."""

    def pids_text(self) -> str:
        """The clause that names the processes, for the `pids` of GINN-W-RELAY-ORPHAN."""
        if not self.kill_ran:
            return "the token kill did not run, so no pid is recorded"
        parts = []
        if self.pids:
            survivors = ", ".join(str(pid) for pid in self.pids)
            parts.append(f"pids {survivors} survived the token kill")
        if self.join_failed:
            parts.append("processes unknown (token not readable)")
        if self.unknown:
            named = ", ".join(str(pid) for pid in self.unknown)
            parts.append(f"unknown pids (token not readable): {named}")
        return "; ".join(parts) if parts else "the token kill recorded no pid"


def _check_exec_lock(paths: OwnedPaths, realm: RealmName, exempt: bool) -> bool:
    """Refuse a user operation while a mutating exec of `realm` holds its lock (FR-123, ADR-014).

    Returns whether an exec holds the lock. An exempt operation is never refused, so the caller
    learns from the result whether the orphan marker may be read. A faulty lock path raises
    GINN-E-INTERNAL from exec_lock_busy. A user operation then fails with it, and an exempt one
    goes on as if the lock were busy, so the marker is left alone.
    """
    try:
        busy = exec_lock_busy(paths, realm)
    except GinnError:
        if not exempt:
            raise
        return True
    if busy and not exempt:
        raise make("GINN-E-REALM-BUSY", op=_exec_label(paths, realm))
    return busy


def _exec_label(paths: OwnedPaths, realm: RealmName) -> str:
    """The `{op}` of the refusal: the running exec, named from the holder line (ADR-014).

    The holder line is `run <pid> podman <pid>`. ginn-exec-run writes it after the fork and clears
    it before the lock is released, so it can be empty for a moment. A run pid that no longer
    exists is treated as empty too, as the relay's own refusal does.
    """
    words = _read_holder(exec_lock_path(paths, realm)).split()
    if len(words) == 4 and words[0] == "run" and words[2] == "podman" and words[1].isdecimal():
        run = int(words[1])
        if run >= 1 and _alive(run) and words[3].isdecimal():
            return (
                f"an exec of realm {realm.value!r} (ginn-exec-run pid {run}, podman pid {words[3]})"
            )
    return f"an exec of realm {realm.value!r} (holder not yet recorded)"


def _read_holder(path: Path) -> str:
    """The holder line of a lock file, read without following a link. Empty when unreadable."""
    fd = _open_unfollowed(path, os.O_RDONLY | os.O_NONBLOCK)
    if fd is None:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        return os.read(fd, HOLDER_MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""
    finally:
        os.close(fd)


def _alive(pid: int) -> bool:
    """True when `pid` names a process that exists. Signal 0 sends nothing."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OverflowError, ProcessLookupError):
        return False
    return True


def _open_unfollowed(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int | None:
    """Open `path` without following a final symlink. None when it is absent or is a link."""
    try:
        return os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        raise


def parse_orphans(raw: bytes) -> tuple[OrphanMarker, ...]:
    """The records of an orphan marker's bytes, in file order (ginn-exec-kill.c, ginn-exec-run.c).

    A record starts at each `token <hex>` line and runs to the next one. Its other lines are `time
    <epoch>`, then `pid <n>` lines, `unknown <n>` lines for processes the kill could not read, or
    `kill not run` when the kill could not start, or `unknown join` when it had no namespace join.
    Lines that do not parse are skipped. A last line
    without its newline is torn, as a crash left it, and is dropped (AuditLog._read_file does the
    same). A marker longer than ORPHAN_MAX_BYTES is cut off, and the last record it reaches is
    marked truncated. The token is kept: it names the processes that the kill missed, and it grants
    nothing.
    """
    text = raw[:ORPHAN_MAX_BYTES].decode("utf-8", errors="replace")
    lines = text.split("\n")
    if not text.endswith("\n"):
        lines.pop()
    blocks: list[list[str]] = []
    for line in lines:
        if line.startswith("token "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    truncated = len(raw) > ORPHAN_MAX_BYTES
    records: list[OrphanMarker] = []
    for index, block in enumerate(blocks):
        token: str | None = None
        when: int | None = None
        pids: list[int] = []
        unknown: list[int] = []
        join_failed = False
        kill_ran = True
        for line in block:
            key, _, value = line.partition(" ")
            if key == "token" and _TOKEN.fullmatch(value):
                token = value
            elif key == "time" and value.isdecimal():
                when = int(value)
            elif key == "pid" and value.isdecimal():
                pids.append(int(value))
            elif key == "unknown" and value.isdecimal():
                unknown.append(int(value))
            elif line == "unknown join":
                join_failed = True
            elif line == "kill not run":
                kill_ran = False
        records.append(
            OrphanMarker(
                token=token,
                when=when,
                pids=tuple(pids),
                kill_ran=kill_ran,
                truncated=truncated and index == len(blocks) - 1,
                unknown=tuple(unknown),
                join_failed=join_failed,
            )
        )
    return tuple(records)


def read_orphans(paths: OwnedPaths, realm: RealmName) -> tuple[OrphanMarker, ...]:
    """The records of the realm's orphan marker, read without changing anything (FR-124, ADR-014).

    `ginn status` calls this. It neither audits nor removes the marker, which `mutating()` does at
    the realm's next operation. A marker that is a link, a directory or not owned by the user gives
    no records, as it does for `mutating()`, and nothing is created. No marker gives the empty
    tuple.
    """
    dir_fd = _open_exec_dir(paths)
    if dir_fd is None:
        return ()
    try:
        raw = _marker_bytes(dir_fd, exec_orphan_path(paths, realm).name)
    finally:
        os.close(dir_fd)
    return () if raw is None else parse_orphans(raw)


def _open_exec_dir(paths: OwnedPaths) -> int | None:
    """A descriptor of `exec/`, or None when it is absent or is a link. Nothing is created."""
    return _open_unfollowed(exec_lock_dir(paths), os.O_RDONLY | os.O_DIRECTORY)


def _marker_bytes(dir_fd: int, name: str) -> bytes | None:
    """The marker's bytes, at most ORPHAN_MAX_BYTES + 1 of them, or None when it is not ours.

    Only a regular file owned by the user is read. It is opened through `dir_fd` without following a
    link, and without blocking, so a FIFO cannot stall the command.
    """
    fd = _open_unfollowed(name, os.O_RDONLY | os.O_NONBLOCK, dir_fd=dir_fd)
    if fd is None:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        return os.read(fd, ORPHAN_MAX_BYTES + 1)
    finally:
        os.close(fd)


def _settle_orphan(env: OpEnv, mutation: Mutation, realm: RealmName) -> None:
    """Audit the realm's orphan records, then remove the marker (FR-124, ADR-014). Best effort.

    Called under the realm lock while no exec holds the exec lock, so the token kill has finished
    writing the marker. Each record is shown on the operation as GINN-W-RELAY-ORPHAN. Each gets one
    audit entry, unless the Audit Log already holds one for its token and time, so a crash after an
    append does not repeat it. The marker is removed only after its entries are written. A marker
    longer than the read bound is not removed: its records past the bound were not read, so they
    would be lost. It is kept, and the next operation audits the same records as already held. An
    error never fails the operation. The marker is then kept, and GINN-W-ORPHAN-UNSETTLED says so.
    """
    try:
        markers = read_orphans(env.paths, realm)
        if not markers:
            return
        mutation.warnings.extend(_orphan_warning(realm, marker) for marker in markers)
        _audit_orphans(env, realm, markers)
        if any(marker.truncated for marker in markers):
            detail = (
                f"the marker is over {ORPHAN_MAX_BYTES} bytes, so its later records are not read"
            )
            mutation.warnings.append(
                warn("GINN-W-ORPHAN-UNSETTLED", realm=realm.value, detail=detail)
            )
            return
        _remove_marker(env.paths, realm)
    except (OSError, GinnError) as exc:
        detail = _reason(exc)
        mutation.warnings.append(warn("GINN-W-ORPHAN-UNSETTLED", realm=realm.value, detail=detail))


def _orphan_warning(realm: RealmName, marker: OrphanMarker) -> GinnWarning:
    """The GINN-W-RELAY-ORPHAN notice for one record of the realm's marker."""
    return warn("GINN-W-RELAY-ORPHAN", realm=realm.value, pids=marker.pids_text())


def _audit_orphans(env: OpEnv, realm: RealmName, markers: tuple[OrphanMarker, ...]) -> None:
    """Append one audit entry per record that the Audit Log does not hold yet (FR-124)."""
    held = _audited_orphans(env, realm)
    for marker in markers:
        key = _orphan_key(marker)
        if key is not None and key in held:
            continue
        env.audit.append(_orphan_entry(env, realm, marker))
        if key is not None:
            held.add(key)  # a second record with the same token and time is audited once


def _audited_orphans(env: OpEnv, realm: RealmName) -> set[tuple[str, str]]:
    """The (token, time) of each orphan entry that the Audit Log already holds for the realm."""
    held: set[tuple[str, str]] = set()
    for entry in env.audit.read(realm=realm):
        token = entry.params.get("token")
        if entry.op == ORPHAN_OP and isinstance(token, str) and entry.ts is not None:
            held.add((token, entry.ts))
    return held


def _orphan_key(marker: OrphanMarker) -> tuple[str, str] | None:
    """The (token, time) that identifies a record in the Audit Log, or None when it has neither."""
    ts = _orphan_ts(marker.when)
    if marker.token is None or ts is None:
        return None
    return marker.token, ts


def _orphan_ts(when: int | None) -> str | None:
    """The marker's time as an audit timestamp, or None when it is absent or does not convert."""
    if when is None:
        return None
    try:
        return utc_iso(utc_from_epoch(when))
    except (OverflowError, OSError, ValueError):
        return None


def _orphan_entry(env: OpEnv, realm: RealmName, marker: OrphanMarker) -> AuditEntry:
    """The audit entry for one record. Its ts is the record's time when that converts, else now.

    The entry is not an operation: it has no op_id, so recovery never indexes it.
    """
    return AuditEntry(
        user=env.user,
        realm=realm.value,
        op=ORPHAN_OP,
        outcome=ORPHAN_OUTCOME,
        params={
            "token": marker.token,
            "pids": list(marker.pids),
            "unknown": list(marker.unknown),
            "join_failed": marker.join_failed,
            "kill_ran": marker.kill_ran,
            "truncated": marker.truncated,
        },
        error_code=ORPHAN_CODE,
        ts=_orphan_ts(marker.when),
    )


def _remove_marker(paths: OwnedPaths, realm: RealmName) -> None:
    """Unlink the realm's marker through a descriptor of `exec/`. An absent marker is no error."""
    dir_fd = _open_exec_dir(paths)
    if dir_fd is None:
        return
    try:
        try:
            os.unlink(exec_orphan_path(paths, realm).name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(dir_fd)


def _reason(exc: Exception) -> str:
    """The text of an exception for the detail of a warning."""
    return exc.message if isinstance(exc, GinnError) else f"{type(exc).__name__}: {exc}"
