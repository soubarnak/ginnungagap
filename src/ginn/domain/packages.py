# SPDX-License-Identifier: Apache-2.0
"""Package names passed to a realm's native package manager (DR-003, AS-027).

The distribution's `package_name_regex` (DR-003, held in its definition under `ginn.distros`)
decides validity. AS-027 adds a check that names the rejected character: globs, version
constraints, paths, capability "provides" and a leading `-` (which an option parser would read as
a flag). A name such as `gimp-1.2` is valid only because the regex allows its characters; it is
never read as a version constraint.
"""

from dataclasses import dataclass
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
