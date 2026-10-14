# SPDX-License-Identifier: Apache-2.0
"""The status window that stands in for the Control Plane when it cannot start (FR-105).

It is shown when the Owned Paths are missing, which means Host Setup has not run for this user
(GINN-E-HOST-NOT-SET-UP). It names the remedy and reads no state. The text is in the viewmodel.
"""

from __future__ import annotations

from typing import Any

from ginn.gui.viewmodels.labels import Failure
from ginn.gui.viewmodels.setup import status_text
from ginn.gui.views._gtk import Adw, GLib


def status_window(app: Any, failure: Failure) -> Any:
    """A window with one status page for `failure`. It has no Control and no watch."""
    text = status_text(failure)
    page = Adw.StatusPage()
    page.set_icon_name("dialog-warning-symbolic")
    page.set_title(text.title)
    # The description label parses markup, and the remedy names placeholders such as <name>.
    page.set_description(GLib.markup_escape_text(text.description))
    header = Adw.HeaderBar()
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(header)
    toolbar.set_content(page)
    window = Adw.ApplicationWindow(application=app, title="Ginnungagap")
    window.set_default_size(560, 360)
    window.set_content(toolbar)
    return window
