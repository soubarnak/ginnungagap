# SPDX-License-Identifier: Apache-2.0
"""Trust Manifest loader: the pinned image digests that ship with ginn (DR-040, FR-011, IR-021).

The manifest is data: src/ginn/trust/data/trust.toml, packaged with ginn. ginn reads it and never
writes it (DR-040). It changes only through the refresh procedure in spikes/results/S-10.md
section 6, which tools/refresh-trust-manifest.py automates for the digests.

Trust is the digest. An image is pulled only at the digest pinned here (FR-011), never by tag: a
tag can move between refreshes, a digest cannot. Signatures are recorded (S-10 section 3) but ginn
does not verify them, so none grants trust (VERIFIED_SIGNATURE_KINDS). The channels are the
official images named by IR-021. AS-024 chooses the release tag each channel carries, and a new
major release is a rebuild (FR-021), so `image` changes only at a refresh.

Errors come from the catalogue (NFR-040). A distro with no entry is an image that cannot be
verified: GINN-E-IMAGE-UNVERIFIED (FR-011). A manifest that does not validate is a defect in ginn's
own data: GINN-E-INTERNAL, with the reason in the message. That summary has no placeholder, so the
error is built with GinnError directly.
"""

import datetime as dt
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Final

from ginn.errors import CATALOGUE, GinnError, make

__all__ = [
    "SUPPORTED_SCHEMA",
    "VERIFIED_SIGNATURE_KINDS",
    "SignatureKind",
    "TrustEntry",
    "TrustManifest",
    "load",
    "load_file",
    "parse",
    "pull_reference",
]

SUPPORTED_SCHEMA: Final = 1
PACKAGED_NAME: Final = "trust.toml"


class SignatureKind(StrEnum):
    """Signature metadata recorded per entry (S-10 section 3). Recorded, not verified."""

    NONE = "none"
    SIGSTORE_UNVERIFIED = "sigstore-unverified"


VERIFIED_SIGNATURE_KINDS: Final[frozenset[SignatureKind]] = frozenset()
"""Signature kinds that count as verification material (DR-040). Empty: no kind is verified yet.

A kind joins this set only after ginn checks its chain, certificate identity and transparency-log
inclusion. Until then every signature kind is metadata only.
"""

_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
_KEY_RE: Final = re.compile(r"[a-z][a-z0-9-]*")
_HOST: Final = r"[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::[0-9]{1,5})?"
_PATH_PART: Final = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REPOSITORY_RE: Final = re.compile(rf"{_HOST}(?:/{_PATH_PART})+")
_TAG_RE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_SIZE_FIELDS: Final = (
    "compressed_size_bytes",
    "estimated_installed_bytes",
    "measured_installed_bytes",
)
_ENTRY_FIELDS: Final = frozenset(
    {
        "image",
        "digest_kind",
        "digest",
        *_SIZE_FIELDS,
        "signature",
        "verified_on",
    }
)


@dataclass(frozen=True)
class TrustEntry:
    """One distribution's pinned image.

    `digest` is the linux/amd64 platform manifest digest (digest_kind = "platform"), not the index
    digest. The index digest changes whenever another architecture or an attestation is rebuilt,
    which would force a refresh with no change for x86_64.
    """

    key: str
    repository: str
    tag: str
    digest: str
    compressed_size_bytes: int
    estimated_installed_bytes: int
    measured_installed_bytes: int
    signature: SignatureKind
    verified_on: dt.date

    @property
    def image(self) -> str:
        """The registry reference with its tag, as written in trust.toml."""
        return f"{self.repository}:{self.tag}"

    @property
    def signature_verified(self) -> bool:
        """True only for a signature kind that ginn verifies (VERIFIED_SIGNATURE_KINDS)."""
        return self.signature in VERIFIED_SIGNATURE_KINDS


@dataclass(frozen=True)
class TrustManifest:
    """A validated Trust Manifest: the schema version and one entry per distribution."""

    schema: int
    entries: tuple[TrustEntry, ...]
    source: str

    def entry(self, key: str) -> TrustEntry:
        """Return the entry for distribution `key`, or raise GINN-E-IMAGE-UNVERIFIED (FR-011)."""
        for item in self.entries:
            if item.key == key:
                return item
        raise make("GINN-E-IMAGE-UNVERIFIED", distro=key)

    def pull_reference(self, key: str) -> str:
        """Return `repository@sha256:<digest>` for distribution `key`.

        This is the only reference ginn pulls by (FR-011). The digest is the linux/amd64 platform
        manifest, not the index. S-10b pulled each recommended image by its platform digest on
        x86_64, and the local image digest matched the pinned value. The reference holds no tag,
        so a tag that moves between refreshes cannot change what ginn pulls.
        """
        item = self.entry(key)
        return f"{item.repository}@{item.digest}"


@cache
def load() -> TrustManifest:
    """Return the Trust Manifest packaged with ginn, validated (DR-040). Read once per process."""
    source = f"ginn.trust/data/{PACKAGED_NAME}"
    try:
        text = (resources.files("ginn.trust") / "data" / PACKAGED_NAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _invalid(source, f"cannot be read: {exc}") from exc
    return parse(text, source)


def load_file(path: Path) -> TrustManifest:
    """Read and validate a Trust Manifest file. The packaged copy is `load()`."""
    source = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _invalid(source, f"cannot be read: {exc}") from exc
    return parse(text, source)


def parse(text: str, source: str) -> TrustManifest:
    """Validate manifest TOML text. `source` names the text in error messages."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _invalid(source, f"not valid TOML: {exc}") from exc
    _check_keys(data, frozenset({"schema", "distro"}), "", source)
    schema = data["schema"]
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise _invalid(source, "schema must be an integer")
    if schema != SUPPORTED_SCHEMA:
        raise _invalid(source, f"schema {schema} is not supported (expected {SUPPORTED_SCHEMA})")
    distros = _table(data["distro"], "distro", source)
    if not distros:
        raise _invalid(source, "distro must list at least one distro")
    entries = tuple(_entry(key, distros[key], source) for key in sorted(distros))
    return TrustManifest(schema=schema, entries=entries, source=source)


def pull_reference(key: str, manifest: TrustManifest | None = None) -> str:
    """Return the pull reference for `key` from `manifest`, default the packaged manifest."""
    if manifest is None:
        manifest = load()
    return manifest.pull_reference(key)


def _invalid(source: str, problem: str) -> GinnError:
    """Return GINN-E-INTERNAL for a manifest that cannot be used. It is a defect in ginn's data."""
    spec = CATALOGUE["GINN-E-INTERNAL"]
    return GinnError(
        spec.code,
        f"trust manifest {source} is invalid: {problem}",
        spec.remedy,
        spec.exit_code,
        details={"source": source, "problem": problem},
    )


def _path(where: str, name: str) -> str:
    return f"{where}.{name}" if where else name


def _check_keys(
    table: Mapping[str, object], required: frozenset[str], where: str, source: str
) -> None:
    """Reject keys that are not in the schema, then keys that are required but absent."""
    label = where or "top level"
    unknown = sorted(set(table) - required)
    if unknown:
        raise _invalid(source, f"{label} has unknown key(s): {', '.join(unknown)}")
    missing = sorted(required - set(table))
    if missing:
        raise _invalid(source, f"{label} has no {missing[0]}")


def _table(value: object, where: str, source: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid(source, f"{where} must be a table")
    return value


def _field(table: Mapping[str, object], name: str, where: str, source: str) -> object:
    # _check_keys ran first, so the key is present. This keeps the typing honest.
    if name not in table:
        raise _invalid(source, f"{_path(where, name)} is missing")
    return table[name]


def _text(table: Mapping[str, object], name: str, where: str, source: str) -> str:
    value = _field(table, name, where, source)
    if not isinstance(value, str):
        raise _invalid(source, f"{_path(where, name)} must be a string")
    return value


def _size(table: Mapping[str, object], name: str, where: str, source: str) -> int:
    value = _field(table, name, where, source)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _invalid(source, f"{_path(where, name)} must be a positive integer of bytes")
    return value


def _image(image: str, where: str, source: str) -> tuple[str, str]:
    """Split `repository:tag` at the first colon after the last slash.

    A registry port (quay.io:443/x) also has a colon, but it sits before the last slash. The tag
    never holds a colon, so this split is exact.
    """
    colon = image.find(":", image.rfind("/") + 1)
    if colon < 0:
        raise _invalid(source, f"{where}.image {image!r} needs a tag, such as :base")
    repository, tag = image[:colon], image[colon + 1 :]
    if not _REPOSITORY_RE.fullmatch(repository):
        raise _invalid(
            source,
            f"{where}.image repository {repository!r} must be lowercase, with an explicit "
            "registry host (for example quay.io/archlinux/archlinux)",
        )
    if not _TAG_RE.fullmatch(tag):
        raise _invalid(source, f"{where}.image tag {tag!r} is not a valid tag")
    return repository, tag


def _signature(value: str, where: str, source: str) -> SignatureKind:
    try:
        return SignatureKind(value)
    except ValueError:
        allowed = ", ".join(kind.value for kind in SignatureKind)
        raise _invalid(
            source, f"{_path(where, 'signature')} {value!r} must be one of: {allowed}"
        ) from None


def _date(value: object, where: str, source: str) -> dt.date:
    problem = f"{_path(where, 'verified_on')} must be a date such as 2026-10-12"
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise _invalid(source, problem) from exc
    raise _invalid(source, problem)


def _entry(key: str, raw: object, source: str) -> TrustEntry:
    where = f"distro.{key}"
    if not _KEY_RE.fullmatch(key):
        raise _invalid(
            source,
            f"distro key {key!r} must be lowercase letters, digits or hyphens, "
            "starting with a letter",
        )
    table = _table(raw, where, source)
    _check_keys(table, _ENTRY_FIELDS, where, source)
    repository, tag = _image(_text(table, "image", where, source), where, source)
    if _text(table, "digest_kind", where, source) != "platform":
        raise _invalid(source, f'{where}.digest_kind must be "platform"')
    digest = _text(table, "digest", where, source)
    if not _DIGEST_RE.fullmatch(digest):
        raise _invalid(
            source, f"{where}.digest must be sha256: followed by 64 lowercase hex digits"
        )
    compressed = _size(table, "compressed_size_bytes", where, source)
    estimated = _size(table, "estimated_installed_bytes", where, source)
    measured = _size(table, "measured_installed_bytes", where, source)
    if estimated < compressed:
        raise _invalid(
            source, f"{where}.estimated_installed_bytes is smaller than compressed_size_bytes"
        )
    return TrustEntry(
        key=key,
        repository=repository,
        tag=tag,
        digest=digest,
        compressed_size_bytes=compressed,
        estimated_installed_bytes=estimated,
        measured_installed_bytes=measured,
        signature=_signature(_text(table, "signature", where, source), where, source),
        verified_on=_date(_field(table, "verified_on", where, source), where, source),
    )
