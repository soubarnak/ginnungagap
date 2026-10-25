# SPDX-License-Identifier: Apache-2.0
"""The one module of the views that imports gi (ADR-001, NFR-061).

`gi.require_version` runs here, before any `gi.repository` import. The GTK, Adwaita, Gio and GLib
names are typed `Any`, because PyGObject ships no stubs for them. A view calls only the widget
methods it needs. Every view imports these names from here, never from gi itself.
`tests/unit/test_gui_backend.py` checks that gi is imported by this module and `_gio.py` only.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

import gi  # type: ignore[import-untyped]

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")


def _namespace(name: str) -> Any:
    """A gi.repository namespace, loaded by its full name. PyGObject ships no stubs, and strict
    mypy would need an ignore on every `from gi.repository import` line."""
    return import_module(f"gi.repository.{name}")


Adw: Any = _namespace("Adw")
Gio: Any = _namespace("Gio")
GLib: Any = _namespace("GLib")
Gtk: Any = _namespace("Gtk")

SOURCE_CONTINUE: bool = True
SOURCE_REMOVE: bool = False


def _run_once(fn: Callable[[], None]) -> bool:
    fn()
    return SOURCE_REMOVE


def run_on_main(fn: Callable[[], None]) -> None:
    """Run `fn` on the GLib main loop of the default context, as soon as the loop is idle.

    This is the `dispatch` of the Control Plane. Worker threads call it, and nothing else in the
    views runs off the main loop.
    """
    GLib.idle_add(_run_once, fn)
