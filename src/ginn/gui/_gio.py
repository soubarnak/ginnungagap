# SPDX-License-Identifier: Apache-2.0
"""Gio.FileMonitor implementation of RefreshSource.

This is the only module in the gui package that imports gi. Nothing else in
the backend or the viewmodels depends on it. The main loop of the GTK
application delivers the callbacks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import gi  # type: ignore[import-untyped]

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # type: ignore[import-untyped]  # noqa: E402

from ginn.gui.refresh import RefreshSource  # noqa: E402


class GioRefreshSource(RefreshSource):
    """Calls back on any change in the watched directories, except attribute-only changes."""

    def __init__(self, directories: Sequence[Path]) -> None:
        self._directories = tuple(directories)
        self._monitors: list[Any] = []
        self._callback: Callable[[], None] | None = None

    def subscribe(self, callback: Callable[[], None]) -> None:
        """Start one directory monitor per existing directory. A missing one is skipped."""
        self._callback = callback
        for directory in self._directories:
            if not directory.is_dir():
                continue
            gfile = Gio.File.new_for_path(str(directory))
            monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            monitor.connect("changed", self._on_changed)
            self._monitors.append(monitor)

    def close(self) -> None:
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors.clear()
        self._callback = None

    def _on_changed(self, _monitor: Any, _gfile: Any, _other: Any, event: Any) -> None:
        if event == Gio.FileMonitorEvent.ATTRIBUTE_CHANGED or self._callback is None:
            return
        self._callback()
