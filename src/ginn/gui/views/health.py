# SPDX-License-Identifier: Apache-2.0
"""Host health: the doctor checks and the blocking integrity banner (FR-004..007, FR-104, IR-005).

A host file that changed since the baseline shows a banner that lists the paths. Acknowledge asks
first, then runs `ginn integrity --ack` through the progress dialog. The checks come from
`ginn doctor`, and each one shows the fix commands that the report gives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ginn.gui import actions
from ginn.gui.viewmodels.health import BANNER_REMEDY
from ginn.gui.viewmodels.progress import Phase, ProgressModel
from ginn.gui.views import common
from ginn.gui.views._gtk import Adw, Gtk
from ginn.gui.views.progress import present_progress

if TYPE_CHECKING:
    from ginn.gui.views.window import Session


class HealthPage:
    """The health page. It renders the health viewmodel, which the Control keeps up to date."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._banner = Adw.Banner(use_markup=False)
        self._banner.set_button_label("Acknowledge")
        self._banner.connect("button-clicked", lambda _banner: self._ask_acknowledge())
        self._banner.set_revealed(False)
        self._overall = common.label("Checking the host.", css=("heading",), wrap=True)
        self._failure = common.label("", css=("error",), wrap=True)
        self._checks = Adw.PreferencesGroup()
        self._checks.set_title("Checks")
        self._check_rows: list[Any] = []
        self.dialog: Any = None
        self.last_progress: ProgressModel | None = None

        page = Adw.PreferencesPage()
        overview = Adw.PreferencesGroup()
        overview.add(self._overall)
        overview.add(self._failure)
        page.add(overview)
        page.add(self._checks)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self._banner)
        body.append(page)
        header = Adw.HeaderBar()
        header.pack_start(
            common.icon_button("view-refresh-symbolic", "Run the host checks again", self.refresh)
        )
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(body)
        self.page: Any = Adw.NavigationPage.new(toolbar, "Host health")
        unsubscribe = session.control.subscribe(self._on_change)
        self.page.connect("hidden", lambda _page: unsubscribe())
        self.refresh()

    def refresh(self) -> None:
        self._session.control.refresh_health()

    def _on_change(self) -> None:
        health = self._session.control.health
        self._overall.set_label(f"Overall: {health.badge.value}")
        failure = self._session.control.health_failure
        self._failure.set_label("" if failure is None else common.error_text(failure))
        for old in self._check_rows:
            self._checks.remove(old)
        self._check_rows = []
        for check in health.checks:
            row = Adw.ActionRow(use_markup=False)
            row.set_title(check.name)
            detail = " ".join([check.message, *check.fixes])
            row.set_subtitle(detail)
            row.add_suffix(common.label(check.status, css=("dim-label",)))
            self._checks.add(row)
            self._check_rows.append(row)
        banner = health.banner
        if banner is None:
            self._banner.set_revealed(False)
        else:
            self._banner.set_title(f"{banner.title}: {', '.join(banner.paths)}")
            self._banner.set_revealed(True)

    def _ask_acknowledge(self) -> None:
        banner = self._session.control.health.banner
        paths = ", ".join(banner.paths) if banner is not None else "the listed paths"
        remedy = BANNER_REMEDY if banner is None else banner.remedy
        self.dialog = common.confirm(
            self._session.window,
            heading="Acknowledge the host change?",
            body=f"{paths}\n\n{remedy}",
            confirm_label="Acknowledge",
            destructive=False,
            on_confirm=lambda _checked: self._acknowledge(),
        )

    def _acknowledge(self) -> None:
        present_progress(
            self._session,
            title="Acknowledging the host change",
            calls=(actions.acknowledge_args(),),
            on_done=self._acknowledged,
        )

    def _acknowledged(self, progress: ProgressModel) -> None:
        self.last_progress = progress
        if progress.phase is Phase.DONE:
            self.refresh()
