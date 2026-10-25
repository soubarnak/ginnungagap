# SPDX-License-Identifier: Apache-2.0
"""Control Plane backend: the `ginn --json` runner, the state reader and the refresh scope.

The tests run a fake `ginn` executable (tests/unit/gui_support.py), so no container runtime is
needed. They cover the argv contract, progress streaming, SIGINT cancel and the IR-011 exit-code
mapping. The import-boundary test proves that the GUI code opens no socket and reaches no ops or
runtime module (FR-105, NFR-SEC-009).
"""

from __future__ import annotations

import ast
import os
import threading
from pathlib import Path

import pytest

from ginn.errors import GinnError
from ginn.gui import backend
from ginn.gui.backend import CliLaunchError, CliResult, CliRunner, StateReader, watch_directories
from ginn.store.paths import OwnedPaths
from tests.unit.gui_support import FakeGinn, make_fake_ginn

GUI_DIR = Path(backend.__file__).resolve().parent
GI_MODULES = frozenset({"_gio.py", "views/_gtk.py"})
"""The only gui modules that may import gi: the change source and the views' GTK import point."""


@pytest.fixture
def fake(tmp_path: Path) -> FakeGinn:
    return make_fake_ginn(tmp_path)


@pytest.mark.fr("IR-030")
def test_run_returns_the_json_payload_and_the_argv(fake: FakeGinn) -> None:
    result = fake.runner().run(("--json", "list"))

    assert result.ok
    assert result.exit_code == 0
    assert result.error is None
    assert result.payload is not None
    assert [realm["name"] for realm in result.payload["realms"]] == ["arch", "debian", "fedora"]
    assert result.argv[-2:] == ("--json", "list")
    assert fake.calls() == [["--json", "list"]]


@pytest.mark.fr("FR-100")
def test_state_reader_builds_realm_views_from_list_json(fake: FakeGinn) -> None:
    listing = StateReader(fake.runner()).list_realms()

    assert listing.realms is not None
    arch, debian, fedora = listing.realms
    assert (arch.name, arch.state, arch.tier, arch.services) == ("arch", "stopped", "basic", False)
    assert arch.disk is not None
    assert arch.disk.realm_home_bytes == 1610612736
    assert arch.exported_apps is None
    assert (debian.state, debian.services, debian.tier) == ("running", True, "development")
    assert debian.disk is not None
    assert debian.disk.realm_home_partial is True
    assert debian.disk.container_bytes == 4096
    assert fedora.state == "broken"
    assert fedora.disk is None


@pytest.mark.fr("DR-031")
def test_state_reader_numbers_each_read_in_increasing_order(fake: FakeGinn) -> None:
    reader = StateReader(fake.runner())

    first = reader.list_realms()
    second = reader.list_realms()

    assert second.request_id > first.request_id


@pytest.mark.fr("FR-100")
def test_status_of_an_unknown_realm_maps_the_error_object(fake: FakeGinn) -> None:
    result, view = StateReader(fake.runner()).realm_status("ghost")

    assert view is None
    assert not result.ok
    assert result.exit_code == 3
    assert result.error is not None
    assert result.error.code == "GINN-E-NO-SUCH-REALM"
    assert result.error.exit_code == 3
    assert result.error.remedy == "run 'ginn list' to see the realms"
    assert result.payload is None


@pytest.mark.fr("NFR-SEC-005")
def test_status_rejects_an_invalid_name_before_any_run(fake: FakeGinn) -> None:
    with pytest.raises(GinnError) as excinfo:
        StateReader(fake.runner()).realm_status("Arch!")

    assert excinfo.value.code == "GINN-E-NAME-INVALID"
    assert fake.calls() == []


@pytest.mark.fr("NFR-SEC-005")
def test_run_refuses_a_command_that_is_not_json(fake: FakeGinn) -> None:
    with pytest.raises(ValueError, match="--json"):
        fake.runner().run(("list",))

    assert fake.calls() == []


@pytest.mark.fr("IR-011")
def test_a_failure_without_an_error_object_has_no_error(fake: FakeGinn) -> None:
    result = fake.runner().run(("--json", "bare"))

    assert not result.ok
    assert result.exit_code == 2
    assert result.error is None
    assert "usage: ginn" in result.stderr


@pytest.mark.fr("FR-107")
def test_progress_lines_reach_the_callback_without_any_flag(fake: FakeGinn) -> None:
    seen: list[str] = []

    result = fake.runner().run(("--json", "create", "arch"), on_progress=seen.append)

    assert result.ok
    assert seen == ["create: step pulled done", "create: step integrated done"]
    assert fake.calls() == [["--json", "create", "arch"]]


@pytest.mark.fr("FR-107")
def test_the_runner_passes_the_argv_unchanged_with_or_without_a_callback(fake: FakeGinn) -> None:
    result = fake.runner().run(("--json", "create", "arch"))

    assert result.ok
    assert result.argv[-3:] == ("--json", "create", "arch")
    assert fake.calls() == [["--json", "create", "arch"]]


@pytest.mark.fr("FR-107")
def test_cancel_signals_the_process_group_and_reports_the_interrupt(fake: FakeGinn) -> None:
    runner = fake.runner()
    ready = threading.Event()
    outcome: list[CliResult] = []

    def worker() -> None:
        outcome.append(runner.run(("--json", "group"), on_progress=lambda _text: ready.set()))

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=20), "the fake ginn never reported that its helper was running"

    assert runner.cancel() is True
    thread.join(timeout=20)
    assert not thread.is_alive()

    (result,) = outcome
    assert result.cancelled is True
    assert result.exit_code == 130
    assert result.error is not None
    assert result.error.code == "GINN-E-INTERRUPTED"
    parent_pid, parent_pgid = fake.pids("group-parent")
    helper_pid, helper_pgid = fake.pids("group-helper")
    assert parent_pgid == parent_pid, "the child did not start its own process group"
    assert helper_pgid == parent_pid, "the helper is not in the child's process group"
    assert helper_pid != parent_pid
    assert fake.helper_interrupted(), "the group SIGINT did not reach the helper"


@pytest.mark.fr("FR-107")
def test_cancel_with_no_running_child_returns_false(fake: FakeGinn) -> None:
    assert fake.runner().cancel() is False


@pytest.mark.fr("FR-107")
def test_an_error_in_the_progress_callback_is_raised_after_the_child_exits(
    fake: FakeGinn,
) -> None:
    def broken(text: str) -> None:
        raise RuntimeError(f"view failed on {text}")

    with pytest.raises(RuntimeError, match="view failed"):
        fake.runner().run(("--json", "create", "arch"), on_progress=broken)

    assert fake.calls() == [["--json", "create", "arch"]]


@pytest.mark.fr("IR-011")
def test_a_missing_program_raises_a_launch_error(tmp_path: Path) -> None:
    runner = CliRunner((str(tmp_path / "no-such-ginn"),))

    with pytest.raises(CliLaunchError):
        runner.run(("--json", "list"))


@pytest.mark.fr("FR-104")
def test_integrity_reports_changed_paths_and_the_host_changed_warning(fake: FakeGinn) -> None:
    result, state = StateReader(fake.runner()).integrity()

    assert result.ok
    assert state is not None
    assert state.status == "changed"
    assert state.changed == ("/etc/hosts", "/usr/bin/tool")
    assert state.pending == ("/etc/hosts", "/usr/bin/tool")
    assert [warning.code for warning in state.warnings] == ["GINN-W-HOST-CHANGED"]


@pytest.mark.fr("FR-104")
def test_doctor_report_is_parsed_even_when_a_check_fails(fake: FakeGinn) -> None:
    result, report = StateReader(fake.runner()).doctor()

    assert result.exit_code == 1
    assert result.error is not None
    assert result.error.code == "GINN-E-DOCTOR-FAILED"
    assert report is not None
    assert report.ok is False
    assert report.counts == {"PASS": 1, "WARN": 0, "FAIL": 1, "INFO": 0}
    assert [check.name for check in report.checks] == ["subuid", "podman"]
    assert report.checks[0].fixes == ("ginn host-setup --user tester",)


@pytest.mark.fr("FR-103")
def test_permissions_view_comes_from_the_tier_output(fake: FakeGinn) -> None:
    result, view = StateReader(fake.runner()).permissions("lab")

    assert result.ok
    assert view is not None
    assert view.tier == "custom"
    assert view.toggles == ("downloads", "gpu")
    assert view.shares[0].path == "/home/tester/projects"
    assert view.shares[0].mode == "ro"


@pytest.mark.fr("DR-030")
def test_watch_directories_are_the_records_and_the_state_directory(tmp_path: Path) -> None:
    uid = os.getuid()
    runtime = tmp_path / "run" / "ginn" / str(uid)
    runtime.mkdir(parents=True)
    runtime.chmod(0o700)
    env = {
        "HOME": str(tmp_path / "home"),
        "GINN_RUNTIME_DIR": str(runtime),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    paths = OwnedPaths.from_env(env, uid=uid, system_run=tmp_path / "run")

    watched = watch_directories(paths)

    assert watched == (paths.realms, paths.state)
    assert paths.audit_log.parent == paths.state


def _imported_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


@pytest.mark.fr("FR-105")
@pytest.mark.fr("NFR-SEC-009")
def test_gui_code_opens_no_socket_and_reaches_no_ops_or_runtime_module() -> None:
    """The Control Plane is a client of the CLI. It has no listener and imports no ops module."""
    sources = sorted(GUI_DIR.rglob("*.py"))
    assert sources, "the gui package has no sources"
    never_imported = ("socket", "ginn.ops", "ginn.podman", "ginn.runtime", "ginn.hostsetup")
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for name in _imported_names(tree):
            assert not any(
                name == root or name.startswith(root + ".") for root in never_imported
            ), f"{source.name} imports {name}"
            if name == "gi" or name.startswith("gi."):
                assert source.relative_to(GUI_DIR).as_posix() in GI_MODULES, (
                    f"{source.name} imports gi"
                )
            if name == "subprocess":
                assert source.name == "backend.py", f"{source.name} starts processes"
