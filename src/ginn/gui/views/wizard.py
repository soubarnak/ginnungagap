# SPDX-License-Identifier: Apache-2.0
"""The create-realm wizard: distribution, name, tier, services, packages and Kali "everything"
(FR-101, IR-002).

The wizard viewmodel checks every input and builds the argv. This view renders its state and
forwards each change to it. The name is checked as it is typed (FR-101 AC-1), and Create stays off
until the wizard can create. The Kali "everything" option is offered for Kali only (FR-101 AC-2),
and it asks before the run, because the install is large. The run uses the progress dialog.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from ginn.domain import Tier
from ginn.gui.viewmodels.health import BLOCKED_TEXT
from ginn.gui.viewmodels.progress import Phase, ProgressModel
from ginn.gui.viewmodels.wizard import CreateWizard
from ginn.gui.views import common
from ginn.gui.views._gtk import Adw, Gtk
from ginn.gui.views.progress import present_progress

if TYPE_CHECKING:
    from collections.abc import Callable

    from ginn.gui.views.window import Session

TIER_NAMES: tuple[str, ...] = tuple(tier.value for tier in Tier)
MODE_NAMES: tuple[str, ...] = ("ro", "rw")
EVERYTHING_BODY = (
    "The Kali everything set is large. ginn checks the free disk space before it installs, "
    "and the progress shows each step. Continue?"
)


class WizardPage:
    """A navigation page that builds one realm, one field at a time."""

    def __init__(self, session: Session, distros: tuple[str, ...]) -> None:
        self._session = session
        self._wizard = CreateWizard(distros)
        self._syncing = False
        self.dialog: Any = None
        self.last_progress: ProgressModel | None = None

        self._distro = Adw.ComboRow()
        self._distro.set_title("Distribution")
        self._distro.set_model(Gtk.StringList.new(list(distros)))
        self._distro.connect("notify::selected", lambda *_args: self._on_distro())

        self._name = Adw.EntryRow()
        self._name.set_title("Name")
        self._name.connect("changed", lambda *_args: self._on_name())
        self._name_error = common.label("", css=("error",), wrap=True)

        self._tier = Adw.ComboRow()
        self._tier.set_title("Permission tier")
        self._tier.set_model(Gtk.StringList.new(list(TIER_NAMES)))
        self._tier.set_selected(TIER_NAMES.index(Tier.BASIC.value))
        self._tier.connect("notify::selected", lambda *_args: self._on_tier())

        self._services = Adw.SwitchRow()
        self._services.set_title("Services")
        self._services.set_subtitle("Start the realm's services with the session (FR-080)")
        self._services.connect("notify::active", lambda *_args: self._on_services())
        self._services_error = common.label("", css=("error",), wrap=True)

        self._everything = Adw.SwitchRow()
        self._everything.set_title("Everything (Kali only)")
        self._everything.set_subtitle("Install the full Kali set. It asks before it runs.")
        self._everything.connect("notify::active", lambda *_args: self._on_everything())

        self._package = Adw.EntryRow()
        self._package.set_title("Add a package")
        self._package.add_suffix(common.text_button("Add", self._add_package))
        self._package_error = common.label("", css=("error",), wrap=True)
        self._packages = Adw.PreferencesGroup()
        self._packages.set_title("Packages")
        self._package_rows: list[Any] = []

        self._create = common.text_button("Create", self._create_clicked, css=("suggested-action",))
        header = Adw.HeaderBar()
        header.pack_end(self._create)

        identity = Adw.PreferencesGroup()
        identity.set_title("Realm")
        identity.add(self._distro)
        identity.add(self._name)
        identity.add(self._name_error)

        options = Adw.PreferencesGroup()
        options.set_title("Options")
        options.add(self._tier)
        options.add(self._services)
        options.add(self._services_error)
        options.add(self._everything)

        packages = Adw.PreferencesGroup()
        packages.set_title("Extra packages")
        packages.add(self._package)
        packages.add(self._package_error)

        page = Adw.PreferencesPage()
        for group in (identity, options, packages, self._packages):
            page.add(group)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        self.page: Any = Adw.NavigationPage.new(toolbar, "Create realm")
        self._unsubscribe: Callable[[], None] | None = None
        self.page.connect("shown", lambda _page: self._shown())
        self.page.connect("hidden", lambda _page: self._hidden())
        self._on_distro_default()
        self._render()

    def _shown(self) -> None:
        """Follow the host change while the page is shown, so Create is off while it blocks."""
        if self._unsubscribe is None:
            self._unsubscribe = self._session.control.subscribe(self._render)
        self._render()

    def _hidden(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _on_distro_default(self) -> None:
        """Choose the first distribution offered, so the name and the packages have a base."""
        if not self._wizard.distros:
            return
        self._syncing = True
        try:
            self._wizard.choose_distro(self._wizard.distros[0])
            self._distro.set_selected(0)
            self._name.set_text(self._wizard.name)
        finally:
            self._syncing = False

    def _on_distro(self) -> None:
        if self._syncing:
            return
        index = self._distro.get_selected()
        distro = self._wizard.distros[index]
        dropped = self._wizard.choose_distro(distro)
        self._syncing = True
        try:
            self._name.set_text(self._wizard.name)
            # choose_distro clears the option for a distribution other than Kali (FR-101 AC-2).
            self._everything.set_active(self._wizard.everything)
        finally:
            self._syncing = False
        if dropped:
            self._package_error.set_label(f"Removed for {distro}: {', '.join(dropped)}")
        self._render()

    def _on_name(self) -> None:
        if not self._syncing:
            self._wizard.set_name(self._name.get_text())
            self._render()

    def _on_tier(self) -> None:
        if not self._syncing:
            self._wizard.set_tier(Tier(TIER_NAMES[self._tier.get_selected()]))

    def _on_services(self) -> None:
        if not self._syncing:
            self._wizard.set_services(bool(self._services.get_active()))

    def _on_everything(self) -> None:
        if not self._syncing:
            self._wizard.set_everything(bool(self._everything.get_active()))
            self._render()

    def _add_package(self) -> None:
        raw = self._package.get_text().strip()
        if not raw:
            return
        failure = self._wizard.add_package(raw)
        if failure is None:
            self._package.set_text("")
            self._package_error.set_label("")
        else:
            self._package_error.set_label(common.error_text(failure))
        self._render()

    def _remove_package(self, package: str) -> None:
        self._wizard.remove_package(package)
        self._render()

    def _render(self) -> None:
        failure = self._wizard.name_failure()
        self._name_error.set_label("" if failure is None else common.error_text(failure))
        self._everything.set_visible(self._wizard.show_everything)
        self._services_error.set_label(self._wizard.services_conflict() or "")
        blocked = self._session.control.host_changed
        self._create.set_sensitive(self._wizard.can_create() and not blocked)
        self._create.set_tooltip_text(BLOCKED_TEXT if blocked else "")
        self._render_packages()

    def _render_packages(self) -> None:
        for row in self._package_rows:
            self._packages.remove(row)
        self._package_rows = []
        for package in self._wizard.packages:
            row = Adw.ActionRow(use_markup=False)
            row.set_title(package)
            row.add_suffix(
                common.icon_button(
                    "user-trash-symbolic",
                    f"Remove {package}",
                    partial(self._remove_package, package),
                )
            )
            self._packages.add(row)
            self._package_rows.append(row)

    def _create_clicked(self) -> None:
        if self._session.control.host_changed:
            return
        if self._wizard.everything:
            self.dialog = common.confirm(
                self._session.window,
                heading="Install the Kali everything set?",
                body=EVERYTHING_BODY,
                confirm_label="Create",
                destructive=False,
                on_confirm=lambda _checked: self._start(),
            )
        else:
            self._start()

    def _start(self) -> None:
        if not self._wizard.can_create() or self._session.control.host_changed:
            return
        args = self._wizard.create_args()
        name = self._wizard.name
        present_progress(
            self._session,
            title=f"Creating {name}",
            calls=(args,),
            on_done=self._created,
        )

    def _created(self, model: ProgressModel) -> None:
        self.last_progress = model
        if model.phase is Phase.DONE:
            self._session.pop()
            self._session.control.refresh_dashboard()
