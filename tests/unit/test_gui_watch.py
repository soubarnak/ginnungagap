# SPDX-License-Identifier: Apache-2.0
"""Refresh trigger: Gio.FileMonitor calls back when a watched record changes (DR-030, NFR-010).

The test pumps the default GLib main context itself, so no GTK window or application is needed.
It skips when PyGObject is not installed, because the core CLI does not depend on it (ADR-001).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from ginn.gui._gio import GioRefreshSource  # noqa: E402
from ginn.gui.watch import RefreshSource  # noqa: E402


def _pump_until(condition: Callable[[], bool], timeout: float) -> bool:
    """Run the default main context until `condition` holds or `timeout` seconds pass."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.01)
    return condition()


@pytest.mark.fr("NFR-010")
def test_a_new_record_fires_the_callback_through_the_main_loop(tmp_path: Path) -> None:
    calls: list[int] = []
    source: RefreshSource = GioRefreshSource([tmp_path])
    source.subscribe(lambda: calls.append(1))
    try:
        (tmp_path / "arch.json").write_text("{}\n", encoding="utf-8")
        fired = _pump_until(lambda: bool(calls), timeout=10.0)
    finally:
        source.close()

    assert fired, "no refresh after a record was written"


@pytest.mark.fr("DR-030")
def test_no_callback_arrives_after_close(tmp_path: Path) -> None:
    calls: list[int] = []
    source = GioRefreshSource([tmp_path])
    source.subscribe(lambda: calls.append(1))
    source.close()

    (tmp_path / "debian.json").write_text("{}\n", encoding="utf-8")
    _pump_until(lambda: False, timeout=0.3)

    assert calls == []


@pytest.mark.fr("DR-030")
def test_a_missing_directory_is_skipped_and_close_is_repeatable(tmp_path: Path) -> None:
    source = GioRefreshSource([tmp_path / "not-created-yet", tmp_path])
    source.subscribe(lambda: None)

    source.close()
    source.close()
