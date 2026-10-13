# SPDX-License-Identifier: Apache-2.0
"""Operation journal for crash recovery (FR-122, NFR-030).

A mutating operation writes `paths.journal/<realm>.json` when it begins and updates the step
as it goes. It removes the file when it finishes. A file still present when ginn next starts is
an interrupted operation. `scan()` reads every file on its own and never follows a symbolic link
or blocks on a FIFO. A damaged file comes back with a `problem` and never hides the others.
`pending()` and `orphans()` are strict wrappers over `scan()`: they raise GINN-E-INTERNAL on the
first damaged file. Recovery (`ops/recovery.py`, T2.8) is the only reader that acts on a scan.

The journal is written under the realm lock. `begin` refuses to replace an existing entry,
because that entry is the evidence recovery needs.

Pid reuse: an entry stores the process start time (field 22 of /proc/<pid>/stat, in clock ticks)
as well as the pid. A live process with a different start time is a different process, so the
entry counts as orphaned.
"""

import errno
import json
import os
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from ginn.domain import RealmName
from ginn.errors import GinnError, make
from ginn.store._time import now_iso, parse_utc
from ginn.store.paths import OWNED_DIR_MODE, OwnedPaths
from ginn.store.records import fsync_dir, write_json_atomic

SCHEMA_VERSION = 1
MAX_JOURNAL_BYTES = 64 * 1024
"""The largest journal file that is read, and the largest that is written (FR-122). A larger file is
reported and never parsed, so one hostile file cannot exhaust memory or time. The writers keep every
file under this size: `begin` refuses params that do not fit, and `step` drops step
params that would overflow and marks the entry with PARAMS_OVERFLOW. The operation's changes are
already made, so the step is recorded and not refused."""
PARAMS_OVERFLOW: Final = "params_overflow"
"""The params marker of an entry whose step params were dropped for size (see `step`)."""
ESSENTIAL_PARAMS: Final = ("op_id", "packages", "aur")
"""The params that `begin` writes and that a step never drops. Recovery reads them when the rest
of the params overflowed (ops/recovery.py)."""
MAX_PARAM_DEPTH = 16
"""The deepest nesting allowed in an entry's params. Real params are two or three levels deep."""
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


def _serialized_size(entry: "JournalEntry") -> int:
    """The bytes `write_json_atomic` would write for `entry`, the size the reader checks."""
    return len((json.dumps(entry.to_json(), indent=2, sort_keys=True) + "\n").encode("utf-8"))


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
            # The start time is copied into the audit entry as its ts, so it must parse here.
            started = _str(data["started"])
            parse_utc(started)
            return cls(
                op=_str(data["op"]),
                realm=realm,
                pid=pid,
                started=started,
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


_SUFFIX = ".json"


@dataclass(frozen=True)
class JournalFile:
    """One journal file: its entry, or the reason it cannot be trusted (FR-122).

    `realm` is the file name without `.json`. Exactly one of `entry` and `problem` is set. A
    symbolic link, a file that is not regular, a file over MAX_JOURNAL_BYTES, a file whose entry
    names another realm, and a file that does not parse are all reported in `problem`. None of them
    is written or deleted, because a damaged file has no pid that could be checked.
    """

    path: Path
    realm: str
    entry: JournalEntry | None
    problem: str | None

    def is_live(self) -> bool:
        """True when the entry's process still runs, with the same start time (see is_pid_alive)."""
        return self.entry is not None and is_pid_alive(self.entry.pid, self.entry.pid_start)


def _problem(path: Path, realm: str, reason: str) -> JournalFile:
    return JournalFile(path, realm, None, reason)


def _open_problem(exc: OSError) -> str:
    if exc.errno == errno.ELOOP:
        return "a symbolic link, which is never followed"
    return f"cannot be opened: {exc.strerror or exc}"


def _read_capped(fd: int) -> bytes:
    """Read at most one byte past the limit, so an oversized file is noticed and not loaded."""
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_JOURNAL_BYTES:
        chunk = os.read(fd, MAX_JOURNAL_BYTES + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _nested_deeper_than(value: object, limit: int) -> bool:
    """True when `value` nests more than `limit` levels. Iterative, so depth cannot recurse."""
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > limit:
            return True
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return False


def _parse(path: Path, realm: str, data: bytes) -> JournalFile:
    try:
        parsed: object = json.loads(data.decode("utf-8"))
    except RecursionError:
        return _problem(path, realm, "nested too deeply to parse")
    except MemoryError:
        return _problem(path, realm, "too large to parse in memory")
    except ValueError as exc:
        return _problem(path, realm, f"not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        return _problem(path, realm, "not a JSON object")
    version = parsed.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        return _problem(path, realm, f"expected schema_version {SCHEMA_VERSION}")
    if _nested_deeper_than(parsed.get("params"), MAX_PARAM_DEPTH):
        return _problem(path, realm, f"params nested deeper than {MAX_PARAM_DEPTH} levels")
    try:
        entry = JournalEntry.from_json(parsed, str(path))
    except GinnError as exc:
        return _problem(path, realm, str(exc.details.get("reason", exc.message)))
    if entry.realm != realm:
        reason = f"the file names realm '{realm}' but its entry names '{entry.realm}'"
        return _problem(path, realm, reason)
    return JournalFile(path, realm, entry, None)


def _read_journal_file(path: Path) -> JournalFile | None:
    """Read one journal file without trusting it. None when the file does not exist.

    The file is opened without following a symbolic link and without blocking on a FIFO. Only a
    regular file of at most MAX_JOURNAL_BYTES is read. A file that cannot be parsed, or that nests
    too deeply, is a problem and never an exception.
    """
    realm = path.name[: -len(_SUFFIX)] if path.name.endswith(_SUFFIX) else path.name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return _problem(path, realm, _open_problem(exc))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return _problem(path, realm, "not a regular file, so it is not read")
        if info.st_size > MAX_JOURNAL_BYTES:
            return _problem(path, realm, f"larger than {MAX_JOURNAL_BYTES} bytes, so not read")
        data = _read_capped(fd)
    except OSError as exc:
        return _problem(path, realm, f"unreadable: {exc.strerror or exc}")
    finally:
        os.close(fd)
    if len(data) > MAX_JOURNAL_BYTES:
        return _problem(path, realm, f"grew past {MAX_JOURNAL_BYTES} bytes while it was read")
    return _parse(path, realm, data)


def damaged_reason(file: JournalFile) -> str:
    """The reason for a damaged file, with its remedy: inspect the file, then remove it (FR-122).

    The remedy names the path, so an operator can act on it from the outcome or the error alone.
    """
    path = file.path
    return (
        f"journal file {path} cannot be used ({file.problem or 'damaged'}), and it blocks new "
        f"operations on realm {file.realm} until it is removed. Inspect it with 'cat {path}'. "
        f"When no ginn process runs for realm {file.realm} and 'ginn status {file.realm}' shows a "
        f"state you accept, remove it with 'rm {path}'"
    )


def _damaged(file: JournalFile) -> GinnError:
    """The strict error for a damaged file. It names the path and remedy, since no op is named."""
    err = make("GINN-E-INTERNAL")
    err.details.update(
        {
            "path": str(file.path),
            "reason": file.problem or "damaged",
            "remedy": damaged_reason(file),
        }
    )
    return err


class Journal:
    """Begin, step and finish journal entries; list the pending and orphaned ones.

    Callers hold the realm lock. The journal does not take it.
    """

    def __init__(self, paths: OwnedPaths) -> None:
        self._paths = paths

    def _file(self, realm: str) -> Path:
        return self._paths.journal / f"{realm}.json"

    def begin(
        self,
        op: str,
        realm: RealmName,
        params: Mapping[str, Any] | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> JournalEntry:
        """Record that `op` has started on `realm`. Refuses to overwrite an existing entry.

        `clock` (epoch seconds) stamps `started`, so the journal and the audit entry of one
        operation can carry the same start time. Any existing file refuses the begin, whether its
        entry is readable or not. A readable entry raises GINN-E-RECOVERY-PENDING (exit 4), which
        names its op. A damaged file raises GINN-E-INTERNAL with its path, because it names no op.
        The SRS defines no code for an unrecovered interrupted operation, so the catalogue entry is
        ginn-defined.
        """
        self._paths.journal.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
        path = self._file(realm.value)
        existing = _read_journal_file(path)
        if existing is not None:
            if existing.entry is None:
                raise _damaged(existing)
            err = make("GINN-E-RECOVERY-PENDING", realm=realm.value, op=existing.entry.op)
            err.details["pid"] = existing.entry.pid
            raise err
        pid = os.getpid()
        entry = JournalEntry(
            op=op,
            realm=realm.value,
            pid=pid,
            started=now_iso(clock),
            pid_start=process_start_ticks(pid),
            step="begin",
            params=dict(params or {}),
        )
        size = _serialized_size(entry)
        if size > MAX_JOURNAL_BYTES:
            detail = (
                f"the operation's parameters need {size} bytes, over the {MAX_JOURNAL_BYTES} limit"
            )
            raise make("GINN-E-USAGE", detail=detail)
        write_json_atomic(path, entry.to_json())
        return entry

    def step(
        self,
        entry: JournalEntry,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        drop: Sequence[str] = (),
    ) -> JournalEntry:
        """Record that the operation reached step `name`. Returns the updated entry.

        `params`, when given, are merged into the entry's params and written with the step, and
        `drop` names params to remove. An AUR build records its package sets this way (FR-043). A
        caller whose params are a whole set clears the overflow marker with `drop`, by naming
        PARAMS_OVERFLOW; no step clears it implicitly, because the params it dropped are still lost.
        A file that would pass MAX_JOURNAL_BYTES is not written in full. The step params are dropped
        and the entry carries PARAMS_OVERFLOW. If even the entry's own params would not fit, only
        ESSENTIAL_PARAMS are kept. Either way the step is recorded, because the operation's changes
        have already been made. The writers never produce a file the reader refuses.
        """
        merged = {key: value for key, value in entry.params.items() if key not in drop}
        if params is not None:
            merged.update(params)
        updated = replace(entry, step=name, params=merged)
        if _serialized_size(updated) > MAX_JOURNAL_BYTES:
            kept = {key: value for key, value in entry.params.items() if key not in drop}
            updated = replace(entry, step=name, params={**kept, PARAMS_OVERFLOW: True})
        if _serialized_size(updated) > MAX_JOURNAL_BYTES:
            essentials: dict[str, Any] = {
                key: entry.params[key] for key in ESSENTIAL_PARAMS if key in entry.params
            }
            updated = replace(entry, step=name, params={**essentials, PARAMS_OVERFLOW: True})
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
        """Every entry still on disk, sorted by realm name. A damaged file raises GINN-E-INTERNAL.

        Recovery uses `scan()`, which reports a damaged file and goes on. This strict form is for
        callers that must not go on past one.
        """
        entries: list[JournalEntry] = []
        for file in self.scan():
            if file.entry is None:
                raise _damaged(file)
            entries.append(file.entry)
        return entries

    def orphans(self) -> list[JournalEntry]:
        """Pending entries whose process is no longer running (same pid and start time)."""
        return [e for e in self.pending() if not is_pid_alive(e.pid, e.pid_start)]

    def scan(self) -> list[JournalFile]:
        """Every file in the journal directory, sorted by name, each read on its own.

        A file that cannot be read does not stop the scan. It comes back with `problem` set (see
        JournalFile). An empty or missing directory gives an empty list.
        """
        if not self._paths.journal.is_dir():
            return []
        files: list[JournalFile] = []
        for path in sorted(self._paths.journal.glob(f"*{_SUFFIX}")):
            file = _read_journal_file(path)
            if file is not None:
                files.append(file)
        return files

    def load(self, realm: RealmName) -> JournalFile | None:
        """The journal file of `realm` as `scan()` reads it, or None when there is none."""
        return _read_journal_file(self._file(realm.value))
