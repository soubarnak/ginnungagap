# SPDX-License-Identifier: Apache-2.0
"""A probe of the /proc/<pid>/environ read that the token kill helper makes (FR-124).

native/ginn-exec-kill.c reads the environ of every process in a realm's cgroup. A process whose
environ cannot be read is left running and reported as GINN-W-RELAY-ORPHAN. Some containers refuse
that read even to a child of the same user, so the tests that need a token kill ask first, and skip
with the errno they got when the read is refused.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time

import pytest

PROBE_MARK = "probe-environ"
_SLEEP = "import time; time.sleep(60)"
_ENTRY = f"GINN_EXEC_TOKEN={PROBE_MARK}".encode() + b"\0"
PROBE_WAIT_S = 5.0
"""How long the probe waits for the child's environment. exec releases the start's pipe before the
kernel copies the environment into the new image, so an early read is empty, not refused."""


def require_environ_readable() -> None:
    """Skip the calling test unless this process can read the environ of a child it starts.

    The child starts with a GINN_EXEC_TOKEN entry, as the kill tests' tokened sleepers do, and the
    read is the open and the read the helper makes. A refusal skips with the errno it returned.
    """
    child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _SLEEP], env={"GINN_EXEC_TOKEN": PROBE_MARK}
    )
    try:
        reason = _unreadable_reason(child.pid)
    finally:
        child.kill()
        child.wait()
    if reason is not None:
        pytest.skip(f"{reason}; the token kill cannot read the realm's processes here (FR-124)")


def _unreadable_reason(pid: int) -> str | None:
    """None when the environ of `pid` holds the probe's entry. Otherwise the reason it does not."""
    path = f"/proc/{pid}/environ"
    deadline = time.monotonic() + PROBE_WAIT_S
    while True:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as exc:
            name = errno.errorcode.get(exc.errno or 0, str(exc.errno))
            return f"this container refuses the open of {path} of a child ({name})"
        try:
            data = os.read(fd, 65536)
        except OSError as exc:
            name = errno.errorcode.get(exc.errno or 0, str(exc.errno))
            return f"this container refuses the read of {path} of a child ({name})"
        finally:
            os.close(fd)
        if _ENTRY in data:
            return None
        if time.monotonic() >= deadline:
            return f"the environ at {path} of a child lacks the probe's entry"
        time.sleep(0.02)
