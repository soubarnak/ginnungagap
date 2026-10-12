# SPDX-License-Identifier: Apache-2.0
"""Realm records and atomic JSON files (DR-001, DR-034, DR-035, FR-122).

A realm record is one file, `paths.realms/<name>.json`. `write_json_atomic` writes a temporary
file, fsyncs it, renames it over the target and fsyncs the directory. After a crash a reader sees
the old record or the new one, never a partial file (DR-035).

`RealmStore` does not take the realm lock itself. Callers hold it (store/locks.py) around every
load-modify-save, because flock conflicts even between descriptors in one process.
"""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from ginn.domain import (
    REMOVED,
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
from ginn.store.paths import OwnedPaths, fsync_dir, write_bytes_atomic

_PREVIOUS_BOUND: frozenset[RealmState] = frozenset({RealmState.UPDATING, RealmState.REBUILDING})
_RESTORE_TO: frozenset[RealmState] = frozenset({RealmState.STOPPED, RealmState.RUNNING})
"""The only states that lead to `updating` or `rebuilding`, so the only valid previous states."""


def write_json_atomic(path: Path, obj: object) -> None:
    """Write `obj` as JSON to `path` atomically, with the DR-035 steps of `write_bytes_atomic`."""
    write_bytes_atomic(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8"))


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


def _corrupt(path: Path, reason: str) -> GinnError:
    err = make("GINN-E-INTERNAL")
    err.details.update({"path": str(path), "reason": reason})
    return err


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
    """The stored state of one realm (DR-001). The settings and previous_state are validated at
    construction (DR-020)."""

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

    def __post_init__(self) -> None:
        """Refuse a `previous_state` that DR-020 does not allow for this state.

        `updating` and `rebuilding` must record `stopped` or `running` as the state they return
        to. Every other state must record none. `from_json` builds through this constructor, so a
        stored record that breaks the rule is reported as GINN-E-INTERNAL.
        """
        if self.state in _PREVIOUS_BOUND:
            if self.previous_state not in _RESTORE_TO:
                raise ValueError(f"{self.state.value} needs previous_state stopped or running")
        elif self.previous_state is not None:
            raise ValueError(f"{self.state.value} must not record previous_state")

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
        """Remove the record. DR-020 allows this from `creating` (rollback) and `deleting` only.

        Any other state raises IllegalTransition and the record stays. The check uses the same
        `transition` table as `set_state`, so removal has one rule.
        """
        record = self.load(name)
        transition(record.state, REMOVED, record.previous_state)
        path = self._file(name)
        try:
            path.unlink()
        except FileNotFoundError:
            raise make("GINN-E-NO-SUCH-REALM", realm=name.value) from None
        fsync_dir(path.parent)

    def remove_corrupt(self, name: RealmName) -> None:
        """Remove a record file that does not parse, so a damaged realm can be recovered (FR-122).

        `delete()` loads the record first, so a corrupt record can never be deleted through it.
        This is the recovery path for that case. The file goes only when `load` fails with
        GINN-E-INTERNAL, which is what a corrupt record gives. A record that loads raises
        ValueError: a valid realm is removed with `delete()`, which checks DR-020. A corrupt
        record has no readable state, so DR-020 cannot be checked here. Callers hold the realm
        lock, and write the audit entry for the removal.
        """
        path = self._file(name)
        if not path.is_file():
            raise make("GINN-E-NO-SUCH-REALM", realm=name.value)
        try:
            self.load(name)
        except GinnError as exc:
            if exc.code != "GINN-E-INTERNAL":
                raise
        else:
            raise ValueError("the record loads: remove a valid realm with delete(), not this")
        try:
            path.unlink()
        except FileNotFoundError:
            raise make("GINN-E-NO-SUCH-REALM", realm=name.value) from None
        fsync_dir(path.parent)

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
    "fsync_dir",
    "read_json",
    "write_json_atomic",
]
