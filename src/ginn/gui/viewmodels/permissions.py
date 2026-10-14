# SPDX-License-Identifier: Apache-2.0
"""Permissions editor viewmodel: tier, Custom toggles and shared paths (FR-060..066, FR-103).

Shared paths are checked with the DR-004 domain validator before they enter the pending list. A
path that fails the check is refused with its GINN-E-PATH-FORBIDDEN error and is never saved
(FR-103 AC-1). `commands` returns the argv of the `ginn --json tier` and `ginn --json perms` calls
that apply the pending changes, in the order they must run. Pure Python, no GTK.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ginn.domain import Tier
from ginn.domain.paths import Mode, SharedPath
from ginn.domain.permissions import Toggle, effective_toggles
from ginn.errors import GinnError
from ginn.gui.backend import PermissionsView
from ginn.gui.viewmodels.labels import Failure, failure_of_error


@dataclass(frozen=True)
class Share:
    """One shared path in the pending list: the validated path and its mode."""

    path: str
    mode: Mode


class PermissionsEditor:
    """Pending edits to one realm's tier, toggles and shares, checked as they are made."""

    def __init__(
        self,
        view: PermissionsView,
        *,
        home: Path,
        owned_paths: Sequence[Path],
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._realm = view.name
        self._home = home
        self._owned = tuple(owned_paths)
        self._env = env
        self._origin_tier = Tier.parse(view.tier)
        # A Basic or Development realm shows its tier's set. That set is not its Custom set, so
        # the Custom toggles start empty and only the user's changes are sent.
        self._origin_toggles = (
            frozenset(Toggle(raw) for raw in view.toggles)
            if self._origin_tier is Tier.CUSTOM
            else frozenset()
        )
        self._origin_shares: dict[str, Mode] = {
            share.path: _mode(share.mode) for share in view.shares
        }
        self._tier = self._origin_tier
        self._toggles: set[Toggle] = set(self._origin_toggles)
        self._shares: dict[str, Mode] = dict(self._origin_shares)

    @property
    def realm(self) -> str:
        return self._realm

    @property
    def tier(self) -> Tier:
        return self._tier

    @property
    def editable(self) -> bool:
        """Toggles and shares are edited only in the Custom tier (FR-063)."""
        return self._tier is Tier.CUSTOM

    @property
    def toggles(self) -> frozenset[Toggle]:
        """The toggles that are on. In Basic or Development they show the tier's set (FR-103)."""
        return effective_toggles(self._tier, frozenset(self._toggles))

    @property
    def shares(self) -> tuple[Share, ...]:
        return tuple(Share(path=path, mode=mode) for path, mode in self._shares.items())

    @property
    def dirty(self) -> bool:
        return bool(self.commands())

    def set_tier(self, tier: Tier) -> None:
        """Change the pending tier. Moving away from Custom drops the Custom edits from the plan."""
        self._tier = tier

    def set_toggle(self, toggle: Toggle, on: bool) -> None:
        if not self.editable:
            raise ValueError("toggles are edited only in the Custom tier")
        if on:
            self._toggles.add(toggle)
        else:
            self._toggles.discard(toggle)

    def add_share(self, raw: str, mode: Mode) -> Failure | None:
        """Add a shared path after the DR-004 check. A refused path is not saved.

        Returns the refusal (for example GINN-E-PATH-FORBIDDEN for `/etc`), or None on success.
        """
        if not self.editable:
            raise ValueError("shared paths are edited only in the Custom tier")
        try:
            shared = SharedPath.parse(
                raw, home=self._home, owned_paths=self._owned, mode=mode, env=self._env
            )
        except GinnError as err:
            return failure_of_error(err)
        self._shares[str(shared.path)] = mode
        return None

    def remove_share(self, path: str) -> None:
        if not self.editable:
            raise ValueError("shared paths are edited only in the Custom tier")
        self._shares.pop(path, None)

    def commands(self) -> tuple[tuple[str, ...], ...]:
        """The argv calls, after `ginn`, that apply the pending changes. Empty when none.

        A tier change comes first. A `perms` call follows only when the tier ends as Custom and
        its toggles or shares changed, because `perms` requires the Custom tier.
        """
        calls: list[tuple[str, ...]] = []
        if self._tier is not self._origin_tier:
            calls.append(("--json", "tier", self._realm, self._tier.value))
        if self._tier is Tier.CUSTOM:
            perms = self._perms_args()
            if perms is not None:
                calls.append(perms)
        return tuple(calls)

    def _perms_args(self) -> tuple[str, ...] | None:
        args: list[str] = ["--json", "perms", self._realm]
        enable = sorted(self._toggles - self._origin_toggles, key=lambda toggle: toggle.value)
        disable = sorted(self._origin_toggles - self._toggles, key=lambda toggle: toggle.value)
        removed = [path for path in self._origin_shares if path not in self._shares]
        added = [
            (path, mode)
            for path, mode in self._shares.items()
            if self._origin_shares.get(path) != mode
        ]
        for toggle in enable:
            args.extend(["--enable", toggle.value])
        for toggle in disable:
            args.extend(["--disable", toggle.value])
        for path in removed:
            args.extend(["--no-share", path])
        for path, mode in added:
            args.extend(["--share" if mode == "rw" else "--share-ro", path])
        if len(args) == 3:
            return None
        return tuple(args)


def _mode(raw: str) -> Mode:
    if raw == "ro":
        return "ro"
    if raw == "rw":
        return "rw"
    raise ValueError(f"mode must be 'ro' or 'rw', not {raw!r}")
