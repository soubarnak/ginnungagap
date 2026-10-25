# SPDX-License-Identifier: Apache-2.0
"""Package names passed to a realm's native package manager (DR-003, AS-027).

The distribution's `package_name_regex` (DR-003, held in its definition under `ginn.distros`)
decides validity. AS-027 adds a check that names the rejected character: globs, version
constraints, paths, capability "provides" and a leading `-` (which an option parser would read as
a flag). A name such as `gimp-1.2` is valid only because the regex allows its characters; it is
never read as a version constraint.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

from ginn.distros import load
from ginn.domain._errors import reject
from ginn.domain.enums import DistroId

PACKAGE_CODE: Final = "GINN-E-PKG-NAME-INVALID"

_AS027_CHARS: Final = frozenset("*?[]=<>/")


def _check(raw: str, distro: DistroId) -> None:
    if not isinstance(raw, str):
        raise TypeError(f"package name must be a str, not {type(raw).__name__}")
    for ch in raw:
        if ch in _AS027_CHARS or ch.isspace():
            shown = "whitespace" if ch.isspace() else repr(ch)
            raise reject(
                PACKAGE_CODE,
                {"rule": f"AS-027: {shown} is a glob, constraint, path or separator"},
                name=raw,
            )
    if raw.startswith("-"):
        raise reject(PACKAGE_CODE, {"rule": "AS-027: leading '-' reads as an option"}, name=raw)
    if raw.endswith("-"):
        raise reject(
            PACKAGE_CODE,
            {"rule": "AS-027: trailing '-' reads as a removal in apt-get"},
            name=raw,
        )
    pattern = load(distro.value).name_pattern
    if pattern.fullmatch(raw) is None:
        raise reject(
            PACKAGE_CODE,
            {"rule": f"must match {pattern.pattern} for {distro.value} (DR-003)"},
            name=raw,
        )


@dataclass(frozen=True)
class PackageName:
    """A package name valid for one distribution. Validated on construction."""

    value: str
    distro: DistroId

    def __post_init__(self) -> None:
        if not isinstance(self.distro, DistroId):
            raise TypeError("distro must be a DistroId")
        _check(self.value, self.distro)

    @classmethod
    def parse(cls, raw: str, distro: DistroId) -> Self:
        return cls(raw, distro)

    def __str__(self) -> str:
        return self.value


class InstallSource(StrEnum):
    """Where an Install Manifest entry came from (DR-007)."""

    REPO = "repo"
    AUR = "aur"
    METAPACKAGE = "metapackage"


class InstallMethod(StrEnum):
    """How an Install Manifest entry was installed (DR-007). `ginn` is `ginn install`. `native` is
    the realm's own package manager, recorded by the explicit-package diff of a `--root` session
    (FR-042 AC-1)."""

    GINN = "ginn"
    NATIVE = "native"


@dataclass(frozen=True)
class ManifestEntry:
    """One Install Manifest entry (DR-007): a package name, its source and its install method.

    The manifest holds one entry per package name, and it belongs to one realm (DR-001). A stored
    entry is read back with `from_dict`, which raises TypeError or ValueError on a bad field.
    """

    name: str
    source: InstallSource
    method: InstallMethod
    init: bool = False
    """True when the package is in the init set (ADR-015 Decision 6, T7.19)."""

    def to_dict(self) -> dict[str, str | bool]:
        data: dict[str, str | bool] = {
            "name": self.name,
            "source": self.source.value,
            "method": self.method.value,
        }
        if self.init:
            data["init"] = True
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        name = data["name"]
        if not isinstance(name, str) or not name:
            raise TypeError("manifest entry name must be a non-empty string")
        source = data["source"]
        method = data["method"]
        if not isinstance(source, str) or not isinstance(method, str):
            raise TypeError("manifest entry source and method must be strings")
        init = data.get("init", False)
        if not isinstance(init, bool):
            raise TypeError("manifest entry init must be a boolean")
        return cls(name=name, source=InstallSource(source), method=InstallMethod(method), init=init)
