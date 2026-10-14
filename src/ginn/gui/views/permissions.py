# SPDX-License-Identifier: Apache-2.0
"""The permissions editor: tier, the Custom toggles and the shared paths (FR-103, IR-004).

The editor viewmodel checks each shared path against DR-004 as it is added. A refused path shows
its GINN-E-PATH-FORBIDDEN error inline and is not saved (FR-103 AC-1). Toggles and shares are
editable only in the Custom tier. Save runs the commands that the editor plans, in order, through
the progress dialog. The page reads the realm when it is shown, and a read never replaces edits
that are not saved yet. A finished save discards the pending edits and shows what is on disk. While
an unacknowledged host change blocks the page, every control that changes the realm is off.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ginn.domain import Tier
from ginn.domain.paths import Mode
from ginn.domain.permissions import Toggle
from ginn.gui.backend import CliResult, PermissionsView
from ginn.gui.viewmodels.permissions import PermissionsEditor
from ginn.gui.viewmodels.progress import ProgressModel
from ginn.gui.views import common
from ginn.gui.views._gtk import Adw, Gtk
from ginn.gui.views.progress import present_progress

if TYPE_CHECKING:
    from collections.abc import Callable

    from ginn.gui.views.window import Session

TIER_NAMES: tuple[str, ...] = tuple(tier.value for tier in Tier)
MODE_NAMES: tuple[str, ...] = ("ro", "rw")


class PermissionsPage:
    """The page of one realm's tier and permissions. It is built from a `tier` read."""

    def __init__(self, session: Session, name: str) -> None:
        self._session = session
        self._name = name
        self._editor: PermissionsEditor | None = None
        self._syncing = False
        self._tier = Adw.ComboRow()
        self._tier.set_title("Permission tier")
        self._tier.set_model(Gtk.StringList.new(list(TIER_NAMES)))
        self._tier.connect("notify::selected", lambda *_args: self._on_tier())
        self._toggle_rows: dict[Toggle, Any] = {}
        self._toggles = Adw.PreferencesGroup()
        self._toggles.set_title("Custom toggles")
        self._shares = Adw.PreferencesGroup()
        self._shares.set_title("Shared paths")
        self._share_rows: list[Any] = []
        self._new_path = Adw.EntryRow()
        self._new_path.set_title("Path to share")
        self._new_mode = Adw.ComboRow()
        self._new_mode.set_title("Access")
        self._new_mode.set_model(Gtk.StringList.new(list(MODE_NAMES)))
        self._error = common.label("", css=("error",), wrap=True)
        self._status = common.label("Reading the permissions.", css=("dim-label",), wrap=True)
        self._save = common.text_button("Save", self._save_clicked, css=("suggested-action",))
        self._save.set_sensitive(False)
        self.last_progress: ProgressModel | None = None
        self._unsubscribe: Callable[[], None] | None = None

        header = Adw.HeaderBar()
        header.pack_end(self._save)
        tier_group = Adw.PreferencesGroup()
        tier_group.add(self._tier)
        tier_group.add(self._status)
        self._add_group = Adw.PreferencesGroup()
        self._add_group.set_title("Add a shared path")
        self._add_group.add(self._new_path)
        self._add_group.add(self._new_mode)
        self._add_group.add(self._error)
        self._add_button = common.text_button("Add path", self._add_share)
        self._add_group.add(self._add_button)

        page = Adw.PreferencesPage()
        for group in (tier_group, self._toggles, self._shares, self._add_group):
            page.add(group)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        self.page: Any = Adw.NavigationPage.new(toolbar, f"Permissions of {name}")
        # The page is read once each time it is shown. It is not read at construction as well.
        self.page.connect("shown", lambda _page: self._shown())
        self.page.connect("hidden", lambda _page: self._hidden())
        self._render()

    def _shown(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self._session.control.subscribe(self._render)
        self._render()
        self.reload()

    def _hidden(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def reload(self) -> None:
        self._session.control.read_permissions(self._name, self._read_done, self._read_failed)

    def _read_done(self, result: CliResult, view: PermissionsView | None) -> None:
        if view is None:
            self._error.set_label(
                common.failure_text(result) or "The permissions could not be read."
            )
            return
        self._error.set_label("")
        if self._editor is not None and self._editor.dirty:
            # FR-103: a read never replaces the edits that are not saved yet.
            return
        self._editor = PermissionsEditor(
            view,
            home=Path.home(),
            owned_paths=self._owned_roots(),
        )
        self._status.set_label(
            "Toggles and shared paths are edited in the Custom tier only."
            if view.tier != Tier.CUSTOM.value
            else "Custom tier: edit the toggles and shared paths below."
        )
        self._render()

    def _read_failed(self, exc: BaseException) -> None:
        self._error.set_label(str(exc))

    def _owned_roots(self) -> list[Path]:
        paths = self._session.control.paths
        return [paths.data, paths.config, paths.state, paths.runtime, paths.bin]

    def _render(self) -> None:
        editor = self._editor
        # While an unacknowledged host change blocks the page, nothing here can change the realm.
        live = not self._session.control.host_changed
        self._tier.set_sensitive(editor is not None and live)
        self._add_group.set_sensitive(editor is not None and live)
        if editor is None:
            self._save.set_sensitive(False)
            return
        self._syncing = True
        try:
            self._tier.set_selected(TIER_NAMES.index(editor.tier.value))
            self._sync_toggles(editor, live)
            self._sync_shares(editor, live)
        finally:
            self._syncing = False
        self._save.set_sensitive(live and bool(editor.commands()))

    def _sync_toggles(self, editor: PermissionsEditor, live: bool) -> None:
        on = editor.toggles
        if not self._toggle_rows:
            for toggle in Toggle:
                row = Adw.SwitchRow()
                row.set_title(toggle.value.replace("_", " "))
                row.connect(
                    "notify::active",
                    lambda widget, _pspec, item=toggle: self._on_toggle(item, widget),
                )
                self._toggles.add(row)
                self._toggle_rows[toggle] = row
        for toggle, row in self._toggle_rows.items():
            row.set_active(toggle in on)
            row.set_sensitive(editor.editable and live)

    def _sync_shares(self, editor: PermissionsEditor, live: bool) -> None:
        for row in self._share_rows:
            self._shares.remove(row)
        self._share_rows = []
        for share in editor.shares:
            row = Adw.ActionRow(use_markup=False)
            row.set_title(share.path)
            row.set_subtitle("read-write" if share.mode == "rw" else "read-only")
            row.add_suffix(
                common.icon_button(
                    "user-trash-symbolic",
                    f"Stop sharing {share.path}",
                    partial(self._remove_share, share.path),
                )
            )
            row.set_sensitive(editor.editable and live)
            self._shares.add(row)
            self._share_rows.append(row)

    def _on_tier(self) -> None:
        if self._syncing or self._editor is None:
            return
        self._editor.set_tier(Tier(TIER_NAMES[self._tier.get_selected()]))
        self._render()

    def _on_toggle(self, toggle: Toggle, widget: Any) -> None:
        if self._syncing or self._editor is None or not self._editor.editable:
            return
        self._editor.set_toggle(toggle, bool(widget.get_active()))
        self._render()

    def _add_share(self) -> None:
        editor = self._editor
        if editor is None:
            return
        raw = self._new_path.get_text().strip()
        if not raw:
            return
        mode: Mode = "rw" if self._new_mode.get_selected() == MODE_NAMES.index("rw") else "ro"
        failure = editor.add_share(raw, mode)
        if failure is None:
            self._new_path.set_text("")
            self._error.set_label("")
        else:
            self._error.set_label(common.error_text(failure))
        self._render()

    def _remove_share(self, path: str) -> None:
        if self._editor is not None:
            self._editor.remove_share(path)
            self._render()

    def _save_clicked(self) -> None:
        editor = self._editor
        if editor is None or self._session.control.host_changed:
            return
        calls = editor.commands()
        if not calls:
            return
        present_progress(
            self._session,
            title=f"Saving permissions of {self._name}",
            calls=calls,
            on_done=self._saved,
        )

    def _saved(self, progress: ProgressModel) -> None:
        # Whatever the outcome, the page shows the realm as it is on disk now. The pending edits are
        # dropped, so the next read builds a new editor and no change is sent twice.
        self.last_progress = progress
        self._editor = None
        self._render()
        self.reload()
        self._session.control.refresh_dashboard()
