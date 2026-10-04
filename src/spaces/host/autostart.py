"""Autostart of spaces on Void (runit + elogind).

Upstream starts a space through two systemd mechanisms: the user unit
`spaces@NAME.service` (WantedBy=default.target, enabled for a user by
`spaces create` / `spaces configure`) and the system unit `spaces@NAME`
(`systemctl enable`, starts on boot). Void has neither, so this module keeps
the same two switches as files and a runit service, `spaces-autostart`,
acts on them:

    /var/lib/spaces/NAME/autostart-users   one user name per line (written by
                                           LxcBackend.enable_user_autostart)
    /var/lib/spaces/NAME/autostart-boot    flag file: start at boot

The daemon (`python -m spaces.host.autostart`, see void/runit/spaces-autostart)
watches the elogind login state and, at start and on every change:

  * boot flag: `sv once` the space once per boot (a deliberate stop is kept);
  * per user: when elogind reports the user as active, online or lingering and
    a login that the daemon has not acted on yet exists (a session id that is
    new, or the lingering state that just started), `sv once` every space the
    user enabled, unless it already runs. A deliberate `sv down` therefore
    stays down until that user's next login.

Enabling does nothing until the service is linked into /var/service:
`sudo ln -s /etc/sv/spaces-autostart /var/service/`. The state that makes
the daemon idempotent lives on tmpfs (/run/spaces/autostart/state.json), so a
restart of the daemon neither repeats nor forgets, and a reboot starts over.
This module must stay light: no textual, no PIL.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import json
import os
import pwd
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

STATE_ROOT = Path("/var/lib/spaces")
RUN_DIR = Path("/run/spaces/autostart")
SERVICE_NAME = "spaces-autostart"
ACTIVE_STATES = frozenset({"active", "online", "lingering"})
LINGER_TOKEN = "@lingering"
USERS_FILE = "autostart-users"
BOOT_FILE = "autostart-boot"
POLL_SECONDS = 30.0


def log(message: str) -> None:
    print(f"spaces-autostart: {message}", flush=True)


# ------------------------------------------------------------- state files


def space_names(root: Path = STATE_ROOT) -> list[str]:
    """Names of existing spaces (a directory with info.json)."""

    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [
        entry.name
        for entry in entries
        if not entry.name.startswith(".") and (entry / "info.json").is_file()
    ]


def read_users(name: str, root: Path = STATE_ROOT) -> list[str]:
    try:
        text = (root / name / USERS_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    return text.split()


def boot_enabled(name: str, root: Path = STATE_ROOT) -> bool:
    return (root / name / BOOT_FILE).exists()


def _write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, mode)
    if os.geteuid() == 0:
        os.chown(temporary, 0, 0)
    os.rename(temporary, path)


def _check_user(user: str) -> str:
    if not user or any(c.isspace() or c == "/" for c in user):
        raise ValueError(f"bad user name: {user!r}")
    return user


def _space_dir(name: str, root: Path) -> Path:
    space = root / name
    if "/" in name or not (space / "info.json").is_file():
        raise ValueError(f"no such space: {name}")
    return space


def set_user(name: str, user: str, enabled: bool, root: Path = STATE_ROOT) -> bool:
    """Add or remove user in the space's autostart-users; True if it changed."""

    space = _space_dir(name, root)
    _check_user(user)
    users = read_users(name, root)
    if enabled == (user in users):
        return False
    users = [*users, user] if enabled else [u for u in users if u != user]
    path = space / USERS_FILE
    if users:
        _write_atomic(path, "".join(f"{u}\n" for u in users))
    else:
        # An empty file would be a different state from "never enabled".
        path.unlink(missing_ok=True)
    return True


def set_boot(name: str, enabled: bool, root: Path = STATE_ROOT) -> bool:
    space = _space_dir(name, root)
    flag = space / BOOT_FILE
    if enabled == flag.exists():
        return False
    if enabled:
        _write_atomic(flag, "")
    else:
        flag.unlink()
    return True


# ------------------------------------------------------------ runit control


class Runner(Protocol):
    def running(self, name: str) -> bool: ...

    def start(self, name: str) -> bool: ...


class RunitRunner:
    """`sv once` for spaces, through the helpers the LXC backend uses."""

    def __init__(self) -> None:
        from . import lxc

        self._lxc = lxc

    def _link(self, name: str) -> Path:
        return self._lxc._runit_dirs()[1] / self._lxc._service_name(name)

    def running(self, name: str) -> bool:
        return self._lxc._service_state(name) == "run"

    def start(self, name: str) -> bool:
        """Start the space's service without waiting; True when requested."""

        lxc = self._lxc
        if not self._link(name).exists():
            # A space that was never started has no service yet.
            lxc.ensure_service(name)
        if lxc._service_state(name) == "run":
            return False
        completed = lxc._sv("once", str(self._link(name)))
        if completed.returncode != 0:
            log(f"sv once failed for {name}: {completed.stdout.strip()}{completed.stderr.strip()}")
            return False
        return True


# ------------------------------------------------------------- elogind

class Logins(Protocol):
    def state(self, uid: int) -> str | None: ...

    def sessions(self, uid: int) -> list[str]: ...

    def wait(self, timeout: float) -> None: ...


class ElogindLogins:
    """sd-login state and monitor through libelogind (ctypes, no extras)."""

    def __init__(self, library_name: str | None = None) -> None:
        name = (
            library_name
            or ctypes.util.find_library("elogind")
            or "libelogind.so.0"
        )
        self._library = library = ctypes.CDLL(name, use_errno=True)
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.free.argtypes = [ctypes.c_void_p]
        self._libc.free.restype = None
        pointer = ctypes.c_void_p
        library.sd_login_monitor_new.argtypes = [ctypes.c_char_p, ctypes.POINTER(pointer)]
        library.sd_login_monitor_new.restype = ctypes.c_int
        library.sd_login_monitor_unref.argtypes = [pointer]
        library.sd_login_monitor_unref.restype = pointer
        library.sd_login_monitor_flush.argtypes = [pointer]
        library.sd_login_monitor_flush.restype = ctypes.c_int
        library.sd_login_monitor_get_fd.argtypes = [pointer]
        library.sd_login_monitor_get_fd.restype = ctypes.c_int
        library.sd_login_monitor_get_events.argtypes = [pointer]
        library.sd_login_monitor_get_events.restype = ctypes.c_int
        library.sd_uid_get_state.argtypes = [ctypes.c_uint, ctypes.POINTER(pointer)]
        library.sd_uid_get_state.restype = ctypes.c_int
        library.sd_uid_get_sessions.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(ctypes.POINTER(pointer)),
        ]
        library.sd_uid_get_sessions.restype = ctypes.c_int
        self._monitor = pointer()
        result = library.sd_login_monitor_new(None, ctypes.byref(self._monitor))
        if result < 0:
            raise OSError(-result, "sd_login_monitor_new failed")
        self._fd = library.sd_login_monitor_get_fd(self._monitor)
        self._events = library.sd_login_monitor_get_events(self._monitor)
        if self._fd < 0 or self._events < 0:
            raise OSError("sd-login monitor is unusable")

    def state(self, uid: int) -> str | None:
        value = ctypes.c_void_p()
        if self._library.sd_uid_get_state(uid, ctypes.byref(value)) < 0 or not value.value:
            return None
        try:
            return ctypes.string_at(value.value).decode("utf-8", "replace")
        finally:
            self._libc.free(value)

    def sessions(self, uid: int) -> list[str]:
        array = ctypes.POINTER(ctypes.c_void_p)()
        count = self._library.sd_uid_get_sessions(uid, 0, ctypes.byref(array))
        if count <= 0 or not array:
            return []
        found = []
        try:
            for index in range(count):
                if array[index]:
                    found.append(ctypes.string_at(array[index]).decode("utf-8", "replace"))
                    self._libc.free(array[index])
        finally:
            self._libc.free(ctypes.cast(array, ctypes.c_void_p))
        return found

    def wait(self, timeout: float) -> None:
        poller = select.poll()
        poller.register(self._fd, self._events)
        with contextlib.suppress(InterruptedError):
            poller.poll(int(timeout * 1000))
        self._library.sd_login_monitor_flush(self._monitor)


# ------------------------------------------------------------------ daemon


def _uid_of(user: str) -> int | None:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


class Autostarter:
    """One evaluation per call of `evaluate`; all effects through the Runner."""

    def __init__(
        self,
        logins: Logins,
        runner: Runner,
        *,
        root: Path = STATE_ROOT,
        run_dir: Path = RUN_DIR,
        uid_of: Callable[[str], int | None] = _uid_of,
    ) -> None:
        self.logins = logins
        self.runner = runner
        self.root = root
        self.state_path = run_dir / "state.json"
        self.uid_of = uid_of
        self.booted: set[str] = set()
        self.seen: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.booted = {str(n) for n in data.get("boot", [])}
            self.seen = {str(u): {str(s) for s in v} for u, v in data.get("seen", {}).items()}
        except (OSError, ValueError, AttributeError):
            self.booted, self.seen = set(), {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(
            {"boot": sorted(self.booted), "seen": {u: sorted(s) for u, s in self.seen.items()}}
        )
        temporary = self.state_path.with_name(f".state.{os.getpid()}")
        temporary.write_text(text, encoding="utf-8")
        os.rename(temporary, self.state_path)

    def _start(self, name: str, reason: str) -> None:
        try:
            if self.runner.running(name):
                return
            if self.runner.start(name):
                log(f"started {name} ({reason})")
        except (OSError, subprocess.SubprocessError, TimeoutError, ValueError) as error:
            log(f"could not start {name} ({reason}): {error}")

    def evaluate(self) -> None:
        names = space_names(self.root)
        before = (set(self.booted), {u: set(s) for u, s in self.seen.items()})
        for name in names:
            if boot_enabled(name, self.root) and name not in self.booted:
                self.booted.add(name)
                self._start(name, "boot")
        wanted: dict[int, list[str]] = {}
        for name in names:
            for user in read_users(name, self.root):
                uid = self.uid_of(user)
                if uid is not None:
                    wanted.setdefault(uid, []).append(name)
        # Users that lost their enablement or went away must not keep a stale
        # login record: forget them so that re-enabling counts as a new login.
        for uid in [u for u in self.seen if int(u) not in wanted]:
            del self.seen[uid]
        for uid, spaces in sorted(wanted.items()):
            key = str(uid)
            state = self.logins.state(uid)
            if state not in ACTIVE_STATES:
                self.seen[key] = set()
                continue
            tokens = set(self.logins.sessions(uid))
            if state == "lingering" and not tokens:
                tokens = {LINGER_TOKEN}
            fresh = tokens - self.seen.get(key, set())
            self.seen[key] = tokens
            if not fresh:
                continue
            for name in spaces:
                self._start(name, f"login of uid {uid}")
        if (self.booted, self.seen) != before:
            self._save()

    def run(self, stop: Callable[[], bool], interval: float = POLL_SECONDS) -> None:
        while not stop():
            try:
                self.evaluate()
            except Exception as error:  # noqa: BLE001 - the daemon must survive
                log(f"evaluation failed: {error}")
            self.logins.wait(interval)


# ----------------------------------------------------------------- service


def gc_services(
    *,
    root: Path = STATE_ROOT,
    svdir: Path | None = None,
    forget: Callable[[str], None] | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Remove runit service dirs of spaces whose info.json no longer exists.

    The autostart service itself is never touched. Returns the space names.
    """

    from . import lxc

    base = svdir if svdir is not None else lxc._runit_dirs()[0]
    try:
        candidates = sorted(base.glob("spaces-*"))
    except OSError:
        return []
    removed = []
    for service in candidates:
        if service.name == SERVICE_NAME or not service.is_dir():
            continue
        name = service.name[len("spaces-") :]
        if (root / name / "info.json").exists():
            continue
        removed.append(name)
        if dry_run:
            continue
        if forget is not None:
            forget(name)
        else:
            lxc.LxcBackend().forget_unit(name)
    return removed


def main(argv: Iterable[str] | None = None) -> int:
    del argv
    if os.geteuid() != 0:
        print("spaces-autostart: must run as root", file=sys.stderr)
        return 1
    def handler(_signum: int, _frame: object) -> None:
        # poll() would otherwise resume after the signal until its timeout.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    try:
        logins = ElogindLogins()
    except OSError as error:
        log(f"libelogind is not usable: {error}")
        time.sleep(5)
        return 1
    log("started")
    try:
        Autostarter(logins, RunitRunner()).run(lambda: False)
    except SystemExit:
        pass
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
