# SPDX-License-Identifier: Apache-2.0
"""Variable grants of a realm, as the grant list stores them (FR-061, FR-064, FR-066, ADR-008).

These are data types only. `runtime/attach.py` builds and attaches the grants, and
`store/records.py` keeps the list in a realm record. They live in the domain so that the store
does not import the runtime (AGENTS.md layout).

Loading checks each field. A grant whose realm path is not absolute is refused, and so is one
whose host path is not absolute. The one exception is a session slot that no session binds yet,
which has an empty host (runtime/env.py, FR-084).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Self, TypeGuard

from ginn.domain.paths import Mode

SESSION_KIND: Final = "session"
"""The kind of a grant that a session binds, and the only kind that may have an empty host."""


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _mode(value: object) -> Mode:
    if value == "ro":
        return "ro"
    if value == "rw":
        return "rw"
    raise ValueError(f"mode must be 'ro' or 'rw', not {value!r}")


def _require_object(data: object, what: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise TypeError(f"{what} must be a JSON object, not {type(data).__name__}")
    return data


@dataclass(frozen=True)
class SourceIdentity:
    """The host identity of a grant's source.

    `below` lists the mount points that hang from the mount the source resolves to and lie under the
    source (ADR-008, Resolution A). A change in any of them changes the identity, so the next enter
    attaches the source again.
    """

    dev: int
    ino: int
    below: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"dev": self.dev, "ino": self.ino, "below": list(self.below)}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        data = _require_object(data, "a source identity")
        dev, ino, below = data.get("dev"), data.get("ino"), data.get("below")
        if not (_is_int(dev) and _is_int(ino) and isinstance(below, list)):
            raise ValueError(f"not a source identity: {dict(data)!r}")
        if not all(isinstance(point, str) for point in below):
            raise ValueError(f"not a source identity: {dict(data)!r}")
        return cls(dev, ino, tuple(below))


@dataclass(frozen=True)
class GrantRecord:
    """One grant of the list fixed at the last start (FR-066). `identity` is None while skipped.

    `host` and `realm` are absolute, except that a session grant may have an empty `host`, until a
    session binds its realm path. `is_dir` says what the placeholder must be: a folder for a
    directory source, and an empty file for a socket or a file.

    `not_visible` is the hold key. It is the host identity read before an attach whose tree did not
    match it. While the host identity still equals it, the grant is not passed again, so a repeated
    mismatch costs no helper run at enter. A changed host identity releases it (ADR-008, Resolution
    A). The caller stores the list with `to_dict`, and reads it back with `from_dict`.
    """

    kind: str
    host: str
    realm: str
    mode: Mode
    is_dir: bool
    identity: SourceIdentity | None = None
    not_visible: SourceIdentity | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "host": self.host,
            "realm": self.realm,
            "mode": self.mode,
            "is_dir": self.is_dir,
            "identity": None if self.identity is None else self.identity.to_dict(),
            "not_visible": None if self.not_visible is None else self.not_visible.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        data = _require_object(data, "a grant record")
        kind, host, realm = data.get("kind"), data.get("host"), data.get("realm")
        is_dir, raw = data.get("is_dir"), data.get("identity")
        hidden = data.get("not_visible")
        if not (isinstance(kind, str) and isinstance(host, str) and isinstance(realm, str)):
            raise ValueError(f"not a grant record: {dict(data)!r}")
        if not isinstance(is_dir, bool) or not (raw is None or isinstance(raw, Mapping)):
            raise ValueError(f"not a grant record: {dict(data)!r}")
        if not (hidden is None or isinstance(hidden, Mapping)):
            raise ValueError(f"not a grant record: {dict(data)!r}")
        if not realm.startswith("/"):
            raise ValueError(f"grant realm path must be absolute: {realm!r}")
        if not (host.startswith("/") or (host == "" and kind == SESSION_KIND)):
            raise ValueError(f"grant host path must be absolute: {host!r}")
        identity = None if raw is None else SourceIdentity.from_dict(raw)
        not_visible = None if hidden is None else SourceIdentity.from_dict(hidden)
        return cls(kind, host, realm, _mode(data.get("mode")), is_dir, identity, not_visible)
