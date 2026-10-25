# SPDX-License-Identifier: Apache-2.0
"""Icons of exported entries: lookup inside the realm, then a checked copy out (FR-044, IR-029).

The realm is untrusted, so every icon is checked before it reaches the host:

- The name is a theme name (`[A-Za-z0-9][A-Za-z0-9._+-]*`, no slash) or an absolute path under
  `/usr/share/icons/hicolor/` or `/usr/share/pixmaps/`. Any other path is refused, and so is a
  `..` that leaves those directories. Symbolic links inside the realm are followed one level and
  the target is checked the same way.
- The lookup order is `hicolor/scalable/apps/<name>.svg`, then the largest `hicolor/<N>x<N>/apps`
  PNG or XPM of at most 256 pixels (the smallest larger one when none is that small), then
  `/usr/share/pixmaps/<name>.{png,svg,xpm}`. One `find` in the realm lists all candidates, so a
  sync costs one listing, not one call per icon.
- A file over ICON_MAX_BYTES is refused, both by its size in the listing and after the copy.
- The copy is one bounded `cat` of the listed file (see _realm.read_realm_file): it is cut at the
  cap, so a file swapped after the listing cannot fill ginn's disk or memory. A symbolic link is
  copied from its target, which is a listed regular file. The bytes must match the extension
  (PNG signature, XPM header, or SVG without an entity declaration). Only then is the icon written
  to the host, under the name `<app>.<ext>`.

A refusal raises IconError with the reason. The caller warns (GINN-W-ICON-SKIPPED) and exports the
entry without that icon.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ginn.export._files import write_file_atomic
from ginn.export._realm import (
    RealmFileTooLarge,
    RealmFileUnreadable,
    read_realm_file,
    run_listing,
)
from ginn.podman.adapter import ExecTarget, Podman

ICON_MAX_BYTES: Final = 2 * 1024 * 1024
"""The largest icon file ginn copies out of a realm, in bytes (realm input is untrusted)."""
ICON_TARGET_PX: Final = 256
ICON_EXTENSIONS: Final = (".png", ".svg", ".xpm")
HICOLOR: Final = "/usr/share/icons/hicolor"
PIXMAPS: Final = "/usr/share/pixmaps"
ICON_LIST: Final = (
    "find",
    HICOLOR,
    PIXMAPS,
    "-mindepth",
    "1",
    "-maxdepth",
    "3",
    "(",
    "-type",
    "f",
    "-o",
    "-type",
    "l",
    ")",
    "(",
    "-path",
    "*/apps/*",
    "-o",
    "-path",
    f"{PIXMAPS}/*",
    ")",
    "-printf",
    "%y %s %p %l\\n",
)
"""The argv of the one listing a sync runs in the realm. find exits 1 when one root is missing,
and its output for the other root is still used (see IconIndex.build)."""
_ALLOWED_PREFIXES: Final = (f"{HICOLOR}/", f"{PIXMAPS}/")
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_SIZED_DIR: Final = re.compile(r"(\d{1,4})x\1")
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_XPM_HEADER: Final = b"/* XPM */"
_SVG_ENTITY: Final = b"<!ENTITY"


class IconError(Exception):
    """An icon cannot be used. The message is the reason the entry is exported without it."""


@dataclass(frozen=True)
class _Candidate:
    """A file a lookup may return. `stem` is the name the listing gives it, which for a symbolic
    link is the link's own name, not its target's. `path` and `ext` are the target's."""

    stem: str
    listed: str
    path: str
    rank: tuple[int, int]
    ext: str
    size: int
    """The listed size of the file this candidate copies (the target's, for a symbolic link)."""


class IconIndex:
    """The icon files of one realm, from one listing. Build it with `build`."""

    def __init__(self, by_stem: dict[str, list[_Candidate]], error: str | None) -> None:
        self._by_stem = by_stem
        self._by_path: dict[str, _Candidate] = {}
        for candidates in by_stem.values():
            for candidate in candidates:
                self._by_path[candidate.listed] = candidate
        self.error = error
        """Why the listing failed, or None. A failed listing leaves theme names unresolved."""

    @classmethod
    def build(
        cls, podman: Podman, container_id: str, *, realm: str, target: ExecTarget
    ) -> IconIndex:
        """Run `ICON_LIST` in the realm, bounded. A bad status gives an empty index with its reason.

        A podman failure, a hang or an overflow raises GINN-E-EXPORT-LIST (see `run_listing`).
        """
        result = run_listing(podman, container_id, ICON_LIST, realm=realm, target=target)
        if result.exit_code not in (0, 1):
            return cls({}, f"find exited with status {result.exit_code}")
        return cls.from_listing(result.stdout)

    @classmethod
    def from_listing(cls, listing: str) -> IconIndex:
        rows: list[tuple[str, str, str, str]] = []
        for line in listing.splitlines():
            parts = line.split(" ", 3)
            if len(parts) == 4:
                rows.append((parts[0], parts[1], parts[2], parts[3]))
        # A symbolic link is used only when its target is a listed regular file, and then with
        # that file's listed size. Every copy therefore has a size that the cap checks first.
        regular = {
            posixpath.normpath(listed): int(size_text)
            for kind, size_text, listed, _target in rows
            if kind == "f" and size_text.isdigit()
        }
        by_stem: dict[str, list[_Candidate]] = {}
        for kind, _size_text, listed, target in rows:
            candidate = _candidate(kind, listed, target, regular)
            if candidate is None:
                continue
            by_stem.setdefault(candidate.stem, []).append(candidate)
        return cls(by_stem, None)

    def at(self, path: str) -> _Candidate | None:
        """The candidate listed at the absolute `path`, or None when it is not listed."""
        return self._by_path.get(path)

    def lookup(self, name: str) -> _Candidate:
        """The best candidate for a theme `name`, with or without an extension."""
        if self.error is not None:
            raise IconError(f"the realm's icon listing failed: {self.error}")
        stem, ext = _split_extension(name)
        matches = [c for c in self._by_stem.get(stem, []) if not ext or c.ext == ext]
        if not matches:
            raise IconError(f"no icon named {name!r} in the realm's hicolor or pixmaps directories")
        return min(matches, key=lambda candidate: candidate.rank)


def _candidate(kind: str, listed: str, target: str, regular: dict[str, int]) -> _Candidate | None:
    """One listing row as a candidate, or None when the row is not an icon ginn may copy.

    `regular` maps each listed regular file to its size. A regular file is its own copy. A
    symbolic link is its target, which must be in `regular`. A link with no listed target is
    refused.
    """
    if kind == "l":
        if not target:
            return None
        joined = (
            target if target.startswith("/") else posixpath.join(posixpath.dirname(listed), target)
        )
        path = posixpath.normpath(joined)
    elif kind == "f":
        path = posixpath.normpath(listed)
    else:
        return None
    size = regular.get(path)
    if size is None or not _allowed(path):
        return None
    ext = posixpath.splitext(path)[1]
    rank = _rank(path, ext)
    if rank is None:
        return None
    listed_name = posixpath.basename(listed)
    stem = listed_name[: -len(posixpath.splitext(listed_name)[1])]
    if posixpath.splitext(listed_name)[1] not in ICON_EXTENSIONS:
        stem = listed_name
    return _Candidate(
        stem=stem, listed=posixpath.normpath(listed), path=path, rank=rank, ext=ext, size=size
    )


def _rank(path: str, ext: str) -> tuple[int, int] | None:
    """Lower is better: the scalable SVG, then the sized icon nearest the target, then pixmaps."""
    if posixpath.dirname(path) == PIXMAPS:
        return (3, 0)
    if not path.startswith(f"{HICOLOR}/"):
        return None
    parts = path[len(HICOLOR) + 1 :].split("/")
    if len(parts) != 3 or parts[2] != posixpath.basename(path):
        return None
    size_dir = parts[0]
    if size_dir == "scalable":
        return (0, 0) if ext == ".svg" else None
    sized = _SIZED_DIR.fullmatch(size_dir)
    if sized is None or parts[1] != "apps":
        return None
    px = int(sized.group(1))
    return (1, -px) if px <= ICON_TARGET_PX else (2, px)


def _allowed(path: str) -> bool:
    """True for a normalized absolute path in an icon root, with an icon extension and a safe
    name."""
    if not path.startswith(_ALLOWED_PREFIXES) or "\x00" in path:
        return False
    if posixpath.splitext(path)[1] not in ICON_EXTENSIONS:
        return False
    return (
        _NAME.fullmatch(posixpath.basename(path)[: -len(posixpath.splitext(path)[1])]) is not None
    )


def _split_extension(name: str) -> tuple[str, str]:
    """`firefox.png` gives ("firefox", ".png"); a name without a known extension gives ("", ...)."""
    stem, ext = posixpath.splitext(name)
    if ext in ICON_EXTENSIONS:
        return stem, ext
    return name, ""


def resolve_icon(index: IconIndex, name: str) -> tuple[str, int]:
    """The realm path of the icon `Icon=name` refers to, and its listed size.

    An absolute path must be in the listing, so it has a listed size and a symbolic link that
    leads out of the icon roots is not used. Raise IconError when the name cannot be used.
    """
    if name.startswith("/"):
        if index.error is not None:
            raise IconError(f"the realm's icon listing failed: {index.error}")
        found = index.at(posixpath.normpath(name))
        if found is None:
            raise IconError(f"absolute icon path {name!r} is not an icon in the realm")
        return found.path, found.size
    stem, _ext = _split_extension(name)
    if "/" in name or _NAME.fullmatch(stem) is None:
        raise IconError(f"icon name {name!r} is not a valid theme name")
    best = index.lookup(name)
    return best.path, best.size


def copy_icon(
    podman: Podman,
    container_id: str,
    realm_path: str,
    *,
    app: str,
    dest_dir: Path,
    size: int,
    target: ExecTarget,
) -> Path:
    """Copy one realm icon to `dest_dir/<app><ext>` after the checks of the module docstring.

    `size` is the listed size, refused before any read when it is over the cap. The read is capped
    at the same size. Raise IconError for a refusal, a failed read, or a too-large file.
    """
    if size > ICON_MAX_BYTES:
        raise IconError(f"{realm_path} is larger than {ICON_MAX_BYTES} bytes")
    ext = posixpath.splitext(realm_path)[1]
    try:
        data = read_realm_file(podman, container_id, realm_path, cap=ICON_MAX_BYTES, target=target)
    except RealmFileTooLarge as exc:
        raise IconError(f"{realm_path} is larger than {ICON_MAX_BYTES} bytes") from exc
    except RealmFileUnreadable as exc:
        raise IconError(f"cannot copy {realm_path} out of the realm: {exc}") from exc
    if not _matches_extension(ext, data):
        raise IconError(f"{realm_path} does not hold a {ext[1:]} image")
    dest = dest_dir / f"{app}{ext}"
    write_file_atomic(dest, data, 0o644)
    return dest


def _matches_extension(ext: str, data: bytes) -> bool:
    if ext == ".png":
        return data.startswith(_PNG_SIGNATURE)
    if ext == ".xpm":
        return data.lstrip().startswith(_XPM_HEADER)
    if ext == ".svg":
        return b"<svg" in data and _SVG_ENTITY not in data
    return False
