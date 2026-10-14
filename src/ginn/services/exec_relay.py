# SPDX-License-Identifier: Apache-2.0
"""The supervisor relay service `_exec` and its runtime paths (FR-084, FR-081, ADR-014).

A services-on realm's processes live in the supervisor leaf, so a user-session `podman exec` cannot
move a process into them (ADR-014, F2). The relay is a per-user listener, `ginn-exec-relay`, that
runs as one runit service named `_exec` in the user's supervision directory. Its run script is
written here from the adapter's podman command and the ginn-owned environment, and it is installed
once for every user who has a services-on realm.

`sync_exec_service` is the one entry point. ops calls it after every change to the set of
services-on realms and after every change of a realm's running state. A realm needs the relay while
its container was created services-on and its record reads running, rebuilding or updating, and a
services-on record that does not load counts as needing it, so the relay stays up. While one does,
the service is installed or refreshed, and it is kept up. Otherwise `down` is written and `sv down`
runs, so no ginn process stays resident while no realm needs it (NFR-001). The service is never
removed here. Host Setup undo removes it, without sv, through directory descriptors
(hostsetup/undo.py). Every sync holds the sync lock from before it reads the store until it has
settled the service, so two syncs never interleave (ADR-014).

Every path is an Owned Path under the runtime root (DR-010, CR-006). The name `_exec` is fixed and
is not a RealmName, which cannot hold an underscore, so no realm can collide with it.

The relay's argv is `ginn-exec-relay <runtime root> <podman global argv...>`, the contract of
native/ginn-exec-relay.c. The listener requires the runtime root to be a 0700 directory of its own
uid, which the Host Setup runsv run script makes before runsvdir starts (runit_system). It binds
`<runtime root>/exec.sock`, and the lock directory `<runtime root>/exec/` is derived from the same
root. The first element of the podman argv is the absolute podman binary, and the listener passes
it to each worker as the fixed prefix.
"""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from ginn.domain import RealmState
from ginn.errors import GinnError, make, warn
from ginn.errors import Warning as GinnWarning
from ginn.export._files import write_file_atomic
from ginn.podman.adapter import Podman
from ginn.podman.cli import PodmanCli
from ginn.services._exec_lock import (
    LOCK_DIR_NAME,
    exec_lock_busy,
    exec_lock_dir,
    exec_lock_path,
    exec_orphan_path,
)
from ginn.services.runit_user import DOWN, DOWN_MODE, RUN, SCRIPT_MODE
from ginn.services.supervisor import (
    COMMAND_MARGIN_S,
    GONE_MARKERS,
    SUPERVISE_DIR,
    SUPERVISE_OK,
    SV_FALLBACKS,
    SupervisorHooks,
)
from ginn.store.paths import OWNED_DIR_MODE, OWNED_FILE_MODE, OwnedPaths, fsync_dir
from ginn.store.records import RealmStore

EXEC_SERVICE: Final = "_exec"
"""The runit service name of the relay, in the user's supervision directory (ADR-014)."""
EXEC_RELAY_HELPER: Final = "/usr/libexec/ginn/ginn-exec-relay"
"""The installed listener (native/ginn-exec-relay.c), beside ginn-wait (runit_user.WAIT_HELPER)."""
SOCKET_NAME: Final = "exec.sock"
"""The listener's socket, directly under the runtime root (ADR-014)."""
OWNED_ENV: Final = ("XDG_RUNTIME_DIR", "CONTAINERS_CONF", "CONTAINERS_STORAGE_CONF", "TMPDIR")
"""The ginn-owned keys of PodmanCli._env() that the run script exports. Others are not carried.
PATH is not among them: the relay has a fixed PATH of its own (`relay_path`)."""
RELAY_PATH_SYSTEM: Final = ("/usr/local/bin", "/usr/bin", "/bin")
"""The system directories that follow the podman directory on the relay's fixed PATH."""
ACTIVE_STATES: Final = frozenset({RealmState.RUNNING, RealmState.REBUILDING, RealmState.UPDATING})
"""The states in which a services-on realm runs podman exec from the session (ADR-014)."""
TEMP_PREFIX: Final = "._exec."
"""The prefix of the directories `_create` builds in. runsvdir skips dotted names (ADR-014)."""
SYNC_LOCK_NAME: Final = ".exec-relay.lock"
"""The lock file that serialises every sync, in the supervision directory (ADR-014). Dotted, so
runsvdir skips it, and not under TEMP_PREFIX, so a sweep of the temporary directories skips it."""
_FORBIDDEN_IN_SCRIPT: Final = ("\n", "\r", "\0")


def exec_socket(paths: OwnedPaths) -> Path:
    """The listener's socket, `<runtime root>/exec.sock` (ADR-014, DR-010)."""
    return paths.runtime / SOCKET_NAME


def exec_service_dir(paths: OwnedPaths) -> Path:
    """The relay's runit service directory, `<SV_DIR>/_exec` (ADR-014)."""
    return paths.sv / EXEC_SERVICE


def relay_path(paths: OwnedPaths, binary: str) -> str:
    """The fixed PATH of the relay: the podman directory, then the system directories (ADR-014).

    It depends on the podman binary and the Owned Paths only, never on the caller's PATH, so the
    same inputs always give the same script. Duplicates are dropped, keeping the first. ginn's own
    bin directory is never on it, so a launcher there cannot stand in for a program.
    """
    ginn_bin = os.path.realpath(paths.bin)
    kept: list[str] = []
    for entry in (os.path.dirname(binary), *RELAY_PATH_SYSTEM):
        normal = os.path.normpath(entry)
        if normal in kept or os.path.realpath(normal) == ginn_bin:
            continue
        kept.append(normal)
    return ":".join(kept)


def render(paths: OwnedPaths, podman: PodmanCli) -> str:
    """The text of the relay's run script (FR-084, ADR-014). Pure: it writes nothing.

    The script exports the ginn-owned podman environment and the fixed relay PATH (`relay_path`),
    then execs the listener with the runtime root and the podman global argv as its arguments. Each
    value is one single-quoted POSIX word, so a path with spaces stays one argument. A value with a
    line break or a NUL is refused. A podman binary that is not an absolute path is refused too, so
    the relay never finds podman through a PATH lookup.
    """
    binary = podman.global_argv()[0]
    if not os.path.isabs(binary):
        raise _internal(f"the relay needs an absolute podman path, not {binary!r}")
    env = podman._env()
    lines = [
        "#!/bin/sh",
        f"# ginn exec relay {EXEC_SERVICE} (FR-084, ADR-014).",
        "# Written by ginn. ginn rewrites this file; do not edit it.",
    ]
    for key in OWNED_ENV:
        lines.append(f"export {key}={_word(env[key])}")
    lines.append(f"export PATH={_word(relay_path(paths, binary))}")
    argv = [EXEC_RELAY_HELPER, str(paths.runtime), *podman.global_argv()]
    lines.append("exec " + " ".join(_word(arg) for arg in argv))
    return "\n".join(lines) + "\n"


def sync_exec_service(
    paths: OwnedPaths, podman: Podman | None = None, *, hooks: SupervisorHooks | None = None
) -> None:
    """Make the relay service follow the services-on realms of the store (FR-084, ADR-014).

    The service is kept up only while a services-on realm needs the relay (NFR-001, see
    `_services_state`). While none does, `down` is written and `sv down` runs when runsv supervises
    the service. The service is never removed here, and when none was ever created nothing is
    written (FR-082).

    The run script is installed or refreshed when a services-on realm exists and `podman` is a
    PodmanCli, the adapter whose podman command the script renders. Without one (None, or a test
    fake) no script is written, and only the running state of an existing service is applied.
    `hooks` are the sv seams, and the defaults are the real ones.

    The store is read under the sync lock, and the decision is made from that read. Before the
    lock, one unlocked read decides only whether anything could be written or stopped. When neither
    holds, nothing is created, so a services-off host with no service directory gets no files.
    """
    seams = SupervisorHooks() if hooks is None else hooks
    service = exec_service_dir(paths)
    if not _services_state(paths)[0] and not service.is_symlink() and not service.exists():
        return
    paths.sv.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
    with _sync_lock(paths):
        on, active = _services_state(paths)
        if on and isinstance(podman, PodmanCli):
            _sweep_temps(paths.sv)
            _install(paths, render(paths, podman), seams, active)
            return
        if service.is_symlink() or not service.exists():
            return
        if not service.is_dir():
            raise _internal(f"the relay service is not a directory: {service}")
        _settle(service, active, seams, changed=False)


def relay_failure(exc: Exception) -> GinnWarning:
    """The GINN-W-RELAY-SYNC warning for a failed sync after a recorded operation (ADR-014).

    The operation keeps its result, so the failure is a warning. The next sync retries it.
    """
    detail = exc.message if isinstance(exc, GinnError) else f"{type(exc).__name__}: {exc}"
    return warn("GINN-W-RELAY-SYNC", detail=detail)


def _services_state(paths: OwnedPaths) -> tuple[bool, bool]:
    """(a services-on realm is stored, a services-on realm needs the relay now) (ADR-014).

    A realm is services-on when its record has `services_at_create` or `settings.services` set.
    It needs the relay only while its container was created services-on (`services_at_create`)
    and its record reads running, rebuilding or updating. A container created services-off runs its
    execs directly, whatever the flag says now, and the flag takes effect at the rebuild.

    A record that does not load counts as a services-on realm that needs the relay, so the relay
    stays up for it (NFR-001 gives way to the safe answer for a record ginn cannot read).
    """
    store = RealmStore(paths)
    on = False
    for name in store.list_names():
        try:
            record = store.load(name)
        except (GinnError, OSError):
            return True, True
        if not (record.services_at_create or record.settings.services):
            continue
        on = True
        if record.services_at_create and record.state in ACTIVE_STATES:
            return True, True
    return on, False


@contextmanager
def _sync_lock(paths: OwnedPaths) -> Iterator[None]:
    """Hold the sync lock, `<SV_DIR>/.exec-relay.lock`, for one sync (ADR-014).

    The file is opened without following a link, and closing it releases the lock. Each sync takes
    the lock after it has taken its realm lock, never before, so no realm lock waits on it in the
    other order. A sync can hold the lock for the length of an sv wait.
    """
    fd = os.open(
        paths.sv / SYNC_LOCK_NAME,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        OWNED_FILE_MODE,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _sweep_temps(sv: Path) -> None:
    """Remove the temporary directories that an interrupted creator left (ADR-014).

    Only the sync lock holds the directory at this point, so every `._exec.*` directory is stale.
    """
    for entry in sv.iterdir():
        if entry.name.startswith(TEMP_PREFIX):
            _remove_tree(entry)


def _install(paths: OwnedPaths, text: str, hooks: SupervisorHooks, active: bool) -> None:
    """Create the service, or update its script, then apply the running state (FR-084, ADR-014).

    A changed script is replaced atomically. The state is applied by `_settle`, which restarts a
    supervised service whose script changed, so the new script runs.
    """
    service = exec_service_dir(paths)
    if not service.exists() and not service.is_symlink():
        if _create(paths, text, down=not active):
            return
    if service.is_symlink() or not service.is_dir():
        raise _internal(f"the relay service is not a directory: {service}")
    run = service / RUN
    previous = None if run.is_symlink() else _read_or_none(run)
    changed = previous != text.encode("utf-8")
    if changed:
        write_file_atomic(run, text.encode("utf-8"), SCRIPT_MODE)
    _settle(service, active, hooks, changed=changed)


def _create(paths: OwnedPaths, text: str, *, down: bool) -> bool:
    """Build the service in a private dotted directory and rename it into place (ADR-014).

    `mkdtemp` gives the directory a name of its own (mode 0700, OWNED_DIR_MODE), so two creators
    never share a half-built directory. runsvdir skips dotted names, so it never sees the
    directory. `down` is written before the rename when no realm needs the relay, so runsvdir does
    not start it. A rename that finds the service already present (ENOTEMPTY or EEXIST) means a
    concurrent creator installed it, and this returns False so that the caller updates it instead.
    The sync lock already serialises the creators; the unique name keeps `_create` safe alone.
    """
    sv = paths.sv
    sv.mkdir(mode=OWNED_DIR_MODE, parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=sv))
    try:
        write_file_atomic(temp / RUN, text.encode("utf-8"), SCRIPT_MODE)
        if down:
            write_file_atomic(temp / DOWN, b"", DOWN_MODE)
        os.rename(temp, exec_service_dir(paths))
    except OSError as exc:
        _remove_tree(temp)
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
            return False
        raise
    fsync_dir(sv)
    return True


def _settle(service: Path, active: bool, hooks: SupervisorHooks, *, changed: bool) -> None:
    """Apply the running state to an installed service (NFR-001, ADR-014).

    Active: `down` is cleared. A supervised service is restarted when its script changed, so the
    new script runs, and brought up otherwise, because a stopped service needs `sv up`. Inactive:
    `down` is written, so runsvdir does not start the service, and a supervised service is stopped
    with `sv down`. Each sv command is idempotent and is sent on every sync, so a failed earlier
    one is retried by the next sync.
    """
    if active:
        _clear_down(service)
        if _supervised(service):
            _sv(hooks, "restart" if changed else "up", service, hooks.up_wait_s)
        return
    write_file_atomic(service / DOWN, b"", DOWN_MODE)
    if _supervised(service):
        _sv(hooks, "down", service, hooks.down_wait_s)


def _supervised(service: Path) -> bool:
    """True when a runsv watches the directory: runsv created supervise/ok (runsv(8))."""
    return os.path.exists(service / SUPERVISE_DIR / SUPERVISE_OK)


def _clear_down(service: Path) -> None:
    try:
        os.unlink(service / DOWN)
    except FileNotFoundError:
        return
    fsync_dir(service)


def _sv(hooks: SupervisorHooks, verb: str, service: Path, wait_s: int) -> None:
    """Run `sv -w <wait> <verb> <dir>` and raise GINN-E-SERVICE-CONTROL on failure (ADR-014).

    A `down` that finds no runsv for the directory is not a failure: nothing runs the service then.
    Any other failure is raised, so the caller does not claim the service is stopped.
    """
    argv = [_sv_path(hooks), "-w", str(wait_s), verb, str(service)]
    result = hooks.run(argv, wait_s + COMMAND_MARGIN_S)
    if result.code == 0:
        return
    text = result.out + result.err
    if verb == "down" and any(marker in text for marker in GONE_MARKERS):
        return
    raise make(
        "GINN-E-SERVICE-CONTROL",
        realm=EXEC_SERVICE,
        action=verb,
        detail=_first_line(result.err + "\n" + result.out) or "sv failed",
    )


def _first_line(text: str) -> str:
    """The first non-blank line of sv's output, for a message."""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _sv_path(hooks: SupervisorHooks) -> str:
    """The absolute path of sv: the hook's, else the one on PATH, else a standard location."""
    if hooks.sv is not None:
        return hooks.sv
    found = shutil.which("sv")
    if found is not None and os.path.isabs(found):
        return found
    for candidate in SV_FALLBACKS:
        if os.access(candidate, os.X_OK):
            return candidate
    raise make(
        "GINN-E-SERVICE-CONTROL",
        realm=EXEC_SERVICE,
        action="control",
        detail="sv (runit) is not installed",
    )


def _read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _word(value: str) -> str:
    """One POSIX sh word in single quotes. A value that could break the script is refused."""
    if any(char in value for char in _FORBIDDEN_IN_SCRIPT):
        raise _internal("a value of the relay script holds a line break or a NUL")
    return "'" + value.replace("'", "'\\''") + "'"


def _remove_tree(path: Path) -> None:
    """Remove a directory tree, or a file or link, that ginn made. Absent is a no-op."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _internal(detail: str) -> GinnError:
    err = make("GINN-E-INTERNAL")
    err.details.update({"detail": detail})
    return err


__all__ = [
    "EXEC_RELAY_HELPER",
    "EXEC_SERVICE",
    "LOCK_DIR_NAME",
    "OWNED_ENV",
    "relay_path",
    "relay_failure",
    "SOCKET_NAME",
    "SYNC_LOCK_NAME",
    "TEMP_PREFIX",
    "exec_lock_busy",
    "exec_lock_dir",
    "exec_lock_path",
    "exec_orphan_path",
    "exec_service_dir",
    "exec_socket",
    "render",
    "sync_exec_service",
]
