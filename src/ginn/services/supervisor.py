# SPDX-License-Identifier: Apache-2.0
"""The `sv` control of a services-on realm's runit service (FR-081, ADR-003, AS-028).

ginn never runs runsv itself. The user's runsvdir starts one runsv for each realm directory under
SV_DIR (runit_user.py), and `sv` talks to that runsv through its control FIFO. This module runs
`sv` as an argv list with an absolute path, and it reads `sv status` into a typed RunitStatus.

Waits. `up` and `down` run `sv -w <secs>`, so sv waits for the command to take effect. runsvdir
scans its directory about every 5 s, so a new realm directory has no runsv for a while. `up` waits
first, bounded by SUPERVISE_WAIT_S, until runsv has created supervise/ok. The output of the wait is
the runit's own: `ok: run: <dir>: (pid N) 1s` when the command took effect, and `timeout: ...` when
it did not. A success of `sv -w up` means run has a pid, not that the realm runs. The caller checks
the record for that (ops/lifecycle.py).

Output, as runit 2.x prints it (probed on this host, and tests/unit/test_services_supervisor.py
pins the same forms): `run: <dir>: (pid N) Ns` while run runs, `down: <dir>: Ns, normally up` when
the service is down and no `down` file exists, and `fail: <dir>: runsv not running` or `warning:
<dir>: unable to open supervise/ok: file does not exist` when no runsv supervises the directory.
A failed command exits 1 (one per failing service).

Failures. Only the output that proves runsv is not running the directory is GINN-E-SUPERVISOR-DOWN
(GONE_MARKERS). That is the one answer after which a caller may stop the realm with podman, since
nothing restarts it. A command that the wait ran out on is GINN-E-SERVICE-TIMEOUT. Any other
failure,
such as a control FIFO that cannot be opened while runsv may still run, is GINN-E-SERVICE-CONTROL:
the
caller must not assume that runsv is gone.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, NamedTuple

from ginn.domain import RealmName
from ginn.errors import GinnError, make
from ginn.services.runit_user import service_dir
from ginn.store.paths import OwnedPaths

RUN: Final = "run"
DOWN: Final = "down"
FINISH: Final = "finish"
UNKNOWN: Final = "unknown"
SUPERVISE_DIR: Final = "supervise"
SUPERVISE_OK: Final = "ok"
"""The FIFO that runsv creates when it starts, which sv opens (sv(8))."""
UP_WAIT_S: Final = 10
"""Seconds `sv -w up` waits for run to get a pid (sv(8)). The realm's own wait is longer."""
DOWN_WAIT_S: Final = 60
"""Seconds `sv -w down` waits for run and finish to end: ginn-wait stops the container first."""
SUPERVISE_WAIT_S: Final = 10.0
"""Seconds `up` waits for runsvdir to start a runsv for a new directory (its scan is about 5 s)."""
SUPERVISE_POLL_S: Final = 0.25
COMMAND_MARGIN_S: Final = 15
"""Seconds added to the sv wait before ginn gives up on the process itself."""
STATUS_TIMEOUT_S: Final = 10
TIMED_OUT: Final = -1
CANNOT_RUN: Final = -2
GONE_MARKERS: Final = (
    "runsv not running",
    "unable to open supervise/ok",
    "unable to change to service directory",
)
"""sv's words for a directory that no runsv supervises or that is gone (probed on runit 2.x). Only
these prove that runsv does not run the realm (GINN-E-SUPERVISOR-DOWN)."""
SV_FALLBACKS: Final = ("/usr/sbin/sv", "/sbin/sv", "/usr/bin/sv", "/bin/sv")
SV_ENV: Final = {"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
"""The environment sv runs with. The output is in the C locale, so its words stay the same."""

_STATUS_LINE: Final = re.compile(
    r"^(?P<state>run|down|finish): (?P<dir>.+?): "
    r"(?:\(pid (?P<pid>\d+)\) )?(?P<seconds>\d+)s(?P<rest>[^;]*)"
)


class Result(NamedTuple):
    """One finished sv process: its exit status (or TIMED_OUT, CANNOT_RUN) and its output."""

    code: int
    out: str
    err: str


Runner = Callable[[Sequence[str], float], Result]
"""Runs argv for at most `timeout` seconds and returns its result. Tests replace it."""


def _run_sv(argv: Sequence[str], timeout: float) -> Result:
    """Run argv as a process, with no shell, and return what it printed (FR-081)."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell (NFR-SEC-005)
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(SV_ENV),
        )
    except subprocess.TimeoutExpired:
        return Result(TIMED_OUT, "", "")
    except OSError as exc:
        return Result(CANNOT_RUN, "", exc.strerror or str(exc))
    return Result(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class SupervisorHooks:
    """The seams of the sv control. The defaults are the real ones.

    `sv` is the absolute path of sv; None looks it up. `run` runs sv. `clock` and `sleep` bound
    the wait for runsv to pick up a directory. `up_wait_s` and `down_wait_s` are the sv waits.
    """

    sv: str | None = None
    run: Runner = _run_sv
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    up_wait_s: int = UP_WAIT_S
    down_wait_s: int = DOWN_WAIT_S


@dataclass(frozen=True)
class RunitStatus:
    """The runit state of one realm directory, from `sv status` (FR-081 AC-1).

    `state` is run, down, finish or unknown. `pid` is set while run runs, and `seconds` is the
    time in that state. `normally_up` is False when the `down` file exists (autostart off).
    `want_up` is set while runsv means to start run: a `down` service that is restarting after a
    crash says `want up`, and only a `down` service that does not want up has been given up.
    `want_down` is set once `sv down` has been sent, so the end of run is a user stop (CL-24).
    """

    state: str
    pid: int | None = None
    seconds: int | None = None
    normally_up: bool = False
    want_up: bool = False
    want_down: bool = False

    def describe(self) -> str:
        """The state for `ginn status`, for example `run, pid 4321, 12 s`."""
        if self.state == UNKNOWN:
            return UNKNOWN
        parts = [self.state]
        if self.pid is not None:
            parts.append(f"pid {self.pid}")
        if self.seconds is not None:
            parts.append(f"{self.seconds} s")
        return ", ".join(parts)


def parse_status(text: str) -> RunitStatus:
    """Read the first line of `sv status` output. Text that is not a state line is UNKNOWN."""
    lines = text.splitlines()
    match = _STATUS_LINE.match(lines[0]) if lines else None
    if match is None:
        return RunitStatus(UNKNOWN)
    pid = match["pid"]
    return RunitStatus(
        state=match["state"],
        pid=int(pid) if pid is not None else None,
        seconds=int(match["seconds"]),
        normally_up=", normally up" in match["rest"],
        want_up=", want up" in match["rest"],
        want_down=", want down" in match["rest"],
    )


def sv_path(hooks: SupervisorHooks, name: RealmName) -> str:
    """The absolute path of sv: the hook's, else the one on PATH, else a standard location."""
    if hooks.sv is not None:
        return hooks.sv
    found = shutil.which("sv")
    if found is not None and os.path.isabs(found):
        return found
    for candidate in SV_FALLBACKS:
        if os.access(candidate, os.X_OK):
            return candidate
    raise _control(name, "status", "sv (runit) is not installed")


def is_supervised(paths: OwnedPaths, name: RealmName) -> bool:
    """True when a runsv watches the realm's directory: runsv created supervise/ok."""
    return os.path.exists(service_dir(paths, name) / SUPERVISE_DIR / SUPERVISE_OK)


def wait_supervised(paths: OwnedPaths, name: RealmName, hooks: SupervisorHooks) -> None:
    """Wait, bounded, until runsvdir has started a runsv for the realm's directory (FR-081).

    A runtime directory that is missing means the user's supervisor is not running (ADR-004), so
    there is nothing to wait for. Otherwise the wait ends after SUPERVISE_WAIT_S with an error.
    """
    if not paths.runtime.is_dir():
        raise _down(name, f"the runtime directory {paths.runtime} is missing")
    deadline = hooks.clock() + SUPERVISE_WAIT_S
    while not is_supervised(paths, name):
        if hooks.clock() >= deadline:
            raise _down(
                name,
                f"runsvdir did not start a runsv for {service_dir(paths, name)} within "
                f"{SUPERVISE_WAIT_S:g} s",
            )
        hooks.sleep(SUPERVISE_POLL_S)


def up(paths: OwnedPaths, name: RealmName, *, hooks: SupervisorHooks) -> None:
    """Start the realm's runit service, once, even when its `down` file exists (FR-081, DR-012).

    `sv up` sends the `u` control, which starts run whatever the `down` file says, and runsv then
    restarts run whenever it ends. The wait for the directory comes first.
    """
    wait_supervised(paths, name, hooks)
    _command(paths, name, "up", hooks.up_wait_s, "start", hooks)


def down(paths: OwnedPaths, name: RealmName, *, hooks: SupervisorHooks) -> None:
    """Stop the realm's runit service and do not restart it (FR-016, FR-081).

    `sv down` sends TERM, and ginn-wait then stops the container and exits 0, so the finish script
    records the realm stopped (services/supervise.py). sv waits until the service is down.
    """
    _command(paths, name, "down", hooks.down_wait_s, "stop", hooks)


def stop_if_supervised(paths: OwnedPaths, name: RealmName, *, hooks: SupervisorHooks) -> None:
    """`sv down` for the realm's runit service when a runsv watches it, and nothing else (FR-081).

    A runsv that is proven gone (GINN-E-SUPERVISOR-DOWN) is passed over: nothing restarts the realm
    then, and the caller stops the container itself. Every other failure, and a timeout, is raised,
    so the caller stops nothing while a runsv may still restart the realm (AS-028, FR-081 AC-2).
    """
    if not is_supervised(paths, name):
        return
    try:
        down(paths, name, hooks=hooks)
    except GinnError as err:
        if err.code != "GINN-E-SUPERVISOR-DOWN":
            raise


def status(paths: OwnedPaths, name: RealmName, *, hooks: SupervisorHooks) -> RunitStatus:
    """Read the runit state of the realm's directory with `sv status` (FR-081 AC-1).

    Read-only. A directory that runsv is known not to supervise raises GINN-E-SUPERVISOR-DOWN, and
    any other failure raises GINN-E-SERVICE-CONTROL. Output that is not a state line is UNKNOWN.
    """
    argv = [sv_path(hooks, name), "status", str(service_dir(paths, name))]
    result = hooks.run(argv, STATUS_TIMEOUT_S)
    if result.code == CANNOT_RUN:
        raise _control(name, "status", f"sv cannot run: {result.err or 'no detail'}")
    if result.code != 0:
        raise _failed(name, "status", result)
    return parse_status(result.out)


def _command(
    paths: OwnedPaths,
    name: RealmName,
    verb: str,
    wait_s: int,
    action: str,
    hooks: SupervisorHooks,
) -> None:
    """Run `sv -w <wait> <verb> <dir>` and map its failure to a catalogue error."""
    argv = [sv_path(hooks, name), "-w", str(wait_s), verb, str(service_dir(paths, name))]
    result = hooks.run(argv, wait_s + COMMAND_MARGIN_S)
    if result.code == 0:
        return
    if result.code == CANNOT_RUN:
        raise _control(name, action, f"sv cannot run: {result.err or 'no detail'}")
    if result.code == TIMED_OUT or "timeout:" in result.out + result.err:
        err = make(
            "GINN-E-SERVICE-TIMEOUT",
            realm=name.value,
            action=action,
            seconds=wait_s,
            detail=_detail(result) or "sv did not finish in time",
        )
        raise err
    raise _failed(name, action, result)


def _failed(name: RealmName, action: str, result: Result) -> GinnError:
    """A failed sv command: GINN-E-SUPERVISOR-DOWN when its output proves runsv is gone, else
    GINN-E-SERVICE-CONTROL, which a caller must not read as "runsv is gone"."""
    text = result.out + result.err
    detail = _detail(result) or "sv failed"
    if any(marker in text for marker in GONE_MARKERS):
        return _down(name, detail)
    return _control(name, action, detail)


def _detail(result: Result) -> str:
    """The first line of sv's output, for a message."""
    for line in (result.err + "\n" + result.out).splitlines():
        if line.strip():
            return line.strip()
    return ""


def _down(name: RealmName, detail: str) -> GinnError:
    return make("GINN-E-SUPERVISOR-DOWN", realm=name.value, detail=detail)


def _control(name: RealmName, action: str, detail: str) -> GinnError:
    return make("GINN-E-SERVICE-CONTROL", realm=name.value, action=action, detail=detail)
