# SPDX-License-Identifier: Apache-2.0
"""Undo steps for a mutating operation that fails part way (FR-120, FR-121, DR-018, AS-018).

An operation adds one undo callable per artifact it makes, in the order it makes them. `run()`
calls them newest first, so a container goes before the home it was built in, and an image goes
before the record that names it. Each undo is best effort: a failure is returned as a note and the
next undo still runs, so one stuck artifact never hides the others or the error that started the
rollback. The caller decides what a note means for the realm record (DR-020).

This module makes no podman call and touches no file itself. The callables it runs do.
"""

from collections.abc import Callable


class Rollback:
    """A stack of undo callables, run newest first, each at most once."""

    def __init__(self) -> None:
        self._steps: list[tuple[str, Callable[[], None]]] = []

    def add(self, label: str, undo: Callable[[], None]) -> None:
        """Register `undo` for an artifact that exists now. `label` names it in notes."""
        self._steps.append((label, undo))

    def run(self) -> list[str]:
        """Run every registered undo, newest first. Return one note per undo that failed."""
        notes: list[str] = []
        while self._steps:
            label, undo = self._steps.pop()
            try:
                undo()
            except Exception as exc:  # noqa: BLE001 - a failed undo must not hide the others
                notes.append(f"{label}: {type(exc).__name__}: {exc}")
        return notes


__all__ = ["Rollback"]
