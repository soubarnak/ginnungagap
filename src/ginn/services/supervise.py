# SPDX-License-Identifier: Apache-2.0
"""Crash window, restart delay and runsv control for a services-on realm (AS-028, NFR-033).

A crash is a run that ended without a stop request: ginn-wait exits 1 when conmon dies, 3 to 5 on
its own errors, or a signal ended it (runsv(8): finish gets -1 then). The finish script keeps the
last CRASH_LIMIT crash times in the realm's crash log. When CRASH_LIMIT crashes fall within
CRASH_WINDOW_S, the realm is given up: the `down` file is written, and runsv is told not to restart
it. Otherwise finish waits RESTART_DELAY_S, and runsv restarts run inside the 10 s of NFR-033.

The crash log is the supervisor's own bookkeeping, not realm state. A stop request clears it.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from ginn.store.paths import write_bytes_atomic

CRASH_LIMIT: Final = 5
"""Crashes that give a realm up (AS-028)."""
CRASH_WINDOW_S: Final = 60.0
"""Seconds that the last CRASH_LIMIT crashes must span to give up (AS-028)."""
RESTART_DELAY_S: Final = 5.0
"""Seconds finish waits before runsv restarts run after a crash (AS-028, NFR-033)."""
CONTROL_DOWN: Final = b"d"
"""The runsv control byte for `sv down`: stop, and do not restart (runsv(8))."""


def note_crash(prior: Sequence[float], now: float) -> tuple[list[float], bool]:
    """Add one crash at `now` to `prior`. Return the kept crash times and whether to give up.

    Only the last CRASH_LIMIT times are kept. The realm is given up when there are CRASH_LIMIT
    of them and the oldest is within CRASH_WINDOW_S of `now`.
    """
    kept = [*prior, now][-CRASH_LIMIT:]
    give_up = len(kept) == CRASH_LIMIT and now - kept[0] <= CRASH_WINDOW_S
    return kept, give_up


def read_crashes(path: Path) -> list[float]:
    """The crash times in `path`. A missing or unreadable log holds none."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [float(item) for item in data if isinstance(item, int | float)][-CRASH_LIMIT:]


def write_crashes(path: Path, stamps: Sequence[float]) -> None:
    """Write the crash times atomically (DR-035), mode 0600."""
    write_bytes_atomic(path, json.dumps([float(s) for s in stamps]).encode("utf-8"))


def signal_runsv_down(service_dir: Path) -> bool:
    """Write `d` to runsv's control FIFO of `service_dir`, as `sv down` does. True when it wrote.

    runsv reads `d` while it runs the finish script, sets the service to down, and does not restart
    run after finish exits. A FIFO with no reader (ENXIO) is not an error: no runsv runs the
    service, so nothing restarts it. The FIFO is opened without blocking, so finish never hangs.
    """
    control = service_dir / "supervise" / "control"
    try:
        fd = os.open(control, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ENOENT):
            return False
        raise
    try:
        os.write(fd, CONTROL_DOWN)
    finally:
        os.close(fd)
    return True
