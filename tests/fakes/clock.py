# SPDX-License-Identifier: Apache-2.0
"""Settable fake clock for store tests (audit timestamps, journal start times)."""

from __future__ import annotations


class FakeClock:
    """Callable clock returning epoch seconds; tests set or advance it explicitly."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = float(start)

    def __call__(self) -> float:
        return self._now

    def set(self, epoch_seconds: float) -> None:
        self._now = float(epoch_seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        self._now += float(seconds)
