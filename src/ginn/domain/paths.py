# SPDX-License-Identifier: Apache-2.0
"""Host paths a realm may be given (DR-004, AS-011, NFR-SEC-006).

`path_refusal` is the one rule check. `SharedPath.parse` calls it, and so do the mount plan in
runtime/mounts.py and the session environment in runtime/env.py, so a path is refused the same way
wherever it comes from. The caller passes `env`, so the AS-011 XDG overrides come from the host
that the caller describes. Only `SharedPath.parse` falls back to `os.environ`, and only when no env
is given.

`SharedPath.parse` is the only way to get a `SharedPath`. It resolves symlinks and `..` on the raw
string with `os.path.realpath`, then checks the resolved target. The stored `path` is that resolved
target, so the bind mount uses exactly what was validated. Lexical `normpath` is not run first:
`link/../x` means something different once `link` is followed.

Rule order: the input must be absolute and hold no NUL, no `:`, no `,` and no newline. Podman's
`-v` and `--mount` syntax splits on those characters, so such a path would bind a different mount.
The resolved target gets the same character check, because a symlink can hide them. Then the
resolved target must not be a forbidden path, must not contain one, and must not sit inside one
(see `_conflicts`). `SharedPath.parse` also requires it to lie strictly under an allowed root.
Forbidden rules are checked before the root rule, so `/etc` reports its own rule.

`FORBIDDEN_RULES` is the data that docs/capabilities.md and the GUI render. The two AS-011 desktop
and autostart rules also follow `XDG_DATA_HOME` and `XDG_CONFIG_HOME` when those are set to absolute
paths (`_xdg_rules`). The hard-coded defaults always apply as well.

A fixed system grant (AS-009) is the one exception the caller can ask for. Its real source may lie
under a system subtree, for example `/etc/localtime` resolves to `/usr/share/zoneinfo`. The rule
that holds that source is exempt for that source only (see `path_refusal`).

Validation happens at share time. Symlinks created inside a share later are not checked here.
`SharedPath.revalidate()` runs the checks again for the mount builder at create and start.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self

from ginn.domain._errors import reject
from ginn.errors import GinnError

Mode = Literal["ro", "rw"]

PATH_CODE: Final = "GINN-E-PATH-FORBIDDEN"
OWNED: Final = "<owned>"
"""`ForbiddenRule.path` value that stands for every Owned Path the caller supplies."""

ROOTS_REASON: Final = "DR-004: outside $HOME, /run/media, /media and /mnt"
"""The refusal for a share whose resolved target lies outside every allowed root."""

_TOKEN: Final = object()
_XDG_NAMES: Final = ("XDG_DATA_HOME", "XDG_CONFIG_HOME")
_UNBINDABLE: Final = (":", ",", "\0", "\n", "\r")
_UNBINDABLE_REASON: Final = (
    "contains ':' or ',', a NUL or a newline; podman's -v and --mount syntax cannot carry it"
)


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


def _xdg_rules(env: Mapping[str, str]) -> tuple[ForbiddenRule, ...]:
    """AS-011 rules for `$XDG_DATA_HOME/applications` and `$XDG_CONFIG_HOME/autostart`.

    The host desktop and autostart directories move when these variables are set. Only absolute
    values count, as in the XDG Base Directory spec. An empty or relative value is ignored, so
    the defaults in FORBIDDEN_RULES are the only rules that apply.
    """
    rules: list[ForbiddenRule] = []
    data_home = env.get("XDG_DATA_HOME", "")
    if os.path.isabs(data_home):
        apps = str(Path(data_home) / "applications")
        rules.append(ForbiddenRule("AS-011.applications", apps, "host desktop entries"))
    config_home = env.get("XDG_CONFIG_HOME", "")
    if os.path.isabs(config_home):
        autostart = str(Path(config_home) / "autostart")
        rules.append(ForbiddenRule("AS-011.autostart", autostart, "host autostart entries"))
    return tuple(rules)


def _xdg_pairs(env: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """The XDG overrides from `env`. A SharedPath keeps them, so revalidate uses the same rules."""
    return tuple((name, env[name]) for name in _XDG_NAMES if name in env)


def _forbidden(
    home: Path, owned: Sequence[Path], extra: Sequence[ForbiddenRule]
) -> list[tuple[ForbiddenRule, Path]]:
    pairs: list[tuple[ForbiddenRule, Path]] = []
    for rule in (*FORBIDDEN_RULES, *extra):
        if rule.path == OWNED:
            pairs.extend((rule, _real(path)) for path in owned)
        else:
            pairs.append((rule, _real(_expand(rule.path, home))))
    return pairs


def _roots(home: Path) -> tuple[Path, ...]:
    home_real = _real(home)
    return tuple(home_real if root == "~" else _real(root) for root in ALLOWED_ROOTS)


def under_allowed_root(target: Path, *, home: Path, root_itself: bool = False) -> bool:
    """True when `target`, already resolved, lies strictly under one of ALLOWED_ROOTS.

    With `root_itself`, a target equal to a root also counts. The mount roots `/media` and `/mnt`
    are attached as themselves (FR-061), so they pass with it. A user share never does.
    """
    roots = _roots(home)
    if root_itself and target in roots:
        return True
    return any(_strictly_under(target, root) for root in roots)


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


def _exempt_by_grant(rule: ForbiddenRule, forbidden: Path, target: Path, grant: Path) -> bool:
    """True when `rule` is the fixed grant's own system subtree, and `target` is that grant.

    Only an absolute subtree rule can be exempt, so `~/.ssh` and `OWNED` never are. Only the rule
    whose forbidden path is an ancestor of the grant's real source qualifies. The target must be
    that source or lie under it.
    """
    if not (rule.subtree and rule.path.startswith("/")):
        return False
    at_grant = target == grant or grant in target.parents
    return at_grant and forbidden in grant.parents


def _refuse(raw: str, reason: str) -> GinnError:
    return reject(PATH_CODE, {"reason": reason}, path=raw)


def path_refusal(
    target: str | Path,
    *,
    env: Mapping[str, str],
    home: Path,
    owned_paths: Sequence[Path],
    mode: Mode,
    fixed: str | Path | None = None,
) -> str | None:
    """Return the rule that forbids binding `target`, or None when no rule applies.

    The reason is `RULE_ID: description`, or a plain sentence for a path that is not absolute or
    holds a character podman cannot carry. `target` may be raw text. It is checked as given, then
    resolved and checked again, so a symlink to a forbidden or unusable path is refused like the
    path itself.

    A rule applies when the target is the forbidden path, when the forbidden path lies under the
    target (the bind would expose it), or, for a subtree rule, when the target lies under the
    forbidden path. A read-only bind is exempt from the AS-011 startup-file rules.

    `fixed` names an AS-009 grant, such as /usr/share/fonts or /etc/localtime. Its real path is the
    source that is bound. The one absolute subtree rule that holds that source is exempt, and only
    for a target at the source or under it. Every other rule still applies, so a fixed grant that
    resolves into ~/.ssh is refused.
    """
    raw = str(target)
    if not os.path.isabs(raw):
        return "not an absolute path"
    if any(char in raw for char in _UNBINDABLE):
        return _UNBINDABLE_REASON
    resolved = _real(raw)
    if any(char in str(resolved) for char in _UNBINDABLE):
        return f"resolves to a path that {_UNBINDABLE_REASON}"
    grant = None if fixed is None else _real(fixed)
    owned_real = [_real(path) for path in owned_paths]
    for rule, forbidden in _forbidden(_real(home), owned_real, _xdg_rules(env)):
        if grant is not None and _exempt_by_grant(rule, forbidden, resolved, grant):
            continue
        if _conflicts(resolved, forbidden, rule, mode):
            return f"{rule.rule_id}: {rule.description}"
    return None


@dataclass(frozen=True)
class SharedPath:
    """A host path validated for sharing with a realm. Build it with `parse`.

    `path` is the resolved target. `target_exists` is False when the target is missing, which is
    allowed but reported. `_raw`, `_home`, `_owned` and `_xdg` are the parse inputs, kept so that
    `revalidate` can run the same checks again. `_xdg` holds only the AS-011 overrides. The
    `_token` field refuses direct construction without validation.
    """

    path: Path
    mode: Mode
    target_exists: bool
    _raw: str
    _home: Path
    _owned: tuple[Path, ...]
    _xdg: tuple[tuple[str, str], ...]
    _token: object

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise TypeError("SharedPath is built with SharedPath.parse(), which validates it")

    def revalidate(self) -> None:
        """Run the share checks again. Refuse if the source now resolves to another target.

        `parse` runs once, when the share is added. A symlink on the source path can be replaced
        after that, so a share that was valid then may now resolve to a forbidden or different
        target. The mount builder (runtime/mounts.py in M4, and the start path in M6) must call
        this at realm create and at every start, before it passes `self.path` to podman.

        Raises GINN-E-PATH-FORBIDDEN when the source no longer validates, or when it resolves to a
        target other than `self.path`. This narrows the window but does not close it. Podman
        resolves the path again when it creates the mount, so a swap between this check and the
        mount still gets through. Returns None when the share is unchanged.
        """
        fresh = SharedPath.parse(
            self._raw,
            home=self._home,
            owned_paths=self._owned,
            mode=self.mode,
            env=dict(self._xdg),
        )
        if fresh.path != self.path:
            raise _refuse(
                self._raw,
                f"resolved target changed from {self.path} to {fresh.path} after validation",
            )

    @classmethod
    def parse(
        cls,
        raw: str,
        *,
        home: Path,
        owned_paths: Sequence[Path],
        mode: Mode,
        env: Mapping[str, str] | None = None,
    ) -> Self:
        """Validate `raw` as a share. `env` carries the AS-011 overrides; None reads os.environ."""
        if mode not in ("ro", "rw"):
            raise ValueError(f"mode must be 'ro' or 'rw', not {mode!r}")
        if not isinstance(raw, str):
            raise TypeError(f"shared path must be a str, not {type(raw).__name__}")
        if not home.is_absolute():
            raise ValueError(f"home must be absolute, not {home}")
        environ: Mapping[str, str] = os.environ if env is None else env
        if not raw.startswith("/"):
            raise _refuse(raw, "not absolute; give the full path, since ~ is not expanded")
        reason = path_refusal(raw, env=environ, home=home, owned_paths=owned_paths, mode=mode)
        if reason is not None:
            raise _refuse(raw, reason)
        target = _real(raw)
        if not under_allowed_root(target, home=home):
            raise _refuse(raw, ROOTS_REASON)
        return cls(
            path=target,
            mode=mode,
            target_exists=os.path.exists(target),
            _raw=raw,
            _home=home,
            _owned=tuple(owned_paths),
            _xdg=_xdg_pairs(environ),
            _token=_TOKEN,
        )

    def __str__(self) -> str:
        return str(self.path)
