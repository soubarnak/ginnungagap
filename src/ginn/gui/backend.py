# SPDX-License-Identifier: Apache-2.0
"""Control Plane backend: reads and operations through the `ginn --json` CLI (IR-010, FR-106).

`CliRunner` starts `ginn --json <command>` as a child process of the same user, with an argv
list and no shell (NFR-SEC-005). It reads the child's output and returns a typed `CliResult`.
`StateReader` builds the typed views of realm, doctor and integrity output on top of it.
`watch_directories` names the directories whose changes trigger a refresh (DR-030).

The CLI JSON contract is in src/ginn/cli/output.py. In JSON mode:
- stdout holds one JSON object, the result. It is empty on failure, except for `doctor`, which
  prints its report before it fails.
- stderr holds JSON lines. `{"progress": {"step", "message"}}` lines come after each finished step
  of a long operation, and the runner passes their message to `on_progress`. `{"verbose": ...}`
  lines appear only when the caller passes `--verbose`, and they reach `on_progress` too.
  `{"warning": {...}}` lines come before a failure. The last line is `{"error": {...}}` on
  failure, and it carries the GINN-E-* code, the remedy and exit_code.

The GUI never starts a server. Nothing here listens on a socket, so no peer from another user
can connect (NFR-SEC-009). The runner's `on_progress` callback runs on a stderr reader thread of
the runner. The views move each update onto their own main loop.
"""

from __future__ import annotations

import itertools
import json
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from ginn.domain import RealmName
from ginn.store.paths import OwnedPaths

PROGRAM: Final[tuple[str, ...]] = ("ginn",)
HOST_CHANGED_CODE: Final = "GINN-W-HOST-CHANGED"
"""The integrity warning (FR-006, FR-104). The CLI raises it in src/ginn/integrity/state.py."""


class CliLaunchError(RuntimeError):
    """The ginn program could not be started, for example because it is not installed."""


class CliProtocolError(ValueError):
    """The CLI printed JSON that does not match the documented shape."""


@dataclass(frozen=True)
class CliWarning:
    """A GINN-W-* notice. It does not fail the command (FR-006, FR-007)."""

    code: str
    message: str
    remedy: str


@dataclass(frozen=True)
class CliError:
    """The failure object that the CLI prints as the last stderr line (IR-011)."""

    code: str
    message: str
    remedy: str
    exit_code: int
    details: Mapping[str, object]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class CliResult:
    """The outcome of one `ginn --json` run.

    `payload` is the JSON object from stdout, or None when stdout held none. `error` is set when
    stderr ended with an error object. `ok` is true only for exit status 0. `cancelled` is true
    when `CliRunner.cancel` sent SIGINT to this run.
    """

    argv: tuple[str, ...]
    exit_code: int
    payload: dict[str, object] | None
    error: CliError | None
    warnings: tuple[CliWarning, ...]
    stderr: str
    cancelled: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.error is None


def _progress_text(line: str) -> str | None:
    """The text of a progress line on stderr, or None for any other line.

    `{"progress": {"step", "message"}}` is written by the CLI after each step of a create or a
    delete (FR-107). `{"verbose": text}` is written only when the caller asks for --verbose.
    """
    obj = _json_object(line)
    if obj is None:
        return None
    verbose = obj.get("verbose")
    if isinstance(verbose, str):
        return verbose
    progress = obj.get("progress")
    if isinstance(progress, dict):
        message = progress.get("message")
        if isinstance(message, str):
            return message
    return None


def _json_object(text: str) -> dict[str, object] | None:
    """Parse `text` as one JSON object, or return None."""
    try:
        value: object = json.loads(text)
    except ValueError:
        return None
    if isinstance(value, dict):
        return value
    return None


def _warning(obj: object) -> CliWarning:
    if not isinstance(obj, dict):
        raise CliProtocolError("warning is not an object")
    return CliWarning(
        code=_req_str(obj, "code"),
        message=_req_str(obj, "message"),
        remedy=_req_str(obj, "remedy"),
    )


def _error(obj: dict[str, object]) -> CliError:
    details = obj.get("details")
    notes = obj.get("notes")
    exit_code = obj.get("exit_code")
    if not isinstance(details, dict) or not isinstance(notes, list):
        raise CliProtocolError("error object lacks details or notes")
    if not isinstance(exit_code, int):
        raise CliProtocolError("error object lacks an integer exit_code")
    return CliError(
        code=_req_str(obj, "code"),
        message=_req_str(obj, "message"),
        remedy=_req_str(obj, "remedy"),
        exit_code=exit_code,
        details=details,
        notes=tuple(str(note) for note in notes),
    )


def _parse_stderr(lines: Sequence[str]) -> tuple[CliError | None, tuple[CliWarning, ...]]:
    """Find the error object (the last one wins) and every warning in the stderr lines."""
    error: CliError | None = None
    warnings: list[CliWarning] = []
    for line in lines:
        obj = _json_object(line)
        if obj is None:
            continue
        if "error" in obj and isinstance(obj["error"], dict):
            error = _error(obj["error"])
        elif "warning" in obj:
            warnings.append(_warning(obj["warning"]))
    return error, tuple(warnings)


def _read_stderr(
    stream: IO[str],
    lines: list[str],
    on_progress: Callable[[str], None] | None,
    failures: list[BaseException],
) -> None:
    """Collect every stderr line, and pass each progress line to `on_progress` as it arrives.

    A callback that raises is recorded in `failures` and the drain goes on. Otherwise the child
    could block on a full stderr pipe.
    """
    for raw in stream:
        line = raw.rstrip("\n")
        lines.append(line)
        if on_progress is None or failures:
            continue
        text = _progress_text(line)
        if text is None:
            continue
        try:
            on_progress(text)
        except Exception as exc:
            failures.append(exc)


class CliRunner:
    """Runs `ginn --json <args>` as a child process and returns a `CliResult`.

    `args` must begin with `--json`, because the Control Plane reads only JSON. `program` is the
    ginn executable. Tests pass `(sys.executable, fake_script)`. `env` replaces the child's
    environment when given, and inherits the GUI's environment when None.

    `run` blocks until the child exits. Only one run may be active at a time on a runner. `cancel`
    may be called from another thread. `run` calls `on_progress` from a reader thread that it
    starts for stderr, so a view must hand its updates to its own main loop.
    """

    def __init__(
        self, program: Sequence[str] = PROGRAM, *, env: Mapping[str, str] | None = None
    ) -> None:
        if not program:
            raise ValueError("program must name the ginn executable")
        self._program = tuple(program)
        self._env = None if env is None else dict(env)
        self._lock = threading.Lock()
        self._child: subprocess.Popen[str] | None = None
        self._cancel_requested = False

    def run(
        self, args: Sequence[str], on_progress: Callable[[str], None] | None = None
    ) -> CliResult:
        """Run one command and return its typed result.

        The argv is the program and `args`, unchanged. The CLI prints a progress line for each
        finished step without any flag (FR-107), so no `--verbose` is added. Raises
        CliLaunchError when the program cannot start. An exception raised by `on_progress` is
        re-raised here after the child has exited.
        """
        if not args or args[0] != "--json":
            raise ValueError("the Control Plane runs ginn only in --json mode")
        argv = self._program + tuple(args)
        with self._lock:
            if self._child is not None:
                raise RuntimeError("a ginn command is already running on this runner")
            try:
                child = subprocess.Popen(  # noqa: S603 - argv list, no shell
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=self._env,
                    start_new_session=True,
                )
            except OSError as exc:
                raise CliLaunchError(f"cannot start {self._program[0]}: {exc}") from exc
            self._child = child
            self._cancel_requested = False
        if child.stdout is None or child.stderr is None:
            child.kill()
            child.wait()
            with self._lock:
                self._child = None
            raise CliLaunchError("ginn was started without its output pipes")
        stderr_lines: list[str] = []
        failures: list[BaseException] = []
        reader = threading.Thread(
            target=_read_stderr,
            args=(child.stderr, stderr_lines, on_progress, failures),
            daemon=True,
        )
        reader.start()
        stdout_text = child.stdout.read()
        exit_code = child.wait()
        reader.join()
        with self._lock:
            cancelled = self._cancel_requested
            self._child = None
            self._cancel_requested = False
        if failures:
            raise failures[0]
        error, warnings = _parse_stderr(stderr_lines)
        stdout_clean = stdout_text.strip()
        return CliResult(
            argv=argv,
            exit_code=exit_code,
            payload=None if not stdout_clean else _json_object(stdout_clean),
            error=error,
            warnings=warnings,
            stderr="\n".join(stderr_lines),
            cancelled=cancelled,
        )

    def cancel(self) -> bool:
        """Send SIGINT to the running child's process group, as a terminal Ctrl-C would (FR-107).

        The child starts a session of its own, so its group holds ginn and the helpers it starts,
        such as podman. ginn ignores SIGINT while podman runs (podman/cli.py), so a signal sent
        only to ginn would be lost. The group signal reaches podman directly. Return False when no
        child runs or the child has already exited.

        A cancel that lands while a helper runs can surface as that step's error code, not as
        GINN-E-INTERRUPTED. `CliResult.cancelled` is the authoritative flag for the cancel.
        """
        with self._lock:
            child = self._child
            if child is None or child.poll() is not None:
                return False
            self._cancel_requested = True
            try:
                os.killpg(child.pid, signal.SIGINT)
            except ProcessLookupError:
                self._cancel_requested = False
                return False
            return True


@dataclass(frozen=True)
class DiskUse:
    """Disk use of one realm, from `disk` in the status JSON (FR-020)."""

    realm_home_bytes: int
    realm_home_partial: bool
    container_bytes: int | None


@dataclass(frozen=True)
class RealmView:
    """One realm as `ginn --json status` and `ginn --json list` show it (FR-020).

    `exported_apps` is None until the CLI counts exports, which it does not do yet.
    """

    name: str
    state: str | None
    container_state: str | None
    state_mismatch: bool | None
    distro: str | None
    tier: str | None
    services: bool | None
    generation: int | None
    restart_required: bool | None
    share_skipped: tuple[str, ...] | None
    disk: DiskUse | None
    exported_apps: int | None
    error: str | None


@dataclass(frozen=True)
class RealmListing:
    """The answer to one `list` read.

    `request_id` rises with each read the reader starts, so a view can drop an older answer that
    arrives late (DR-031). `realms` is None when the command failed.
    """

    request_id: int
    result: CliResult
    realms: tuple[RealmView, ...] | None


@dataclass(frozen=True)
class ShareView:
    """One shared path from the permissions JSON (FR-063). `mode` is `ro` or `rw`."""

    path: str
    mode: str


@dataclass(frozen=True)
class PermissionsView:
    """The tier, the effective toggles and the shares of one realm (FR-060, FR-063, FR-066).

    `toggles` are the effective ones. A Basic or Development realm shows its tier's set.
    """

    name: str
    tier: str
    toggles: tuple[str, ...]
    shares: tuple[ShareView, ...]
    restart_required: bool


def permissions_view(payload: Mapping[str, object]) -> PermissionsView:
    """Build a PermissionsView from `tier` or `perms` output. Raises CliProtocolError on a shape."""
    raw_shares = payload.get("shares")
    if not isinstance(raw_shares, list):
        raise CliProtocolError("shares is not a list")
    shares: list[ShareView] = []
    for item in raw_shares:
        if not isinstance(item, dict):
            raise CliProtocolError("a share is not an object")
        shares.append(ShareView(path=_req_str(item, "path"), mode=_req_str(item, "mode")))
    restart = payload.get("restart_required")
    if not isinstance(restart, bool):
        raise CliProtocolError("restart_required is not a boolean")
    return PermissionsView(
        name=_req_str(payload, "name"),
        tier=_req_str(payload, "tier"),
        toggles=_strs(payload.get("toggles", []), "toggles"),
        shares=tuple(shares),
        restart_required=restart,
    )


@dataclass(frozen=True)
class DoctorCheck:
    """One row of the doctor report (FR-007)."""

    name: str
    status: str
    message: str
    fixes: tuple[str, ...]


@dataclass(frozen=True)
class DoctorReport:
    """The JSON report of `ginn --json doctor`, schema version 1."""

    user: str
    uid: int
    ok: bool
    counts: Mapping[str, int]
    checks: tuple[DoctorCheck, ...]


@dataclass(frozen=True)
class IntegrityState:
    """The answer of `ginn --json integrity` (FR-006).

    `status` is one of baseline-created, changed, unchanged or acknowledged. A changed status
    means unacknowledged host changes, which block mutating operations (GINN-E-HOST-UNACKED).
    """

    status: str
    changed: tuple[str, ...]
    pending: tuple[str, ...] | None
    warnings: tuple[CliWarning, ...]


def _req_str(obj: Mapping[str, object], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise CliProtocolError(f"{key} is not a string")
    return value


def _opt_str(obj: Mapping[str, object], key: str) -> str | None:
    value = obj.get(key)
    if value is None or isinstance(value, str):
        return value
    raise CliProtocolError(f"{key} is not a string or null")


def _opt_bool(obj: Mapping[str, object], key: str) -> bool | None:
    value = obj.get(key)
    if value is None or isinstance(value, bool):
        return value
    raise CliProtocolError(f"{key} is not a boolean or null")


def _opt_int(obj: Mapping[str, object], key: str) -> int | None:
    value = obj.get(key)
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise CliProtocolError(f"{key} is not an integer or null")


def _opt_strs(obj: Mapping[str, object], key: str) -> tuple[str, ...] | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CliProtocolError(f"{key} is not a list of strings or null")
    return tuple(value)


def _disk(value: object) -> DiskUse | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CliProtocolError("disk is not an object or null")
    home = value.get("realm_home_bytes")
    partial = value.get("realm_home_partial")
    if not isinstance(home, int) or not isinstance(partial, bool):
        raise CliProtocolError("disk lacks realm_home_bytes or realm_home_partial")
    container = _opt_int(value, "container_bytes")
    return DiskUse(realm_home_bytes=home, realm_home_partial=partial, container_bytes=container)


def realm_view(obj: Mapping[str, object]) -> RealmView:
    """Build a RealmView from one status object. Raises CliProtocolError on a wrong shape."""
    return RealmView(
        name=_req_str(obj, "name"),
        state=_opt_str(obj, "state"),
        container_state=_opt_str(obj, "container_state"),
        state_mismatch=_opt_bool(obj, "state_mismatch"),
        distro=_opt_str(obj, "distro"),
        tier=_opt_str(obj, "tier"),
        services=_opt_bool(obj, "services"),
        generation=_opt_int(obj, "generation"),
        restart_required=_opt_bool(obj, "restart_required"),
        share_skipped=_opt_strs(obj, "share_skipped"),
        disk=_disk(obj.get("disk")),
        exported_apps=_opt_int(obj, "exported_apps"),
        error=_opt_str(obj, "error"),
    )


def _warnings(payload: Mapping[str, object]) -> tuple[CliWarning, ...]:
    raw = payload.get("warnings")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CliProtocolError("warnings is not a list")
    return tuple(_warning(item) for item in raw)


def _strs(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CliProtocolError(f"{key} is not a list of strings")
    return tuple(value)


class StateReader:
    """Reads realm, doctor and integrity state by running `ginn --json` commands."""

    def __init__(self, runner: CliRunner) -> None:
        self._runner = runner
        self._requests = itertools.count(1)
        self._lock = threading.Lock()

    def _next_request(self) -> int:
        with self._lock:
            return next(self._requests)

    def list_realms(self) -> RealmListing:
        """Run `ginn --json list`. The realms are None when the command failed (FR-020)."""
        request_id = self._next_request()
        result = self._runner.run(("--json", "list"))
        return RealmListing(request_id=request_id, result=result, realms=_listing(result))

    def realm_status(self, name: str) -> tuple[CliResult, RealmView | None]:
        """Run `ginn --json status <realm>`. The view is None when the command failed."""
        checked = RealmName.parse(name).value
        result = self._runner.run(("--json", "status", checked))
        view = None if not result.ok or result.payload is None else realm_view(result.payload)
        return result, view

    def permissions(self, name: str) -> tuple[CliResult, PermissionsView | None]:
        """Run `ginn --json tier <realm>`, which shows the tier and permissions (FR-060, FR-063)."""
        checked = RealmName.parse(name).value
        result = self._runner.run(("--json", "tier", checked))
        view = None if not result.ok or result.payload is None else permissions_view(result.payload)
        return result, view

    def doctor(self) -> tuple[CliResult, DoctorReport | None]:
        """Run `ginn --json doctor`. Its report is parsed even when a check fails (FR-007)."""
        result = self._runner.run(("--json", "doctor"))
        return result, None if result.payload is None else _doctor(result.payload)

    def integrity(self) -> tuple[CliResult, IntegrityState | None]:
        """Run `ginn --json integrity` (FR-006). This does not acknowledge a change."""
        result = self._runner.run(("--json", "integrity"))
        if result.payload is None or not result.ok:
            return result, None
        return result, _integrity(result.payload)


def _listing(result: CliResult) -> tuple[RealmView, ...] | None:
    if not result.ok or result.payload is None:
        return None
    raw = result.payload.get("realms")
    if not isinstance(raw, list):
        raise CliProtocolError("list payload lacks a realms list")
    views: list[RealmView] = []
    for item in raw:
        if not isinstance(item, dict):
            raise CliProtocolError("a realm entry is not an object")
        views.append(realm_view(item))
    return tuple(views)


def _doctor(payload: Mapping[str, object]) -> DoctorReport:
    raw_checks = payload.get("checks")
    counts = payload.get("counts")
    if not isinstance(raw_checks, list) or not isinstance(counts, dict):
        raise CliProtocolError("doctor payload lacks checks or counts")
    checks: list[DoctorCheck] = []
    for item in raw_checks:
        if not isinstance(item, dict):
            raise CliProtocolError("a doctor check is not an object")
        checks.append(
            DoctorCheck(
                name=_req_str(item, "name"),
                status=_req_str(item, "status"),
                message=_req_str(item, "message"),
                fixes=_strs(item.get("fixes", []), "fixes"),
            )
        )
    uid = payload.get("uid")
    ok = payload.get("ok")
    if not isinstance(uid, int) or not isinstance(ok, bool):
        raise CliProtocolError("doctor payload lacks uid or ok")
    return DoctorReport(
        user=_req_str(payload, "user"),
        uid=uid,
        ok=ok,
        counts={key: int(value) for key, value in counts.items() if isinstance(value, int)},
        checks=tuple(checks),
    )


def _integrity(payload: Mapping[str, object]) -> IntegrityState:
    pending = payload.get("pending")
    return IntegrityState(
        status=_req_str(payload, "status"),
        changed=_strs(payload.get("changed", []), "changed"),
        pending=None if pending is None else _strs(pending, "pending"),
        warnings=_warnings(payload),
    )


def watch_directories(paths: OwnedPaths) -> tuple[Path, ...]:
    """The directories whose changes should trigger a refresh (DR-030, FR-124).

    Realm records are the files under `realms`, and the Audit Log is `audit.log` in `state`. A
    monitor does not see files in a subdirectory, so both directories are watched. `realms` is a
    child of `state`, so the watch on `state` also sees the first create make `realms`. Either one
    may be missing; the watcher skips a missing directory.
    """
    return (paths.realms, paths.state)
