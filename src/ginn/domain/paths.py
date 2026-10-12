# SPDX-License-Identifier: Apache-2.0
"""Host paths a realm may be given (DR-004, AS-011, NFR-SEC-006).

`SharedPath.parse` is the only way to get a `SharedPath`. It resolves symlinks and `..` on the
raw string with `os.path.realpath`, then checks the resolved target. The stored `path` is that
resolved target, so the bind mount uses exactly what was validated. Lexical `normpath` is not run
first: `link/../x` means something different once `link` is followed.

Rule order: the input must be absolute with no NUL byte and no `:` or `,`. Podman's `-v` and
`--mount` syntax splits on those characters, so such a path would bind a different mount. The
resolved target gets the same character check, because a symlink can hide them. Then the resolved
target must not be a forbidden path, must not contain one, and must not sit inside one (see
`_conflicts`). It must lie strictly under an allowed root. Forbidden rules are checked before the
root rule, so `/etc` reports its own rule.

`FORBIDDEN_RULES` is the data that docs/capabilities.md and the GUI render. The rules are checked
against resolved paths, so a symlink to a forbidden target is refused like the target itself.
Validation happens at share time. Symlinks created inside a share later are not checked here.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self

from ginn.domain._errors import reject
from ginn.errors import GinnError

Mode = Literal["ro", "rw"]

PATH_CODE: Final = "GINN-E-PATH-FORBIDDEN"
OWNED: Final = "<owned>"
"""`ForbiddenRule.path` value that stands for every Owned Path the caller supplies."""

_TOKEN: Final = object()


@dataclass(frozen=True)
class ForbiddenRule:
    """One never-shareable path. `path` is `~` (home), `~/...`, an absolute path or `OWNED`.

    `subtree` True also forbids anything under the path. `ro_only` True allows a read-only share
    (shell startup files) and forbids only a writable one.
    """

    rule_id: str
    path: str
    description: str
    subtree: bool = True
    ro_only: bool = False


FORBIDDEN_RULES: Final[tuple[ForbiddenRule, ...]] = (
    ForbiddenRule("DR-004.root", "/", "the file system root", subtree=False),
    ForbiddenRule("DR-004.etc", "/etc", "system configuration"),
    ForbiddenRule("DR-004.usr", "/usr", "system files"),
    ForbiddenRule("DR-004.home", "~", "the home directory itself", subtree=False),
    ForbiddenRule("DR-004.ssh", "~/.ssh", "SSH keys"),
    ForbiddenRule("DR-004.gnupg", "~/.gnupg", "GnuPG keys"),
    ForbiddenRule("DR-004.owned", OWNED, "ginn's Owned Paths"),
    ForbiddenRule("AS-011.applications", "~/.local/share/applications", "host desktop entries"),
    ForbiddenRule("AS-011.autostart", "~/.config/autostart", "host autostart entries"),
    ForbiddenRule("AS-011.bashrc", "~/.bashrc", "bash startup file", subtree=False, ro_only=True),
    ForbiddenRule(
        "AS-011.bash_profile", "~/.bash_profile", "bash startup file", subtree=False, ro_only=True
    ),
    ForbiddenRule(
        "AS-011.bash_login", "~/.bash_login", "bash startup file", subtree=False, ro_only=True
    ),
    ForbiddenRule(
        "AS-011.profile", "~/.profile", "shell startup file", subtree=False, ro_only=True
    ),
    ForbiddenRule("AS-011.zshrc", "~/.zshrc", "zsh startup file", subtree=False, ro_only=True),
    ForbiddenRule(
        "AS-011.zprofile", "~/.zprofile", "zsh startup file", subtree=False, ro_only=True
    ),
    ForbiddenRule("AS-011.zshenv", "~/.zshenv", "zsh startup file", subtree=False, ro_only=True),
    ForbiddenRule(
        "AS-011.fish",
        "~/.config/fish/config.fish",
        "fish startup file",
        subtree=False,
        ro_only=True,
    ),
)

ALLOWED_ROOTS: Final[tuple[str, ...]] = ("~", "/run/media", "/media", "/mnt")
"""Roots a share must lie strictly under. `~` is `$HOME`."""


def _real(path: str | Path) -> Path:
    return Path(os.path.realpath(path))


def _expand(template: str, home: Path) -> Path:
    if template == "~":
        return home
    if template.startswith("~/"):
        return home / template[2:]
    return Path(template)


def _strictly_under(path: Path, ancestor: Path) -> bool:
    return ancestor in path.parents


def _forbidden(home: Path, owned: Sequence[Path]) -> list[tuple[ForbiddenRule, Path]]:
    pairs: list[tuple[ForbiddenRule, Path]] = []
    for rule in FORBIDDEN_RULES:
        if rule.path == OWNED:
            pairs.extend((rule, _real(path)) for path in owned)
        else:
            pairs.append((rule, _real(_expand(rule.path, home))))
    return pairs


def _roots(home: Path) -> tuple[Path, ...]:
    return tuple(home if root == "~" else _real(root) for root in ALLOWED_ROOTS)


def _conflicts(target: Path, forbidden: Path, rule: ForbiddenRule, mode: Mode) -> bool:
    """Return True when a share of `target` breaks `rule`, for a share in `mode`.

    A share conflicts when it is the forbidden path, when it sits under a subtree-forbidden path,
    or when the forbidden path sits under it. The exception is a read-only share of an `ro_only`
    path, which is allowed.
    """
    hit = (
        target == forbidden
        or (rule.subtree and _strictly_under(target, forbidden))
        or _strictly_under(forbidden, target)
    )
    exempt = rule.ro_only and mode == "ro"
    return hit and not exempt


def _refuse(raw: str, reason: str) -> GinnError:
    return reject(PATH_CODE, {"reason": reason}, path=raw)


def _podman_separator(text: str) -> bool:
    """True when `text` holds a character that podman's -v and --mount syntax splits on."""
    return ":" in text or "," in text


_SEPARATOR_REASON: Final = "contains ':' or ','; podman's -v and --mount syntax cannot carry it"


@dataclass(frozen=True)
class SharedPath:
    """A host path validated for sharing with a realm. Build it with `parse`.

    `path` is the resolved target. `target_exists` is False when the target is missing, which is
    allowed but reported. The `_token` field refuses direct construction without validation.
    """

    path: Path
    mode: Mode
    target_exists: bool
    _token: object

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise TypeError("SharedPath is built with SharedPath.parse(), which validates it")

    @classmethod
    def parse(
        cls,
        raw: str,
        *,
        home: Path,
        owned_paths: Sequence[Path],
        mode: Mode,
    ) -> Self:
        if mode not in ("ro", "rw"):
            raise ValueError(f"mode must be 'ro' or 'rw', not {mode!r}")
        if not isinstance(raw, str):
            raise TypeError(f"shared path must be a str, not {type(raw).__name__}")
        if not home.is_absolute():
            raise ValueError(f"home must be absolute, not {home}")
        if "\0" in raw:
            raise _refuse(raw, "contains a NUL byte")
        if not raw.startswith("/"):
            raise _refuse(raw, "not absolute; give the full path, since ~ is not expanded")
        if _podman_separator(raw):
            raise _refuse(raw, _SEPARATOR_REASON)

        home_real = _real(home)
        owned_real = [_real(path) for path in owned_paths]
        target = _real(raw)
        if _podman_separator(str(target)):
            raise _refuse(raw, f"resolves to a path that {_SEPARATOR_REASON}")
        for rule, forbidden in _forbidden(home_real, owned_real):
            if _conflicts(target, forbidden, rule, mode):
                raise _refuse(raw, f"{rule.rule_id}: {rule.description}")
        if not any(_strictly_under(target, root) for root in _roots(home_real)):
            raise _refuse(raw, "outside $HOME, /run/media, /media and /mnt")
        return cls(
            path=target,
            mode=mode,
            target_exists=os.path.exists(target),
            _token=_TOKEN,
        )

    def __str__(self) -> str:
        return str(self.path)
