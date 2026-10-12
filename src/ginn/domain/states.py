# SPDX-License-Identifier: Apache-2.0
"""Realm state transitions, as in the DR-020 table.

`TRANSITIONS` lists every target a state may reach. `updating` and `rebuilding` return to the
state they were in before the operation (`previous`). For those two, the targets `stopped` and
`running` are allowed only when they equal `previous`. `updating` may also reach `broken` (FR-120).
`REMOVED` is the sentinel for "realm no longer exists": creating (rollback or cancel) and deleting
end there. It is not a `RealmState`.

An illegal transition is a programming error, so it raises `IllegalTransition`, a `ValueError`,
and not a `GinnError`.
"""

from enum import Enum
from typing import Final

from ginn.domain.enums import RealmState


class Removed(Enum):
    """Sentinel target: the realm has been removed."""

    REMOVED = "removed"


REMOVED: Final = Removed.REMOVED

Target = RealmState | Removed


class IllegalTransition(ValueError):
    """Raised when the code asks for a transition that DR-020 does not allow."""


_S = RealmState

TRANSITIONS: Final[dict[RealmState, frozenset[Target]]] = {
    _S.CREATING: frozenset({_S.STOPPED, _S.BROKEN, REMOVED}),
    _S.STOPPED: frozenset({_S.RUNNING, _S.REBUILDING, _S.UPDATING, _S.DELETING}),
    _S.RUNNING: frozenset({_S.STOPPED, _S.REBUILDING, _S.UPDATING, _S.FAILED, _S.DELETING}),
    _S.UPDATING: frozenset({_S.STOPPED, _S.RUNNING, _S.BROKEN}),
    _S.REBUILDING: frozenset({_S.STOPPED, _S.RUNNING}),
    _S.FAILED: frozenset({_S.RUNNING, _S.STOPPED, _S.DELETING}),
    _S.BROKEN: frozenset({_S.CREATING, _S.DELETING}),
    _S.DELETING: frozenset({REMOVED}),
}

_PREVIOUS_BOUND: Final = frozenset({_S.UPDATING, _S.REBUILDING})
_RESTORABLE: Final = frozenset({_S.STOPPED, _S.RUNNING})


def transition(current: RealmState, target: Target, previous: RealmState | None = None) -> Target:
    """Return `target` when moving from `current` to it is legal, else raise IllegalTransition.

    `previous` is the state before the current `updating` or `rebuilding` operation. It is ignored
    for every other state.
    """
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(f"{current.value} -> {_show(target)} is not allowed (DR-020)")
    if current in _PREVIOUS_BOUND and target in _RESTORABLE and target != previous:
        prior = "no previous state" if previous is None else previous.value
        raise IllegalTransition(
            f"{current.value} may only return to its previous state ({prior}), "
            f"not {_show(target)} (DR-020)"
        )
    return target


def can_transition(current: RealmState, target: Target, previous: RealmState | None = None) -> bool:
    """Return True when `transition` would accept the move."""
    try:
        transition(current, target, previous)
    except IllegalTransition:
        return False
    return True


def assert_transition(
    current: RealmState, target: Target, previous: RealmState | None = None
) -> None:
    """Raise IllegalTransition when the move is not allowed; return None otherwise."""
    transition(current, target, previous)


def _show(target: Target) -> str:
    return "removed" if target is REMOVED else target.value
