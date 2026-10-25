# SPDX-License-Identifier: Apache-2.0
"""The window and its navigation. A `Session` opens each page, and each page talks to the Control.

The window is an Adw.ApplicationWindow with one Adw.NavigationView. The dashboard is its root page.
The create wizard, the realm detail, the permissions editor and the host health push on top of it.
The progress dialog is an Adw.Dialog over whichever page started the operation.
"""

from __future__ import annotations

from typing import Any

from ginn.distros import available
from ginn.gui.control import Control
from ginn.gui.jobs import Operation
from ginn.gui.views import common
from ginn.gui.views._gtk import Adw
from ginn.gui.views.dashboard import DashboardPage
from ginn.gui.views.detail import DetailPage
from ginn.gui.views.health import HealthPage
from ginn.gui.views.permissions import PermissionsPage
from ginn.gui.views.wizard import WizardPage

WINDOW_TITLE = "Ginnungagap"
CLOSE_BLOCKED_HEADING = "An operation is still running"
CLOSE_BLOCKED_BODY = (
    "Wait for it to end, or cancel it in its progress dialog. Then close the window again."
)


class Session:
    """The window of one Control Plane session. Pages ask it to open other pages."""

    def __init__(self, control: Control) -> None:
        self.control = control
        self.window: Any = None
        self.dashboard: DashboardPage | None = None
        self.notice: Any = None
        self._navigation: Any = None
        self._running: set[Operation] = set()

    @property
    def busy(self) -> bool:
        """True while an operation runs. The window does not close then (FR-107)."""
        return bool(self._running)

    def began(self, operation: Operation) -> None:
        self._running.add(operation)

    def ended(self, operation: Operation) -> None:
        self._running.discard(operation)

    def build(self, app: Any | None = None) -> Any:
        """Build the window with the dashboard as its root page. Nothing is read yet."""
        self._navigation = Adw.NavigationView()
        if app is None:
            self.window = Adw.ApplicationWindow(title=WINDOW_TITLE)
        else:
            self.window = Adw.ApplicationWindow(application=app, title=WINDOW_TITLE)
        self.window.set_default_size(960, 680)
        self.window.set_content(self._navigation)
        self.dashboard = DashboardPage(self)
        self._navigation.add(self.dashboard.page)
        self.window.connect("close-request", self._on_close)
        return self.window

    def open_create(self, distros: tuple[str, ...] | None = None) -> WizardPage:
        """The create wizard. `distros` defaults to the distributions that have a data file."""
        page = WizardPage(self, available() if distros is None else distros)
        self._push(page.page)
        return page

    def open_detail(self, name: str) -> DetailPage:
        page = DetailPage(self, name)
        self._push(page.page)
        return page

    def open_permissions(self, name: str) -> PermissionsPage:
        page = PermissionsPage(self, name)
        self._push(page.page)
        return page

    def open_health(self) -> HealthPage:
        page = HealthPage(self)
        self._push(page.page)
        return page

    def pop(self) -> None:
        self._navigation.pop()

    def _push(self, page: Any) -> None:
        self._navigation.push(page)

    def _on_close(self, _window: Any) -> bool:
        """Refuse the close while an operation runs, and say why. Otherwise stop the control.

        libadwaita answers a close while a dialog is up before this handler runs, so the progress
        dialog also says that the window stays open until the run ends.
        """
        if self.busy:
            self.notice = common.inform(
                self.window, heading=CLOSE_BLOCKED_HEADING, body=CLOSE_BLOCKED_BODY
            )
            return True
        self.control.close()
        return False
