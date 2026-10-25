# SPDX-License-Identifier: Apache-2.0
"""One Control Plane session: the reads, the models and the watch on realm state (FR-100, DR-030).

The session owns a `ReadWorker` for reads, the dashboard and health viewmodels, and a refresh
source. Views ask it to read and to start operations, and they subscribe to hear that a model
changed. The session never touches a widget. Each listener runs on the thread that `dispatch`
names, which is the main loop in the application and a direct call in the tests.

A change on disk refreshes the dashboard. The realms directory is created by the first create, so
the watch is set up again when it appears. The state directory above it is always watched. Reads are
coalesced: while the realm list is being read, a refresh asked for then makes one more read start
when that read returns (FR-100). An unacknowledged host change blocks the actions that change the
host or the realms (FR-104 AC-1), and `host_changed` tells the views so.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ginn.gui.backend import (
    PROGRAM,
    CliResult,
    CliRunner,
    DoctorReport,
    IntegrityState,
    PermissionsView,
    RealmListing,
    RealmView,
    StateReader,
    watch_directories,
)
from ginn.gui.jobs import Dispatch, Operation, ReadWorker
from ginn.gui.viewmodels.dashboard import DashboardViewModel
from ginn.gui.viewmodels.health import HealthViewModel
from ginn.gui.viewmodels.labels import Failure, failure_of
from ginn.gui.watch import RefreshSource
from ginn.store.paths import OwnedPaths

MakeSource = Callable[[Sequence[Path]], RefreshSource]
"""Builds a refresh source over the given directories. The application passes a Gio monitor."""


class Control:
    """The reads, the models and the watch of one Control Plane window."""

    def __init__(
        self,
        *,
        paths: OwnedPaths,
        dispatch: Dispatch,
        make_source: MakeSource,
        program: Sequence[str] = PROGRAM,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths
        self.program = tuple(program)
        self.env = None if env is None else dict(env)
        self.dispatch = dispatch
        self.dashboard = DashboardViewModel()
        self.health = HealthViewModel()
        self.health_failure: Failure | None = None
        self._make_source = make_source
        self._reads = ReadWorker(dispatch)
        self._reader = StateReader(CliRunner(self.program, env=self.env))
        self._listeners: list[Callable[[], None]] = []
        self._refresh_pending = False
        self._refresh_again = False
        self._source: RefreshSource | None = None
        self._watching_realms = False
        self._closed = False

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` on the dispatch thread after a model changed. The result stops it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    # Reads. Each one runs on the read worker, so the reader is never used by two threads.

    def refresh_dashboard(self) -> None:
        """Read the realm list again. While a read is pending, the request waits for one more read.

        A change that arrives during a read is never lost: the pending read may have started before
        it.
        """
        if self._closed:
            return
        if self._refresh_pending:
            self._refresh_again = True
            return
        self._submit_listing()

    def _submit_listing(self) -> None:
        self._refresh_pending = True
        self._refresh_again = False
        self._reads.submit(self._reader.list_realms, self._listed, self._dashboard_failed)

    def _listed(self, listing: RealmListing) -> None:
        self._refresh_pending = False
        if self.dashboard.apply(listing):
            self._notify()
        self._read_again_if_asked()

    def _dashboard_failed(self, exc: BaseException) -> None:
        self._refresh_pending = False
        self.dashboard.fail(_failure_of_exception(exc))
        self._notify()
        self._read_again_if_asked()

    def _read_again_if_asked(self) -> None:
        if self._refresh_again and not self._closed:
            self._submit_listing()

    def read_realm(
        self,
        name: str,
        on_done: Callable[[CliResult, RealmView | None], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        """`status <realm>`. `on_done` gets the result and the view, which is None on failure."""
        self._reads.submit(
            lambda: self._reader.realm_status(name),
            lambda pair: on_done(pair[0], pair[1]),
            on_error,
        )

    def read_permissions(
        self,
        name: str,
        on_done: Callable[[CliResult, PermissionsView | None], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        """`tier <realm>`, which shows the tier, the toggles and the shares (FR-103)."""
        self._reads.submit(
            lambda: self._reader.permissions(name),
            lambda pair: on_done(pair[0], pair[1]),
            on_error,
        )

    @property
    def host_changed(self) -> bool:
        """True while an unacknowledged host change blocks the actions that change the host."""
        return self.health.blocks_mutations

    def refresh_health(self) -> None:
        """Run `doctor` and `integrity` and apply both to the health model (FR-104)."""

        def work() -> tuple[CliResult, DoctorReport | None, CliResult, IntegrityState | None]:
            doctor_result, report = self._reader.doctor()
            integrity_result, state = self._reader.integrity()
            return doctor_result, report, integrity_result, state

        self._reads.submit(work, self._health_read, self._health_failed)

    def _health_read(
        self,
        read: tuple[CliResult, DoctorReport | None, CliResult, IntegrityState | None],
    ) -> None:
        doctor_result, report, integrity_result, state = read
        if report is not None:
            self.health.apply_doctor(report)
        if state is not None:
            self.health.apply_integrity(state)
        self.health_failure = failure_of(doctor_result) or failure_of(integrity_result)
        self._notify()

    def _health_failed(self, exc: BaseException) -> None:
        self.health_failure = _failure_of_exception(exc)
        self._notify()

    # Operations.

    def operation(
        self,
        calls: Sequence[Sequence[str]],
        *,
        on_progress: Callable[[str], None],
        on_finished: Callable[[tuple[CliResult, ...], BaseException | None, bool], None],
    ) -> Operation:
        """An operation that runs `calls` in order. The caller starts it (FR-107)."""
        return Operation(
            self.program,
            calls,
            dispatch=self.dispatch,
            on_progress=on_progress,
            on_finished=on_finished,
            env=self.env,
        )

    # Watch.

    def start_watching(self) -> None:
        """Watch the realm state on disk, and refresh the dashboard when it changes.

        It also reads the host health, so an unacknowledged host change shows when the window opens
        (FR-104 AC-1).
        """
        self._watch()
        self.refresh_dashboard()
        self.refresh_health()

    def _watch(self) -> None:
        if self._source is not None:
            self._source.close()
        self._source = self._make_source(watch_directories(self.paths))
        self._source.subscribe(self._on_disk_change)
        self._watching_realms = self.paths.realms.is_dir()

    def _on_disk_change(self) -> None:
        if self._closed:
            return
        if not self._watching_realms and self.paths.realms.is_dir():
            self._watch()
        self.refresh_dashboard()

    def close(self) -> None:
        """Stop the watch and the read worker. Calling it twice is safe."""
        self._closed = True
        if self._source is not None:
            self._source.close()
            self._source = None
        self._reads.close()


def _failure_of_exception(exc: BaseException) -> Failure:
    return Failure(code=None, message=str(exc) or type(exc).__name__, remedy=None)
