# SPDX-License-Identifier: Apache-2.0
"""Worker threads of the Control Plane: reads, and operations with progress and cancel (FR-107).

Nothing here imports gi. A `Dispatch` function carries each result back to the thread that owns
the widgets. The views pass `GLib.idle_add`, and the tests pass a direct call. The Control Plane
never blocks its main loop on a `ginn` child: a read runs on `ReadWorker`, and an operation runs
on its own thread through `Operation`.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Sequence
from typing import Final, TypeVar

from ginn.gui.backend import CliResult, CliRunner

T = TypeVar("T")

Dispatch = Callable[[Callable[[], None]], None]
"""Runs a callable on the thread that owns the widgets. It returns at once."""

_STOP: Final = None


class ReadWorker:
    """Runs reads one after another on one thread, so their replies arrive in the order sent.

    A read is a `StateReader` call. Its runner is not safe to share between two threads, and one
    thread keeps the request numbers of `RealmListing` increasing (DR-031). `submit` returns at
    once. Its callbacks run through `dispatch`, on the main loop.
    """

    def __init__(self, dispatch: Dispatch) -> None:
        self._dispatch = dispatch
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._lock = threading.Lock()

    def submit(
        self,
        work: Callable[[], T],
        on_done: Callable[[T], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        """Queue `work`. Then `on_done` gets its value, or `on_error` gets what it raised.

        A read submitted after `close` is dropped: a view may still ask while the window goes away.
        A reply that is ready after `close` is dropped too.
        """

        def job() -> None:
            try:
                value = work()
            except Exception as exc:
                error: BaseException = exc
                self._reply(lambda: on_error(error))
                return
            self._reply(lambda: on_done(value))

        with self._lock:
            if self._closed:
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="ginn-reads", daemon=True)
                self._thread.start()
            self._queue.put(job)

    def close(self) -> None:
        """Drop the queued reads and stop the thread after the read it runs now.

        It does not wait for the thread, because the close runs on the main loop and a read may be
        slow. The thread ends by itself, and its reply is dropped. Calling it twice is safe.
        """
        with self._lock:
            thread = self._thread
            self._thread = None
            self._closed = True
            if thread is not None:
                self._drop_queued()
                self._queue.put(_STOP)

    def _drop_queued(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _reply(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._closed:
                return
        self._dispatch(callback)

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is _STOP:
                return
            job()


class Operation:
    """Runs `calls` in order, each as `ginn --json <args>`, on a thread of its own (FR-107).

    `on_progress` gets the text of each progress line as the child prints it. `on_finished` runs
    once, with the results so far, the exception that stopped the run or None, and whether the run
    was cancelled before its last call. A run stops after the first call that fails, and after a
    cancel. A cancel sends SIGINT to the child that runs now (`CliRunner.cancel`); calls that have
    not started are dropped.
    """

    def __init__(
        self,
        program: Sequence[str],
        calls: Sequence[Sequence[str]],
        *,
        dispatch: Dispatch,
        on_progress: Callable[[str], None],
        on_finished: Callable[[tuple[CliResult, ...], BaseException | None, bool], None],
        env: dict[str, str] | None = None,
    ) -> None:
        self._program = tuple(program)
        self._calls = tuple(tuple(args) for args in calls)
        self._dispatch = dispatch
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._env = env
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._runner: CliRunner | None = None
        self._thread: threading.Thread | None = None

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()

    def start(self) -> None:
        """Start the thread. Calling it a second time does nothing."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="ginn-operation", daemon=True)
            self._thread.start()

    def cancel(self) -> bool:
        """Stop the run and interrupt the running child. False when no child is running."""
        self._stop.set()
        with self._lock:
            runner = self._runner
        return False if runner is None else runner.cancel()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the thread has ended. Tests use it; the views never wait."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _progress(self, text: str) -> None:
        self._dispatch(lambda: self._on_progress(text))

    def _run(self) -> None:
        results: list[CliResult] = []
        failure: BaseException | None = None
        for args in self._calls:
            runner = CliRunner(self._program, env=self._env)
            with self._lock:
                if self._stop.is_set():
                    break
                self._runner = runner
            try:
                result = runner.run(args, on_progress=self._progress)
            except Exception as exc:
                failure = exc
                break
            finally:
                with self._lock:
                    self._runner = None
            results.append(result)
            if not result.ok:
                break
        final = tuple(results)
        # A cancel between two calls stops the run with fewer results than calls.
        cancelled = self._stop.is_set() and len(final) < len(self._calls)
        self._dispatch(lambda: self._on_finished(final, failure, cancelled))
