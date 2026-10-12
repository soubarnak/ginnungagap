# SPDX-License-Identifier: Apache-2.0
"""Realm records and atomic JSON files (DR-001, DR-034, DR-035, FR-122).

A realm record is one file, `paths.realms/<name>.json`. `write_json_atomic` writes a temporary
file, fsyncs it, renames it over the target and fsyncs the directory. After a crash a reader sees
the old record or the new one, never a partial file (DR-035).

`RealmStore` does not take the realm lock itself. Callers hold it (store/locks.py) around every
load-modify-save, because flock conflicts even between descriptors in one process.
"""

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from ginn.domain import (
    DistroId,
    RealmName,
    RealmSettings,
    RealmState,
    Target,
    Tier,
    transition,
)
from ginn.errors import GinnError, make
from ginn.store._time import parse_utc, utc_from_epoch, utc_iso
from ginn.store.paths import OwnedPaths

_PREVIOUS_BOUND: frozenset[RealmState] = frozenset({RealmState.UPDATING, RealmState.REBUILDING})


def write_json_atomic(path: Path, obj: object) -> None:
    """Write `obj` as JSON to `path`: temporary file, fsync, rename, directory fsync (DR-035).

    The temporary file is `path.with_suffix(".tmp")` in the same directory, created with mode
    0600. If the write or the rename fails, the temporary file is removed and the error is raised.
    The old file at `path` is then unchanged.
    """
    data = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        _unlink_quiet(tmp)
        raise
    _fsync_dir(path.parent)


def read_json(path: Path, schema_version: int) -> dict[str, Any]:
    """Read a JSON object whose `schema_version` equals `schema_version`.

    Malformed JSON, a non-object, or a different schema version raises GINN-E-INTERNAL with the
    path in `details`. No catalogue code describes a corrupt store file, so this one is used.
    """
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise _corrupt(path, f"not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _corrupt(path, "not a JSON object")
    version = parsed.get("schema_version")
    if type(version) is not int or version != schema_version:
        raise _corrupt(path, f"expected schema_version {schema_version}")
    return parsed


def fsync_dir(directory: Path) -> None:
    """Flush a directory entry change (rename, create, unlink) to disk."""
    _fsync_dir(directory)


def _corrupt(path: Path, reason: str) -> GinnError:
    err = make("GINN-E-INTERNAL")
    err.details.update({"path": str(path), "reason": reason})
    return err


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {type(value).__name__}")
    return value


def _opt_str(value: object) -> str | None:
    return None if value is None else _str(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"expected a boolean, got {type(value).__name__}")
    return value


def _int(value: object) -> int:
    if type(value) is not int:
        raise TypeError(f"expected an integer, got {type(value).__name__}")
    return value


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"expected a list, got {type(value).__name__}")
    return [_str(item) for item in value]


@dataclass
class RealmRecord:
    """The stored state of one realm (DR-001). The settings are validated at construction."""

    schema_version: ClassVar[int] = 1

    settings: RealmSettings
    state: RealmState
    previous_state: RealmState | None
    image_ref: str
    image_digest: str
    container_id: str | None
    generation: int
    created_at: datetime
    updated_at: datetime
    restart_required: bool = False
    share_skipped: list[str] = field(default_factory=list)

    @property
    def name(self) -> RealmName:
        return self.settings.name

    def to_json(self) -> dict[str, Any]:
        settings = self.settings
        return {
            "schema_version": self.schema_version,
            "settings": {
                "name": settings.name.value,
                "distro": settings.distro.value,
                "tier": settings.tier.value,
                "services": settings.services,
                "autostart": settings.autostart,
                "everything": settings.everything,
            },
            "state": self.state.value,
            "previous_state": None if self.previous_state is None else self.previous_state.value,
            "image_ref": self.image_ref,
            "image_digest": self.image_digest,
            "container_id": self.container_id,
            "generation": self.generation,
            "created_at": utc_iso(self.created_at),
            "updated_at": utc_iso(self.updated_at),
            "restart_required": self.restart_required,
            "share_skipped": list(self.share_skipped),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any], source: str = "realm record") -> "RealmRecord":
        """Rebuild a record. Any invalid field raises GINN-E-INTERNAL with the reason."""
        try:
            if _int(data.get("schema_version")) != cls.schema_version:
                raise ValueError(f"schema_version must be {cls.schema_version}")
            raw = data["settings"]
            settings = RealmSettings(
                name=RealmName(_str(raw["name"])),
                distro=DistroId.parse(_str(raw["distro"])),
                tier=Tier.parse(_str(raw["tier"])),
                services=_bool(raw["services"]),
                autostart=_bool(raw["autostart"]),
                everything=_bool(raw["everything"]),
            )
            previous = _opt_str(data["previous_state"])
            container = _opt_str(data["container_id"])
            return cls(
                settings=settings,
                state=RealmState.parse(_str(data["state"])),
                previous_state=None if previous is None else RealmState.parse(previous),
                image_ref=_str(data["image_ref"]),
                image_digest=_str(data["image_digest"]),
                container_id=container,
                generation=_int(data["generation"]),
                created_at=parse_utc(_str(data["created_at"])),
                updated_at=parse_utc(_str(data["updated_at"])),
                restart_required=_bool(data["restart_required"]),
                share_skipped=_str_list(data["share_skipped"]),
            )
        except (KeyError, TypeError, ValueError, GinnError) as exc:
            raise _corrupt(Path(source), f"{type(exc).__name__}: {exc}") from exc


class RealmStore:
    """Load, save, list and delete realm records under `paths.realms` (DR-001).

    `clock` returns epoch seconds and sets `updated_at`. Callers must hold the realm lock.
    """

    def __init__(self, paths: OwnedPaths, clock: Callable[[], float] = time.time) -> None:
        self._paths = paths
        self._clock = clock

    def _file(self, name: RealmName) -> Path:
        return self._paths.realms / f"{name.value}.json"

    def exists(self, name: RealmName) -> bool:
        return self._file(name).is_file()

    def load(self, name: RealmName) -> RealmRecord:
        path = self._file(name)
        if not path.is_file():
            raise make("GINN-E-NO-SUCH-REALM", realm=name.value)
        return RealmRecord.from_json(read_json(path, RealmRecord.schema_version), str(path))

    def save(self, record: RealmRecord) -> None:
        self._paths.realms.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json_atomic(self._file(record.name), record.to_json())

    def list_names(self) -> list[RealmName]:
        """Names of the stored records, sorted. Temporary files and invalid names are skipped."""
        if not self._paths.realms.is_dir():
            return []
        names: list[RealmName] = []
        for entry in sorted(self._paths.realms.glob("*.json")):
            if not entry.is_file():
                continue
            try:
                names.append(RealmName(entry.stem))
            except GinnError:
                continue
        return names

    def delete(self, name: RealmName) -> None:
        path = self._file(name)
        try:
            path.unlink()
        except FileNotFoundError:
            raise make("GINN-E-NO-SUCH-REALM", realm=name.value) from None
        _fsync_dir(path.parent)

    def set_state(self, name: RealmName, new_state: Target) -> RealmRecord:
        """Move the realm to `new_state` if DR-020 allows it, then save and return the record.

        Entering `updating` or `rebuilding` records the current state as `previous_state`.
        Leaving either one clears it. An illegal move raises IllegalTransition and nothing is
        written. Removal is `delete()`, so a REMOVED target is refused here.
        """
        record = self.load(name)
        checked = transition(record.state, new_state, record.previous_state)
        if not isinstance(checked, RealmState):
            raise ValueError("a realm is removed with RealmStore.delete(), not set_state()")
        previous = record.state if checked in _PREVIOUS_BOUND else None
        updated = replace(
            record,
            state=checked,
            previous_state=previous,
            updated_at=utc_from_epoch(self._clock()),
        )
        self.save(updated)
        return updated


__all__ = [
    "RealmRecord",
    "RealmStore",
    "read_json",
    "write_json_atomic",
]
