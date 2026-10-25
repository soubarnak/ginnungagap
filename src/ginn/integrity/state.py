# SPDX-License-Identifier: Apache-2.0
"""Host integrity state: the baseline, the pending change and the audited checks (FR-006, FR-124).

Two files live in `paths.integrity` (`$XDG_STATE_HOME/ginn/integrity`, directory mode 0700, files
mode 0600, written with write_json_atomic, DR-035):
- baseline.json: the fingerprint the host is expected to match, and the root it was taken from.
- pending.json: the paths that changed since the baseline and are not yet acknowledged.

Rules:
- The baseline changes only when it is first created, and when the user acknowledges. A change is
  never absorbed into the baseline without an acknowledgement.
- An acknowledgement accepts only the paths that were pending. If the host differs from the
  baseline in any other path, nothing is accepted: the new list becomes pending, and the call
  refuses with GINN-E-HOST-UNACKED listing those paths.
- While pending.json exists, a mutating operation is refused with GINN-E-HOST-UNACKED.
- Each check, operation or acknowledgement that writes these files appends one Audit Log entry:
  op "integrity" for a baseline created or a change found, op "integrity-ack" for an
  acknowledgement. A check that finds the host matching the baseline writes nothing and appends
  nothing.
- Ordering: the audit entry is appended before the state it records is written. A crash between
  the two leaves a duplicate entry after the re-run, never an unaudited event.
- The baseline records the root it was taken from. A check or an operation under another root is
  refused with GINN-E-USAGE, and the remedy names baseline.json and says to run
  'ginn integrity --ack'.
- A baseline.json or pending.json that does not parse stops a check or an operation with
  GINN-E-INTERNAL, and the remedy names the file. An acknowledgement treats such a file as absent,
  and it treats a baseline recorded under another root the same way. Each one is named under
  "discarded" in the audit entry with its reason, and the host is recaptured.

Mutating-operation protocol (T3.9 calls it around the operation):
    before = state.before_op(audit, user)           # refuses while pending or changed
    ...the operation...
    warning = state.after_op(before, audit, user)   # a change during the operation

`before_op` compares the host with the baseline, not only with the previous capture, so a change
made between two ginn operations is caught too. `after_op` compares with the `before` capture.

Locking: the compare-and-act methods (check, check_report, before_op, after_op, acknowledge,
acknowledge_report) take one blocking flock on the `lock` file in the integrity directory, so two
writers never interleave. A second writer waits; it is not refused. Callers need no lock. The
readers (baseline, pending, require_acknowledged, capture) take none: every file is replaced by
rename, so a reader sees the old file or the new one.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.integrity.fingerprint import HOST_ROOT, Fingerprint, PathStat, capture, diff
from ginn.store._time import now_iso
from ginn.store.audit import AuditEntry, AuditLog
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths
from ginn.store.records import fsync_dir, read_json, write_json_atomic

SCHEMA_VERSION = 1
MAX_AUDIT_PATHS = 200
PATH_SUMMARY_LIMIT = 20
BASELINE_FILE = "baseline.json"
PENDING_FILE = "pending.json"
LOCK_FILE = "lock"


class _Stored(NamedTuple):
    fingerprint: Fingerprint
    root: str


@dataclass(frozen=True)
class Report:
    """The outcome of a check or an acknowledgement, from one read of the state files.

    `changed` is what differs from the baseline; for an acknowledgement, it is the paths accepted.
    `pending` is the pending list after the call. `discarded` maps the name of each state file that
    was treated as absent to the reason (acknowledgement only). `count` is the number of paths in
    the baseline after the call, and `created` says that this call created the baseline.
    """

    created: bool
    changed: list[str]
    pending: list[str] | None
    discarded: dict[str, str]
    count: int


class IntegrityState:
    """The baseline and pending files under `paths.integrity`, for the host under `root_dir`."""

    def __init__(
        self,
        paths: OwnedPaths,
        root_dir: Path = HOST_ROOT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._paths = paths
        self._root = root_dir
        self._clock = clock

    # Readers. No lock is taken; see the module docstring.

    def capture(self) -> Fingerprint:
        """Fingerprint the host now. Writes nothing."""
        return capture(self._root)

    def baseline(self) -> Fingerprint | None:
        """The stored baseline, or None when none exists yet."""
        stored = self._stored_baseline()
        return None if stored is None else stored.fingerprint

    def pending(self) -> list[str] | None:
        """The unacknowledged changed paths, sorted, or None when nothing is pending."""
        return self._stored_pending()

    def require_acknowledged(self) -> None:
        """Refuse with GINN-E-HOST-UNACKED while a change is pending. The paths go in details."""
        paths = self.pending()
        if paths is not None:
            raise _unacknowledged(paths)

    # Compare-and-act methods. Each takes the integrity lock once, and calls only unlocked helpers.

    def before_op(self, audit: AuditLog, user: str) -> Fingerprint:
        """Check the host before a mutating operation and return its fingerprint (FR-006).

        Refuses while a change is pending. Creates the baseline when there is none. When the host
        differs from the baseline, the change is audited and recorded as pending, and the operation
        is refused with GINN-E-HOST-UNACKED, because the user must acknowledge it first.
        """
        with self._locked():
            self.require_acknowledged()
            current = self.capture()
            stored = self._stored_baseline()
            if stored is None:
                self._create_baseline(current, audit, user)
                return current
            self._require_same_root(stored)
            changed = diff(stored.fingerprint, current)
            if changed:
                self._flag(
                    changed,
                    stored.fingerprint,
                    current,
                    audit,
                    user,
                    against="baseline",
                    pending=None,
                )
                raise _unacknowledged(changed)
            return current

    def after_op(self, before: Fingerprint, audit: AuditLog, user: str) -> GinnWarning | None:
        """Compare the host with the `before` capture after an operation (FR-006).

        Returns GINN-W-HOST-CHANGED listing every changed path, after auditing it and recording it
        as pending. Returns None when nothing changed during the operation.
        """
        with self._locked():
            after = self.capture()
            changed = diff(before, after)
            if not changed:
                return None
            self._flag(
                changed,
                before,
                after,
                audit,
                user,
                against="before-operation",
                pending=self._stored_pending(),
            )
            return warn("GINN-W-HOST-CHANGED", paths=", ".join(changed))

    def check(self, audit: AuditLog, user: str) -> list[str]:
        """Compare the host with the baseline and return the changed paths (see check_report)."""
        return self.check_report(audit, user).changed

    def check_report(self, audit: AuditLog, user: str) -> Report:
        """Compare the host with the baseline for `ginn integrity`, reading each file once.

        With no baseline yet, the current host becomes the baseline and nothing is reported. A
        change is audited and recorded as pending, unless exactly that change is already pending.
        """
        with self._locked():
            current = self.capture()
            stored = self._stored_baseline()
            pending = self._stored_pending()
            if stored is None:
                self._create_baseline(current, audit, user)
                return Report(
                    created=True,
                    changed=[],
                    pending=pending,
                    discarded={},
                    count=len(current.entries),
                )
            self._require_same_root(stored)
            base = stored.fingerprint
            changed = diff(base, current)
            if changed:
                self._flag(changed, base, current, audit, user, against="baseline", pending=pending)
                pending = changed
            return Report(
                created=False,
                changed=changed,
                pending=pending,
                discarded={},
                count=len(base.entries),
            )

    def acknowledge(self, audit: AuditLog, user: str) -> list[str]:
        """Accept the pending change and return the accepted paths (see acknowledge_report)."""
        return self.acknowledge_report(audit, user).changed

    def acknowledge_report(self, audit: AuditLog, user: str) -> Report:
        """Accept the host as it is now, but only the paths the user has seen (FR-006).

        The paths that differ from the baseline must all be pending. If one is not, nothing is
        accepted: the full list becomes pending, it is audited, and the call refuses with
        GINN-E-HOST-UNACKED listing it. Otherwise the audit entry is written, then the baseline
        becomes the current fingerprint and the pending file is removed. A state file that is
        unreadable, or a baseline taken under another root, is treated as absent and named in
        `discarded` with its reason.
        """
        with self._locked():
            current = self.capture()
            discarded: dict[str, str] = {}
            stored = _tolerant(self._stored_baseline, BASELINE_FILE, discarded)
            if stored is not None and stored.root != str(self._root):
                discarded[BASELINE_FILE] = self._root_reason(stored)
                stored = None
            stored_pending = _tolerant(self._stored_pending, PENDING_FILE, discarded)
            was_pending = stored_pending or []
            base: Fingerprint | None = None
            changed: list[str] = []
            if stored is not None:
                base = stored.fingerprint
                changed = diff(base, current)
            unseen = sorted(set(changed) - set(was_pending))
            if base is not None and unseen:
                self._flag(
                    changed,
                    base,
                    current,
                    audit,
                    user,
                    against="baseline",
                    pending=stored_pending,
                )
                raise _unacknowledged(changed)
            outcome = "acknowledged"
            if discarded:
                outcome = f"acknowledged; state discarded and rewritten: {', '.join(discarded)}"
            self._audit(
                audit,
                user,
                "integrity-ack",
                outcome,
                {
                    "changed": _path_list(changed),
                    "was_pending": _path_list(was_pending),
                    "discarded": list(discarded),
                    "discard_reasons": dict(discarded),
                },
                None if base is None else {"digest": base.digest},
                {"digest": current.digest},
            )
            self._set_baseline(current)
            self._clear_pending()
            return Report(
                created=stored is None,
                changed=changed,
                pending=None,
                discarded=discarded,
                count=len(current.entries),
            )

    def _set_baseline(self, current: Fingerprint) -> None:
        """Store `current` as the baseline for this root. No audit entry: callers audit first.

        Private on purpose: a write that skips the lock or the audit-first rule would break both.
        """
        payload = {
            "schema_version": SCHEMA_VERSION,
            "captured_at": now_iso(self._clock),
            "root": str(self._root),
            "entries": {path: list(stat) for path, stat in current.entries.items()},
        }
        self._ensure_dir()
        write_json_atomic(self._baseline_path(), payload)

    # Unlocked helpers. They run under the lock taken by a public method above.

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_dir()
        fd = os.open(self._paths.integrity / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # Blocking on purpose: a second writer waits for the first one. It is never refused.
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _flag(
        self,
        changed: list[str],
        before: Fingerprint,
        after: Fingerprint,
        audit: AuditLog,
        user: str,
        *,
        against: str,
        pending: list[str] | None,
    ) -> None:
        """Audit `changed`, then record it as pending. Nothing is written when `pending` equals it.

        `against` names the reference of the comparison, "baseline" or "before-operation", so the
        entry reads the same either way. The audit entry comes first (see the module docstring).
        """
        if pending == changed:
            return
        self._audit(
            audit,
            user,
            "integrity",
            f"GINN-W-HOST-CHANGED: {len(changed)} path(s) changed",
            {"against": against, "changed": _path_list(changed)},
            {"digest": before.digest},
            {"digest": after.digest},
        )
        self._store_pending(changed)

    def _create_baseline(self, current: Fingerprint, audit: AuditLog, user: str) -> None:
        self._audit(
            audit,
            user,
            "integrity",
            "baseline created",
            {"count": len(current.entries)},
            None,
            {"digest": current.digest},
        )
        self._set_baseline(current)

    def _audit(
        self,
        audit: AuditLog,
        user: str,
        op: str,
        outcome: str,
        params: dict[str, Any],
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        """Append one host-integrity entry. Host events have no realm (FR-124)."""
        audit.append(
            AuditEntry(
                user=user,
                realm=None,
                op=op,
                outcome=outcome,
                params=params,
                before=before,
                after=after,
            )
        )

    def _stored_baseline(self) -> _Stored | None:
        path = self._baseline_path()
        if not path.is_file():
            return None
        data = _read_state(path)
        root = data.get("root")
        if not isinstance(root, str):
            raise _corrupt(path, "root must be a string")
        return _Stored(_fingerprint_from_json(data, path), root)

    def _stored_pending(self) -> list[str] | None:
        path = self._pending_path()
        if not path.is_file():
            return None
        return _paths_from_json(_read_state(path).get("paths"), path)

    def _require_same_root(self, stored: _Stored) -> None:
        if stored.root != str(self._root):
            raise self._root_error(stored)

    def _root_reason(self, stored: _Stored) -> str:
        return f"was made for root {stored.root}, not {self._root}"

    def _root_error(self, stored: _Stored) -> GinnError:
        """GINN-E-USAGE for a baseline under another root. The remedy names the file."""
        path = self._baseline_path()
        return GinnError(
            "GINN-E-USAGE",
            f"integrity baseline {path} {self._root_reason(stored)}",
            f"run 'ginn integrity --ack' to accept this host under root {self._root} and rewrite "
            f"{path}",
            exit_code=2,
            details={"path": str(path), "recorded_root": stored.root, "root": str(self._root)},
        )

    def _store_pending(self, changed: list[str]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "detected_at": now_iso(self._clock),
            "paths": changed,
        }
        self._ensure_dir()
        write_json_atomic(self._pending_path(), payload)

    def _clear_pending(self) -> None:
        path = self._pending_path()
        try:
            path.unlink()
        except FileNotFoundError:
            return
        fsync_dir(path.parent)

    def _ensure_dir(self) -> None:
        self._paths.integrity.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)

    def _baseline_path(self) -> Path:
        return self._paths.integrity / BASELINE_FILE

    def _pending_path(self) -> Path:
        return self._paths.integrity / PENDING_FILE


def _path_list(paths: list[str]) -> dict[str, Any]:
    """The audit form of a path list: its count, the first MAX_AUDIT_PATHS, and a truncated flag."""
    return {
        "count": len(paths),
        "paths": paths[:MAX_AUDIT_PATHS],
        "truncated": len(paths) > MAX_AUDIT_PATHS,
    }


def _path_summary(paths: list[str]) -> str:
    """The first PATH_SUMMARY_LIMIT paths, comma separated, then "and N more" when cut short."""
    shown = ", ".join(paths[:PATH_SUMMARY_LIMIT])
    extra = len(paths) - PATH_SUMMARY_LIMIT
    return shown if extra <= 0 else f"{shown} and {extra} more"


def _unacknowledged(paths: list[str]) -> GinnError:
    """GINN-E-HOST-UNACKED with the summary in the text and the full list in details."""
    err = make("GINN-E-HOST-UNACKED", paths=_path_summary(paths))
    err.details["paths"] = list(paths)
    return err


def _corrupt(path: Path, reason: str) -> GinnError:
    """GINN-E-INTERNAL for a state file that does not parse, with a remedy that names the file.

    The code is the catalogue's, but the message and remedy are built here, because the generic
    catalogue remedy does not name the file the user has to act on.
    """
    return GinnError(
        "GINN-E-INTERNAL",
        f"integrity state file {path} is not valid: {reason}",
        f"review 'ginn integrity', then run 'ginn integrity --ack' to accept the host and rewrite "
        f"{path}",
        exit_code=70,
        details={"path": str(path), "reason": reason},
    )


def _read_state(path: Path) -> dict[str, Any]:
    """Read one state file. A file that does not parse raises the file-naming GINN-E-INTERNAL."""
    try:
        return read_json(path, SCHEMA_VERSION)
    except GinnError as err:
        if err.code != "GINN-E-INTERNAL":
            raise
        raise _corrupt(path, str(err.details.get("reason", "not valid JSON"))) from err


def _tolerant[T](read: Callable[[], T | None], name: str, discarded: dict[str, str]) -> T | None:
    """Call `read`. An unreadable state file counts as absent: `discarded` maps its name to why."""
    try:
        return read()
    except GinnError as err:
        if err.code != "GINN-E-INTERNAL":
            raise
        discarded[name] = err.message
        return None


def _paths_from_json(value: object, path: Path) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _corrupt(path, "paths must be a list of strings")
    return list(value)


def _fingerprint_from_json(data: dict[str, Any], path: Path) -> Fingerprint:
    raw = data.get("entries")
    if not isinstance(raw, dict):
        raise _corrupt(path, "entries must be an object")
    entries = {str(key): _stat_from_json(value, path) for key, value in raw.items()}
    return Fingerprint.of(entries)


def _stat_from_json(value: object, path: Path) -> PathStat:
    if not isinstance(value, list) or len(value) != len(PathStat._fields):
        raise _corrupt(path, "a path record must be a list of the stat fields")
    kind, mode, uid, gid, size, mtime_ns, ctime_ns, inode, target, unreadable = value
    ints = (mode, uid, gid, size, mtime_ns, ctime_ns, inode)
    if not (
        isinstance(kind, str)
        and all(type(item) is int for item in ints)
        and (target is None or isinstance(target, str))
        and isinstance(unreadable, bool)
    ):
        raise _corrupt(path, "a path record has a field of the wrong type")
    return PathStat(kind, mode, uid, gid, size, mtime_ns, ctime_ns, inode, target, unreadable)
