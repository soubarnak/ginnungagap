# SPDX-License-Identifier: Apache-2.0
"""Operation journal for crash recovery (FR-122, NFR-030).

A mutating operation writes `paths.journal/<realm>.json` when it begins and updates the step
as it goes. It removes the file when it finishes. A file still present when ginn next starts is
an interrupted operation. `orphans()` returns those whose process is gone. Recovery itself
(`ops/recovery.py`) is M8's job.

The journal is written under the realm lock. `begin` refuses to replace an existing entry,
because that entry is the evidence recovery needs.

Pid reuse: an entry stores the process start time (field 22 of /proc/<pid>/stat, in clock ticks)
as well as the pid. A live process with a different start time is a different process, so the
entry counts as orphaned.
"""

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ginn.domain import RealmName
from ginn.errors import GinnError, make
from ginn.store._time import now_iso
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths
from ginn.store.records import fsync_dir, read_json, write_json_atomic

SCHEMA_VERSION = 1
_STAT_START_INDEX = 19  # field 22 is index 19 counted from field 3 (the state field)
_DEAD_STATES = frozenset({"Z", "X"})  # zombie and dead: the process has exited


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """Return (state, start ticks) from /proc/<pid>/stat, or None when it cannot be read."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    _head, sep, tail = text.rpartition(")")  # comm may hold spaces and parentheses
    fields = tail.split()
    if not sep or len(fields) <= _STAT_START_INDEX:
        return None
    try:
        return fields[0], int(fields[_STAT_START_INDEX])
    except ValueError:
        return None


def process_start_ticks(pid: int) -> int | None:
    """Return the start time of `pid` in clock ticks since boot, or None if /proc has no entry."""
    stat = _proc_stat(pid)
    return None if stat is None else stat[1]


def is_pid_alive(pid: int, start_ticks: int | None = None) -> bool:
    """True when `pid` names a running process, and, if `start_ticks` is given, the same one.

    `os.kill(pid, 0)` decides existence. PermissionError also means the process exists, because
    it belongs to another user. A zombie counts as dead. When `start_ticks` is given, a different
    start time means the pid was reused.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    stat = _proc_stat(pid)
    if stat is None:
        return start_ticks is None
    state, ticks = stat
    if state in _DEAD_STATES:
        return False
    return start_ticks is None or ticks == start_ticks


@dataclass(frozen=True)
class JournalEntry:
    """One interrupted or running operation on a realm."""

    op: str
    realm: str
    pid: int
    started: str
    pid_start: int | None
    step: str
    params: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "op": self.op,
            "realm": self.realm,
            "pid": self.pid,
            "started": self.started,
            "pid_start": self.pid_start,
            "step": self.step,
            "params": self.params,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any], source: str) -> "JournalEntry":
        try:
            realm = RealmName(_str(data["realm"])).value
            pid = _int(data["pid"])
            pid_start = data["pid_start"]
            if pid_start is not None:
                pid_start = _int(pid_start)
            params = data["params"]
            if not isinstance(params, dict):
                raise TypeError("params must be an object")
            return cls(
                op=_str(data["op"]),
                realm=realm,
                pid=pid,
                started=_str(data["started"]),
                pid_start=pid_start,
                step=_str(data["step"]),
                params=dict(params),
            )
        except (KeyError, TypeError, ValueError, GinnError) as exc:
            err = make("GINN-E-INTERNAL")
            err.details.update({"path": source, "reason": f"{type(exc).__name__}: {exc}"})
            raise err from exc


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {type(value).__name__}")
    return value


def _int(value: object) -> int:
    if type(value) is not int:
        raise TypeError(f"expected an integer, got {type(value).__name__}")
    return value


class Journal:
    """Begin, step and finish journal entries; list the pending and orphaned ones.

    Callers hold the realm lock. The journal does not take it.
    """

    def __init__(self, paths: OwnedPaths) -> None:
        self._paths = paths

    def _file(self, realm: str) -> Path:
        return self._paths.journal / f"{realm}.json"

    def begin(
        self, op: str, realm: RealmName, params: Mapping[str, Any] | None = None
    ) -> JournalEntry:
        """Record that `op` has started on `realm`. Refuses to overwrite an existing entry.

        An existing entry raises GINN-E-RECOVERY-PENDING (exit 4). The SRS defines no code for an
        unrecovered interrupted operation, so the catalogue entry is ginn-defined.
        """
        self._paths.journal.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
        path = self._file(realm.value)
        if path.exists():
            existing = JournalEntry.from_json(read_json(path, SCHEMA_VERSION), str(path))
            err = make("GINN-E-RECOVERY-PENDING", realm=realm.value, op=existing.op)
            err.details["pid"] = existing.pid
            raise err
        pid = os.getpid()
        entry = JournalEntry(
            op=op,
            realm=realm.value,
            pid=pid,
            started=now_iso(time.time),
            pid_start=process_start_ticks(pid),
            step="begin",
            params=dict(params or {}),
        )
        write_json_atomic(path, entry.to_json())
        return entry

    def step(self, entry: JournalEntry, name: str) -> JournalEntry:
        """Record that the operation reached step `name`. Returns the updated entry."""
        updated = replace(entry, step=name)
        write_json_atomic(self._file(entry.realm), updated.to_json())
        return updated

    def finish(self, entry: JournalEntry) -> None:
        """Remove the entry once the operation has completed. Missing entries are ignored."""
        path = self._file(entry.realm)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        fsync_dir(path.parent)

    def pending(self) -> list[JournalEntry]:
        """Every entry still on disk, sorted by realm name."""
        if not self._paths.journal.is_dir():
            return []
        entries: list[JournalEntry] = []
        for path in sorted(self._paths.journal.glob("*.json")):
            if path.is_file():
                data = read_json(path, SCHEMA_VERSION)
                entries.append(JournalEntry.from_json(data, str(path)))
        return entries

    def orphans(self) -> list[JournalEntry]:
        """Pending entries whose process is no longer running (same pid and start time)."""
        return [e for e in self.pending() if not is_pid_alive(e.pid, e.pid_start)]
