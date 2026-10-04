"""Host desktop session environment for hosts without a systemd user manager.

Two halves feed ``LxcBackend.host_user_environment``:

* ``publish`` runs as the user inside the graphical session (for example from
  niri's ``spawn-at-startup``) and writes ``$XDG_RUNTIME_DIR/spaces/environment``.
* ``resolve_environment`` runs as root in the launcher. It prefers the
  published file (after checking it still names an active graphical elogind
  session of the user) and otherwise scans /proc for a process of the user that
  elogind places in that session and reads its environment.

It also owns the ``/run/user/UID/bus`` symlink that lets code which assumes the
systemd runtime layout reach an ephemeral ``dbus-run-session`` bus.

Usage: ``python3 -I -m spaces.host.session_env publish|show``
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import re
import stat
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol

RUN_USER = Path("/run/user")
PROC = Path("/proc")
ENVIRONMENT_DIRECTORY = "spaces"
ENVIRONMENT_FILE = "environment"
BUS_NAME = "bus"
MAX_FILE_SIZE = 64 * 1024
MAX_VALUE = 4096
GRAPHICAL_TYPES = frozenset({"wayland", "x11"})
EXTRA_KEYS = frozenset(
    {
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_ID",
    }
)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def publish_keys() -> frozenset[str]:
    """Return the names a published environment may carry."""

    from .. import session

    return frozenset(session.DESKTOP_ENVIRONMENT) | EXTRA_KEYS


# ---------------------------------------------------------------- file format


def _safe_value(value: str) -> bool:
    return (
        0 < len(value) <= MAX_VALUE
        and "\0" not in value
        and "\n" not in value
        and "\r" not in value
    )


def format_environment(environment: Mapping[str, str]) -> str:
    return "".join(f"{key}={environment[key]}\n" for key in sorted(environment))


def parse_environment(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines, silently dropping malformed ones."""

    result: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if separator and _NAME.fullmatch(name) and _safe_value(value):
            result[name] = value
    return result


def environment_path(uid: int, run_user: Path = RUN_USER) -> Path:
    return run_user / str(uid) / ENVIRONMENT_DIRECTORY / ENVIRONMENT_FILE


def write_published(
    environment: Mapping[str, str],
    runtime_dir: Path,
    *,
    uid: int | None = None,
) -> Path:
    """Atomically write the environment file below runtime_dir."""

    uid = os.geteuid() if uid is None else uid
    directory = runtime_dir / ENVIRONMENT_DIRECTORY
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid:
        raise OSError(f"unsafe directory {directory}")
    os.chmod(directory, 0o700)
    target = directory / ENVIRONMENT_FILE
    temporary = directory / f".{ENVIRONMENT_FILE}.{os.getpid()}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(format_environment(environment))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    return target


def read_published(uid: int, run_user: Path = RUN_USER) -> dict[str, str] | None:
    """Read the user's published file; None unless it is safe to trust.

    The directory and the file must belong to the user, must not be a
    symlink and must not be writable by anyone else.
    """

    directory = run_user / str(uid) / ENVIRONMENT_DIRECTORY
    try:
        dir_status = directory.lstat()
        if (
            not stat.S_ISDIR(dir_status.st_mode)
            or dir_status.st_uid != uid
            or dir_status.st_mode & 0o022
        ):
            return None
        descriptor = os.open(
            directory / ENVIRONMENT_FILE,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        status = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != uid
            or status.st_mode & 0o022
            or status.st_size > MAX_FILE_SIZE
        ):
            return None
        return parse_environment(handle.read().decode("utf-8", errors="replace"))


# ---------------------------------------------------------------- elogind


class LoginInfo(Protocol):
    def graphical_session(self, uid: int, session_id: str) -> bool: ...

    def pid_session(self, pid: int) -> str | None: ...


class Elogind:
    """The few sd-login calls needed, through libelogind."""

    def __init__(self, library_name: str | None = None) -> None:
        name = (
            library_name
            or ctypes.util.find_library("elogind")
            or "libelogind.so.0"
        )
        self._library = ctypes.CDLL(name, use_errno=True)
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.free.argtypes = [ctypes.c_void_p]
        self._libc.free.restype = None
        library = self._library
        library.sd_uid_get_sessions.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ]
        library.sd_uid_get_sessions.restype = ctypes.c_int
        library.sd_session_is_active.argtypes = [ctypes.c_char_p]
        library.sd_session_is_active.restype = ctypes.c_int
        library.sd_session_is_remote.argtypes = [ctypes.c_char_p]
        library.sd_session_is_remote.restype = ctypes.c_int
        for function in (
            library.sd_session_get_type,
            library.sd_session_get_class,
        ):
            function.argtypes = [
                ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            function.restype = ctypes.c_int
        library.sd_pid_get_session.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        library.sd_pid_get_session.restype = ctypes.c_int

    def _string(self, function: Callable[..., int], *args: object) -> str | None:
        pointer = ctypes.c_void_p()
        if function(*args, ctypes.byref(pointer)) < 0 or not pointer.value:
            return None
        try:
            return ctypes.string_at(pointer.value).decode("utf-8", "replace")
        finally:
            self._libc.free(pointer)

    def user_sessions(self, uid: int) -> list[str]:
        pointer = ctypes.POINTER(ctypes.c_void_p)()
        count = self._library.sd_uid_get_sessions(uid, 0, ctypes.byref(pointer))
        if count <= 0 or not pointer:
            return []
        result = []
        try:
            for index in range(count):
                value = pointer[index]
                if value:
                    result.append(ctypes.string_at(value).decode("utf-8", "replace"))
                    self._libc.free(value)
        finally:
            self._libc.free(ctypes.cast(pointer, ctypes.c_void_p))
        return result

    def pid_session(self, pid: int) -> str | None:
        return self._string(self._library.sd_pid_get_session, pid)

    def graphical_session(self, uid: int, session_id: str) -> bool:
        """True for an active, local, user-class wayland or x11 session of uid."""

        if session_id not in self.user_sessions(uid):
            return False
        raw = session_id.encode()
        return (
            self._library.sd_session_is_active(raw) > 0
            and self._library.sd_session_is_remote(raw) == 0
            and self._string(self._library.sd_session_get_class, raw) == "user"
            and self._string(self._library.sd_session_get_type, raw)
            in GRAPHICAL_TYPES
        )


def open_login() -> LoginInfo | None:
    try:
        return Elogind()
    except (OSError, AttributeError):
        return None


# ---------------------------------------------------------------- /proc scan

_scan_cache: dict[int, tuple[int, str]] = {}


def _read_environ(pid_dir: Path, keys: frozenset[str]) -> dict[str, str] | None:
    """Read the allowlisted part of a process environment."""

    try:
        raw = (pid_dir / "environ").read_bytes()
    except OSError:
        return None
    result = {}
    for item in raw.split(b"\0"):
        key, separator, value = item.decode("utf-8", "replace").partition("=")
        if separator and key in keys and _safe_value(value):
            result[key] = value
    return result


def _usable(environment: Mapping[str, str]) -> bool:
    return bool(
        environment.get("DBUS_SESSION_BUS_ADDRESS")
        and (environment.get("WAYLAND_DISPLAY") or environment.get("DISPLAY"))
    )


def scan_session_environment(
    uid: int,
    login: LoginInfo,
    proc: Path = PROC,
) -> dict[str, str] | None:
    """Environment of the lowest-pid process in uid's active graphical session.

    A process counts when elogind places it in an active local graphical
    session of uid and its environment names the session bus and a display.
    """

    keys = publish_keys()
    cached = _scan_cache.get(uid)
    if cached is not None:
        pid, session_id = cached
        environment = _read_environ(proc / str(pid), keys)
        try:
            same = (proc / str(pid)).stat().st_uid == uid
        except OSError:
            same = False
        if (
            same
            and environment is not None
            and _usable(environment)
            and login.pid_session(pid) == session_id
            and login.graphical_session(uid, session_id)
        ):
            environment["XDG_SESSION_ID"] = session_id
            return environment
        _scan_cache.pop(uid, None)
    try:
        entries = sorted(
            (int(entry.name), entry)
            for entry in proc.iterdir()
            if entry.name.isdigit()
        )
    except OSError:
        return None
    verdicts: dict[str, bool] = {}
    for pid, entry in entries:
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        session_id = login.pid_session(pid)
        if session_id is None:
            continue
        if session_id not in verdicts:
            verdicts[session_id] = login.graphical_session(uid, session_id)
        if not verdicts[session_id]:
            continue
        environment = _read_environ(entry, keys)
        if environment is None or not _usable(environment):
            continue
        environment["XDG_SESSION_ID"] = session_id
        _scan_cache[uid] = (pid, session_id)
        return environment
    return None


# ---------------------------------------------------------------- resolution


def _published_is_current(
    environment: Mapping[str, str], uid: int, login: LoginInfo | None
) -> bool:
    if not _usable(environment):
        return False
    if login is None:
        return True
    session_id = environment.get("XDG_SESSION_ID")
    return bool(session_id) and login.graphical_session(uid, session_id)


def resolve_environment(
    uid: int,
    *,
    login: LoginInfo | None = None,
    run_user: Path = RUN_USER,
    proc: Path = PROC,
) -> dict[str, str] | None:
    """Return the host session environment of uid: published file, else scan."""

    if login is None:
        login = open_login()
    published = read_published(uid, run_user)
    if published is not None and _published_is_current(published, uid, login):
        return published
    if login is None:
        return None
    return scan_session_environment(uid, login, proc)


def environment_block(environment: Mapping[str, str] | None) -> str | None:
    return None if environment is None else format_environment(environment)


# ---------------------------------------------------------------- bus symlink


def parse_bus_address(address: str) -> tuple[str, str] | None:
    """Return ("path", p) or ("abstract", name) for a unix: bus address."""

    if not address.startswith("unix:"):
        return None
    for part in address[5:].split(","):
        key, _separator, value = part.partition("=")
        if key in {"path", "abstract"} and value:
            return key, value
    return None


def ensure_bus_link(
    uid: int,
    address: str,
    run_user: Path = RUN_USER,
) -> str:
    """Make /run/user/UID/bus reach address when that is possible.

    Returns the address clients should use: ``unix:path=<run_user>/UID/bus``
    when the link (or a real socket) is in place, else address unchanged.
    An existing non-symlink is never replaced; abstract sockets cannot be
    linked and are returned as they are.
    """

    parsed = parse_bus_address(address)
    standard = f"unix:path={run_user}/{uid}/bus"
    if parsed is None or parsed[0] != "path":
        return address
    target = parsed[1]
    if not os.path.isabs(target) or "\0" in target:
        return address
    try:
        socket_status = os.stat(target)
    except OSError:
        return address
    if not stat.S_ISSOCK(socket_status.st_mode) or socket_status.st_uid != uid:
        return address
    try:
        dirfd = os.open(
            run_user / str(uid),
            os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError:
        return address
    try:
        dir_status = os.fstat(dirfd)
        if dir_status.st_uid != uid:
            return address
        try:
            current = os.stat(BUS_NAME, dir_fd=dirfd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None:
            if not stat.S_ISLNK(current.st_mode):
                # A real socket (systemd-style layout) or something foreign.
                return standard if stat.S_ISSOCK(current.st_mode) else address
            if current.st_uid not in {uid, 0}:
                return address
            if os.readlink(BUS_NAME, dir_fd=dirfd) == target:
                return standard
        temporary = f".{BUS_NAME}.spaces.{os.getpid()}"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=dirfd)
        os.symlink(target, temporary, dir_fd=dirfd)
        try:
            os.chown(
                temporary,
                uid,
                dir_status.st_gid,
                dir_fd=dirfd,
                follow_symlinks=False,
            )
            os.replace(
                temporary, BUS_NAME, src_dir_fd=dirfd, dst_dir_fd=dirfd
            )
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=dirfd)
            raise
        return standard
    except OSError:
        return address
    finally:
        os.close(dirfd)


def remove_stale_bus_link(uid: int, run_user: Path = RUN_USER) -> bool:
    """Unlink /run/user/UID/bus when it is a dangling symlink."""

    try:
        dirfd = os.open(
            run_user / str(uid),
            os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError:
        return False
    try:
        status = os.stat(BUS_NAME, dir_fd=dirfd, follow_symlinks=False)
        if not stat.S_ISLNK(status.st_mode) or status.st_uid not in {uid, 0}:
            return False
        try:
            os.stat(BUS_NAME, dir_fd=dirfd)
        except FileNotFoundError:
            os.unlink(BUS_NAME, dir_fd=dirfd)
            return True
        except OSError:
            return False
        return False
    except OSError:
        return False
    finally:
        os.close(dirfd)


# ---------------------------------------------------------------- command line


def collect_published_environment(
    source: Mapping[str, str],
    keys: Iterable[str],
    login: LoginInfo | None = None,
) -> dict[str, str]:
    environment = {
        key: source[key]
        for key in keys
        if key in source and _NAME.fullmatch(key) and _safe_value(source[key])
    }
    if "XDG_SESSION_ID" not in environment and login is not None:
        session_id = login.pid_session(os.getpid())
        if session_id:
            environment["XDG_SESSION_ID"] = session_id
    return environment


def _publish() -> int:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not os.path.isabs(runtime):
        print("spaces-session-env: XDG_RUNTIME_DIR is not set", file=sys.stderr)
        return 1
    environment = collect_published_environment(
        os.environ, publish_keys(), open_login()
    )
    if not _usable(environment):
        print(
            "spaces-session-env: no graphical session in this environment "
            "(need DBUS_SESSION_BUS_ADDRESS and WAYLAND_DISPLAY or DISPLAY)",
            file=sys.stderr,
        )
        return 1
    try:
        target = write_published(environment, Path(runtime))
    except OSError as error:
        print(f"spaces-session-env: {error}", file=sys.stderr)
        return 1
    print(f"spaces-session-env: wrote {target} ({len(environment)} variables)")
    return 0


def _show() -> int:
    uid = os.geteuid()
    login = open_login()
    published = read_published(uid)
    if published is None:
        print(f"published: none ({environment_path(uid)} missing or unsafe)")
    else:
        current = _published_is_current(published, uid, login)
        print(f"published: {environment_path(uid)} ({'current' if current else 'stale'})")
        sys.stdout.write(format_environment(published))
    scanned = scan_session_environment(uid, login) if login else None
    print(
        "scan: "
        + (
            f"session {scanned['XDG_SESSION_ID']}, {len(scanned)} variables"
            if scanned
            else "nothing found" if login else "libelogind unavailable"
        )
    )
    return 0 if published is not None or scanned is not None else 1


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["publish"]:
        return _publish()
    if arguments == ["show"]:
        return _show()
    print("usage: spaces-session-env publish|show", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
