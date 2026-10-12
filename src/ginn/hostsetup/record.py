# SPDX-License-Identifier: Apache-2.0
"""The Host Setup Records: one per-user record and one shared record (DR-009, ADR-013).

Schema 3 has two kinds of record. Both are JSON, mode 0600 and root-owned, and both are written
with the atomic helper in `_atomic`, under a fixed temporary name.

The per-user record is `<root>/etc/ginn/setup-<user>.json`, with `"kind": "setup"`:

- `paths`: what this user's setup wrote. These are the user's own files, directories and service
  link, and the two subordinate ID files (kind `append-lines`, because other users' setups also
  append to them). Each entry carries its pre-image, the state before setup, captured before the
  first write. Once the record is complete, each entry also carries a post-image. The record
  lists itself with the post-image `self`, because its own digest cannot be stored in it.
- `service_paths`: this user's paths that the supervisor run script writes at service start
  (ADR-003). They carry pre-images only.
- `subids`, `status`, `timestamp`, `uid`, `gid` and `boot_id`.

The shared record is `<root>/etc/ginn/shared.json`, with `"kind": "shared"`:

- `paths`: the shared directories that setup created, and the shared record's own file, with the
  pre-image absent. Every entry is `shared: true`.
- `service_paths`: the shared paths that the run script writes (the root `cgroup.subtree_control`,
  the `ginn` cgroup directory and `run/ginn`), with pre-images only.
- `boot_id` and `timestamp`. It has no status and no post-images. Undo removes a shared directory
  only where its pre-image is absent, and never writes shared bytes back (ADR-013, Decision 15).

The first setup writes it in the same step that creates etc/ginn. A later setup adds to it only
the shared directories that it must create and that the record lacks.

A file pre-image is stored with its mode and its bytes in base64, so undo can restore it exactly.
A file post-image has its mode and a sha256 digest, because it is only compared. A directory image
has its mode. A symbolic link stores its target.

A record of schema 1 or 2 is refused by `load` and `load_shared` with GINN-E-SETUP-PATH-UNSAFE.
It is never read as an older form.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ginn.errors import GinnError, make
from ginn.hostsetup._atomic import atomic_write
from ginn.hostsetup.subids import SubIdEntry

SCHEMA_VERSION = 3
KIND_SETUP = "setup"
KIND_SHARED = "shared"
RECORD_DIR = "etc/ginn"
RECORD_MODE = 0o600
SHARED_NAME = "shared.json"
SHARED_RELPATH = f"{RECORD_DIR}/{SHARED_NAME}"
SUBID_FILES: frozenset[str] = frozenset({"etc/subuid", "etc/subgid"})
"""The subordinate ID files. Every user's lines are in them, so each user's record keeps them."""
USER_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
STATUSES: frozenset[str] = frozenset({"pending", "complete"})
KINDS: frozenset[str] = frozenset({"file", "append-lines", "dir", "symlink"})
STATE_OF_KIND: dict[str, str] = {
    "file": "file",
    "append-lines": "file",
    "dir": "dir",
    "symlink": "symlink",
}
"""The image state that a path of each kind must have on the host (`append-lines` is a file)."""
OLD_SCHEMA_REASON = "written by a ginn build before F8a/F9a; remove it by hand after checking"
"""The reason for a schema 1 or 2 record. Nothing has shipped, so none is migrated."""
_PRE_FIELDS: dict[str, frozenset[str]] = {
    "absent": frozenset(),
    "file": frozenset({"mode", "data"}),
    "dir": frozenset({"mode"}),
    "symlink": frozenset({"target"}),
}
_POST_FIELDS: dict[str, frozenset[str]] = {
    "absent": frozenset(),
    "file": frozenset({"mode", "sha256"}),
    "dir": frozenset({"mode"}),
    "symlink": frozenset({"target"}),
    "self": frozenset(),
}
_SETUP_KEYS = frozenset(
    {
        "schema",
        "kind",
        "user",
        "uid",
        "gid",
        "timestamp",
        "status",
        "subids",
        "boot_id",
        "paths",
        "service_paths",
    }
)
_SHARED_KEYS = frozenset({"schema", "kind", "timestamp", "boot_id", "paths", "service_paths"})
_ENTRY_KEYS = frozenset({"path", "kind", "shared", "pre"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Image:
    """The state of one path. `state` is absent, file, dir, symlink, or self (record file only).

    A file image has `mode`, and a pre-image also has `data`, while a post-image has `sha256`. A
    directory image has `mode`. A symbolic link has `target`.
    """

    state: str
    mode: int | None = None
    data: bytes | None = None
    sha256: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class PathEntry:
    """One host path. `kind` is file, append-lines, dir or symlink. `post` is None until done."""

    path: str
    kind: str
    pre: Image
    post: Image | None = None
    shared: bool = False


@dataclass(frozen=True)
class SubIds:
    """The subordinate ID ranges that setup recorded for the user (AS-005)."""

    uid: SubIdEntry
    gid: SubIdEntry


@dataclass(frozen=True)
class HostSetupRecord:
    """The per-user Host Setup Record. Status is pending until every write has finished."""

    user: str
    uid: int
    gid: int
    timestamp: str
    status: str
    subids: SubIds | None
    paths: tuple[PathEntry, ...]
    service_paths: tuple[PathEntry, ...] = ()
    boot_id: str | None = None


@dataclass(frozen=True)
class SharedRecord:
    """The shared record, `etc/ginn/shared.json`: pre-images of every path that all users share.

    `paths` lists the shared directories this setup created, and the record's own file. Each has
    the pre-image absent. The record's own file has the post-image `self`, because its digest
    cannot be stored in it, and no other entry has a post-image. `service_paths` holds the shared
    paths that the run script writes, with pre-images only.
    """

    timestamp: str
    paths: tuple[PathEntry, ...]
    service_paths: tuple[PathEntry, ...] = ()
    boot_id: str | None = None


class RecordError(ValueError):
    """The bytes are not a Host Setup Record of this schema and kind."""


def relpath(user: str) -> str:
    """Return the root-relative path of the per-user record for `user`."""
    return f"{RECORD_DIR}/setup-{user}.json"


def is_record_name(name: str) -> bool:
    """True for a file name that a per-user record can have, such as setup-alice.json."""
    return name.startswith("setup-") and name.endswith(".json")


def unsafe(path: Path, reason: str) -> GinnError:
    """Return GINN-E-SETUP-PATH-UNSAFE for `path`. The remedy is the catalogue text."""
    return make("GINN-E-SETUP-PATH-UNSAFE", path=str(path), reason=reason)


def check(record: HostSetupRecord) -> None:
    """Raise ValueError when a per-user `record` breaks a rule that `encode` and `decode` keep."""
    if not USER_NAME.fullmatch(record.user):
        raise ValueError(f"invalid user name {record.user!r}")
    if record.status not in STATUSES:
        raise ValueError(f"status {record.status!r} is neither pending nor complete")
    if record.status == "complete" and record.subids is None:
        raise ValueError("a complete record has no subordinate ID ranges")
    if record.boot_id is not None and not _is_boot_id(record.boot_id):
        raise ValueError(f"boot_id {record.boot_id!r} is not a boot id")
    own = relpath(record.user)
    seen: set[str] = set()
    _check_entries((*record.paths, *record.service_paths), seen)
    if own not in seen:
        raise ValueError(f"the record does not list its own file {own}")
    for entry in (*record.paths, *record.service_paths):
        if entry.shared and entry.path not in SUBID_FILES:
            raise ValueError(
                f"{entry.path} is marked shared, but a per-user record keeps only the "
                "subordinate ID files as shared"
            )
    for entry in record.paths:
        if entry.post is None:
            if record.status == "complete":
                raise ValueError(f"{entry.path} has no post-image in a complete record")
            continue
        if entry.post.state == "self":
            if entry.path != own:
                raise ValueError(f"{entry.path} claims the record's own post-image")
        elif entry.post.state not in ("absent", STATE_OF_KIND[entry.kind]):
            raise ValueError(f"{entry.path}: post-image {entry.post.state} is not a {entry.kind}")
        _image_obj(entry.post, pre=False)
    for entry in record.service_paths:
        if entry.post is not None:
            raise ValueError(f"{entry.path} is service-written and carries no post-image")


def check_shared(record: SharedRecord) -> None:
    """Raise ValueError when the shared `record` breaks a rule that `encode_shared` keeps.

    Every entry is shared. Every `paths` entry is a directory whose pre-image is absent, except the
    record's own file, which is listed, has a pre-image that is absent and a post-image `self`.
    `service_paths` entries have no post-image.
    """
    if not record.timestamp or "\0" in record.timestamp:
        raise ValueError("the shared record has no timestamp")
    if record.boot_id is not None and not _is_boot_id(record.boot_id):
        raise ValueError(f"boot_id {record.boot_id!r} is not a boot id")
    seen: set[str] = set()
    _check_entries((*record.paths, *record.service_paths), seen)
    if SHARED_RELPATH not in {entry.path for entry in record.paths}:
        raise ValueError(f"the shared record does not list its own file {SHARED_RELPATH}")
    for entry in (*record.paths, *record.service_paths):
        if not entry.shared:
            raise ValueError(f"{entry.path} is in the shared record but not marked shared")
        if entry.path in SUBID_FILES:
            raise ValueError(f"{entry.path} is kept per user, not in the shared record")
    for entry in record.service_paths:
        if entry.post is not None:
            raise ValueError(f"{entry.path} is service-written and carries no post-image")
    for entry in record.paths:
        if entry.path == SHARED_RELPATH:
            if (entry.kind, entry.pre.state) != ("file", "absent"):
                raise ValueError(f"{SHARED_RELPATH} must be a file whose pre-image is absent")
            if entry.post != Image("self"):
                raise ValueError(f"{SHARED_RELPATH} must have the post-image self")
            continue
        if (entry.kind, entry.pre.state) != ("dir", "absent"):
            raise ValueError(f"{entry.path} must be a shared directory whose pre-image is absent")
        if entry.post is not None:
            raise ValueError(f"{entry.path} is a shared directory and carries no post-image")


def encode(record: HostSetupRecord) -> bytes:
    """Return the per-user record as JSON bytes. Raises ValueError when the record is not valid."""
    check(record)
    obj: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "kind": KIND_SETUP,
        "user": record.user,
        "uid": record.uid,
        "gid": record.gid,
        "timestamp": record.timestamp,
        "status": record.status,
        "subids": None
        if record.subids is None
        else {"uid": _sub_obj(record.subids.uid), "gid": _sub_obj(record.subids.gid)},
        "boot_id": record.boot_id,
        "paths": [_entry_obj(entry, with_post=True) for entry in record.paths],
        "service_paths": [_entry_obj(entry, with_post=False) for entry in record.service_paths],
    }
    return _dump(obj)


def encode_shared(record: SharedRecord) -> bytes:
    """Return the shared record as JSON bytes. Raises ValueError when the record is not valid."""
    check_shared(record)
    obj: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "kind": KIND_SHARED,
        "timestamp": record.timestamp,
        "boot_id": record.boot_id,
        "paths": [_entry_obj(entry, with_post=True) for entry in record.paths],
        "service_paths": [_entry_obj(entry, with_post=False) for entry in record.service_paths],
    }
    return _dump(obj)


def decode(raw: bytes) -> HostSetupRecord:
    """Parse per-user record bytes. Raises RecordError for anything that is not a valid record."""
    obj = _parse(raw, KIND_SETUP, _SETUP_KEYS)
    try:
        record = HostSetupRecord(
            user=_text(obj["user"], "user"),
            uid=_int(obj["uid"], "uid"),
            gid=_int(obj["gid"], "gid"),
            timestamp=_text(obj["timestamp"], "timestamp"),
            status=_text(obj["status"], "status"),
            subids=_subids_from(obj["subids"]),
            boot_id=_boot_from(obj["boot_id"]),
            paths=tuple(_entry_from(item, with_post=True) for item in _items(obj["paths"])),
            service_paths=tuple(
                _entry_from(item, with_post=False) for item in _items(obj["service_paths"])
            ),
        )
        check(record)
    except ValueError as exc:
        raise RecordError(str(exc)) from exc
    return record


def decode_shared(raw: bytes) -> SharedRecord:
    """Parse shared record bytes. Raises RecordError for anything that is not a valid record."""
    obj = _parse(raw, KIND_SHARED, _SHARED_KEYS)
    try:
        record = SharedRecord(
            timestamp=_text(obj["timestamp"], "timestamp"),
            boot_id=_boot_from(obj["boot_id"]),
            paths=tuple(_entry_from(item, with_post=True) for item in _items(obj["paths"])),
            service_paths=tuple(
                _entry_from(item, with_post=False) for item in _items(obj["service_paths"])
            ),
        )
        check_shared(record)
    except ValueError as exc:
        raise RecordError(str(exc)) from exc
    return record


def load(path: Path) -> HostSetupRecord | None:
    """Return the per-user record at `path`, or None when there is none.

    A record that exists but cannot be read, or that is of an older schema, is refused with
    GINN-E-SETUP-PATH-UNSAFE. It is never treated as absent: that would capture pre-images from a
    host that setup has already changed.
    """
    raw = _read_regular(path, "a Host Setup Record")
    if raw is None:
        return None
    if _is_old_schema(raw):
        raise unsafe(path, OLD_SCHEMA_REASON)
    try:
        return decode(raw)
    except RecordError as exc:
        raise unsafe(path, f"holds a Host Setup Record that cannot be read: {exc}") from exc


def load_shared(path: Path) -> SharedRecord | None:
    """Return the shared record at `path`, or None when it is missing. Refusals as for `load`."""
    raw = _read_regular(path, "the shared Host Setup Record")
    if raw is None:
        return None
    if _is_old_schema(raw):
        raise unsafe(path, OLD_SCHEMA_REASON)
    try:
        return decode_shared(raw)
    except RecordError as exc:
        raise unsafe(path, f"holds a shared Host Setup Record that cannot be read: {exc}") from exc


def save(path: Path, record: HostSetupRecord) -> None:
    """Write the per-user record atomically with mode 0600 (DR-009), under the fixed temp name."""
    atomic_write(path, encode(record), RECORD_MODE, fixed_tmp=True)


def save_shared(path: Path, record: SharedRecord) -> None:
    """Write the shared record atomically with mode 0600 (DR-009), under the fixed temp name."""
    atomic_write(path, encode_shared(record), RECORD_MODE, fixed_tmp=True)


def _read_regular(path: Path, what: str) -> bytes | None:
    """Return the bytes of the regular file at `path`, or None when it does not exist."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise unsafe(path, f"is not a regular file, so it cannot be {what}")
    return path.read_bytes()


def _is_old_schema(raw: bytes) -> bool:
    """True when `raw` is a JSON object of schema 1 or 2, the forms before F8a/F9a."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError:
        return False
    schema = obj.get("schema") if isinstance(obj, dict) else None
    return type(schema) is int and schema in (1, 2)


def _parse(raw: bytes, kind: str, keys: frozenset[str]) -> dict[str, Any]:
    """Parse the JSON object and check schema, kind and top-level keys, in that order."""
    try:
        obj: object = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise RecordError(f"it is not JSON ({exc})") from exc
    if not isinstance(obj, dict):
        raise RecordError("its top level is not a JSON object")
    schema = obj.get("schema")
    if type(schema) is not int or schema != SCHEMA_VERSION:
        raise RecordError(f"its schema is {schema!r}, not {SCHEMA_VERSION}")
    if obj.get("kind") != kind:
        raise RecordError(f"its kind is {obj.get('kind')!r}, not {kind!r}")
    if set(obj) != keys:
        raise RecordError("its top level does not have exactly the record fields")
    return obj


def _check_rel(text: str) -> None:
    """Refuse a path that is not a clean root-relative POSIX path."""
    if not text or text.startswith("/") or "\0" in text or "\n" in text:
        raise ValueError(f"{text!r} is not a root-relative path")
    if PurePosixPath(text).as_posix() != text:
        raise ValueError(f"{text!r} is not a normalised path")
    if any(part in ("", ".", "..") for part in PurePosixPath(text).parts):
        raise ValueError(f"{text!r} leaves the root or is not normalised")


def _check_entries(entries: Iterable[PathEntry], seen: set[str]) -> None:
    """Check each entry's path, kind and pre-image, and that no path is listed twice."""
    for entry in entries:
        _check_rel(entry.path)
        if entry.path in seen:
            raise ValueError(f"{entry.path} is listed twice")
        seen.add(entry.path)
        if entry.kind not in KINDS:
            raise ValueError(
                f"{entry.path}: kind {entry.kind!r} is not file, append-lines, dir or symlink"
            )
        if entry.pre.state not in ("absent", STATE_OF_KIND[entry.kind]):
            raise ValueError(f"{entry.path}: pre-image {entry.pre.state} does not fit {entry.kind}")
        _image_obj(entry.pre, pre=True)


def _is_boot_id(text: str) -> bool:
    return bool(text) and "\0" not in text and "\n" not in text and "\r" not in text


def _dump(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _image_obj(image: Image, *, pre: bool) -> dict[str, Any]:
    """Return the JSON object for one image. Raises ValueError when its fields do not fit."""
    fields = _PRE_FIELDS if pre else _POST_FIELDS
    if image.state not in fields:
        raise ValueError(f"an image cannot have the state {image.state!r} here")
    obj: dict[str, Any] = {"state": image.state}
    if image.mode is not None:
        obj["mode"] = image.mode
    if image.data is not None:
        obj["data"] = base64.b64encode(image.data).decode("ascii")
    if image.sha256 is not None:
        obj["sha256"] = image.sha256
    if image.target is not None:
        obj["target"] = image.target
    if set(obj) != {"state", *fields[image.state]}:
        needed = sorted(fields[image.state])
        raise ValueError(f"a {image.state} image needs exactly the fields {needed}")
    return obj


def _image_from(obj: object, *, pre: bool) -> Image:
    if not isinstance(obj, dict):
        raise RecordError("an image is not an object")
    fields = _PRE_FIELDS if pre else _POST_FIELDS
    state = obj.get("state")
    if not isinstance(state, str) or state not in fields:
        raise RecordError(f"an image has the unknown state {state!r}")
    if set(obj) != {"state", *fields[state]}:
        raise RecordError(f"a {state} image does not have exactly its fields")
    if state == "file" and pre:
        return Image("file", mode=_int(obj["mode"], "mode"), data=_base64(obj["data"]))
    if state == "file":
        digest = _text(obj["sha256"], "sha256")
        if _SHA256.fullmatch(digest) is None:
            raise RecordError("a sha256 digest is not 64 lowercase hex digits")
        return Image("file", mode=_int(obj["mode"], "mode"), sha256=digest)
    if state == "dir":
        return Image("dir", mode=_int(obj["mode"], "mode"))
    if state == "symlink":
        return Image("symlink", target=_text(obj["target"], "target"))
    return Image(state)


def _entry_obj(entry: PathEntry, *, with_post: bool) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "path": entry.path,
        "kind": entry.kind,
        "shared": entry.shared,
        "pre": _image_obj(entry.pre, pre=True),
    }
    if with_post:
        obj["post"] = None if entry.post is None else _image_obj(entry.post, pre=False)
    return obj


def _entry_from(item: object, *, with_post: bool) -> PathEntry:
    if not isinstance(item, dict):
        raise RecordError("a path entry is not an object")
    keys = _ENTRY_KEYS | ({"post"} if with_post else set())
    if set(item) != keys:
        raise RecordError("a path entry does not have exactly its fields")
    if not isinstance(item["shared"], bool):
        raise RecordError("a path entry's shared flag is not true or false")
    post = None
    if with_post and item["post"] is not None:
        post = _image_from(item["post"], pre=False)
    return PathEntry(
        path=_text(item["path"], "path"),
        kind=_text(item["kind"], "kind"),
        pre=_image_from(item["pre"], pre=True),
        post=post,
        shared=item["shared"],
    )


def _sub_obj(entry: SubIdEntry) -> dict[str, Any]:
    return {"name": entry.name, "start": entry.start, "count": entry.count}


def _subids_from(obj: object) -> SubIds | None:
    if obj is None:
        return None
    if not isinstance(obj, dict) or set(obj) != {"uid", "gid"}:
        raise RecordError("subids must hold exactly uid and gid")
    return SubIds(uid=_sub_from(obj["uid"]), gid=_sub_from(obj["gid"]))


def _sub_from(obj: object) -> SubIdEntry:
    if not isinstance(obj, dict) or set(obj) != {"name", "start", "count"}:
        raise RecordError("a subordinate ID range does not have exactly name, start and count")
    return SubIdEntry(
        name=_text(obj["name"], "subids name"),
        start=_int(obj["start"], "subids start"),
        count=_int(obj["count"], "subids count"),
    )


def _boot_from(value: object) -> str | None:
    if value is None:
        return None
    text = _text(value, "boot_id")
    if not _is_boot_id(text):
        raise RecordError("boot_id is not a boot id")
    return text


def _items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise RecordError("a path list is not a JSON array")
    return list(value)


def _int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordError(f"{what} is not an integer")
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise RecordError(f"{what} is not non-empty text")
    return value


def _base64(value: object) -> bytes:
    if not isinstance(value, str):
        raise RecordError("a file pre-image is not base64 text")
    return base64.b64decode(value, validate=True)
