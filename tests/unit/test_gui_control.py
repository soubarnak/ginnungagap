# SPDX-License-Identifier: Apache-2.0
"""The Control Plane session: reads into the models, health, and the watch on realm state (DR-030).

The session runs the fake `ginn` through the read worker. Its dispatch is a direct call, so the
models change on the worker thread, and each test waits for the change with a short poll. The
refresh source is a fake that records its directories and fires on demand (FR-100, FR-104).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ginn.gui.control import Control
from ginn.gui.watch import RefreshSource
from ginn.store.paths import OwnedPaths
from tests.unit.gui_support import FakeGinn, make_fake_ginn

WAIT_S = 10.0


class RecordingSource(RefreshSource):
    """A refresh source that keeps the callback, so a test can fire it."""

    made: list[RecordingSource] = []

    def __init__(self, directories: tuple[Path, ...]) -> None:
        self.directories = directories
        self.callback: Callable[[], None] | None = None
        self.closed = False
        RecordingSource.made.append(self)

    def subscribe(self, callback: Callable[[], None]) -> None:
        self.callback = callback

    def close(self) -> None:
        self.closed = True


def _direct(fn: Callable[[], None]) -> None:
    fn()


def _wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "the session did not reach the expected state"
        time.sleep(0.01)


def _paths(tmp_path: Path, *, realms: bool) -> OwnedPaths:
    uid = os.getuid()
    runtime = tmp_path / "run" / "ginn" / str(uid)
    runtime.mkdir(parents=True)
    runtime.chmod(0o700)
    state = tmp_path / "state" / "ginn"
    state.mkdir(parents=True)
    if realms:
        (state / "realms").mkdir()
    env = {
        "HOME": str(tmp_path / "home"),
        "GINN_RUNTIME_DIR": str(runtime),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    return OwnedPaths.from_env(env, uid=uid, system_run=tmp_path / "run")


@pytest.fixture
def fake(tmp_path: Path) -> FakeGinn:
    return make_fake_ginn(tmp_path)


def _control(paths: OwnedPaths, fake: FakeGinn, program: tuple[str, ...] | None = None) -> Control:
    env = {**os.environ, "FAKE_GINN_DIR": str(fake.directory), "FAKE_GINN_HELPER": str(fake.helper)}
    return Control(
        paths=paths,
        dispatch=_direct,
        make_source=RecordingSource,
        program=program or (sys.executable, str(fake.script)),
        env=env,
    )


@pytest.fixture(autouse=True)
def _forget_sources() -> Iterator[None]:
    RecordingSource.made.clear()
    yield
    RecordingSource.made.clear()


@pytest.mark.fr("FR-100")
def test_a_refresh_applies_the_realm_list_to_the_dashboard(tmp_path: Path, fake: FakeGinn) -> None:
    control = _control(_paths(tmp_path, realms=True), fake)
    seen: list[bool] = []
    control.subscribe(lambda: seen.append(control.dashboard.loaded))
    try:
        control.refresh_dashboard()
        _wait_for(lambda: control.dashboard.loaded)
    finally:
        control.close()

    assert [row.name for row in control.dashboard.rows] == ["arch", "debian", "fedora"]
    assert seen and seen[-1] is True
    assert fake.calls() == [["--json", "list"]]


@pytest.mark.fr("FR-100")
def test_a_missing_ginn_program_shows_the_failure_and_keeps_no_rows(
    tmp_path: Path, fake: FakeGinn
) -> None:
    control = _control(
        _paths(tmp_path, realms=True), fake, program=("/nonexistent/ginn-for-the-test",)
    )
    try:
        control.refresh_dashboard()
        _wait_for(lambda: control.dashboard.failure is not None)
    finally:
        control.close()

    assert control.dashboard.rows == ()
    assert control.dashboard.loaded is False


@pytest.mark.fr("FR-104")
def test_the_health_model_gets_the_doctor_report_and_the_integrity_banner(
    tmp_path: Path, fake: FakeGinn
) -> None:
    control = _control(_paths(tmp_path, realms=True), fake)
    try:
        control.refresh_health()
        _wait_for(lambda: control.health.banner is not None)
    finally:
        control.close()

    assert control.health.report is not None
    assert control.health.badge.value == "fail"
    banner = control.health.banner
    assert banner is not None
    assert banner.paths == ("/etc/hosts", "/usr/bin/tool")
    assert control.health_failure is not None
    assert control.health_failure.code == "GINN-E-DOCTOR-FAILED"


@pytest.mark.fr("DR-030")
def test_the_watch_is_set_up_again_when_the_realms_directory_appears(
    tmp_path: Path, fake: FakeGinn
) -> None:
    paths = _paths(tmp_path, realms=False)
    control = _control(paths, fake)
    try:
        control.start_watching()
        first = RecordingSource.made[0]
        assert first.directories == (paths.realms, paths.state)
        assert control._watching_realms is False

        paths.realms.mkdir()
        assert first.callback is not None
        first.callback()
        _wait_for(lambda: control.dashboard.loaded)
    finally:
        control.close()

    assert len(RecordingSource.made) == 2
    assert first.closed is True
    assert control._watching_realms is True


@pytest.mark.fr("DR-030")
def test_close_stops_the_watch_and_later_refreshes_read_nothing(
    tmp_path: Path, fake: FakeGinn
) -> None:
    control = _control(_paths(tmp_path, realms=True), fake)
    control.start_watching()
    _wait_for(lambda: control.dashboard.loaded and control.health.banner is not None)
    source = RecordingSource.made[0]

    control.close()
    control.refresh_dashboard()
    control.refresh_health()

    assert source.closed is True
    assert fake.calls() == [["--json", "list"], ["--json", "doctor"], ["--json", "integrity"]]


@pytest.mark.fr("FR-104")
def test_opening_the_window_reads_the_host_health_and_blocks_the_changing_actions(
    tmp_path: Path, fake: FakeGinn
) -> None:
    control = _control(_paths(tmp_path, realms=True), fake)
    try:
        control.start_watching()
        _wait_for(lambda: control.dashboard.loaded and control.host_changed)
    finally:
        control.close()

    assert fake.calls() == [["--json", "list"], ["--json", "doctor"], ["--json", "integrity"]]


@pytest.mark.fr("FR-100")
def test_a_refresh_asked_while_a_read_is_pending_is_read_once_more(
    tmp_path: Path, fake: FakeGinn
) -> None:
    control = _control(_paths(tmp_path, realms=True), fake)
    release = threading.Event()
    try:
        # The read worker is busy with this job, so the list read below waits behind it.
        control._reads.submit(lambda: release.wait(WAIT_S), lambda _value: None, _ignore)
        control.refresh_dashboard()
        control.refresh_dashboard()
        release.set()
        _wait_for(lambda: len(fake.calls()) == 2 and not control._refresh_pending)
    finally:
        release.set()
        control.close()

    assert fake.calls() == [["--json", "list"], ["--json", "list"]]
    assert control.dashboard.loaded is True


def _ignore(_exc: BaseException) -> None:
    return None


@pytest.mark.fr("FR-100")
def test_subscribers_can_be_removed(tmp_path: Path, fake: FakeGinn) -> None:
    control = _control(_paths(tmp_path, realms=True), fake)
    calls: list[int] = []
    unsubscribe = control.subscribe(lambda: calls.append(1))
    unsubscribe()
    try:
        control.refresh_dashboard()
        _wait_for(lambda: control.dashboard.loaded)
    finally:
        control.close()

    assert calls == []
