"""Keep host locale variables from naming locales the guest does not have.

The session forwards the host's LANG and LC_* to guest commands. A guest that
never generated that locale makes GTK and perl warn on every start, so the
variables for missing locales are replaced by C.UTF-8 (LANG) or dropped.
"""

from __future__ import annotations

import os
import re
import struct
from collections.abc import Mapping
from pathlib import Path

ARCHIVE_MAGIC = 0xDE020109
FALLBACK = "C.UTF-8"
_HEADER = struct.Struct("<14I")
_ENTRY = struct.Struct("<III")
_cache: dict[tuple[str, int, int], frozenset[str] | None] = {}


def normalize(name: str) -> str:
    """Canonical form glibc uses for locale names: en_US.UTF-8 -> en_US.utf8."""

    base, at, modifier = name.partition("@")
    language, dot, codeset = base.partition(".")
    if dot:
        codeset = re.sub(r"[^a-z0-9]", "", codeset.lower())
        language = f"{language}.{codeset}"
    return f"{language}{at}{modifier}"


def _archive_names(path: Path) -> frozenset[str] | None:
    """Names stored in a locale-archive, or None if it cannot be read."""

    try:
        status = path.stat()
        key = (str(path), status.st_mtime_ns, status.st_size)
        if key in _cache:
            return _cache[key]
        data = path.read_bytes()
    except OSError:
        return None
    names: frozenset[str] | None = None
    if len(data) >= _HEADER.size:
        header = _HEADER.unpack_from(data)
        if header[0] == ARCHIVE_MAGIC:
            offset, size = header[2], header[4]
            found: set[str] = set()
            try:
                for index in range(size):
                    _hash, name_offset, _record = _ENTRY.unpack_from(
                        data, offset + index * _ENTRY.size
                    )
                    if name_offset:
                        end = data.index(b"\0", name_offset)
                        found.add(data[name_offset:end].decode("utf-8", "replace"))
                names = frozenset(found)
            except (struct.error, ValueError):
                names = None
    _cache.clear()
    _cache[key] = names
    return names


def available(rootfs: Path) -> frozenset[str] | None:
    """Normalized locale names of a rootfs; None means "cannot tell"."""

    if not rootfs.is_dir():
        return None
    directory = rootfs / "usr/lib/locale"
    names: set[str] = set()
    archive = directory / "locale-archive"
    if archive.is_file():
        stored = _archive_names(archive)
        if stored is None:
            return None
        names.update(normalize(name) for name in stored)
    try:
        names.update(
            normalize(entry.name)
            for entry in directory.iterdir()
            if entry.is_dir()
        )
    except OSError:
        pass
    return frozenset(names)


def _usable(value: str, names: frozenset[str]) -> bool:
    if value in ("", "C", "POSIX") or value.startswith("C."):
        return True
    return normalize(value) in names


def adjust(rootfs: Path, env: Mapping[str, str]) -> dict[str, str]:
    """Return env with locale variables the guest lacks substituted or dropped."""

    result = dict(env)
    if not any(k in result for k in ("LANG", "LANGUAGE", "LC_ALL")) and not any(
        k.startswith("LC_") for k in result
    ):
        return result
    names = available(rootfs)
    if names is None:
        return result
    missing_lang = False
    for key in sorted(result):
        if key == "LANG":
            if not _usable(result[key], names):
                result[key] = FALLBACK
                missing_lang = True
        elif key.startswith("LC_") and not _usable(result[key], names):
            del result[key]
            missing_lang = True
    if missing_lang:
        result.pop("LANGUAGE", None)
    return result


def guest_rootfs(name: str) -> Path:
    from .. import core

    return core.STATE_ROOT / name / "rootfs"


__all__ = ["adjust", "available", "guest_rootfs", "normalize"]
