# SPDX-License-Identifier: Apache-2.0
"""Realm state transitions (DR-020), checked against the table written out here."""

import pytest

from ginn.domain.enums import RealmState
from ginn.domain.states import (
    GIVE_UP_EDGES,
    REMOVED,
    TRANSITIONS,
    IllegalTransition,
    Removed,
    assert_transition,
    can_transition,
    transition,
)

pytestmark = [pytest.mark.fr("DR-020")]

# The DR-020 table in GINN-SRS-001 section 6.1, copied by hand. It is the oracle for this file.
# "removed" is the REMOVED sentinel. "previous" is the state before the operation.
TABLE: dict[str, set[str]] = {
    "creating": {"stopped", "broken", "removed"},
    "stopped": {"running", "rebuilding", "updating", "deleting"},
    "running": {"stopped", "rebuilding", "updating", "failed", "deleting"},
    "failed": {"running", "stopped", "deleting"},
    "broken": {"creating", "deleting"},
    "deleting": {"removed"},
}
PREVIOUS_RESTORED = {"stopped", "running"}
STATES = [state.value for state in RealmState]
TARGETS = [*STATES, "removed"]


def oracle(current: str, target: str, previous: str | None) -> bool:
    if current in TABLE:
        return target in TABLE[current]
    if current == "updating":
        # Back to the previous state, or to broken (FR-120).
        if target == "broken":
            return True
        return target in PREVIOUS_RESTORED and target == previous
    if current == "rebuilding":
        # Back to the previous state only; the old root stays intact on failure.
        return target in PREVIOUS_RESTORED and target == previous
    raise AssertionError(f"no oracle row for {current}")


def as_target(value: str) -> RealmState | Removed:
    return REMOVED if value == "removed" else RealmState(value)


def test_every_state_has_a_row_in_the_transition_table() -> None:
    assert set(TRANSITIONS) == set(RealmState)


# The rows of the TABLE, with the two "previous state" rows spelled out as their possible targets.
# The flat table also holds the give-up edge stopped to failed (CL-24). TABLE does not: it is the
# answer without give_up, and that edge is refused without it.
ROWS: dict[str, set[str]] = {
    **TABLE,
    "stopped": {"running", "rebuilding", "updating", "deleting", "failed"},
    "updating": {"stopped", "running", "broken"},
    "rebuilding": {"stopped", "running"},
}


@pytest.mark.parametrize("current", STATES)
def test_transition_table_rows_match_dr020(current: str) -> None:
    expected = {as_target(value) for value in ROWS[current]}
    assert set(TRANSITIONS[RealmState(current)]) == expected


@pytest.mark.parametrize("current", STATES)
def test_full_matrix_matches_dr020_for_every_target_and_previous(current: str) -> None:
    previous_values: list[str | None] = [None, *STATES]
    for target in TARGETS:
        for previous in previous_values:
            expected = oracle(current, target, previous)
            actual = can_transition(RealmState(current), as_target(target), _as_previous(previous))
            assert actual is expected, (current, target, previous)


def _as_previous(value: str | None) -> RealmState | None:
    return None if value is None else RealmState(value)


@pytest.mark.parametrize(
    ("current", "target", "previous"),
    [
        ("updating", "stopped", "stopped"),
        ("updating", "running", "running"),
        ("updating", "broken", None),
        ("rebuilding", "stopped", "stopped"),
        ("rebuilding", "running", "running"),
        ("creating", "stopped", None),
        ("creating", "broken", None),
        ("creating", "removed", None),
        ("stopped", "running", None),
        ("stopped", "rebuilding", None),
        ("stopped", "updating", None),
        ("stopped", "deleting", None),
        ("running", "stopped", None),
        ("running", "failed", None),
        ("running", "deleting", None),
        ("failed", "running", None),
        ("failed", "stopped", None),
        ("failed", "deleting", None),
        ("broken", "creating", None),
        ("broken", "deleting", None),
        ("deleting", "removed", None),
    ],
)
def test_legal_edges_are_accepted(current: str, target: str, previous: str | None) -> None:
    assert transition(RealmState(current), as_target(target), _as_previous(previous)) == as_target(
        target
    )
    assert_transition(RealmState(current), as_target(target), _as_previous(previous))


@pytest.mark.parametrize(
    ("current", "target", "previous"),
    [
        ("updating", "stopped", "running"),
        ("updating", "running", "stopped"),
        ("updating", "stopped", None),
        ("updating", "failed", "running"),
        ("updating", "deleting", "running"),
        ("rebuilding", "stopped", "running"),
        ("rebuilding", "running", None),
        ("rebuilding", "broken", "running"),
        ("rebuilding", "deleting", "stopped"),
        ("creating", "running", None),
        ("creating", "deleting", None),
        ("stopped", "stopped", None),
        ("stopped", "failed", None),
        ("stopped", "broken", None),
        ("stopped", "removed", None),
        ("running", "running", None),
        ("running", "creating", None),
        ("failed", "failed", None),
        ("failed", "rebuilding", None),
        ("failed", "updating", None),
        ("broken", "running", None),
        ("broken", "stopped", None),
        ("deleting", "stopped", None),
        ("deleting", "deleting", None),
        ("deleting", "creating", None),
    ],
)
def test_illegal_edges_raise_illegal_transition(
    current: str, target: str, previous: str | None
) -> None:
    assert not can_transition(RealmState(current), as_target(target), _as_previous(previous))
    with pytest.raises(IllegalTransition):
        transition(RealmState(current), as_target(target), _as_previous(previous))
    with pytest.raises(IllegalTransition):
        assert_transition(RealmState(current), as_target(target), _as_previous(previous))


def test_illegal_transition_is_a_value_error_not_a_user_error() -> None:
    assert issubclass(IllegalTransition, ValueError)


def test_previous_state_message_names_the_missing_previous() -> None:
    with pytest.raises(IllegalTransition, match="no previous state"):
        transition(RealmState.UPDATING, RealmState.STOPPED, None)


def test_removed_is_not_a_realm_state() -> None:
    assert not isinstance(REMOVED, RealmState)
    assert {member.value for member in RealmState}.isdisjoint({"removed"})


@pytest.mark.fr("DR-020")
def test_the_give_up_edge_is_refused_without_give_up() -> None:
    assert GIVE_UP_EDGES == {(RealmState.STOPPED, RealmState.FAILED)}
    assert not can_transition(RealmState.STOPPED, RealmState.FAILED)
    assert not can_transition(RealmState.STOPPED, RealmState.FAILED, None, False)
    with pytest.raises(IllegalTransition, match="supervisor's give-up"):
        transition(RealmState.STOPPED, RealmState.FAILED)
    with pytest.raises(IllegalTransition):
        assert_transition(RealmState.STOPPED, RealmState.FAILED, None, give_up=False)


@pytest.mark.fr("DR-020")
def test_the_give_up_edge_is_legal_with_give_up() -> None:
    assert can_transition(RealmState.STOPPED, RealmState.FAILED, give_up=True)
    assert transition(RealmState.STOPPED, RealmState.FAILED, None, True) is RealmState.FAILED
    assert_transition(RealmState.STOPPED, RealmState.FAILED, give_up=True)
