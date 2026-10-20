# SPDX-License-Identifier: Apache-2.0
"""The Install Manifest rules (DR-007, FR-040, FR-041, FR-042 AC-1).

These functions are pure. Each one takes the entries of a realm and returns a new list, sorted by
package name, so the stored JSON is the same for the same set of packages. The manifest is kept
in the realm record (store/records.py), so a write goes through the record's atomic save and the
realm lock (DR-010, DR-035). Nothing here reads or writes a file.

A native entry takes source `repo`. The explicit-package diff does not say where a package came
from, and the realm's own manager installs from its repositories (DR-007 source).
"""

from collections.abc import Iterable, Sequence

from ginn.domain import InstallMethod, InstallSource, ManifestEntry

NATIVE_SOURCE = InstallSource.REPO
"""The source of a package the realm's own manager installed (DR-007)."""


def _sorted(entries: Iterable[ManifestEntry]) -> list[ManifestEntry]:
    return sorted(entries, key=lambda entry: entry.name)


def installed(
    entries: Sequence[ManifestEntry],
    names: Sequence[str],
    *,
    source: InstallSource,
    method: InstallMethod,
) -> list[ManifestEntry]:
    """`entries` with each name in `names` set to `source` and `method` (FR-040).

    An entry already present for a name is replaced, so a package that `ginn install` names again
    takes the `ginn` method. An init entry keeps its `init` flag through the replacement, so the
    init set of a services-on realm stays complete (ADR-015 Decision 6). The other entries stay as
    they are.
    """
    chosen = set(names)
    init = {entry.name for entry in entries if entry.init}
    kept = [entry for entry in entries if entry.name not in chosen]
    added = [
        ManifestEntry(name=name, source=source, method=method, init=name in init) for name in chosen
    ]
    return _sorted([*kept, *added])


def removed(entries: Sequence[ManifestEntry], names: Sequence[str]) -> list[ManifestEntry]:
    """`entries` without the names in `names` (FR-041). A name with no entry changes nothing."""
    chosen = set(names)
    return [entry for entry in entries if entry.name not in chosen]


def recorded_native(entries: Sequence[ManifestEntry], names: Sequence[str]) -> list[ManifestEntry]:
    """`entries` plus a `native` entry for each name that has none (FR-042 AC-1).

    An entry that exists keeps its method. A package that `ginn install` recorded stays `ginn`, so
    the explicit-package diff never turns a ginn install into a native one.
    """
    known = {entry.name for entry in entries}
    fresh = [
        ManifestEntry(name=name, source=NATIVE_SOURCE, method=InstallMethod.NATIVE)
        for name in set(names) - known
    ]
    return _sorted([*entries, *fresh])


__all__ = ["NATIVE_SOURCE", "installed", "recorded_native", "removed"]
