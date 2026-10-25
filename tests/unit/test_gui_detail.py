# SPDX-License-Identifier: Apache-2.0
"""Viewmodels of the realm detail, the progress dialog, the host-setup page and the dashboard
failure path (FR-102, FR-107, FR-105, FR-100). Pure Python: no display and no child process.
"""

from __future__ import annotations

import pytest

from ginn.gui.backend import CliError, CliResult, RealmListing, RealmView
from ginn.gui.viewmodels import detail
from ginn.gui.viewmodels.dashboard import DashboardViewModel
from ginn.gui.viewmodels.labels import Failure
from ginn.gui.viewmodels.progress import Phase, ProgressModel
from ginn.gui.viewmodels.setup import HOST_SETUP_TITLE, status_text


def _realm(**changes: object) -> RealmView:
    fields: dict[str, object] = {
        "name": "arch",
        "state": "stopped",
        "container_state": None,
        "state_mismatch": None,
        "distro": "arch",
        "tier": "basic",
        "services": False,
        "generation": 1,
        "restart_required": None,
        "share_skipped": None,
        "disk": None,
        "exported_apps": None,
        "error": None,
    }
    fields.update(changes)
    return RealmView(**fields)  # type: ignore[arg-type]


def _result(
    *, exit_code: int = 0, error: CliError | None = None, cancelled: bool = False
) -> CliResult:
    return CliResult(
        argv=("ginn", "--json", "create"),
        exit_code=exit_code,
        payload=None,
        error=error,
        warnings=(),
        stderr="",
        cancelled=cancelled,
    )


@pytest.mark.fr("FR-102")
def test_detail_rows_show_unknown_values_as_dashes_and_exports_as_not_reported() -> None:
    rows = dict(detail.detail_rows(_realm(distro=None, tier=None)))

    assert rows["Distribution"] == "-"
    assert rows["Tier"] == "-"
    assert rows["Exported apps"] == detail.EXPORTS_UNKNOWN_TEXT


@pytest.mark.fr("FR-102")
def test_start_and_stop_follow_the_realm_state() -> None:
    stopped = _realm(state="stopped")
    running = _realm(state="running")

    assert detail.can_start(stopped) and not detail.can_stop(stopped)
    assert detail.can_stop(running) and not detail.can_start(running)
    assert detail.can_update(stopped) and detail.can_update(running)
    assert not detail.can_update(_realm(state="broken"))


@pytest.mark.fr("FR-021")
def test_rebuild_is_offered_for_a_stopped_or_running_realm_only() -> None:
    assert detail.REBUILD_AVAILABLE is True
    assert detail.can_rebuild(_realm(state="stopped"))
    assert detail.can_rebuild(_realm(state="running"))
    assert not detail.can_rebuild(_realm(state="broken"))


@pytest.mark.fr("FR-102")
def test_delete_and_rebuild_ask_first_and_delete_offers_the_home_choice() -> None:
    delete = detail.delete_confirmation("arch")
    rebuild = detail.rebuild_confirmation("arch")

    assert delete.destructive is True
    assert delete.check_label == "Also delete home data"
    assert "arch" in delete.heading
    assert rebuild.destructive is False
    assert rebuild.check_label is None


@pytest.mark.fr("FR-107")
def test_a_run_with_no_failure_ends_done_after_its_lines() -> None:
    model = ProgressModel(title="Creating arch")
    model.add_line("create: step pulled done")
    model.finish((_result(),), None)

    assert model.phase is Phase.DONE
    assert model.lines == ("create: step pulled done",)
    assert model.finished is True
    assert model.can_cancel is False


@pytest.mark.fr("FR-107")
def test_a_failed_step_ends_failed_with_its_code_in_the_summary() -> None:
    error = CliError(
        code="GINN-E-NO-SUCH-REALM",
        message="no realm named arch",
        remedy="run 'ginn list'",
        exit_code=3,
        details={},
        notes=(),
    )
    model = ProgressModel(title="Deleting arch")
    model.finish((_result(exit_code=3, error=error),), None)

    assert model.phase is Phase.FAILED
    assert model.summary == "GINN-E-NO-SUCH-REALM: no realm named arch"


@pytest.mark.fr("FR-107")
def test_a_cancel_that_interrupts_a_step_ends_cancelled() -> None:
    model = ProgressModel(title="Creating arch")
    model.request_cancel()
    model.finish((_result(exit_code=130, cancelled=True),), None)

    assert model.phase is Phase.CANCELLED
    assert model.summary == "Cancelled"


@pytest.mark.fr("FR-107")
def test_a_cancel_before_any_step_ends_cancelled_and_drops_later_lines() -> None:
    model = ProgressModel(title="Creating arch")
    model.request_cancel()
    assert model.phase is Phase.CANCELLING
    model.finish((), None)
    model.add_line("too late")

    assert model.phase is Phase.CANCELLED
    assert model.lines == ()


@pytest.mark.fr("FR-107")
def test_an_exception_during_the_run_ends_failed() -> None:
    model = ProgressModel(title="Creating arch")
    model.finish((), RuntimeError("the program is missing"))

    assert model.phase is Phase.FAILED
    assert model.failure is not None
    assert model.failure.message == "the program is missing"


@pytest.mark.fr("FR-105")
def test_a_missing_host_setup_names_the_remedy() -> None:
    text = status_text(Failure(code="GINN-E-HOST-NOT-SET-UP", message="missing", remedy="run it"))
    assert text.title == HOST_SETUP_TITLE
    assert text.description == "run it"

    bare = status_text(Failure(code="GINN-E-HOST-NOT-SET-UP", message="missing", remedy=None))
    assert "host-setup" in bare.description


@pytest.mark.fr("FR-100")
def test_a_failed_read_keeps_the_rows_of_the_last_good_listing() -> None:
    dashboard = DashboardViewModel()
    good = RealmListing(request_id=1, result=_result(), realms=(_realm(),))
    assert dashboard.apply(good) is True

    dashboard.fail(Failure(code=None, message="ginn is not installed", remedy=None))

    assert [row.name for row in dashboard.rows] == ["arch"]
    assert dashboard.failure is not None
    assert dashboard.failure.message == "ginn is not installed"


@pytest.mark.fr("FR-083")
def test_only_the_services_switch_that_changes_the_mode_is_offered() -> None:
    assert detail.SERVICES_TOGGLE_AVAILABLE is True
    assert detail.can_services_on(_realm(services=False))
    assert not detail.can_services_off(_realm(services=False))
    assert detail.can_services_off(_realm(services=True))
    assert not detail.can_services_on(_realm(services=True))
    assert not detail.can_services_on(_realm(services=None))
    assert not detail.can_services_off(_realm(services=None))


@pytest.mark.fr("FR-107")
def test_a_run_stopped_between_two_commands_ends_cancelled() -> None:
    model = ProgressModel(title="Updating arch")
    model.finish((_result(),), None, cancelled=True)

    assert model.phase is Phase.CANCELLED
    assert model.summary == "Cancelled"
