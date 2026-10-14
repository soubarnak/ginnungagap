# SPDX-License-Identifier: Apache-2.0
"""Realm records and atomic JSON files (DR-001, DR-034, DR-035, FR-122).

A realm record is one file, `paths.realms/<name>.json`. `write_json_atomic` writes a temporary
file, fsyncs it, renames it over the target and fsyncs the directory. After a crash a reader sees
the old record or the new one, never a partial file (DR-035).

`RealmStore` does not take the realm lock itself. Callers hold it (store/locks.py) around every
load-modify-save, because flock conflicts even between descriptors in one process.
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from ginn.domain import (
    REMOVED,
    DistroId,
    ManifestEntry,
    RealmName,
    RealmSettings,
    RealmState,
    Target,
    Tier,
    transition,
)
from ginn.domain.grants import GrantRecord
from ginn.domain.permissions import Permissions, Toggle
from ginn.errors import GinnError, make
from ginn.store._time import parse_utc, utc_from_epoch, utc_iso
from ginn.store.paths import OwnedPaths, fsync_dir, write_bytes_atomic

_MACHINE_ID: re.Pattern[str] = re.compile(r"[0-9a-f]{32}")
"""A machine-id as systemd writes it: 32 lowercase hex digits (ADR-007, F6)."""
_PREVIOUS_BOUND: frozenset[RealmState] = frozenset({RealmState.UPDATING, RealmState.REBUILDING})
_RESTORE_TO: frozenset[RealmState] = frozenset({RealmState.STOPPED, RealmState.RUNNING})
"""The only states that lead to `updating` or `rebuilding`, so the only valid previous states."""
_PENDING_STATES: frozenset[RealmState] = frozenset(
    {RealmState.CREATING, RealmState.BROKEN, RealmState.DELETING}
)
"""The states in which a record may keep an unfinished create's request (FR-122). A delete of such
a realm keeps it too, and `create --resume` refuses any state but broken."""


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


def _manifest(value: object) -> list[ManifestEntry]:
    """The Install Manifest of a record: each item is a JSON object read as a `ManifestEntry`."""
    if not isinstance(value, list):
        raise TypeError(f"expected a list, got {type(value).__name__}")
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError(f"expected a manifest object, got {type(item).__name__}")
    return [ManifestEntry.from_dict(item) for item in value]


def _permissions(value: object, tier: Tier) -> Permissions:
    """The Custom part of a record. A key that is absent is format 1 with no toggles listed, so a
    Custom realm reads the grants that format 1 always attached (domain/permissions.py)."""
    if value is None:
        return Permissions.from_dict({"toggles": [], "shares": []}, tier=tier)
    if not isinstance(value, Mapping):
        raise TypeError(f"expected a permissions object, got {type(value).__name__}")
    return Permissions.from_dict(value, tier=tier)


def _grants(value: object) -> list[GrantRecord]:
    """The grant list of a record: each item is a JSON object read as a `GrantRecord` (FR-066).

    Anything else raises TypeError, which `RealmRecord.from_json` reports as GINN-E-INTERNAL.
    """
    if not isinstance(value, list):
        raise TypeError(f"expected a list, got {type(value).__name__}")
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError(f"expected a grant object, got {type(item).__name__}")
    return [GrantRecord.from_dict(item) for item in value]


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
    # The grant list fixed at the last start (FR-066, ADR-008). Added without a schema bump: a
    # record written before it existed has no key, and reads as an empty list.
    grants: list[GrantRecord] = field(default_factory=list)
    # The Install Manifest (DR-007). Added without a schema bump, like `grants`: a record written
    # before it existed has no key and reads as an empty manifest. Deleting the record removes it.
    manifest: list[ManifestEntry] = field(default_factory=list)
    # The Custom toggles and shares (FR-063). Added without a schema bump, like `grants`: a record
    # written before it has no key reads as format 1 (domain/permissions.py), so a Custom realm
    # keeps the grants it always had. Only a Custom realm may hold any toggles or shares.
    permissions: Permissions = field(default_factory=Permissions)
    # The toggle-driven create options that the container was made with, as the names that
    # security/caps.py `option_keys` gives (ADR-006). Empty is the Basic set, and a record written
    # before this field has no key, so it reads as Basic. A change of the names needs a new
    # container.
    created_options: list[str] = field(default_factory=list)
    # Whether the host had /dev/kvm when the container was made (ADR-006). `created_options` shows
    # only the kvm option that was on, so a later change of the kvm toggle needs this probe.
    # Added without a schema bump: a record without the key reads as False.
    kvm_at_create: bool = False
    # Whether the realm was created with the NVIDIA CDI grant (FR-069, ADR-012). The exec
    # environment names the ginn libraries only then. Added without a schema bump.
    gpu_cdi: bool = False
    # The state of the GPU toggle (`/dev/dri`, and the CDI grant with `gpu_cdi`) and of the fonts
    # and timezone toggle at create (FR-061, FR-063). Both are fixed at `podman create`, so a change
    # of either toggle needs a new container, which the next start makes (FR-066). A record without
    # a key was made before these toggles existed, when every realm had both grants, so each reads
    # as True.
    gpu_at_create: bool = True
    fonts_at_create: bool = True
    # The host gpg-agent extra socket, resolved at the last start of an Agent Proxy realm (NFR-004).
    # `gpg_extra_known` is False until a start stores it, and then enter resolves it instead. When
    # True, `gpg_extra` is the socket, or None when the host has none. Added without a schema bump:
    # a record without the key reads as unknown.
    gpg_extra: str | None = None
    gpg_extra_known: bool = False
    # The services mode the container was made with (ADR-003, FR-083). A services change is a
    # create-time change, so the desired mode in `settings` can differ from this until the next
    # start rebuilds the realm (ops/rebuild.py). Added without a schema bump: a record without the
    # key reads as the settings' mode, since no realm could change its mode before this field.
    services_at_create: bool | None = None
    # The container that a rebuild is building, and its generation (ADR-003, DR-034). They are set
    # only while the record reads rebuilding: after the container is created and before the swap.
    # A services-on rebuild starts this container through its runit service (`ginn _service-run`),
    # not the container the record names. `set_state` clears both when the record leaves
    # rebuilding, and the swap clears them too. Added without a schema bump: a record without the
    # keys reads as None.
    building_container_id: str | None = None
    building_generation: int | None = None
    # The autostart flag that a change turning services off displaced (DR-012, FR-083). Autostart
    # needs services on, so that change sets the flag off, and it keeps the flag here. A change that
    # is cancelled before the rebuild (services back on, on the same container) restores it, and
    # the rebuild's swap clears it. Added without a schema bump: a record without the key reads as
    # None.
    autostart_before_off: bool | None = None
    # The create that made this realm is not finished (FR-122, ops/resume.py). `create_pending` is
    # set by the create's `recorded` save, with the packages its request named in `with_at_create`,
    # and the save that writes `stopped` clears all three. `everything_confirmed` is the yes at the
    # Kali everything prompt, saved before that install, so `create --resume` does not ask again.
    # Recovery keeps them, since it moves the record only to `broken`, and a delete keeps them too.
    # Added without a schema bump: a record without the keys reads as no pending create.
    create_pending: bool = False
    with_at_create: list[str] = field(default_factory=list)
    everything_confirmed: bool = False
    # The machine-id of a services-on realm (ADR-007, F6). The create or the rebuild that makes the
    # container writes it with `podman cp` before the first start, and a later rebuild copies the
    # same value, so the realm keeps one identity. Added without a schema bump: a record without
    # the key reads as None, which is every services-off realm and a services-on one made before F6.
    machine_id: str | None = None

    def create_grants_match(self, toggles: frozenset[Toggle]) -> bool:
        """True when `toggles` asks for the GPU and fonts and timezone grants this container has.

        `created_options` covers the options of ADR-006. These two grants are binds and devices
        fixed at create too, so a toggle that differs from its stored flag is a create-time change.
        """
        return (Toggle.GPU in toggles) == self.gpu_at_create and (
            Toggle.FONTS_TIMEZONE in toggles
        ) == self.fonts_at_create

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
        if self.settings.tier is not Tier.CUSTOM and self.permissions != Permissions():
            raise ValueError("only a Custom realm keeps toggles or shares (FR-063)")
        if self.state is not RealmState.REBUILDING and (
            self.building_container_id is not None or self.building_generation is not None
        ):
            raise ValueError(f"{self.state.value} must not record a building container")
        if self.services_at_create is None:
            self.services_at_create = self.settings.services
        if self.create_pending and self.state not in _PENDING_STATES:
            raise ValueError(f"{self.state.value} must not record a pending create")
        if not self.create_pending and (self.with_at_create or self.everything_confirmed):
            raise ValueError("a create's request is kept only while the create is pending")
        if self.machine_id is not None and _MACHINE_ID.fullmatch(self.machine_id) is None:
            raise ValueError("machine_id must be 32 lowercase hex digits")

    @property
    def name(self) -> RealmName:
        return self.settings.name

    def to_json(self) -> dict[str, Any]:
        settings = self.settings
        data: dict[str, Any] = {
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
            "grants": [grant.to_dict() for grant in self.grants],
            "manifest": [entry.to_dict() for entry in self.manifest],
            "permissions": self.permissions.to_dict(),
            "created_options": list(self.created_options),
            "kvm_at_create": self.kvm_at_create,
            "gpu_cdi": self.gpu_cdi,
            "gpu_at_create": self.gpu_at_create,
            "fonts_at_create": self.fonts_at_create,
            "services_at_create": self.services_at_create,
            "building_container_id": self.building_container_id,
            "building_generation": self.building_generation,
            "autostart_before_off": self.autostart_before_off,
            "create_pending": self.create_pending,
            "with_at_create": list(self.with_at_create),
            "everything_confirmed": self.everything_confirmed,
            "machine_id": self.machine_id,
        }
        if self.gpg_extra_known:
            data["gpg_extra"] = self.gpg_extra
        return data

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
                grants=_grants(data.get("grants", [])),
                manifest=_manifest(data.get("manifest", [])),
                permissions=_permissions(data.get("permissions"), settings.tier),
                created_options=_str_list(data.get("created_options", [])),
                kvm_at_create=_bool(data.get("kvm_at_create", False)),
                gpu_cdi=_bool(data.get("gpu_cdi", False)),
                gpu_at_create=_bool(data.get("gpu_at_create", True)),
                fonts_at_create=_bool(data.get("fonts_at_create", True)),
                services_at_create=(
                    _bool(data["services_at_create"]) if "services_at_create" in data else None
                ),
                building_container_id=_opt_str(data.get("building_container_id")),
                building_generation=(
                    None
                    if data.get("building_generation") is None
                    else _int(data["building_generation"])
                ),
                autostart_before_off=(
                    None
                    if data.get("autostart_before_off") is None
                    else _bool(data["autostart_before_off"])
                ),
                gpg_extra=_opt_str(data.get("gpg_extra")),
                gpg_extra_known="gpg_extra" in data,
                create_pending=_bool(data.get("create_pending", False)),
                with_at_create=_str_list(data.get("with_at_create", [])),
                everything_confirmed=_bool(data.get("everything_confirmed", False)),
                machine_id=_opt_str(data.get("machine_id")),
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
        Leaving either one clears it. Leaving `rebuilding` also clears the building container, so
        a rollback or a recovery that returns the record to stopped leaves no container named.
        An illegal move raises IllegalTransition and nothing is written. Removal is `delete()`, so
        a REMOVED target is refused here.
        """
        record = self.load(name)
        checked = transition(record.state, new_state, record.previous_state)
        if not isinstance(checked, RealmState):
            raise ValueError("a realm is removed with RealmStore.delete(), not set_state()")
        previous = record.state if checked in _PREVIOUS_BOUND else None
        leaving_rebuild = checked is not RealmState.REBUILDING
        updated = replace(
            record,
            state=checked,
            previous_state=previous,
            building_container_id=None if leaving_rebuild else record.building_container_id,
            building_generation=None if leaving_rebuild else record.building_generation,
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
