# SPDX-License-Identifier: Apache-2.0
"""Progress dialog viewmodel: the lines of a long operation, its phase and its outcome (FR-107).

The dialog shows each progress line as it arrives. Cancel moves the phase to `cancelling`, and the
operation ends as `cancelled` once the interrupted command has returned. A cancel does not roll
anything back here: the CLI does that (FR-120). Pure Python, no GTK.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ginn.gui.backend import CliResult
from ginn.gui.viewmodels.labels import Failure, failure_of


class Phase(StrEnum):
    RUNNING = "running"
    CANCELLING = "cancelling"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ProgressModel:
    """The state of one progress dialog. The view reads it and never changes it directly."""

    title: str
    phase: Phase = Phase.RUNNING
    failure: Failure | None = None
    results: tuple[CliResult, ...] = ()
    """The results of the run's commands, in order, once it has finished."""
    _lines: list[str] = field(default_factory=list)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self._lines)

    @property
    def current(self) -> str:
        """The newest line, or the phase when no step has finished yet."""
        return self._lines[-1] if self._lines else self.phase_text

    @property
    def can_cancel(self) -> bool:
        return self.phase is Phase.RUNNING

    @property
    def finished(self) -> bool:
        return self.phase in (Phase.DONE, Phase.FAILED, Phase.CANCELLED)

    @property
    def phase_text(self) -> str:
        return {
            Phase.RUNNING: "Working",
            Phase.CANCELLING: "Cancelling",
            Phase.DONE: "Done",
            Phase.FAILED: "Failed",
            Phase.CANCELLED: "Cancelled",
        }[self.phase]

    @property
    def summary(self) -> str:
        """One line for the end of the run: the failure's code and message when it failed."""
        if self.phase is Phase.FAILED and self.failure is not None:
            code = f"{self.failure.code}: " if self.failure.code else ""
            return f"{code}{self.failure.message}"
        return self.phase_text

    def add_line(self, text: str) -> None:
        if not self.finished:
            self._lines.append(text)

    def request_cancel(self) -> None:
        if self.can_cancel:
            self.phase = Phase.CANCELLING

    def finish(
        self, results: Sequence[CliResult], error: BaseException | None, cancelled: bool = False
    ) -> None:
        """Set the outcome. A run that stopped on an exception failed, whatever its results.

        `cancelled` is True when the run was stopped between two of its commands. A cancel that
        interrupted a command shows in that command's result.
        """
        self.results = tuple(results)
        if error is not None:
            self.phase = Phase.FAILED
            self.failure = Failure(
                code=None, message=str(error) or type(error).__name__, remedy=None
            )
            return
        last = results[-1] if results else None
        interrupted = any(result.cancelled for result in results)
        stopped_by_user = self.phase is Phase.CANCELLING and (last is None or not last.ok)
        if cancelled or interrupted or stopped_by_user:
            self.phase = Phase.CANCELLED
            return
        failure = None if last is None else failure_of(last)
        if failure is not None:
            self.phase = Phase.FAILED
            self.failure = failure
            return
        self.phase = Phase.DONE
