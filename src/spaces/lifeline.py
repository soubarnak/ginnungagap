"""A lifeline between the launcher and a helper process that must not outlive it.

The launcher keeps the write end of a pipe open and passes the read end to the helper
(`--death-fd N`). When the launcher dies, even from SIGKILL, the kernel closes the write end, the
read end reports POLLHUP and the helper exits. `PR_SET_PDEATHSIG` cannot do this job: it fires when
the *thread* that forked the helper exits, and the launcher starts helpers from worker threads.
"""

from __future__ import annotations

import os
import subprocess
import weakref


def open_lifeline() -> tuple[int, int]:
    """Return (read end for the helper, write end for the launcher), both close-on-exec."""

    return os.pipe2(os.O_CLOEXEC)


def _close(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def hold(process: subprocess.Popen[bytes], write_end: int) -> None:
    """Keep WRITE_END open until PROCESS is released, or collected after it exited, or the launcher exits.

    (A `Popen` that is dropped while its child still runs stays alive in `subprocess._active`, so
    dropping it does not stop a running helper; the launcher's death or `release` does.)

    The descriptor is close-on-exec, and `subprocess` closes everything else in its children, so
    no other helper inherits it and keeps the helper that it belongs to alive.
    """

    process._spaces_lifeline = weakref.finalize(process, _close, write_end)  # type: ignore[attr-defined]


def release(process: subprocess.Popen[bytes]) -> None:
    """Close the launcher's end now; the helper sees POLLHUP and stops."""

    finalizer = getattr(process, "_spaces_lifeline", None)
    if finalizer is not None:
        finalizer()
