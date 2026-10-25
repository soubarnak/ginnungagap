# SPDX-License-Identifier: Apache-2.0
"""Stand-in helper processes for the lifecycle tests (FR-067, FR-068).

`spawn_sleeper` starts `sleep` in its own session, as ginn starts a helper: it leads its group, and
`stop_group` must reach it. `SleepChild` is a `Child` whose stop goes through the group, and
`SleepLauncher` is a `Launcher` that starts one sleeper per call and records each argv. The test
owns every sleeper it starts: `SleepLauncher.close` stops the ones that are left.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from typing import Final

from ginn.security._proc import Proc, stop_group

SLEEP_SECONDS: Final = 600


def sleep_binary() -> str | None:
    """The absolute path of `sleep`, or None when the host has none."""
    return shutil.which("sleep")


def spawn_sleeper() -> subprocess.Popen[bytes]:
    """Start `sleep` as a session leader. Callers skip first when `sleep_binary()` is None."""
    sleep = sleep_binary()
    if sleep is None:
        raise RuntimeError("sleep is needed for the lifecycle tests")
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sleep, str(SLEEP_SECONDS)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class SleepChild:
    """A `Child` over a sleeper. Its record is taken at start, and stop uses the group."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self.popen = popen
        record = Proc.of(popen.pid)
        if record is None:
            raise RuntimeError("the sleeper exited before it was recorded")
        self.proc = record

    def running(self) -> bool:
        return self.popen.poll() is None and self.proc.alive()

    def stop(self) -> None:
        stop_group(self.proc)


class SleepLauncher:
    """A `Launcher` that starts a sleeper per call and records each argv."""

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []
        self.children: list[SleepChild] = []

    def __call__(self, argv: Sequence[str]) -> SleepChild:
        self.argvs.append(tuple(argv))
        child = SleepChild(spawn_sleeper())
        self.children.append(child)
        return child

    def close(self) -> None:
        for child in self.children:
            child.stop()


class NoLaunch:
    """A `Launcher` that fails the test if anything starts. It proves a start did not happen."""

    def __call__(self, argv: Sequence[str]) -> SleepChild:
        raise AssertionError(f"no helper may start here, but got {tuple(argv)!r}")


def group_members(pgid: int) -> list[int]:
    """The pids of the live (not zombie) processes whose process group is `pgid`. Linux /proc."""
    members = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="ascii", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        tail = text.rpartition(")")[2].split()
        if len(tail) > 2 and tail[2] == str(pgid) and tail[0] not in ("Z", "X"):
            members.append(int(name))
    return members
