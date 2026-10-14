# SPDX-License-Identifier: Apache-2.0
"""Change source interface for the Control Plane.

A RefreshSource calls back when realm records or the Audit Log change on disk,
so the Control Plane re-reads state through the backend (DR-030). The interface
is small so that tests drive it with a fake and never start a main loop.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class RefreshSource(Protocol):
    """Watches on-disk state and calls back on each change."""

    def subscribe(self, callback: Callable[[], None]) -> None:
        """Register the callback. It runs on the thread that owns the event loop."""

    def close(self) -> None:
        """Stop watching. Calling it more than once is safe."""
