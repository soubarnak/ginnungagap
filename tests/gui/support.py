# SPDX-License-Identifier: Apache-2.0
"""Helpers of the view smoke tests: a refresh source that never fires, and a main-loop pump.

The views hand their work to a worker thread and get the result back through GLib.idle_add. A test
therefore pumps the default main context until the state it waits for has been reached.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ginn.gui.watch import RefreshSource


class SilentSource(RefreshSource):
    """A refresh source that records its directories and never calls back."""

    def __init__(self, directories: tuple[Path, ...]) -> None:
        self.directories = directories
        self.subscribed = False
        self.closed = False

    def subscribe(self, callback: Callable[[], None]) -> None:
        self.subscribed = True

    def close(self) -> None:
        self.closed = True


def pump_until(gtk: Any, condition: Callable[[], bool], timeout: float = 15.0) -> None:
    """Run the default main context until `condition` holds, or fail after `timeout` seconds."""
    context = gtk.GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not condition():
        context.iteration(False)
        if time.monotonic() > deadline:
            raise AssertionError("the main loop did not reach the expected state in time")
        time.sleep(0.01)


MAX_DRAIN_EVENTS = 10_000


def drain(gtk: Any) -> None:
    """Run every event that is already pending on the default main context, up to a bound."""
    context = gtk.GLib.MainContext.default()
    for _ in range(MAX_DRAIN_EVENTS):
        if not context.pending():
            return
        context.iteration(False)
    raise AssertionError("events kept arriving on the main context")
