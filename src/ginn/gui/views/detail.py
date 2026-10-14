# SPDX-License-Identifier: Apache-2.0
"""The realm detail: status, disk, exported apps and the actions of one realm (FR-102, IR-003).

The actions are start, stop, services on or off, autostart on or off, update, rebuild and delete.
Rebuild and delete ask first (FR-102). Delete offers "also delete home data" as a separate choice
that is unchecked by default. Each action runs through the progress dialog. The page reads its
status each time it is shown, so a permissions change or a finished operation shows at once. While
an unacknowledged host change blocks the page, its changing actions are off (FR-104 AC-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ginn.gui import actions
from ginn.gui.backend import CliResult, RealmView
from ginn.gui.viewmodels import detail as model
from ginn.gui.viewmodels.health import BLOCKED_TEXT
from ginn.gui.viewmodels.progress import Phase, ProgressModel
from ginn.gui.views import common
from ginn.gui.views._gtk import Adw, Gtk
from ginn.gui.views.progress import present_progress

if TYPE_CHECKING:
    from collections.abc import Callable

    from ginn.gui.views.window import Session


class DetailPage:
    """One realm's page. It holds the last status it read and renders it again on each read."""

    def __init__(self, session: Session, name: str) -> None:
        self._session = session
        self._name = name
        self._realm: RealmView | None = session.control.dashboard.realm(name)
        self._banner = Adw.Banner(use_markup=False)
        self._banner.set_revealed(False)
        self._status = Adw.PreferencesGroup()
        self._status.set_title("Status")
        self._status_rows: list[Any] = []
        self._actions = Adw.PreferencesGroup()
        self._actions.set_title("Actions")
        self._buttons: dict[str, Any] = {}
        self.dialog: Any = None
        self.last_progress: ProgressModel | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._build_actions()

        page = Adw.PreferencesPage()
        for group in (self._status, self._actions):
            page.add(group)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self._banner)
        body.append(page)
        header = Adw.HeaderBar()
        header.pack_start(
            common.icon_button("view-refresh-symbolic", "Reload this realm", self.reload)
        )
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(body)
        self.page: Any = Adw.NavigationPage.new(toolbar, name)
        # The status is read when the page is shown, which is also when a child page pops back.
        self.page.connect("shown", lambda _page: self._shown())
        self.page.connect("hidden", lambda _page: self._hidden())
        self._render()

    def _shown(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self._session.control.subscribe(self._on_change)
        self._set_sensitive(self._realm)
        self.reload()

    def _hidden(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _on_change(self) -> None:
        self._set_sensitive(self._realm)

    def _build_actions(self) -> None:
        self._add_action("start", "Start", lambda: self._run(actions.start_args(self._name)))
        self._add_action("stop", "Stop", lambda: self._run(actions.stop_args(self._name)))
        self._add_action(
            "services-on",
            "Services on",
            lambda: self._run(actions.services_args(self._name, True)),
        )
        self._add_action(
            "services-off",
            "Services off",
            lambda: self._run(actions.services_args(self._name, False)),
        )
        self._add_action(
            "autostart-on",
            "Autostart on",
            lambda: self._run(actions.autostart_args(self._name, True)),
        )
        self._add_action(
            "autostart-off",
            "Autostart off",
            lambda: self._run(actions.autostart_args(self._name, False)),
        )
        self._add_action("update", "Update", lambda: self._run(actions.update_args(self._name)))
        self._add_action("rebuild", "Rebuild", self._ask_rebuild)
        permissions = Adw.ActionRow(use_markup=False)
        permissions.set_title("Permissions")
        permissions.set_subtitle("Tier, toggles and shared paths")
        permissions.set_activatable(True)
        permissions.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        permissions.connect("activated", lambda _row: self._session.open_permissions(self._name))
        self._actions.add(permissions)
        self._add_action("delete", "Delete realm…", self._ask_delete, destructive=True)

    def _add_action(self, key: str, text: str, on_click: Any, *, destructive: bool = False) -> None:
        row = Adw.ActionRow(use_markup=False)
        row.set_title(text)
        button = common.text_button(
            text, on_click, css=("destructive-action",) if destructive else ()
        )
        common.set_accessible_name(button, f"{text} realm {self._name}")
        row.add_suffix(button)
        row.set_activatable_widget(button)
        self._actions.add(row)
        self._buttons[key] = button

    def reload(self) -> None:
        self._session.control.read_realm(self._name, self._read_done, self._read_failed)

    def _read_done(self, result: CliResult, view: RealmView | None) -> None:
        if view is None:
            self._show_failure(common.failure_text(result) or "The realm could not be read.")
            return
        self._realm = view
        self._banner.set_revealed(False)
        self._render()

    def _read_failed(self, exc: BaseException) -> None:
        self._show_failure(str(exc))

    def _show_failure(self, text: str) -> None:
        self._banner.set_title(text)
        self._banner.set_revealed(True)

    def _render(self) -> None:
        for row in self._status_rows:
            self._status.remove(row)
        self._status_rows = []
        realm = self._realm
        for label, value in () if realm is None else model.detail_rows(realm):
            row = Adw.ActionRow(use_markup=False)
            row.set_title(label)
            row.set_subtitle(value)
            self._status.add(row)
            self._status_rows.append(row)
        self._set_sensitive(realm)

    def _set_sensitive(self, realm: RealmView | None) -> None:
        blocked = self._session.control.host_changed
        states = {
            "start": realm is not None and not blocked and model.can_start(realm),
            "stop": realm is not None and not blocked and model.can_stop(realm),
            "services-on": realm is not None and not blocked and model.can_services_on(realm),
            "services-off": realm is not None and not blocked and model.can_services_off(realm),
            "autostart-on": realm is not None and not blocked,
            "autostart-off": realm is not None and not blocked,
            "update": realm is not None and not blocked and model.can_update(realm),
            "rebuild": realm is not None
            and not blocked
            and model.REBUILD_AVAILABLE
            and model.can_rebuild(realm),
            "delete": realm is not None and not blocked,
        }
        for key, enabled in states.items():
            button = self._buttons[key]
            button.set_sensitive(enabled)
            button.set_tooltip_text(BLOCKED_TEXT if blocked else "")

    def _run(self, *calls: tuple[str, ...]) -> None:
        present_progress(
            self._session,
            title=f"{self._name}",
            calls=calls,
            on_done=self._finished,
        )

    def _finished(self, progress: ProgressModel) -> None:
        # A run that failed or was cancelled may still have changed the realm, so it is read again.
        self.last_progress = progress
        self.reload()
        self._session.control.refresh_dashboard()

    def _ask_rebuild(self) -> None:
        confirmation = model.rebuild_confirmation(self._name)
        self.dialog = common.confirm(
            self._session.window,
            heading=confirmation.heading,
            body=confirmation.body,
            confirm_label=confirmation.confirm_label,
            destructive=confirmation.destructive,
            on_confirm=lambda _checked: self._run(actions.rebuild_args(self._name)),
        )

    def _ask_delete(self) -> None:
        confirmation = model.delete_confirmation(self._name)
        self.dialog = common.confirm(
            self._session.window,
            heading=confirmation.heading,
            body=confirmation.body,
            confirm_label=confirmation.confirm_label,
            destructive=confirmation.destructive,
            check_label=confirmation.check_label,
            on_confirm=self._delete_confirmed,
        )

    def _delete_confirmed(self, purge_home: bool) -> None:
        present_progress(
            self._session,
            title=f"Deleting {self._name}",
            calls=(actions.delete_args(self._name, purge_home=purge_home),),
            on_done=self._deleted,
        )

    def _deleted(self, progress: ProgressModel) -> None:
        self.last_progress = progress
        if progress.phase is Phase.DONE:
            self._session.pop()
        else:
            self.reload()
        self._session.control.refresh_dashboard()
