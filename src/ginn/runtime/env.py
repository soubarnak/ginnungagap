# SPDX-License-Identifier: Apache-2.0
"""Session environment for exec and launch in a realm (FR-046, FR-061, IR-025, AS-009).

`session_env` reads an explicit allowlist from the host environment. It never copies the whole
host environment, so SSH, GPG and D-Bus variables stay out (DR-022, AS-012). It returns:
- `env`: the variables to set in the realm for each exec or launch. It never holds a proxy value.
- `pass_through`: the names of the proxy variables that the host sets. Their values are never
  stored in this module's result. The names go on the podman argv as `--env NAME`, and podman reads
  each value from its own environment, so no value appears in `/proc/<pid>/cmdline` (ADR-010).
- `binds`: the session paths to attach, as `SessionBind` pairs. The display sockets, the audio
  sockets and the X11 authority file are here. `mounts.attach_plan` turns them into attach entries
  after start, and ginn-rebind (ADR-008) attaches the same pairs at enter and launch.
- `skipped`: each value or socket left out, with its reason. A value's text is never printed.

Each `SessionBind` is checked with the DR-004 rules (`domain.paths.path_refusal`) before it is
returned, so a path the mount plan would refuse is dropped here with a reason. Each source must also
be of the file type its role needs: a socket, a regular file or a directory. A source of another
type is skipped and reported, as a missing one is, and it never fails a start. The filesystem reads
are the stat probes, which tests replace with a fake, and the `realpath` in the D-Bus check.

Rules, by variable:
- HOME is the host home path, and XDG_RUNTIME_DIR is `/run/user/<uid>`. Both are always set.
- WAYLAND_DISPLAY is passed only when the host sets it. The host socket is a relative name looked
  up under the host runtime directory, or an absolute path. It must be a socket. It is bound into
  the realm at the fixed name `/run/user/<uid>/wayland-0`, and the realm's WAYLAND_DISPLAY is
  `wayland-0`. The host's own name does not reach the realm. A name or path that is, or resolves
  to, the D-Bus session socket `bus` is refused (AS-012). A missing socket drops the variable too,
  so toolkits fall back cleanly.
- DISPLAY is passed only for a local display, `:N` or `:N.S`, when `/tmp/.X11-unix/XN` is a socket.
  The `/tmp/.X11-unix` directory, which must be a directory, is then attached read-only at its own
  path. A remote display is not granted.
- XAUTHORITY is set to the fixed realm path `/run/user/<uid>/ginn-xauthority`, and the host
  authority file, which must be a regular file, is attached read-only there. Because the realm path
  does not depend on the host name, a host authority file that is renamed or replaced is picked up
  by the re-attach at enter and launch, when its source changed (ADR-008). A host that renames the
  file at each login is covered the same way.
- PipeWire (`pipewire-0`) and PulseAudio (`pulse/native`) are bound when they exist, at their
  standard names under `/run/user/<uid>`. Each must be a socket. No variable is set for them,
  because clients find both under XDG_RUNTIME_DIR. A session with no audio socket gives no audio
  binds, and that is not reported as a skip. An audio path of another type is reported.
- LANG and TZ are set in `env` when the host sets them. The proxy variables are named in
  `pass_through` when the host sets them. A value with a NUL or a newline is dropped and reported,
  and its text is never printed. Nothing else from the host environment is passed.

The host runtime directory comes from the host XDG_RUNTIME_DIR when it is absolute. Otherwise it
is `/run/user/<uid>`.
"""

import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ginn.domain.paths import Mode, path_refusal
from ginn.runtime.mounts import SessionBind, Skipped, runtime_dir

X11_DIR: Final = "/tmp/.X11-unix"  # noqa: S108 - fixed host socket dir, not a temp file
PIPEWIRE_SOCKET: Final = "pipewire-0"
PULSE_SOCKET: Final = "pulse/native"
WAYLAND_REALM: Final = "wayland-0"
"""The Wayland socket's name inside the realm, whatever the host calls it."""
AUTHORITY_REALM: Final = "ginn-xauthority"
"""The authority file's name inside the realm, under the runtime directory."""
DBUS_NAME: Final = "bus"
"""The D-Bus session socket's name. A WAYLAND_DISPLAY that is or resolves to it is refused."""
PROXY_VARIABLES: Final = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "no_proxy",
    "NO_PROXY",
)
"""The proxy variables. Only their names are returned, in `SessionEnv.pass_through`."""
PASSED_VARIABLES: Final = ("LANG", "TZ")
"""The host variables that are set in the realm as they are, besides the ones this module sets."""
_SOCKET_NAME: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
_LOCAL_DISPLAY: Final = re.compile(r":([0-9]+)(?:\.[0-9]+)?")
_CONTROL: Final = ("\0", "\n", "\r")
_TYPES: Final = {
    stat.S_IFSOCK: ("socket", "not a socket"),
    stat.S_IFREG: ("file", "not a regular file"),
    stat.S_IFDIR: ("directory", "not a directory"),
}
"""Per file type: the noun for its missing reason, and its wrong-type reason."""

FileType = Callable[[str], int | None]
"""The file type of a host path, as `stat.S_IFMT`, or None when it is missing. Tests fake it."""


def _host_file_type(path: str) -> int | None:
    """The file type of the target that `path` names, or None. A link is followed, as podman does.

    Any failure counts as missing. That covers a dangling link, a permission error and a path that
    holds a NUL, which `os.stat` refuses with ValueError.
    """
    try:
        return stat.S_IFMT(os.stat(path).st_mode)
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class SessionEnv:
    """The exec-time environment, the proxy names, the session binds and the skips for one realm.

    `pass_through` holds names only. A proxy value is never stored in this object.
    """

    env: dict[str, str]
    binds: list[SessionBind]
    skipped: list[Skipped] = field(default_factory=list)
    pass_through: list[str] = field(default_factory=list)


def session_env(
    host_env: Mapping[str, str],
    *,
    home: Path,
    uid: int,
    owned_paths: Sequence[Path],
    file_type: FileType = _host_file_type,
) -> SessionEnv:
    """Return the realm environment, proxy names and session binds for the host session."""
    if not home.is_absolute():
        raise ValueError(f"host home must be absolute, not {home}")
    realm_runtime = runtime_dir(uid)
    host_runtime = _host_runtime(host_env, uid)
    result = SessionEnv(env={"HOME": str(home), "XDG_RUNTIME_DIR": realm_runtime}, binds=[])
    checks = _Checks(env=host_env, home=home, owned_paths=owned_paths, file_type=file_type)
    _wayland(host_env, host_runtime, realm_runtime, checks, result)
    _x11(host_env, checks, result)
    _authority(host_env, realm_runtime, checks, result)
    _audio(host_runtime, realm_runtime, checks, result)
    _values(host_env, result)
    return result


@dataclass(frozen=True)
class _Checks:
    env: Mapping[str, str]
    home: Path
    owned_paths: Sequence[Path]
    file_type: FileType

    def refusal(self, path: str, mode: Mode) -> str | None:
        """Why `path` may not be bound, or None. path_refusal also checks separators and NUL."""
        return path_refusal(
            path, env=self.env, home=self.home, owned_paths=self.owned_paths, mode=mode
        )


def _host_runtime(host_env: Mapping[str, str], uid: int) -> str:
    raw = host_env.get("XDG_RUNTIME_DIR", "")
    if raw and os.path.isabs(raw) and not any(char in raw for char in _CONTROL):
        return raw
    return runtime_dir(uid)


def _skip(result: SessionEnv, name: str, reason: str) -> None:
    result.skipped.append(Skipped("session", name, reason))


def _present(result: SessionEnv, checks: _Checks, name: str, host: str, want: int) -> bool:
    """True when `host` is absolute and is of the file type `want`. Otherwise record a skip.

    A missing source and one of another type are skipped the same way. The absolute test runs
    first, so a relative path is never looked up in the current directory.
    """
    if not os.path.isabs(host):
        _skip(result, name, "not an absolute path")
        return False
    noun, wrong_type = _TYPES[want]
    found = checks.file_type(host)
    if found is None:
        _skip(result, name, f"{noun} missing")
        return False
    if found != want:
        _skip(result, name, wrong_type)
        return False
    return True


def _record(
    result: SessionEnv,
    checks: _Checks,
    name: str,
    host: str,
    realm: str,
    mode: Mode,
    want: int,
    is_dir: bool = False,
) -> bool:
    """Record a bind of `host` when it is present with the file type `want` and passes the check."""
    if not _present(result, checks, name, host, want):
        return False
    reason = checks.refusal(host, mode)
    if reason is not None:
        _skip(result, name, reason)
        return False
    result.binds.append(SessionBind(host, realm, mode, is_dir))
    return True


def _is_dbus(host: str) -> bool:
    """True when `host` is named `bus`, or resolves to a file named `bus` (the D-Bus socket)."""
    return DBUS_NAME in (os.path.basename(host), os.path.basename(os.path.realpath(host)))


def _wayland(
    host_env: Mapping[str, str],
    host_runtime: str,
    realm_runtime: str,
    checks: _Checks,
    result: SessionEnv,
) -> None:
    raw = host_env.get("WAYLAND_DISPLAY", "")
    if not raw:
        return
    if any(char in raw for char in _CONTROL):
        _skip(result, "WAYLAND_DISPLAY", "contains NUL or a newline")
        return
    if raw.startswith("/"):
        host = raw
    elif _SOCKET_NAME.fullmatch(raw):
        host = f"{host_runtime}/{raw}"
    else:
        _skip(result, "WAYLAND_DISPLAY", "not a socket name")
        return
    if _is_dbus(host):
        _skip(result, "WAYLAND_DISPLAY", "AS-012: the D-Bus session socket, not a display")
        return
    realm = f"{realm_runtime}/{WAYLAND_REALM}"
    if _record(result, checks, "WAYLAND_DISPLAY", host, realm, "rw", stat.S_IFSOCK):
        result.env["WAYLAND_DISPLAY"] = WAYLAND_REALM


def _x11(host_env: Mapping[str, str], checks: _Checks, result: SessionEnv) -> None:
    raw = host_env.get("DISPLAY", "")
    if not raw:
        return
    match = _LOCAL_DISPLAY.fullmatch(raw)
    if match is None:
        _skip(result, "DISPLAY", "only a local display is granted (:N)")
        return
    socket = f"{X11_DIR}/X{match.group(1)}"
    if not _present(result, checks, "DISPLAY", socket, stat.S_IFSOCK):
        return
    if _record(result, checks, "DISPLAY", X11_DIR, X11_DIR, "ro", stat.S_IFDIR, is_dir=True):
        result.env["DISPLAY"] = raw


def _authority(
    host_env: Mapping[str, str],
    realm_runtime: str,
    checks: _Checks,
    result: SessionEnv,
) -> None:
    raw = host_env.get("XAUTHORITY", "")
    if not raw:
        return
    realm = f"{realm_runtime}/{AUTHORITY_REALM}"
    if _record(result, checks, "XAUTHORITY", raw, realm, "ro", stat.S_IFREG):
        result.env["XAUTHORITY"] = realm


def _audio(host_runtime: str, realm_runtime: str, checks: _Checks, result: SessionEnv) -> None:
    for socket in (PIPEWIRE_SOCKET, PULSE_SOCKET):
        host = f"{host_runtime}/{socket}"
        # A missing audio socket is silent. One of another type is present, so it is reported.
        if checks.file_type(host) is not None:
            _record(result, checks, socket, host, f"{realm_runtime}/{socket}", "rw", stat.S_IFSOCK)


def _values(host_env: Mapping[str, str], result: SessionEnv) -> None:
    """Set LANG and TZ in `env`, and name each proxy variable the host sets in `pass_through`.

    A proxy value is read here only to check it for NUL and newline. It is never stored.
    """
    for name in (*PASSED_VARIABLES, *PROXY_VARIABLES):
        raw = host_env.get(name, "")
        if not raw:
            continue
        if any(char in raw for char in _CONTROL):
            _skip(result, name, "contains NUL or a newline")
        elif name in PROXY_VARIABLES:
            result.pass_through.append(name)
        else:
            result.env[name] = raw
