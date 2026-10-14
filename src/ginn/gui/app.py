# SPDX-License-Identifier: Apache-2.0
"""The `ginn-gui` entry point: the Control Plane application (FR-100..107, ADR-001).

`main` starts an Adw.Application with the ID `org.bongbetic.ginn`. The window opens the dashboard,
which runs `ginn --json` as the user (FR-105). When the Owned Paths are missing, the error is
GINN-E-HOST-NOT-SET-UP, which means Host Setup has not run. Then the window shows a status page, and
no watch is set up.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any, Final

from ginn.errors import GinnError
from ginn.gui._gio import GioRefreshSource
from ginn.gui.control import Control
from ginn.gui.viewmodels.labels import failure_of_error
from ginn.gui.views._gtk import Adw, Gio, run_on_main
from ginn.gui.views.setup import status_window
from ginn.gui.views.window import Session
from ginn.store.paths import OwnedPaths

APP_ID: Final = "org.bongbetic.ginn"
"""The application ID (ADR-001). The desktop entry of T9.9 uses the same name."""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Control Plane and return its exit status."""
    app: Any = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    app.connect("activate", _activate)
    args = list(sys.argv if argv is None else argv)
    return int(app.run(args))


def _activate(app: Any) -> None:
    window = app.get_active_window()
    if window is None:
        window = _build(app)
    window.present()


def _build(app: Any) -> Any:
    try:
        paths = OwnedPaths.from_env()
    except GinnError as err:
        return status_window(app, failure_of_error(err))
    control = Control(paths=paths, dispatch=run_on_main, make_source=GioRefreshSource)
    session = Session(control)
    window = session.build(app)
    control.start_watching()
    return window
