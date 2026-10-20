#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""NFR-010 (T10.4): the Control Plane shows a CLI change within 1 s of the command returning.

The SRS method is a scripted CLI operation with a GUI state probe, 50 runs. The GUI has no probe
hook of its own: nothing in src/ginn/gui reports when a window drew a state. So this script builds
the part of the Control Plane that decides staleness, without a window. It creates a
`ginn.gui.control.Control` over the real `GioRefreshSource` (Gio.FileMonitor on the realm records,
roadmap section 2) and the real `StateReader` (`ginn --json list`), and subscribes to its model, as
the views do. One run is:

    1. read the realm's state from the dashboard model (running or stopped)
    2. run `ginn --json stop|start REALM` (the scripted CLI operation) on a worker thread, while
       the GLib main loop keeps serving the monitor and the reads
    3. staleness = from the return of that command to the moment the dashboard row shows the
       new state; 0 when it already showed it

The verdict is the p95 (nearest rank) of the runs against 1 s. The wait for the new state starts
when the command returns and ends after --timeout; a run that times out counts at the timeout. One
timeout fails the verdict only when it reaches the p95 rank (with 50 runs, the 48th value, so three
or more). The command itself has its own larger cap, and a command that fails or exceeds it stops
the script. Commands never overlap on the realm: the next one starts after the previous returned.
A series that stopped early cannot PASS: it is NOT RUN, and a FAIL stays a FAIL.

The runs alternate stop and start, so the realm
is back where it began after an even count. A realm that is neither running nor stopped is NOT RUN.

The gap: this measures file notification, the `ginn --json list` read, and the model update. It does
not measure GTK drawing, and it does not run the window. The note on the line says so. A real probe
would need a hook in the application (for example a debug log of each applied listing); that is not
built, because FR-100 to FR-107 do not ask for one.

It needs PyGObject (`gi`) and Gio, but no display. The operation starts and stops the realm
named, so use a test realm. Podman is called only by ginn.

Usage: python3 -I tools/measure/nfr-010-gui-staleness.py --realm REALM [--runs 50] [--timeout 10]
           [--ginn "ginn"]
Exit status: see tools/measure/emit.py.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import emit  # noqa: E402

LIMIT_S = 1.0
RUNS_REQUIRED = 50
LOAD_TIMEOUT_S = 30.0
COMMAND_CAP_S = 600
CAP_EXCEEDED = -2
METRIC = "Control Plane staleness after a CLI state change (headless Control, Gio monitor)"
TARGET = f"p95 <= {LIMIT_S:g} s over {RUNS_REQUIRED} runs"
GAP_NOTE = (
    "no GUI probe hook exists; measures file monitor, 'ginn --json list' and the model, "
    "not GTK drawing"
)


class Probe:
    """The state machine of one probe session. It runs on the GLib main loop thread."""

    def __init__(
        self, glib: Any, control: Any, program: list[str], realm: str, runs: int, timeout: float
    ) -> None:
        self.glib = glib
        self.control = control
        self.program = program
        self.realm = realm
        self.runs = runs
        self.timeout = timeout
        self.loop = glib.MainLoop()
        self.values: list[float] = []
        self.timeouts = 0
        self.error: str | None = None
        self._run = 0
        self._expected: str | None = None
        self._returned_at: float | None = None
        self._seen_early = False
        self._waiting = False
        self._loaded = False

    # --- the model side -----------------------------------------------------------------------

    def _state(self) -> str | None:
        row = self.control.dashboard.row(self.realm)
        return None if row is None else row.state

    def on_model(self) -> None:
        if not self._loaded:
            if self.control.dashboard.loaded:
                self._loaded = True
                self.glib.idle_add(self._first)
            return
        if self._waiting and self._state() == self._expected:
            if self._returned_at is None:
                self._seen_early = True  # shown before the command returned: staleness 0
            else:
                self._done(time.monotonic() - self._returned_at, timed_out=False)

    # --- the run side -------------------------------------------------------------------------

    def _first(self) -> bool:
        if self._state() not in ("running", "stopped"):
            self.error = f"realm {self.realm} is {self._state()!r}, not running or stopped"
            self.loop.quit()
        else:
            self._begin()
        return False

    def _begin(self) -> None:
        verb, expected = ("stop", "stopped") if self._state() == "running" else ("start", "running")
        self._expected = expected
        self._returned_at = None
        self._seen_early = False
        self._waiting = True
        run = self._run
        threading.Thread(target=self._command, args=(verb, run), daemon=True).start()

    def _command(self, verb: str, run: int) -> None:
        argv = [*self.program, "--json", verb, self.realm]
        try:
            code = subprocess.run(  # noqa: S603 - argv list, no shell
                argv, capture_output=True, text=True, check=False, timeout=COMMAND_CAP_S
            ).returncode
        except subprocess.TimeoutExpired:
            code = CAP_EXCEEDED
        except OSError:
            code = -1
        self.glib.idle_add(self._returned, run, code)

    def _returned(self, run: int, code: int) -> bool:
        if run != self._run or not self._waiting:
            return False
        if code == CAP_EXCEEDED:
            self.error = f"ginn --json took more than {COMMAND_CAP_S} s on run {run + 1}"
            self.loop.quit()
            return False
        if code != 0:
            self.error = f"ginn --json exited {code} on run {run + 1}"
            self.loop.quit()
            return False
        self._returned_at = time.monotonic()
        self.glib.timeout_add(int(self.timeout * 1000), self._on_timeout, run)
        if self._seen_early or self._state() == self._expected:
            self._done(0.0, timed_out=False)
        return False

    def _on_timeout(self, run: int) -> bool:
        if run == self._run and self._waiting:
            self._done(self.timeout, timed_out=True)
        return False

    def _done(self, seconds: float, *, timed_out: bool) -> None:
        self._waiting = False
        self.values.append(seconds)
        self.timeouts += 1 if timed_out else 0
        self._run += 1
        if self._run >= self.runs:
            self.loop.quit()
        else:
            self.glib.idle_add(self._next)

    def _next(self) -> bool:
        # After a timeout the new state may still be on its way; let the model settle first.
        if self._state() not in ("running", "stopped"):
            self.glib.timeout_add(200, self._next)
            return False
        self._begin()
        return False

    def run(self) -> None:
        def stop_if_unloaded() -> bool:
            if not self._loaded:
                self.error = f"the dashboard did not load within {LOAD_TIMEOUT_S:g} s"
                self.loop.quit()
            return False

        self.glib.timeout_add(int(LOAD_TIMEOUT_S * 1000), stop_if_unloaded)
        self.control.subscribe(self.on_model)
        self.control.start_watching()
        self.loop.run()


def not_run(reason: str) -> int:
    return emit.emit(emit.line("NFR-010", METRIC, None, "s", TARGET, emit.NOT_RUN, reason))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NFR-010: Control Plane staleness.")
    parser.add_argument("--realm", required=True, help="a test realm that is running or stopped")
    parser.add_argument("--runs", type=int, default=RUNS_REQUIRED)
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds to wait per run")
    parser.add_argument("--ginn", default="ginn", help="the ginn program (split like a shell word)")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.timeout <= LIMIT_S:
        parser.error("--runs must be at least 1 and --timeout above 1 s")
    program = shlex.split(args.ginn)
    if not program:
        parser.error("--ginn names no program")

    try:
        import gi  # type: ignore[import-untyped]

        gi.require_version("Gio", "2.0")
        from gi.repository import GLib  # type: ignore[import-untyped]

        from ginn.errors import GinnError
        from ginn.gui._gio import GioRefreshSource
        from ginn.gui.control import Control
        from ginn.store.paths import OwnedPaths
    except (ImportError, ValueError) as err:
        return not_run(f"PyGObject with Gio is needed: {err}")

    try:
        paths = OwnedPaths.from_env()
    except GinnError as err:
        return not_run(f"Owned Paths unavailable: {err}")

    def dispatch(work: Any) -> None:
        GLib.idle_add(lambda: (work(), False)[1])

    control = Control(paths=paths, dispatch=dispatch, make_source=GioRefreshSource, program=program)
    probe = Probe(GLib, control, program, args.realm, args.runs, args.timeout)
    try:
        probe.run()
    finally:
        control.close()
    if probe.error is not None and not probe.values:
        return not_run(probe.error)
    note = GAP_NOTE
    entry = emit.p95_line(
        "NFR-010",
        METRIC,
        probe.values,
        unit="s",
        target=TARGET,
        limit=LIMIT_S,
        runs_required=RUNS_REQUIRED,
        timeouts=probe.timeouts,
        note=note,
    )
    if probe.error is not None:
        entry = emit.cap_at_not_run(entry, f"stopped early: {probe.error}")
    return emit.emit(entry)


if __name__ == "__main__":
    raise SystemExit(main())
