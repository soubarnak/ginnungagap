# SPDX-License-Identifier: Apache-2.0
"""The Control Plane's worker threads: ordered reads and operations with progress and cancel.

The tests run the fake `ginn` (tests/unit/gui_support.py) through real child processes. The
dispatch function is a direct call here, so the callbacks run on the worker threads, and each test
waits for them with an event (FR-107, NFR-010).
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from ginn.gui import jobs
from ginn.gui.backend import CliLaunchError, CliResult, CliRunner
from ginn.gui.jobs import Operation, ReadWorker
from tests.unit.gui_support import FakeGinn, make_fake_ginn

WAIT_S = 10.0


@pytest.fixture
def fake(tmp_path: Path) -> FakeGinn:
    return make_fake_ginn(tmp_path)


def _program(fake: FakeGinn) -> tuple[str, ...]:
    return (sys.executable, str(fake.script))


def _env(fake: FakeGinn) -> dict[str, str]:
    return {
        **os.environ,
        "FAKE_GINN_DIR": str(fake.directory),
        "FAKE_GINN_HELPER": str(fake.helper),
    }


def _direct(fn: Callable[[], None]) -> None:
    fn()


def _wait(event: threading.Event) -> None:
    assert event.wait(WAIT_S), "the worker did not reply in time"


@pytest.mark.fr("FR-100")
def test_reads_reply_in_the_order_they_were_sent() -> None:
    worker = ReadWorker(_direct)
    replies: list[int] = []
    done = threading.Event()

    def reply(value: int) -> None:
        replies.append(value)
        if len(replies) == 3:
            done.set()

    try:
        for value in (1, 2, 3):
            worker.submit(lambda value=value: _slow(value), reply, _fail_test)
        _wait(done)
    finally:
        worker.close()

    assert replies == [1, 2, 3]


def _slow(value: int) -> int:
    time.sleep(0.01 * (3 - value))
    return value


def _fail_test(exc: BaseException) -> None:
    raise AssertionError(f"unexpected error: {exc!r}")


@pytest.mark.fr("FR-100")
def test_a_read_that_raises_reports_the_exception_to_on_error() -> None:
    worker = ReadWorker(_direct)
    errors: list[BaseException] = []
    done = threading.Event()

    def boom() -> int:
        raise CliLaunchError("ginn is not installed")

    def failed(exc: BaseException) -> None:
        errors.append(exc)
        done.set()

    try:
        worker.submit(boom, lambda _value: None, failed)
        _wait(done)
    finally:
        worker.close()

    assert isinstance(errors[0], CliLaunchError)
    assert str(errors[0]) == "ginn is not installed"


@pytest.mark.fr("FR-107")
def test_an_operation_streams_progress_and_reports_each_result(fake: FakeGinn) -> None:
    progress: list[str] = []
    finished: list[tuple[tuple[CliResult, ...], BaseException | None, bool]] = []
    done = threading.Event()

    def end(results: tuple[CliResult, ...], error: BaseException | None, cancelled: bool) -> None:
        finished.append((results, error, cancelled))
        done.set()

    operation = Operation(
        _program(fake),
        [("--json", "create", "arch")],
        dispatch=_direct,
        on_progress=progress.append,
        on_finished=end,
        env=_env(fake),
    )
    operation.start()
    _wait(done)

    (results, error, _cancelled) = finished[0]
    assert error is None
    assert len(results) == 1 and results[0].ok
    assert progress == ["create: step pulled done", "create: step integrated done"]
    assert fake.calls() == [["--json", "create", "arch"]]


@pytest.mark.fr("FR-107")
def test_an_operation_stops_after_the_first_failed_call(fake: FakeGinn) -> None:
    finished: list[tuple[tuple[CliResult, ...], BaseException | None, bool]] = []
    done = threading.Event()

    def end(results: tuple[CliResult, ...], error: BaseException | None, cancelled: bool) -> None:
        finished.append((results, error, cancelled))
        done.set()

    operation = Operation(
        _program(fake),
        [("--json", "status", "nosuch"), ("--json", "list")],
        dispatch=_direct,
        on_progress=lambda _text: None,
        on_finished=end,
        env=_env(fake),
    )
    operation.start()
    _wait(done)

    (results, _error, _cancelled) = finished[0]
    assert len(results) == 1
    assert results[0].error is not None
    assert results[0].error.code == "GINN-E-NO-SUCH-REALM"
    assert fake.calls() == [["--json", "status", "nosuch"]]


@pytest.mark.fr("FR-107")
def test_a_cancel_before_the_start_runs_no_command(fake: FakeGinn) -> None:
    finished: list[tuple[tuple[CliResult, ...], BaseException | None, bool]] = []
    done = threading.Event()

    def end(results: tuple[CliResult, ...], error: BaseException | None, cancelled: bool) -> None:
        finished.append((results, error, cancelled))
        done.set()

    operation = Operation(
        _program(fake),
        [("--json", "create", "arch")],
        dispatch=_direct,
        on_progress=lambda _text: None,
        on_finished=end,
        env=_env(fake),
    )
    assert operation.cancel() is False
    operation.start()
    _wait(done)

    assert finished[0][0] == ()
    assert finished[0][2] is True
    assert operation.cancelled is True
    assert fake.calls() == []


@pytest.mark.fr("FR-107")
def test_a_cancel_interrupts_the_running_command(fake: FakeGinn) -> None:
    finished: list[tuple[tuple[CliResult, ...], BaseException | None, bool]] = []
    done = threading.Event()

    def end(results: tuple[CliResult, ...], error: BaseException | None, cancelled: bool) -> None:
        finished.append((results, error, cancelled))
        done.set()

    operation = Operation(
        _program(fake),
        [("--json", "group")],
        dispatch=_direct,
        on_progress=lambda _text: None,
        on_finished=end,
        env=_env(fake),
    )
    operation.start()
    deadline = time.monotonic() + WAIT_S
    while not (fake.directory / "group-helper").exists():
        assert time.monotonic() < deadline, "the helper did not start"
        time.sleep(0.01)

    assert operation.cancel() is True
    _wait(done)

    (results, error, _cancelled) = finished[0]
    assert error is None
    assert len(results) == 1 and results[0].cancelled is True
    assert fake.helper_interrupted()


@pytest.mark.fr("FR-107")
def test_a_missing_program_is_reported_as_an_exception(fake: FakeGinn) -> None:
    finished: list[tuple[tuple[CliResult, ...], BaseException | None, bool]] = []
    done = threading.Event()

    def end(results: tuple[CliResult, ...], error: BaseException | None, cancelled: bool) -> None:
        finished.append((results, error, cancelled))
        done.set()

    operation = Operation(
        ("/nonexistent/ginn-for-the-test",),
        [("--json", "list")],
        dispatch=_direct,
        on_progress=lambda _text: None,
        on_finished=end,
    )
    operation.start()
    _wait(done)

    (results, error, _cancelled) = finished[0]
    assert results == ()
    assert isinstance(error, CliLaunchError)


@pytest.mark.fr("FR-107")
def test_a_cancel_before_the_child_starts_interrupts_it_and_the_next_run_is_not_cancelled(
    fake: FakeGinn,
) -> None:
    runner = fake.runner()
    # No child exists yet, so the cancel is kept for the run that comes next.
    assert runner.cancel() is False

    outcome: list[CliResult] = []
    run = threading.Thread(
        target=lambda: outcome.append(runner.run(("--json", "group"))), daemon=True
    )
    run.start()
    run.join(WAIT_S)
    if run.is_alive():
        # A lost cancel leaves the fake waiting for its helper. Stop its group, then fail.
        child = runner._child
        if child is not None:
            os.killpg(child.pid, signal.SIGKILL)
        run.join(WAIT_S)
        pytest.fail("the cancel that came before the child started was lost")

    (first,) = outcome
    assert first.cancelled is True
    assert first.exit_code != 0
    second = runner.run(("--json", "list"))
    assert second.cancelled is False
    assert second.ok


@pytest.mark.fr("FR-107")
def test_a_cancel_between_two_calls_stops_the_run_and_reports_it_cancelled(
    fake: FakeGinn, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished: list[tuple[tuple[CliResult, ...], BaseException | None, bool]] = []
    done = threading.Event()
    holder: list[Operation] = []

    def end(results: tuple[CliResult, ...], error: BaseException | None, cancelled: bool) -> None:
        finished.append((results, error, cancelled))
        done.set()

    class CancelAfterTheFirstCall(CliRunner):
        """Asks for the cancel once the first call has ended, so the stop lands between calls."""

        def run(
            self, args: Sequence[str], on_progress: Callable[[str], None] | None = None
        ) -> CliResult:
            result = super().run(args, on_progress=on_progress)
            holder[0].cancel()
            return result

    monkeypatch.setattr(jobs, "CliRunner", CancelAfterTheFirstCall)
    operation = Operation(
        _program(fake),
        [("--json", "list"), ("--json", "list")],
        dispatch=_direct,
        on_progress=lambda _text: None,
        on_finished=end,
        env=_env(fake),
    )
    holder.append(operation)
    operation.start()
    _wait(done)

    (results, error, cancelled) = finished[0]
    assert error is None
    assert len(results) == 1 and results[0].ok
    assert cancelled is True
    assert fake.calls() == [["--json", "list"]]


@pytest.mark.fr("FR-100")
def test_close_does_not_wait_for_a_read_that_is_running() -> None:
    worker = ReadWorker(_direct)
    release = threading.Event()
    running = threading.Event()
    read_ran = threading.Event()
    queued_ran: list[str] = []
    replies: list[bool] = []

    def slow() -> bool:
        running.set()
        release.wait(WAIT_S)
        read_ran.set()
        return True

    worker.submit(slow, replies.append, _fail_test)
    worker.submit(lambda: queued_ran.append("queued") or True, replies.append, _fail_test)
    _wait(running)
    try:
        started = time.monotonic()
        worker.close()
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
    _wait(read_ran)
    time.sleep(0.05)

    assert replies == []
    assert queued_ran == []
