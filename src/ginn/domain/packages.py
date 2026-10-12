# SPDX-License-Identifier: Apache-2.0
"""Package names passed to a realm's native package manager (DR-003, AS-027).

The per-distribution regexes of DR-003 decide validity. AS-027 adds a check that names the
rejected character: globs, version constraints, paths, capability "provides" and a leading `-`
(which an option parser would read as a flag). A name such as `gimp-1.2` is valid only because
the regex allows its characters; it is never read as a version constraint.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Self

from ginn.domain._errors import reject
from ginn.domain.enums import DistroId

PACKAGE_CODE: Final = "GINN-E-PKG-NAME-INVALID"

PACKAGE_PATTERNS: Final[Mapping[DistroId, str]] = {
    DistroId.ARCH: r"[a-z0-9@_+][a-z0-9@._+-]{0,127}",
    DistroId.DEBIAN: r"[a-z0-9][a-z0-9.+-]{1,127}",
    DistroId.FEDORA: r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}",
    DistroId.KALI: r"[a-z0-9][a-z0-9.+-]{1,127}",
}

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
    pattern = PACKAGE_PATTERNS[distro]
    if re.fullmatch(pattern, raw) is None:
        raise reject(
            PACKAGE_CODE,
            {"rule": f"must match {pattern} for {distro.value} (DR-003)"},
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
