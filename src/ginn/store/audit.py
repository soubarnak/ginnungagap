# SPDX-License-Identifier: Apache-2.0
"""Audit Log: append-only JSON Lines record of operations (DR-050, FR-124, FR-125, NFR-SEC-008).

One JSON object per line in `paths.audit_log` (mode 0600). Each append runs under an exclusive
flock on the audit lock file, so concurrent writers never double-rotate or interleave lines.
Rotation comes first: when the current file has reached `max_bytes` (10 MiB by default, DR-019,
AS-021) it is shifted to `audit.log.1`, older rotated files move up one index, and the oldest
beyond `keep` (5) is removed. Every append is fsynced before it returns.

There is no delete or rewrite API. A torn last line, left by a crash mid-write, is skipped by
`read`. Any other unparseable line raises GINN-E-INTERNAL.

Timestamps come from `clock`, which returns epoch seconds, unless the entry already has `ts`.
"""

import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ginn.domain import RealmName
from ginn.errors import GinnError, make
from ginn.store._time import now_iso, parse_utc
from ginn.store.locks import audit_lock_path
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths
from ginn.store.records import fsync_dir

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # DR-019, AS-021
DEFAULT_KEEP = 5  # DR-019, AS-021


@dataclass(frozen=True)
class AuditEntry:
    """One operation record with the DR-050 fields. `ts` is set by `AuditLog.append` if None."""

    user: str
    realm: str | None
    op: str
    outcome: str
    params: dict[str, Any] = field(default_factory=dict)
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    error_code: str | None = None
    ts: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "user": self.user,
            "realm": self.realm,
            "op": self.op,
            "params": self.params,
            "before": self.before,
            "after": self.after,
            "outcome": self.outcome,
            "error_code": self.error_code,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any], source: str) -> "AuditEntry":
        try:
            params = data["params"]
            if not isinstance(params, dict):
                raise TypeError("params must be an object")
            return cls(
                ts=_str(data["ts"]),
                user=_str(data["user"]),
                realm=_opt_str(data["realm"]),
                op=_str(data["op"]),
                outcome=_str(data["outcome"]),
                params=dict(params),
                before=_opt_dict(data["before"]),
                after=_opt_dict(data["after"]),
                error_code=_opt_str(data["error_code"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            err = make("GINN-E-INTERNAL")
            err.details.update({"path": source, "reason": f"{type(exc).__name__}: {exc}"})
            raise err from exc


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {type(value).__name__}")
    return value


def _opt_str(value: object) -> str | None:
    return None if value is None else _str(value)


def _opt_dict(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("expected an object or null")
    return dict(value)


def _rotated(log: Path, index: int) -> Path:
    return log.with_name(f"{log.name}.{index}")


class AuditLog:
    """Append and read the Audit Log. There is deliberately no way to delete or edit entries."""

    def __init__(
        self,
        paths: OwnedPaths,
        clock: Callable[[], float] = time.time,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep: int = DEFAULT_KEEP,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if keep < 1:
            raise ValueError("keep must be at least 1")
        self._paths = paths
        self._clock = clock
        self._max_bytes = max_bytes
        self._keep = keep

    def append(self, entry: AuditEntry) -> AuditEntry:
        """Write `entry` as one line, rotating first if needed. Returns the stored entry."""
        stamped = entry if entry.ts is not None else replace(entry, ts=now_iso(self._clock))
        line = (json.dumps(stamped.to_json(), sort_keys=True) + "\n").encode("utf-8")
        self._paths.state.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
        lock_fd = os.open(audit_lock_path(self._paths), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._rotate_if_full()
            fd = os.open(self._paths.audit_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                _write_all(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)
        return stamped

    def _rotate_if_full(self) -> None:
        log = self._paths.audit_log
        try:
            size = log.stat().st_size
        except FileNotFoundError:
            return
        if size < self._max_bytes:
            return
        _unlink_if_present(_rotated(log, self._keep))
        for index in range(self._keep - 1, 0, -1):
            source = _rotated(log, index)
            if source.exists():
                os.replace(source, _rotated(log, index + 1))
        os.replace(log, _rotated(log, 1))
        fsync_dir(log.parent)

    def read(
        self,
        realm: RealmName | None = None,
        since: datetime | None = None,
    ) -> Iterator[AuditEntry]:
        """Yield entries oldest first: rotated files from highest index down, then the current one.

        `realm` keeps only that realm's entries. `since` (timezone-aware) keeps entries at or
        after that moment. Host-integrity events have no realm and are kept only when `realm`
        is None.
        """
        if since is not None and since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        floor = None if since is None else since.astimezone(UTC)
        wanted = None if realm is None else realm.value
        for path in self._files_oldest_first():
            yield from self._read_file(path, wanted, floor)

    def _files_oldest_first(self) -> list[Path]:
        log = self._paths.audit_log
        rotated = [_rotated(log, index) for index in range(self._keep, 0, -1)]
        return [path for path in [*rotated, log] if path.is_file()]

    def _read_file(
        self, path: Path, realm: str | None, floor: datetime | None
    ) -> Iterator[AuditEntry]:
        with path.open("rb") as handle:
            for number, raw in enumerate(handle, start=1):
                if not raw.endswith(b"\n"):
                    break  # torn tail from a crash mid-write
                if not raw.strip():
                    continue
                entry = _decode(raw, path, number)
                if realm is not None and entry.realm != realm:
                    continue
                if floor is not None and _ts(entry, path, number) < floor:
                    continue
                yield entry


def _ts(entry: AuditEntry, path: Path, number: int) -> datetime:
    if entry.ts is None:
        raise _line_error(path, number, "missing ts")
    try:
        return parse_utc(entry.ts)
    except ValueError as exc:
        raise _line_error(path, number, f"bad ts: {exc}") from exc


def _decode(raw: bytes, path: Path, number: int) -> AuditEntry:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _line_error(path, number, f"not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _line_error(path, number, "not a JSON object")
    return AuditEntry.from_json(data, f"{path}:{number}")


def _line_error(path: Path, number: int, reason: str) -> GinnError:
    err = make("GINN-E-INTERNAL")
    err.details.update({"path": str(path), "line": number, "reason": reason})
    return err


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
