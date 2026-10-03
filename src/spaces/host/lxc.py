"""LXC host backend for runit systems (Void Linux).

Every lxc-* tool runs through the spaces-lxc wrapper, which hides elogind's
named cgroup v1 mount; see void/spike/RESULTS.md. A space is a runit service
that runs ``spaces.priv launch NAME``, which in turn runs lxc-start in the
foreground through run_launcher().
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import devices_lxc, lxc_config
from .base import HostBackend

DEFAULT_LXC_PATH = "/run/spaces/lxc"
DEFAULT_WRAPPER = "/usr/lib/spaces/spaces-lxc"
DEFAULT_SVDIR = "/etc/sv"
DEFAULT_SERVICE_DIR = "/var/service"
DEFAULT_PRIV = "/usr/bin/spaces.priv"
DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup"
APPARMOR_PROFILE = "spaces-container"
APPARMOR_PROFILES = Path("/sys/kernel/security/apparmor/profiles")
RUNIT_SUPERVISE = "/run/runit"
LOG_ROOT = "/var/log/spaces"
PROC = Path("/proc")
RUN_USER = Path("/run/user")
SYSTEMD_RUN = "/usr/bin/systemd-run"
SERVICE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
START_TIMEOUT = 120.0
STOP_TIMEOUT = 60.0
SERVICE_TIMEOUT = 15.0
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def lxc_path() -> Path:
    return Path(os.environ.get("SPACES_LXC_PATH", DEFAULT_LXC_PATH))


def wrapper_path() -> str:
    configured = os.environ.get("SPACES_LXC_WRAPPER")
    if configured:
        return configured
    if os.path.exists(DEFAULT_WRAPPER):
        return DEFAULT_WRAPPER
    return str(Path(__file__).resolve().parents[3] / "void/bin/spaces-lxc")


def _check_name(name: str) -> str:
    if not _NAME.match(name):
        raise ValueError(f"unsafe space name: {name!r}")
    return name


def _which(tool: str) -> str:
    return shutil.which(tool) or tool


def _lxc(tool: str, name: str, *args: str) -> list[str]:
    return [
        wrapper_path(),
        _which(tool),
        "-P",
        str(lxc_path()),
        "-n",
        _check_name(name),
        *args,
    ]


def _quiet(command: Sequence[str], *, timeout: float = 30) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124)


def _runtime_dir(name: str) -> Path:
    return lxc_path() / _check_name(name)


def _atomic_write(path: Path, text: str, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _write_if_changed(path: Path, text: str, mode: int) -> None:
    try:
        if path.read_text(encoding="utf-8") == text:
            os.chmod(path, mode)
            return
    except OSError:
        pass
    _atomic_write(path, text, mode)


def _ensure_symlink(path: Path, target: str) -> None:
    if path.is_symlink() and os.readlink(path) == target:
        return
    if path.is_symlink() or path.exists():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    path.symlink_to(target)


def _runit_dirs() -> tuple[Path, Path]:
    svdir = Path(os.environ.get("SPACES_RUNIT_SVDIR", DEFAULT_SVDIR))
    service_dir = Path(
        os.environ.get("SPACES_RUNIT_SERVICE_DIR", DEFAULT_SERVICE_DIR)
    )
    # /var/service is itself a symlink; link and unlink in the real directory.
    return svdir, Path(os.path.realpath(service_dir))


def _service_name(name: str) -> str:
    return f"spaces-{_check_name(name)}"


def _sv(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_which("sv"), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _service_state(name: str) -> str | None:
    """Return "run", "down" or None when runsv does not supervise it."""

    _svdir, service_dir = _runit_dirs()
    completed = _sv("status", str(service_dir / _service_name(name)))
    if completed.returncode != 0 and not completed.stdout.strip():
        return None
    word = completed.stdout.partition(":")[0].strip()
    return word if word in ("run", "down", "finish") else None


def ensure_service(name: str) -> Path:
    """Create the runit service for a space and wait for runsv to adopt it."""

    service = _service_name(name)
    svdir, service_dir = _runit_dirs()
    directory = svdir / service
    log_directory = directory / "log"
    log_directory.mkdir(parents=True, exist_ok=True)
    marker = _runtime_dir(name) / "ready"
    priv = os.environ.get("SPACES_PRIV", DEFAULT_PRIV)
    _write_if_changed(
        directory / "run",
        "#!/bin/sh\n"
        "exec 2>&1\n"
        f"export PATH={shlex.quote(SERVICE_PATH)} HOME=/root LANG=C.UTF-8\n"
        f"exec {shlex.quote(priv)} launch {shlex.quote(name)}\n",
        0o755,
    )
    _write_if_changed(
        directory / "finish",
        f"#!/bin/sh\nrm -f {shlex.quote(str(marker))}\n",
        0o755,
    )
    # A permanent down file: runsv restarts services whenever they exit, so
    # spaces are always started with "sv once".
    (directory / "down").touch(mode=0o644)
    _ensure_symlink(directory / "supervise", f"{RUNIT_SUPERVISE}/supervise.{service}")
    log_dir = f"{LOG_ROOT}/{name}"
    _write_if_changed(
        log_directory / "run",
        "#!/bin/sh\n"
        f"mkdir -p {shlex.quote(log_dir)}\n"
        f"exec svlogd -tt {shlex.quote(log_dir)}\n",
        0o755,
    )
    _ensure_symlink(
        log_directory / "supervise",
        f"{RUNIT_SUPERVISE}/supervise.{service}.log",
    )
    link = service_dir / service
    adopted = (link / "supervise" / "ok").exists()
    _ensure_symlink(link, str(directory))
    if not adopted:
        deadline = time.monotonic() + SERVICE_TIMEOUT
        # runsvdir rescans its directory every five seconds.
        while not (link / "supervise" / "ok").exists():
            if time.monotonic() > deadline:
                raise TimeoutError(f"runsv did not adopt {service}")
            time.sleep(0.2)
    return link


def _preexisting_resolv(rootfs: Path, target: str) -> None:
    path = rootfs / target
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        raise OSError(f"{path} is a directory")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o644)


def _precreate(rootfs: Path, relative: str, kind: str) -> None:
    """Create a bind mount point without ever following a symlink."""

    parts = relative.split("/")
    current = rootfs
    for index, part in enumerate(parts):
        current = current / part
        last = index == len(parts) - 1
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            if last and kind == "file":
                current.touch(mode=0o644)
            else:
                current.mkdir(mode=0o755)
            continue
        if stat.S_ISLNK(mode):
            raise OSError(f"refusing to follow symlink at {current}")
        if not last and not stat.S_ISDIR(mode):
            raise OSError(f"{current} is not a directory")
        if last and (kind == "dir") != bool(stat.S_ISDIR(mode)):
            raise OSError(f"{current} has the wrong type for a bind target")


_StdioState = tuple[int, int, int, int]


def _stdio_snapshot(*extra: int | None) -> list[_StdioState]:
    """Record owner and mode of regular files used as standard descriptors."""

    saved: list[_StdioState] = []
    for descriptor in (0, 1, 2, *(e for e in extra if isinstance(e, int))):
        try:
            status = os.fstat(descriptor)
        except OSError:
            continue
        if stat.S_ISREG(status.st_mode):
            saved.append(
                (
                    descriptor,
                    status.st_uid,
                    status.st_gid,
                    stat.S_IMODE(status.st_mode),
                )
            )
    return saved


def _restore_stdio(saved: Sequence[_StdioState]) -> None:
    for descriptor, uid, gid, mode in saved:
        try:
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, mode)
        except OSError:
            pass


@contextlib.contextmanager
def _preserve_stdio(*extra: int | None) -> Iterator[None]:
    """Undo lxc-attach's ownership change of regular-file stdio.

    lxc-attach hands the standard descriptors to the payload's user (root) and
    drops group and other access, which would leave a user's ``> out.txt``
    owned by root with mode 0600 after ``spaces enter NAME -- cmd > out.txt``.
    Pipes, sockets and terminals do not need this.
    """

    saved = _stdio_snapshot(*extra)
    try:
        yield
    finally:
        _restore_stdio(saved)


class _GuestProcess(subprocess.Popen):  # type: ignore[type-arg]
    """A guest command started with spawn_in_guest.

    The command runs in a transient guest unit, so killing the lxc-attach
    client would leave it running. terminate() and kill() stop the unit first.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        name: str,
        unit: str,
        saved: Sequence[_StdioState],
    ) -> None:
        self._guest_name = name
        self._guest_unit = unit
        self._saved_stdio = list(saved)
        super().__init__(command)

    def _restore(self) -> None:
        saved, self._saved_stdio = self._saved_stdio, []
        _restore_stdio(saved)

    def poll(self) -> int | None:
        code = super().poll()
        if code is not None:
            self._restore()
        return code

    def wait(self, timeout: float | None = None) -> int:
        code = super().wait(timeout)
        self._restore()
        return code

    def _stop_unit(self, *arguments: str) -> None:
        _quiet(
            _lxc(
                "lxc-attach",
                self._guest_name,
                "--clear-env",
                "--",
                "/usr/bin/systemctl",
                *arguments,
                self._guest_unit,
            ),
            timeout=30,
        )

    def terminate(self) -> None:
        if self.returncode is None:
            self._stop_unit("stop")
        super().terminate()

    def kill(self) -> None:
        if self.returncode is None:
            self._stop_unit("kill", "--signal=SIGKILL")
        super().kill()


class _Launcher:
    """Popen-like handle for one lxc-start run."""

    def __init__(
        self,
        backend: LxcBackend,
        name: str,
        process: subprocess.Popen[Any],
        cleanup: Sequence[Any],
        stop_event: threading.Event,
        threads: Sequence[threading.Thread],
    ) -> None:
        self._backend = backend
        self._name = name
        self._process = process
        self._cleanup_actions = list(cleanup)
        self._stop_event = stop_event
        self._threads = list(threads)
        self._stop_lock = threading.RLock()
        self._stopper: threading.Thread | None = None
        self._cleaned = False
        self._clean_lock = threading.Lock()
        self.pid = process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def _finish(self) -> None:
        with self._clean_lock:
            if self._cleaned:
                return
            self._cleaned = True
        self._stop_event.set()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=10)
        for action in self._cleanup_actions:
            try:
                action()
            except OSError:
                pass

    def poll(self) -> int | None:
        code = self._process.poll()
        if code is not None:
            self._finish()
        return code

    def wait(self, timeout: float | None = None) -> int:
        code = self._process.wait(timeout)
        self._finish()
        return code

    def _graceful_stop(self) -> None:
        deadline = time.monotonic() + STOP_TIMEOUT
        while self._process.poll() is None and time.monotonic() < deadline:
            completed = _quiet(
                _lxc("lxc-stop", self._name, "-t", "30"), timeout=60
            )
            if completed.returncode == 0:
                return
            # lxc-stop fails until the container exists; retry while the
            # launcher is still starting.
            time.sleep(0.5)

    def send_signal(self, signum: int) -> None:
        if self._process.poll() is not None:
            return
        if signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self._stop_lock:
                if self._stopper is not None and self._stopper.is_alive():
                    return
                self._stopper = threading.Thread(
                    target=self._graceful_stop,
                    name=f"spaces-lxc-stop-{self._name}",
                    daemon=True,
                )
                self._stopper.start()
        else:
            self._process.send_signal(signum)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        _quiet(_lxc("lxc-stop", self._name, "-k"), timeout=30)
        if self._process.poll() is None:
            self._process.kill()


class LxcBackend(HostBackend):
    # ------------------------------------------------------------------ state

    def _state(self, name: str) -> str | None:
        completed = subprocess.run(
            _lxc("lxc-info", name, "-s", "-H"),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None

    def _init_pid(self, name: str) -> int:
        completed = subprocess.run(
            _lxc("lxc-info", name, "-p", "-H"),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return int(completed.stdout.strip())

    def is_running(self, name: str) -> bool:
        return self._state(name) == "RUNNING"

    def probe_registered(self, name: str) -> bool:
        return self.is_running(name)

    def probe_guest_shell(self, name: str) -> bool:
        return _quiet(
            _lxc(
                "lxc-attach",
                name,
                "--clear-env",
                "--",
                SYSTEMD_RUN,
                "--quiet",
                "--wait",
                "--collect",
                "--service-type=exec",
                "/usr/bin/true",
            ),
            timeout=60,
        ).returncode == 0

    # --------------------------------------------------------------- services

    def start_unit(self, name: str) -> int:
        marker = _runtime_dir(name) / "ready"
        try:
            ensure_service(name)
            if _service_state(name) != "run":
                completed = _sv("once", str(_runit_dirs()[1] / _service_name(name)))
                if completed.returncode != 0:
                    return completed.returncode or 1
        except (OSError, TimeoutError, subprocess.SubprocessError):
            return 1
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if marker.exists():
                return 0
            if _service_state(name) != "run":
                # The launcher exited before the guest became ready.
                return 1 if not marker.exists() else 0
            time.sleep(0.5)
        return 1

    def stop_unit(self, name: str) -> int:
        _service_name(name)
        _svdir, service_dir = _runit_dirs()
        link = service_dir / _service_name(name)
        if link.exists():
            _sv("down", str(link))
        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline:
            if self._state(name) in (None, "STOPPED"):
                return 0
            time.sleep(0.5)
        _quiet(_lxc("lxc-stop", name, "-k"), timeout=30)
        time.sleep(1)
        if self._state(name) in (None, "STOPPED"):
            return 0
        raise subprocess.CalledProcessError(
            1, _lxc("lxc-stop", name, "-k"), stderr="space did not stop"
        )

    def try_restart_unit(self, name: str) -> int:
        if _service_state(name) == "run" or self.is_running(name):
            self.stop_unit(name)
            code = self.start_unit(name)
            if code != 0:
                raise subprocess.CalledProcessError(
                    code, ["start", _service_name(name)]
                )
        return 0

    def forget_unit(self, name: str) -> None:
        svdir, service_dir = _runit_dirs()
        service = _service_name(name)
        if (service_dir / service).exists() or (svdir / service).exists():
            self.stop_unit(name)
        link = service_dir / service
        if link.is_symlink():
            link.unlink()
        shutil.rmtree(svdir / service, ignore_errors=True)
        for suffix in ("", ".log"):
            shutil.rmtree(
                Path(RUNIT_SUPERVISE) / f"supervise.{service}{suffix}",
                ignore_errors=True,
            )
        shutil.rmtree(_runtime_dir(name), ignore_errors=True)

    def enable_user_autostart(self, user_name: str, name: str) -> None:
        from .. import core

        command = ["enable-user-autostart", user_name, name]
        if not user_name or any(c.isspace() or c == "/" for c in user_name):
            raise subprocess.CalledProcessError(1, command, stderr="bad user")
        path = core.STATE_ROOT / _check_name(name) / "autostart-users"
        try:
            try:
                users = path.read_text(encoding="utf-8").split()
            except FileNotFoundError:
                users = []
            if user_name not in users:
                users.append(user_name)
            _atomic_write(path, "".join(f"{user}\n" for user in users), 0o644)
            if os.geteuid() == 0:
                os.chown(path, 0, 0)
        except OSError as error:
            raise subprocess.CalledProcessError(
                1, command, stderr=str(error)
            ) from error

    # --------------------------------------------------------------- launcher

    def _cgroup_root(self) -> Path:
        return Path(os.environ.get("SPACES_CGROUP_ROOT", DEFAULT_CGROUP_ROOT))

    def _load_apparmor(self) -> None:
        try:
            if any(
                line.startswith(f"{APPARMOR_PROFILE} ")
                for line in APPARMOR_PROFILES.read_text().splitlines()
            ):
                return
        except OSError:
            pass
        candidates = [
            os.environ.get("SPACES_APPARMOR_PROFILE", ""),
            f"/etc/apparmor.d/{APPARMOR_PROFILE}",
            str(Path(__file__).resolve().parents[3] / "void/apparmor" / APPARMOR_PROFILE),
        ]
        profile = next((c for c in candidates if c and os.path.exists(c)), None)
        if profile is None:
            raise FileNotFoundError(f"AppArmor profile {APPARMOR_PROFILE} not found")
        subprocess.run(
            [_which("apparmor_parser"), "-r", profile],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _load_policy(self, runtime: Path) -> list[str] | None:
        try:
            data = json.loads((runtime / "policy.json").read_text())
        except (OSError, ValueError):
            return []
        return None if data.get("level") == "full" else list(data.get("rules", []))

    def run_launcher(
        self, argv: Sequence[str], env: Mapping[str, str] | None
    ) -> _Launcher:
        environment = dict(env or {})
        machine = next(
            (a.partition("=")[2] for a in argv if a.startswith("--machine=")),
            None,
        )
        if not machine:
            raise lxc_config.UnsupportedLaunchOption("--machine is required")
        name = _check_name(machine)
        runtime = _runtime_dir(name)
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(runtime, 0o700)
        marker = runtime / "ready"
        marker.unlink(missing_ok=True)
        self._load_apparmor()

        cgroup_mode = os.environ.get("SPACES_LXC_CGROUP_MODE", "relative")
        cgroup_base = f"spaces/{name}" if cgroup_mode == "relative" else None
        spec = lxc_config.translate(
            argv,
            environment,
            runtime_dir=runtime,
            device_rules=self._load_policy(runtime),
            cgroup_base=cgroup_base,
        )
        for relative, kind in spec.precreate:
            _precreate(spec.rootfs, relative, kind)
        if spec.resolv_target is not None:
            _preexisting_resolv(spec.rootfs, spec.resolv_target)
        _atomic_write(spec.seccomp_path, spec.seccomp_text, 0o600)
        _atomic_write(spec.devices_path, spec.devices_text, 0o600)
        if spec.resolv_conf is not None:
            _sync_resolv(spec.resolv_conf)
        _atomic_write(runtime / "config", spec.config_text, 0o600)

        command = _lxc(
            "lxc-start", name, "-F", "-o", str(runtime / "lxc.log")
        )
        cleanup: list[Any] = [lambda: marker.unlink(missing_ok=True)]
        if cgroup_base is not None:
            base = self._cgroup_root() / cgroup_base
            base.mkdir(parents=True, exist_ok=True)
            # Move lxc-start into its own base cgroup before it execs, so LXC
            # creates the container below it instead of at the cgroup root.
            command[1:1] = [
                _which("sh"),
                "-c",
                'echo $$ >"$1" && shift && exec "$@"',
                "sh",
                str(base / "cgroup.procs"),
            ]
            cleanup.append(lambda: _remove_cgroup(base))
        process = subprocess.Popen(command, env=environment or None)

        stop = threading.Event()
        threads = [
            threading.Thread(
                target=self._ready_loop,
                args=(name, marker, process, stop),
                name=f"spaces-lxc-ready-{name}",
                daemon=True,
            )
        ]
        if spec.resolv_conf is not None:
            threads.append(
                threading.Thread(
                    target=_resolv_loop,
                    args=(spec.resolv_conf, stop),
                    name=f"spaces-lxc-resolv-{name}",
                    daemon=True,
                )
            )
        for thread in threads:
            thread.start()
        return _Launcher(self, name, process, cleanup, stop, threads)

    def _ready_loop(
        self,
        name: str,
        marker: Path,
        process: subprocess.Popen[Any],
        stop: threading.Event,
    ) -> None:
        while not stop.is_set() and process.poll() is None:
            if self.probe_registered(name) and self.probe_guest_shell(name):
                if process.poll() is None:
                    _atomic_write(marker, "ready\n", 0o644)
                return
            stop.wait(1.0)

    # -------------------------------------------------------------- guest exec

    def _attach(
        self,
        user_name: str,
        name: str,
        command: Sequence[str],
        env: Mapping[str, str] | None,
        unit: str | None = None,
    ) -> list[str]:
        settings = sorted((env or {}).items())
        if user_name == "root":
            return _lxc(
                "lxc-attach",
                name,
                "--clear-env",
                *(a for k, v in settings for a in ("-v", f"{k}={v}")),
                "--",
                *command,
            )
        try:
            interactive = sys.stdin.isatty()
        except (ValueError, OSError):
            interactive = False
        return _lxc(
            "lxc-attach",
            name,
            "--clear-env",
            "--",
            SYSTEMD_RUN,
            "--quiet",
            "--pty" if interactive else "--pipe",
            "--wait",
            "--collect",
            "--service-type=exec",
            *((f"--unit={unit}",) if unit else ()),
            f"--uid={user_name}",
            "-p",
            "PAMName=login",
            # "--working-directory=~" is expanded by the client, which has no
            # HOME under --clear-env; the unit property is expanded in the guest.
            "-p",
            "WorkingDirectory=~",
            *(f"--setenv={k}={v}" for k, v in settings),
            "--",
            *command,
        )

    def exec_in_guest(
        self,
        user_name: str,
        name: str,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = False,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.CompletedProcess[Any]:
        options: dict[str, Any] = {}
        if stdout is not None:
            options["stdout"] = stdout
        if stderr is not None:
            options["stderr"] = stderr
        with _preserve_stdio(stdout, stderr):
            return subprocess.run(
                self._attach(user_name, name, command, env),
                check=check,
                **options,
            )

    def spawn_in_guest(
        self,
        user_name: str,
        name: str,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[Any]:
        # The unit name lets terminate() stop a command whose lxc-attach
        # client is gone. Root commands run without a transient unit.
        unit = (
            None
            if user_name == "root"
            else f"spaces-enter-{secrets.token_hex(6)}"
        )
        command_line = self._attach(user_name, name, command, env, unit)
        if unit is None:
            return subprocess.Popen(command_line)
        return _GuestProcess(
            command_line,
            name=name,
            unit=unit,
            saved=_stdio_snapshot(),
        )

    # ------------------------------------------------------------------ mounts

    def _mountns(self, name: str, *arguments: str) -> None:
        pid = self._init_pid(name)
        package_root = str(Path(__file__).resolve().parents[2])
        subprocess.run(
            [
                sys.executable,
                "-m",
                "spaces.host.mountns",
                arguments[0],
                str(pid),
                *arguments[1:],
            ],
            check=True,
            env={
                "PATH": SERVICE_PATH,
                "PYTHONPATH": package_root,
            },
        )

    def bind_into(
        self,
        name: str,
        source: str,
        destination: str,
        *,
        read_only: bool = False,
        mkdir: bool = True,
    ) -> None:
        self._mountns(
            name,
            "bind",
            *(("--read-only",) if read_only else ()),
            *(() if mkdir else ("--no-mkdir",)),
            "--",
            source,
            destination,
        )

    def unmount_in(self, name: str, destination: str) -> None:
        self._mountns(name, "unbind", "--", destination)

    # ----------------------------------------------------------------- devices

    def set_device_policy(
        self,
        name: str,
        level: str,
        allow: Sequence[tuple[str, str]],
    ) -> None:
        command = ["set-device-policy", name, level]
        try:
            rules = None if level == "full" else devices_lxc.translate(allow)
        except devices_lxc.DeviceSpecError as error:
            raise subprocess.CalledProcessError(
                1, command, stderr=str(error)
            ) from error
        runtime = _runtime_dir(name)
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        previous = self._load_policy(runtime)
        policy = {"level": level if level == "full" else "closed", "rules": rules or []}
        _atomic_write(runtime / "policy.json", json.dumps(policy), 0o600)
        _atomic_write(runtime / "devices.conf", devices_lxc.config_text(rules), 0o600)
        if not self.is_running(name):
            return
        old = None if previous is None else devices_lxc.closed_rules(previous)
        new = None if rules is None else devices_lxc.closed_rules(rules)

        def cgroup(action: str, rule: str) -> None:
            subprocess.run(
                _lxc("lxc-cgroup", name, f"devices.{action}", rule),
                check=True,
                stdout=subprocess.DEVNULL,
            )

        if new is None:
            if old is not None:
                cgroup("allow", "a")
        elif old is None:
            cgroup("deny", "a")
            for rule in new:
                cgroup("allow", rule)
        else:
            # Add before removing so the gap in access stays minimal.
            for rule in new:
                if rule not in old:
                    cgroup("allow", rule)
            for rule in old:
                if rule not in new:
                    cgroup("deny", rule)

    # -------------------------------------------------------- host integration

    def login_library_names(self) -> tuple[str, ...]:
        return ("elogind", "libelogind.so.0")

    def host_user_environment(self, uid: int, gid: int) -> str | None:
        hook = RUN_USER / str(uid) / "spaces" / "environment"
        try:
            descriptor = os.open(hook, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            descriptor = -1
        if descriptor >= 0:
            with os.fdopen(descriptor, "rb") as handle:
                status = os.fstat(handle.fileno())
                if stat.S_ISREG(status.st_mode) and status.st_uid == uid:
                    return handle.read().decode("utf-8", errors="replace")
        return _scan_user_environment(uid)

    def session_bus_address(self, uid: int) -> str:
        default = f"unix:path={RUN_USER}/{uid}/bus"
        try:
            if stat.S_ISSOCK(os.stat(RUN_USER / str(uid) / "bus").st_mode):
                return default
        except OSError:
            pass
        block = self.host_user_environment(uid, uid) or ""
        for line in block.splitlines():
            key, _, value = line.partition("=")
            if key == "DBUS_SESSION_BUS_ADDRESS" and value:
                return value
        return default

    def spawn_user_scope(
        self,
        unit: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        description: str,
        uid: int,
        gid: int,
        pass_fds: Sequence[int] = (),
    ) -> subprocess.Popen[Any]:
        return subprocess.Popen(
            list(argv),
            env=dict(env),
            user=uid,
            group=gid,
            extra_groups=[],
            pass_fds=tuple(pass_fds),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def peer_in_space(self, pid: int, name: str) -> bool:
        try:
            value = (PROC / str(pid) / "cgroup").read_text(encoding="utf-8")
        except OSError:
            return False
        for line in value.splitlines():
            path = line.partition(":")[2].partition(":")[2]
            if path == f"/spaces/{name}/payload" or path.startswith(
                f"/spaces/{name}/payload/"
            ):
                return True
            if f"spaces-{name}" in path.split("/"):
                return True
        return False


def _scan_user_environment(uid: int) -> str | None:
    """Return the environment of the user's oldest graphical process."""

    candidates: list[tuple[int, Path]] = []
    try:
        entries = list(PROC.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.name.isdigit():
            try:
                if entry.stat().st_uid == uid:
                    candidates.append((int(entry.name), entry))
            except OSError:
                continue
    for _pid, entry in sorted(candidates):
        try:
            raw = (entry / "environ").read_bytes()
        except OSError:
            continue
        pairs = [
            item.decode("utf-8", errors="replace")
            for item in raw.split(b"\0")
            if b"=" in item
        ]
        keys = {item.partition("=")[0] for item in pairs}
        if {"WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"} <= keys:
            return "".join(f"{item}\n" for item in pairs)
    return None


def _sync_resolv(path: Path) -> None:
    """Copy the host resolver configuration into the bound runtime file."""

    try:
        content = Path("/etc/resolv.conf").read_bytes()
    except OSError:
        content = b""
    # Same inode every time: a bind mount only sees in-place changes.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.pwrite(descriptor, content, 0)
        os.ftruncate(descriptor, len(content))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o644)


def _resolv_loop(path: Path, stop: threading.Event) -> None:
    def signature() -> tuple[int, int, int] | None:
        try:
            status = os.stat("/etc/resolv.conf")
        except OSError:
            return None
        return (status.st_mtime_ns, status.st_size, status.st_ino)

    seen = signature()
    while not stop.wait(5.0):
        current = signature()
        if current != seen:
            seen = current
            try:
                _sync_resolv(path)
            except OSError:
                pass


def _remove_cgroup(base: Path) -> None:
    for _attempt in range(10):
        try:
            # LXC leaves monitor/pivot/lxc.pivot behind; remove bottom-up.
            children = [c for c in base.rglob("*") if c.is_dir()]
            for child in sorted(children, key=lambda c: len(c.parts), reverse=True):
                child.rmdir()
            base.rmdir()
            try:
                base.parent.rmdir()  # only succeeds once no space remains
            except OSError:
                pass
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.2)
