# SPDX-License-Identifier: Apache-2.0
"""Smoke tests of the Control Plane views under Xvfb, against the fake `ginn` (FR-100..107).

Each view is built on a real window. A click runs the view's handler, which starts the command
through the Control, and the test checks the argv that the fake `ginn` received. The tests skip when
PyGObject, GTK 4, libadwaita or Xvfb is missing (tests/gui/conftest.py).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

try:
    from ginn.gui.control import Control
    from ginn.gui.viewmodels.labels import Failure
    from ginn.gui.viewmodels.progress import Phase, ProgressModel
    from ginn.gui.viewmodels.setup import HOST_SETUP_TITLE
    from ginn.gui.views.progress import present_progress
    from ginn.gui.views.setup import status_window
    from ginn.gui.views.window import Session
except (ImportError, ValueError) as exc:
    pytest.skip(f"the views need PyGObject, GTK 4 and libadwaita: {exc}", allow_module_level=True)

from ginn.domain.permissions import Toggle
from ginn.store.paths import OwnedPaths
from tests.gui.support import SilentSource, drain, pump_until
from tests.unit.gui_support import FakeGinn, make_fake_ginn

WIZARD_DISTROS = ("arch", "debian", "kali")


@pytest.fixture
def fake(tmp_path: Path) -> FakeGinn:
    return make_fake_ginn(tmp_path)


@pytest.fixture
def paths(tmp_path: Path) -> OwnedPaths:
    uid = os.getuid()
    runtime = tmp_path / "run" / "ginn" / str(uid)
    runtime.mkdir(parents=True)
    runtime.chmod(0o700)
    env = {
        "HOME": str(tmp_path / "home"),
        "GINN_RUNTIME_DIR": str(runtime),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    return OwnedPaths.from_env(env, uid=uid, system_run=tmp_path / "run")


@pytest.fixture
def control(gtk: Any, fake: FakeGinn, paths: OwnedPaths) -> Iterator[Control]:
    env = {**os.environ, "FAKE_GINN_DIR": str(fake.directory), "FAKE_GINN_HELPER": str(fake.helper)}
    made = Control(
        paths=paths,
        dispatch=gtk.run_on_main,
        make_source=SilentSource,
        program=(sys.executable, str(fake.script)),
        env=env,
    )
    yield made
    made.close()


@pytest.fixture
def session(gtk: Any, control: Control) -> Session:
    built = Session(control)
    built.build(None)
    return built


def _load_realms(gtk: Any, control: Control) -> None:
    control.refresh_dashboard()
    pump_until(gtk, lambda: control.dashboard.loaded)
    drain(gtk)


@pytest.mark.fr("FR-100")
def test_dashboard_lists_every_realm_from_one_list_read(
    gtk: Any, session: Session, control: Control, fake: FakeGinn
) -> None:
    _load_realms(gtk, control)

    assert session.dashboard is not None
    assert session.dashboard.row_titles() == ("arch", "debian", "fedora")
    assert fake.calls() == [["--json", "list"]]


@pytest.mark.fr("FR-100")
def test_a_failed_first_read_says_the_realm_list_is_unavailable(
    gtk: Any, paths: OwnedPaths
) -> None:
    broken = Control(
        paths=paths,
        dispatch=gtk.run_on_main,
        make_source=SilentSource,
        program=("/nonexistent/ginn-for-the-test",),
    )
    try:
        session = Session(broken)
        session.build(None)
        broken.refresh_dashboard()
        pump_until(gtk, lambda: broken.dashboard.failure is not None)
        drain(gtk)

        assert session.dashboard is not None
        assert session.dashboard._stack.get_visible_child_name() == "empty"
        assert session.dashboard._empty.get_title() == "Realms unavailable"
        assert session.dashboard._banner.get_revealed() is True
    finally:
        broken.close()


@pytest.mark.fr("FR-101")
def test_wizard_shows_the_dr002_error_and_disables_create(gtk: Any, session: Session) -> None:
    wizard = session.open_create(WIZARD_DISTROS)
    wizard._name.set_text("Arch!")
    drain(gtk)

    assert wizard._create.get_sensitive() is False
    assert "GINN-E-NAME-INVALID" in wizard._name_error.get_label()


@pytest.mark.fr("FR-101")
def test_wizard_offers_everything_for_kali_only(gtk: Any, session: Session) -> None:
    wizard = session.open_create(WIZARD_DISTROS)
    drain(gtk)
    assert wizard._everything.get_visible() is False

    wizard._distro.set_selected(WIZARD_DISTROS.index("kali"))
    drain(gtk)

    assert wizard._everything.get_visible() is True


@pytest.mark.fr("FR-101")
def test_leaving_kali_clears_the_everything_switch(gtk: Any, session: Session) -> None:
    wizard = session.open_create(WIZARD_DISTROS)
    wizard._distro.set_selected(WIZARD_DISTROS.index("kali"))
    wizard._everything.set_active(True)
    drain(gtk)

    wizard._distro.set_selected(WIZARD_DISTROS.index("debian"))
    drain(gtk)
    assert wizard._everything.get_active() is False
    assert wizard._wizard.everything is False

    wizard._distro.set_selected(WIZARD_DISTROS.index("kali"))
    drain(gtk)
    assert wizard._everything.get_active() is False
    assert wizard._wizard.everything is False


@pytest.mark.fr("FR-101")
def test_create_runs_the_wizard_argv_through_the_backend(
    gtk: Any, session: Session, fake: FakeGinn
) -> None:
    wizard = session.open_create(WIZARD_DISTROS)
    wizard._name.set_text("arch")
    drain(gtk)
    assert wizard._create.get_sensitive() is True

    wizard._create.emit("clicked")
    pump_until(gtk, lambda: wizard.last_progress is not None)

    assert wizard.last_progress is not None
    assert wizard.last_progress.phase is Phase.DONE
    assert fake.calls() == [
        ["--json", "create", "arch", "--name", "arch", "--tier", "basic", "--services", "off"]
    ]


@pytest.mark.fr("FR-014")
def test_kali_everything_asks_first_and_then_passes_yes(
    gtk: Any, session: Session, fake: FakeGinn
) -> None:
    wizard = session.open_create(WIZARD_DISTROS)
    wizard._distro.set_selected(WIZARD_DISTROS.index("kali"))
    wizard._everything.set_active(True)
    drain(gtk)

    wizard._create.emit("clicked")
    drain(gtk)
    assert fake.calls() == []
    assert wizard.dialog is not None
    wizard.dialog.emit("response", "confirm")
    pump_until(gtk, lambda: wizard.last_progress is not None)

    (argv,) = fake.calls()
    assert argv[-2:] == ["--everything", "--yes"]
    assert argv[argv.index("--name") + 1] == "kali"


@pytest.mark.fr("FR-102")
def test_delete_needs_a_confirmation_and_then_runs_without_purging_home(
    gtk: Any, session: Session, control: Control, fake: FakeGinn
) -> None:
    _load_realms(gtk, control)
    detail = session.open_detail("arch")
    pump_until(gtk, lambda: detail._realm is not None)
    drain(gtk)
    assert detail._buttons["delete"].get_sensitive() is True

    detail._buttons["delete"].emit("clicked")
    drain(gtk)
    assert ["--json", "list"] in fake.calls()
    assert detail.dialog is not None
    detail.dialog.emit("response", "cancel")
    drain(gtk)
    assert ["--json", "delete", "arch", "--yes"] not in fake.calls()

    detail._buttons["delete"].emit("clicked")
    drain(gtk)
    detail.dialog.emit("response", "confirm")
    pump_until(gtk, lambda: detail.last_progress is not None)

    assert detail.last_progress is not None
    assert detail.last_progress.phase is Phase.DONE
    assert ["--json", "delete", "arch", "--yes"] in fake.calls()


@pytest.mark.fr("FR-102")
def test_delete_with_the_home_data_box_ticked_purges_home(
    gtk: Any, session: Session, control: Control, fake: FakeGinn
) -> None:
    _load_realms(gtk, control)
    detail = session.open_detail("arch")
    pump_until(gtk, lambda: detail._realm is not None)
    detail._buttons["delete"].emit("clicked")
    drain(gtk)
    assert detail.dialog is not None
    check = detail.dialog.get_extra_child()
    check.set_active(True)

    detail.dialog.emit("response", "confirm")
    pump_until(gtk, lambda: detail.last_progress is not None)

    assert ["--json", "delete", "arch", "--purge-home", "--yes"] in fake.calls()


@pytest.mark.fr("FR-103")
def test_a_forbidden_shared_path_is_refused_inline_and_not_saved(
    gtk: Any, session: Session
) -> None:
    page = session.open_permissions("lab")
    pump_until(gtk, lambda: page._editor is not None)
    page._new_path.set_text("/etc")
    page._add_share()
    drain(gtk)

    assert "GINN-E-PATH-FORBIDDEN" in page._error.get_label()
    assert page._editor is not None
    assert "/etc" not in [share.path for share in page._editor.shares]


@pytest.mark.fr("FR-103")
def test_saving_a_toggle_runs_the_perms_argv(gtk: Any, session: Session, fake: FakeGinn) -> None:
    page = session.open_permissions("lab")
    pump_until(gtk, lambda: page._editor is not None)
    page._toggle_rows[Toggle.GPU].set_active(False)
    drain(gtk)
    assert page._save.get_sensitive() is True

    page._save.emit("clicked")
    pump_until(gtk, lambda: page.last_progress is not None)

    assert page.last_progress is not None
    assert page.last_progress.phase is Phase.DONE
    assert ["--json", "perms", "lab", "--disable", "gpu"] in fake.calls()


@pytest.mark.fr("FR-104")
def test_an_unacknowledged_host_change_shows_the_blocking_banner(
    gtk: Any, session: Session
) -> None:
    page = session.open_health()
    pump_until(gtk, lambda: session.control.health.banner is not None)
    drain(gtk)

    assert page._banner.get_revealed() is True
    assert "/etc/hosts" in page._banner.get_title()


@pytest.mark.fr("FR-104")
def test_acknowledge_runs_integrity_ack_after_a_confirmation(
    gtk: Any, session: Session, fake: FakeGinn
) -> None:
    page = session.open_health()
    pump_until(gtk, lambda: session.control.health.banner is not None)

    page._ask_acknowledge()
    drain(gtk)
    assert page.dialog is not None
    page.dialog.emit("response", "confirm")
    pump_until(gtk, lambda: page.last_progress is not None)

    assert page.last_progress is not None
    assert page.last_progress.phase is Phase.DONE
    assert ["--json", "integrity", "--ack"] in fake.calls()


@pytest.mark.fr("FR-107")
def test_cancel_interrupts_the_command_and_the_dialog_reports_it(
    gtk: Any, session: Session, fake: FakeGinn
) -> None:
    finished: list[Any] = []
    operation = present_progress(
        session,
        title="Working",
        calls=(("--json", "group"),),
        on_done=finished.append,
    )
    pump_until(gtk, lambda: (fake.directory / "group-helper").exists())

    assert operation.cancel() is True
    pump_until(gtk, lambda: bool(finished))

    assert finished[0].phase is Phase.CANCELLED
    assert fake.helper_interrupted()


@pytest.mark.fr("FR-105")
def test_a_missing_host_setup_shows_the_status_page(gtk: Any) -> None:
    failure = Failure(
        code="GINN-E-HOST-NOT-SET-UP",
        message="missing",
        remedy="run 'sudo ginn host-setup --user <name>'",
    )
    window = status_window(None, failure)

    page = window.get_content().get_content()
    assert page.get_title() == HOST_SETUP_TITLE
    # The label parses markup, so the placeholder is escaped and shows as written.
    assert "&lt;name&gt;" in page.get_description()
    assert "<name>" not in page.get_description()


@pytest.mark.fr("FR-104")
def test_the_window_opens_with_the_host_banner_and_blocks_create_and_the_changing_actions(
    gtk: Any, session: Session, control: Control
) -> None:
    control.start_watching()
    pump_until(gtk, lambda: control.dashboard.loaded and control.host_changed)
    drain(gtk)

    dashboard = session.dashboard
    assert dashboard is not None
    assert dashboard._host_banner.get_revealed() is True
    assert "/etc/hosts" in dashboard._host_banner.get_title()
    assert dashboard._create.get_sensitive() is False

    detail = session.open_detail("arch")
    pump_until(gtk, lambda: detail._realm is not None)
    drain(gtk)
    assert detail._buttons["start"].get_sensitive() is False
    assert detail._buttons["delete"].get_sensitive() is False
    assert detail._buttons["autostart-on"].get_sensitive() is False

    permissions = session.open_permissions("lab")
    pump_until(gtk, lambda: permissions._editor is not None)
    drain(gtk)
    assert permissions._tier.get_sensitive() is False
    assert permissions._save.get_sensitive() is False


@pytest.mark.fr("FR-107")
def test_the_window_refuses_to_close_while_an_operation_runs(
    gtk: Any, session: Session, fake: FakeGinn
) -> None:
    finished: list[Any] = []
    operation = present_progress(
        session,
        title="Working",
        calls=(("--json", "group"),),
        on_done=finished.append,
    )
    pump_until(gtk, lambda: (fake.directory / "group-helper").exists())

    assert session.busy is True
    # libadwaita refuses the close while the progress dialog is up, so the window stays open.
    assert session.window.emit("close-request") is True
    # The backstop of the session says why, with a notice (FR-107).
    assert session._on_close(session.window) is True
    assert session.notice is not None

    assert operation.cancel() is True
    pump_until(gtk, lambda: bool(finished))
    drain(gtk)
    assert session.busy is False
    assert session._on_close(session.window) is False


@pytest.mark.fr("FR-107")
def test_a_run_that_ends_in_failure_still_reads_the_realm_again(
    gtk: Any, session: Session, control: Control
) -> None:
    _load_realms(gtk, control)
    detail = session.open_detail("arch")
    pump_until(gtk, lambda: detail._realm is not None)
    drain(gtk)
    reads: list[int] = []
    detail.reload = lambda: reads.append(1)  # type: ignore[method-assign]
    failed = ProgressModel(title="Starting arch")
    failed.finish((), RuntimeError("the program is missing"))

    detail._finished(failed)

    assert reads == [1]


@pytest.mark.fr("FR-103")
def test_a_read_never_replaces_a_permissions_edit_that_is_not_saved(
    gtk: Any, session: Session, control: Control
) -> None:
    page = session.open_permissions("lab")
    pump_until(gtk, lambda: page._editor is not None)
    drain(gtk)
    page._toggle_rows[Toggle.GPU].set_active(False)
    drain(gtk)
    editor = page._editor
    assert editor is not None and editor.dirty

    result, view = control._reader.permissions("lab")
    assert view is not None
    page._read_done(result, view)

    assert page._editor is editor
    assert page._toggle_rows[Toggle.GPU].get_active() is False
    assert page._save.get_sensitive() is True
