# SPDX-License-Identifier: Apache-2.0
"""The Host Setup Record: every host path Host Setup wrote, with its pre-image (DR-009, ADR-013).

The record is JSON at `<root>/etc/ginn/setup-<user>.json`, mode 0600 and root-owned. It is written
with the atomic helper in `_atomic`, under a fixed temporary name. FR-001 writes it, FR-002 reads it
for the rerun check, and FR-003 (undo) restores from it. Schema 2 holds:

- `paths`: the paths setup writes. Each carries its pre-image, the state before setup, which is
  captured before the first write. `kind` is file, append-lines (a file that other users' setups
  also append to, such as /etc/subuid), dir or symlink. `shared` is true for a path that every
  user shares, and false for a path named after one user. Once the record is complete, each entry
  also carries its post-image. The record file lists itself with the post-image `self`, because
  its own digest cannot be stored in it.
- `service_paths`: the paths the supervisor run script writes when the service starts (ADR-003).
  They carry pre-images only, because setup does not write them.
- `boot_id`: the boot id read when the service-written pre-images were captured, or null when the
  host has none. cgroup state does not survive a reboot, so undo compares it with the current boot.

A file pre-image is stored with its mode and its bytes in base64, so undo can restore it exactly.
A file post-image has its mode and a sha256 digest, because it is only compared. A directory image
has its mode. A symbolic link stores its target.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ginn.errors import GinnError, make
from ginn.hostsetup._atomic import atomic_write
from ginn.hostsetup.subids import SubIdEntry

SCHEMA_VERSION = 2
RECORD_DIR = "etc/ginn"
RECORD_MODE = 0o600
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
_TOP_KEYS = frozenset(
    {
        "schema",
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
_ENTRY_KEYS = frozenset({"path", "kind", "shared", "pre"})
_SHA256 = re.compile(r"[0-9a-f]{64}")

RECORD_FIX = (
    "inspect {path} with 'sudo ls -l {path}', and keep a copy with 'sudo cp -p' before you "
    "change it, because it holds the pre-images"
)
"""The remedy for a record that cannot be read. It never moves the record aside."""
SYSTEM_FIX = (
    "inspect {path} with 'sudo ls -ld {path}' and 'sudo readlink -f {path}', then correct it by "
    "hand"
)
"""The remedy for a system path, such as var/service, etc/sv or etc/ginn. It never moves it."""


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
    """The Host Setup Record for one user. Status is pending until every write has finished."""

    user: str
    uid: int
    gid: int
    timestamp: str
    status: str
    subids: SubIds | None
    paths: tuple[PathEntry, ...]
    service_paths: tuple[PathEntry, ...] = ()
    boot_id: str | None = None


class RecordError(ValueError):
    """The bytes are not a Host Setup Record of this schema."""


def relpath(user: str) -> str:
    """Return the root-relative path of the record for `user`."""
    return f"{RECORD_DIR}/setup-{user}.json"


def unsafe(path: Path, reason: str) -> GinnError:
    """Return GINN-E-SETUP-PATH-UNSAFE for `path`. The remedy depends on what `path` is."""
    is_record = path.name.startswith("setup-") and path.name.endswith(".json")
    fix = (RECORD_FIX if is_record else SYSTEM_FIX).format(path=path)
    return make("GINN-E-SETUP-PATH-UNSAFE", path=str(path), reason=reason, fix=fix)


def check(record: HostSetupRecord) -> None:
    """Raise ValueError when `record` breaks a rule that `encode` and `decode` both keep."""
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
    for entry in (*record.paths, *record.service_paths):
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
    if own not in seen:
        raise ValueError(f"the record does not list its own file {own}")
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


def encode(record: HostSetupRecord) -> bytes:
    """Return the record as JSON bytes. Raises ValueError when the record is not valid."""
    check(record)
    obj: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
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
    return (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode("utf-8")


def decode(raw: bytes) -> HostSetupRecord:
    """Parse record bytes. Raises RecordError for anything that is not a valid record."""
    try:
        obj: object = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise RecordError(f"it is not JSON ({exc})") from exc
    if not isinstance(obj, dict) or set(obj) != _TOP_KEYS:
        raise RecordError("its top level does not have exactly the record fields")
    if obj["schema"] != SCHEMA_VERSION or isinstance(obj["schema"], bool):
        raise RecordError(f"its schema is {obj['schema']!r}, not {SCHEMA_VERSION}")
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


def load(path: Path) -> HostSetupRecord | None:
    """Return the record at `path`, or None when there is none.

    A record that exists but cannot be read is refused with GINN-E-SETUP-PATH-UNSAFE. It is never
    treated as absent: that would capture pre-images from a host that setup has already changed.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise unsafe(path, "is not a regular file, so it cannot be a Host Setup Record")
    try:
        return decode(path.read_bytes())
    except RecordError as exc:
        raise unsafe(path, f"holds a Host Setup Record that cannot be read: {exc}") from exc


def save(path: Path, record: HostSetupRecord) -> None:
    """Write the record atomically with mode 0600 (DR-009), under the fixed temporary name."""
    atomic_write(path, encode(record), RECORD_MODE, fixed_tmp=True)


def _check_rel(text: str) -> None:
    """Refuse a path that is not a clean root-relative POSIX path."""
    if not text or text.startswith("/") or "\0" in text or "\n" in text:
        raise ValueError(f"{text!r} is not a root-relative path")
    if PurePosixPath(text).as_posix() != text:
        raise ValueError(f"{text!r} is not a normalised path")
    if any(part in ("", ".", "..") for part in PurePosixPath(text).parts):
        raise ValueError(f"{text!r} leaves the root or is not normalised")


def _is_boot_id(text: str) -> bool:
    return bool(text) and "\0" not in text and "\n" not in text and "\r" not in text


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
