#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""NFR-009 (T10.4): export and unexport of a desktop entry happen within 5 s of the transaction.

FR-044 and FR-045 name the entry `ginn-<realm>-<app>.desktop` in the host applications directory.
One run is one install and one removal of a package that ships that entry:

    export latency    from the return of the install command to the entry existing
    unexport latency  from the return of the remove command to the entry gone

An entry that already exists when the command returns has a latency of 0. A run in which the entry
does not appear (or go) within --timeout counts at the timeout, which is above the limit. One
timeout fails the verdict only when it reaches the p95 rank (with 20 runs, the 19th value, so two
or more). The verdict is the p95 (nearest rank) of the runs, 20 as the SRS asks. Fewer runs keep
the verdict and add a note.

Before each install the entry must be absent, which is waited for up to --timeout. If a command
fails or that wait runs out, the series stops: the data collected is kept and printed, a PASS
becomes NOT RUN and a FAIL stays a FAIL. When the stop leaves the package installed, one
best-effort remove runs first.

Two ways to run the transaction (--via):
  ginn    `ginn install REALM PKG` and `ginn remove REALM PKG` (default)
  native  the realm's own package manager, as FR-044 AC-1 says:
          `ginn exec --root REALM -- PM... PKG` with --pm-install and --pm-remove, for
          example "pacman -S --noconfirm" and "pacman -R --noconfirm". This path needs ginn-watch.

The timing is the presence of the file, not the moment a desktop environment shows the entry (SRS
R-007). The script does not install anything but --package, and it removes it again each run. An
entry that exists before the first run stops the script with NOT RUN.

Usage: python3 -I tools/measure/nfr-009-export-latency.py --realm REALM --package PKG --app APP
           [--runs 20] [--timeout 30] [--via ginn|native] [--applications DIR] [--ginn "ginn"]
Exit status: see tools/measure/emit.py.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import emit  # noqa: E402

LIMIT_S = 5.0
RUNS_REQUIRED = 20
POLL_S = 0.01
COMMAND_TIMEOUT_S = 1800


class RunError(RuntimeError):
    """A transaction failed or the starting state was wrong. The script stops with NOT RUN."""


def default_applications() -> Path:
    base = os.environ.get("XDG_DATA_HOME", "")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".local" / "share"
    return root / "applications"


def transaction(argv: Sequence[str]) -> None:
    """Run one install or remove command to its end."""
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            list(argv), capture_output=True, text=True, check=False, timeout=COMMAND_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise RunError(f"{argv[0]} did not run: {err}") from err
    if done.returncode != 0:
        raise RunError(f"{' '.join(argv)} exited {done.returncode}")


def wait_until(condition: Callable[[], bool], timeout: float) -> tuple[float, bool]:
    """Seconds until `condition` holds, polled from now, and whether it held within `timeout`."""
    start = time.monotonic()
    while True:
        if condition():
            return time.monotonic() - start, True
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            return timeout, False
        time.sleep(POLL_S)


@dataclass
class Series:
    """What the runs collected. `error` is set when they stopped early; the data is kept."""

    exported: list[float] = field(default_factory=list)
    unexported: list[float] = field(default_factory=list)
    export_timeouts: int = 0
    unexport_timeouts: int = 0
    error: str | None = None


def measure(
    install: Sequence[str],
    remove: Sequence[str],
    entry: Path,
    runs: int,
    timeout: float,
) -> Series:
    """Export and unexport latencies of `runs` install/remove pairs, and the timeout counts.

    Before each install the entry must be absent (it is waited for up to `timeout`). If the series
    stops with the package installed, one best-effort remove runs, and what was collected stays.
    """
    series = Series()
    if entry.exists():
        series.error = f"{entry} exists before the first run; remove it first"
        return series
    installed = False
    try:
        for _ in range(runs):
            _, absent = wait_until(lambda: not entry.exists(), timeout)
            if not absent:
                raise RunError(f"{entry} still exists before an install")
            transaction(install)
            installed = True
            seconds, held = wait_until(entry.exists, timeout)
            series.exported.append(seconds)
            series.export_timeouts += 0 if held else 1
            transaction(remove)
            installed = False
            seconds, held = wait_until(lambda: not entry.exists(), timeout)
            series.unexported.append(seconds)
            series.unexport_timeouts += 0 if held else 1
    except RunError as err:
        series.error = str(err)
    finally:
        if installed:
            try:
                transaction(remove)
            except RunError as err:
                note = f"the package may still be installed: {err}"
                series.error = f"{series.error}; {note}" if series.error else note
    return series


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NFR-009: export and unexport latency.")
    parser.add_argument("--realm", required=True)
    parser.add_argument("--package", required=True, help="a package that ships one desktop entry")
    parser.add_argument("--app", required=True, help="the app part of ginn-<realm>-<app>.desktop")
    parser.add_argument("--runs", type=int, default=RUNS_REQUIRED)
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait per event")
    parser.add_argument("--via", choices=("ginn", "native"), default="ginn")
    parser.add_argument("--pm-install", default="", help="native install command, before PKG")
    parser.add_argument("--pm-remove", default="", help="native remove command, before PKG")
    parser.add_argument("--applications", type=Path, help="host applications directory")
    parser.add_argument("--ginn", default="ginn", help="the ginn program (split like a shell word)")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.timeout <= LIMIT_S:
        parser.error("--runs must be at least 1 and --timeout above 5 s")
    ginn = shlex.split(args.ginn)
    if not ginn:
        parser.error("--ginn names no program")
    if args.via == "ginn":
        install = [*ginn, "install", args.realm, args.package]
        remove = [*ginn, "remove", args.realm, args.package]
    else:
        pm_install = shlex.split(args.pm_install)
        pm_remove = shlex.split(args.pm_remove)
        if not pm_install or not pm_remove:
            parser.error("--via native needs --pm-install and --pm-remove")
        install = [*ginn, "exec", "--root", args.realm, "--", *pm_install, args.package]
        remove = [*ginn, "exec", "--root", args.realm, "--", *pm_remove, args.package]

    applications = args.applications or default_applications()
    entry = applications / f"ginn-{args.realm}-{args.app}.desktop"
    target = f"p95 <= {LIMIT_S:g} s over {RUNS_REQUIRED} runs"
    via = f"via {args.via}"
    series = measure(install, remove, entry, args.runs, args.timeout)

    note = f"{via}; file presence, not the menu of a desktop environment"
    codes: list[int] = []
    for kind, values, late in (
        ("export", series.exported, series.export_timeouts),
        ("unexport", series.unexported, series.unexport_timeouts),
    ):
        result = emit.p95_line(
            "NFR-009",
            f"{kind} latency of {entry.name}",
            values,
            unit="s",
            target=target,
            limit=LIMIT_S,
            runs_required=RUNS_REQUIRED,
            timeouts=late,
            note=note if values else series.error,
        )
        if series.error is not None and values:
            result = emit.cap_at_not_run(result, f"stopped early: {series.error}")
        codes.append(emit.emit(result))
    return emit.worst(codes)


if __name__ == "__main__":
    raise SystemExit(main())
