# SPDX-License-Identifier: Apache-2.0
"""Realm state transitions over every (current, target, previous) triple (DR-020, CL-24).

The domain is finite: 8 states and REMOVED as targets, and `previous` is None or one of the 8
states. So every triple is checked, not sampled. The oracle is the DR-020 table as it was before
the give-up edge, written out here, so the check does not depend on the code it checks.
"""

from __future__ import annotations

import itertools

import pytest

from ginn.domain.enums import RealmState
from ginn.domain.states import REMOVED, IllegalTransition, Target, can_transition, transition

pytestmark = [pytest.mark.fr("DR-020")]

STATES = [state.value for state in RealmState]
TARGETS = [*STATES, "removed"]
PREVIOUS = [None, *STATES]
GIVE_UP_PAIR = ("stopped", "failed")

# DR-020 before CL-24, with the two "previous state" rows of updating and rebuilding.
DR020: dict[str, set[str]] = {
    "creating": {"stopped", "broken", "removed"},
    "stopped": {"running", "rebuilding", "updating", "deleting"},
    "running": {"stopped", "rebuilding", "updating", "failed", "deleting"},
    "failed": {"running", "stopped", "deleting"},
    "broken": {"creating", "deleting"},
    "deleting": {"removed"},
    "updating": {"stopped", "running", "broken"},
    "rebuilding": {"stopped", "running"},
}
RESTORED = {"stopped", "running"}


def oracle(current: str, target: str, previous: str | None) -> bool:
    """True when DR-020, without the give-up edge, allows the move."""
    if target not in DR020[current]:
        return False
    if current in ("updating", "rebuilding") and target in RESTORED:
        return target == previous
    return True


def as_target(value: str) -> Target:
    return REMOVED if value == "removed" else RealmState(value)


def as_previous(value: str | None) -> RealmState | None:
    return None if value is None else RealmState(value)


TRIPLES = list(itertools.product(STATES, TARGETS, PREVIOUS))


def test_the_domain_is_the_whole_grid() -> None:
    assert len(TRIPLES) == len(STATES) * len(TARGETS) * len(PREVIOUS) == 648


def test_without_give_up_every_triple_keeps_its_dr020_result() -> None:
    for current, target, previous in TRIPLES:
        expected = oracle(current, target, previous)
        actual = can_transition(RealmState(current), as_target(target), as_previous(previous))
        assert actual is expected, (current, target, previous)


def test_give_up_changes_only_the_stopped_to_failed_pair() -> None:
    for current, target, previous in TRIPLES:
        plain = can_transition(RealmState(current), as_target(target), as_previous(previous))
        given_up = can_transition(
            RealmState(current), as_target(target), as_previous(previous), give_up=True
        )
        if (current, target) == GIVE_UP_PAIR:
            assert plain is False, (current, target, previous)
            assert given_up is True, (current, target, previous)
        else:
            assert given_up is plain, (current, target, previous)


def test_a_refused_triple_raises_and_a_legal_one_returns_its_target() -> None:
    for current, target, previous in TRIPLES:
        allowed = oracle(current, target, previous)
        if allowed:
            assert transition(
                RealmState(current), as_target(target), as_previous(previous)
            ) == as_target(target)
        else:
            with pytest.raises(IllegalTransition):
                transition(RealmState(current), as_target(target), as_previous(previous))
