# SPDX-License-Identifier: Apache-2.0
"""The dashboard: every realm, with its state, tier, services, disk and badge (FR-100, IR-001).

The rows come from the dashboard viewmodel. A row opens the realm detail. The list refreshes when
the Control reports a change on disk, and the refresh button asks for one. A failed read shows a
banner and keeps the rows of the last good read. While a host change is unacknowledged, a banner
lists the changed paths and Create is off (FR-104 AC-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ginn.gui.viewmodels.dashboard import DashboardViewModel, RealmRow
from ginn.gui.viewmodels.health import BLOCKED_TEXT
from ginn.gui.views import common
from ginn.gui.views._gtk import Adw, Gtk

if TYPE_CHECKING:
    from ginn.gui.views.window import Session

EMPTY_ICON = "view-grid-symbolic"


class DashboardPage:
    """The root page of the window. It owns no state: it renders the dashboard viewmodel."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._list = Gtk.ListBox()
        self._list.add_css_class("boxed-list")
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        common.set_accessible_name(self._list, "Realms")
        self._banner = Adw.Banner(use_markup=False)
        self._banner.set_revealed(False)
        self._host_banner = Adw.Banner(use_markup=False)
        self._host_banner.set_button_label("Review")
        self._host_banner.connect("button-clicked", lambda _banner: session.open_health())
        self._host_banner.set_revealed(False)
        self._empty = Adw.StatusPage()
        self._empty.set_icon_name(EMPTY_ICON)
        self._stack = Gtk.Stack()
        clamp = Adw.Clamp()
        clamp.set_maximum_size(720)
        clamp.set_child(self._list)
        self._stack.add_named(common.scrolled(clamp), "list")
        self._stack.add_named(self._empty, "empty")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self._host_banner)
        body.append(self._banner)
        body.append(self._stack)
        header = Adw.HeaderBar()
        header.pack_start(
            common.icon_button("view-refresh-symbolic", "Refresh the realm list", self.refresh)
        )
        header.pack_end(
            common.icon_button("emblem-system-symbolic", "Host health", self._session.open_health)
        )
        self._create = common.text_button(
            "Create realm", self._session.open_create, css=("suggested-action",)
        )
        header.pack_end(self._create)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(body)
        self.page: Any = Adw.NavigationPage.new(toolbar, "Realms")
        self.page.set_tag("dashboard")

        session.control.subscribe(self._on_change)
        self._on_change()

    def refresh(self) -> None:
        self._session.control.refresh_dashboard()

    def row_titles(self) -> tuple[str, ...]:
        """The realm names in the list, in order. The tests read them."""
        titles: list[str] = []
        child = self._list.get_first_child()
        while child is not None:
            titles.append(child.get_title())
            child = child.get_next_sibling()
        return tuple(titles)

    def _on_change(self) -> None:
        dashboard = self._session.control.dashboard
        common.clear_box(self._list)
        for row in dashboard.rows:
            self._list.append(_realm_row(row, self._session))
        if dashboard.rows:
            self._stack.set_visible_child_name("list")
        else:
            self._stack.set_visible_child_name("empty")
            title, description = _empty_text(dashboard)
            self._empty.set_title(title)
            self._empty.set_description(description)
        failure = dashboard.failure
        if failure is None:
            self._banner.set_revealed(False)
        else:
            self._banner.set_title(common.error_text(failure))
            self._banner.set_revealed(True)
        self._on_host_change()

    def _on_host_change(self) -> None:
        """The host banner lists the changed paths, and Create waits until they are acknowledged."""
        control = self._session.control
        banner = control.health.banner
        if banner is None:
            self._host_banner.set_revealed(False)
        else:
            self._host_banner.set_title(f"{banner.title}: {', '.join(banner.paths)}")
            self._host_banner.set_revealed(True)
        blocked = control.host_changed
        self._create.set_sensitive(not blocked)
        self._create.set_tooltip_text(BLOCKED_TEXT if blocked else "")


def _empty_text(dashboard: DashboardViewModel) -> tuple[str, str]:
    """The title and description of the empty page: no realms, still loading, or a failed read."""
    if dashboard.loaded:
        return "No realms yet", "Create a realm to run a distribution beside the host."
    if dashboard.failure is None:
        return "Loading realms", "Reading the realm list."
    return "Realms unavailable", "The banner above says why the realm list could not be read."


def _realm_row(row: RealmRow, session: Session) -> Any:
    notes = f" · {'; '.join(row.notes)}" if row.notes else ""
    action = Adw.ActionRow(use_markup=False)
    action.set_title(row.name)
    action.set_subtitle(
        f"{row.distro} · {row.state} · tier {row.tier} · services {row.services} · "
        f"disk {row.disk} · exported apps {row.exported_apps}{notes}"
    )
    action.add_suffix(common.label(row.badge.value, css=("dim-label",)))
    action.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
    action.set_activatable(True)
    action.connect("activated", lambda _row: session.open_detail(row.name))
    common.set_accessible_name(action, f"Realm {row.name}, {row.state}")
    return action
