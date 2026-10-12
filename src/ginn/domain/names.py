# SPDX-License-Identifier: Apache-2.0
"""Realm names (DR-002).

Validation runs in `__post_init__`, so `RealmName("root")` fails as well as `RealmName.parse`.
The rule: a lowercase letter, then up to 31 lowercase letters, digits or hyphens. The words
`ginn`, `host`, `root` and `all` are reserved. Matching uses `re.fullmatch` so a trailing newline
is rejected.
"""

import re
from dataclasses import dataclass
from typing import Final, Self

from ginn.domain._errors import reject

NAME_CODE: Final = "GINN-E-NAME-INVALID"
NAME_RULE: Final = "^[a-z][a-z0-9-]{0,31}$ (DR-002)"
RESERVED_NAMES: Final[frozenset[str]] = frozenset({"ginn", "host", "root", "all"})
_NAME_BODY: Final = re.compile(r"[a-z][a-z0-9-]{0,31}")


@dataclass(frozen=True)
class RealmName:
    """A valid realm name. Its value is always one that DR-002 accepts."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError(f"realm name must be a str, not {type(self.value).__name__}")
        if _NAME_BODY.fullmatch(self.value) is None:
            raise reject(NAME_CODE, {"rule": NAME_RULE}, name=self.value)
        if self.value in RESERVED_NAMES:
            raise reject(NAME_CODE, {"rule": "reserved word (DR-002)"}, name=self.value)

    @classmethod
    def parse(cls, raw: str) -> Self:
        return cls(raw)

    def __str__(self) -> str:
        return self.value
