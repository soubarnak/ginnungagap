# SPDX-License-Identifier: Apache-2.0
"""The session D-Bus filter of a realm: realm applications reach the host session bus only
through xdg-dbus-proxy, with notifications and desktop portals allowed (FR-068, AS-012).

The proxy listens on a host socket under the ginn runtime directory. The realm sees that socket
at `/run/user/<uid>/bus`, and `DBUS_SESSION_BUS_ADDRESS` points there at exec. The host bus
socket itself is never bound into the realm, so a Basic realm cannot call the Secret Service or any
other name outside the allowlist (FR-068 AC-1, AS-012). Custom may widen the allowlist, and only the
names the caller passes are added.

A session address may list several buses, separated by `;`. A realm on the host network namespace
(every non-Kali realm, FR-026) can reach an abstract socket directly, so an address with any
`abstract=` member is refused. The proxy is given the first `unix:path=` member only.

The proxy is lazy (ADR-007, NFR-002). It is one per realm. It starts with the first app that ginn
launches in that realm, and it stops when the last such app exits. An idle realm has no proxy.
`ProxyState` records the launched apps and the proxy, each with its start time (`Proc`). The rule
is `reconcile`, and it is stateless: the caller persists `ProxyState` under the realm lock, so a
second `ginn launch` sees the apps of the first. An app that is gone is dropped at the next
reconcile, so a launch that died without its exit leaves no proxy running.

Pure planning: `realm_plan`, `proxy_argv` and `select_host_bus` read nothing. Only `start_proxy`
and `stop_proxy` start or stop a process, through the injected launcher and `stop_group`.
"""

from __future__ import annotations

import enum
import os
import re
import stat
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from ginn.errors import GinnError, make
from ginn.runtime.mounts import SessionBind, runtime_dir
from ginn.security._proc import Child, Launcher, Proc, start_ready, stop_group

PROXY_NAME: Final = "xdg-dbus-proxy"
TALK_DEFAULT: Final[tuple[str, ...]] = ("org.freedesktop.Notifications", "org.freedesktop.portal.*")
"""The Basic allowlist of FR-068 and AS-012: notifications and the desktop portals."""
REALM_BUS: Final = "bus"
"""The realm's session bus socket name, under the runtime directory. The standard path."""
_NAME: Final = re.compile(r"[A-Za-z_-][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+(\.\*)?")
"""A well-known D-Bus name, optionally ending in `.*` to allow a prefix (talk and own only)."""


class Action(enum.Enum):
    """What `reconcile` asks for: start or stop the proxy, or keep it as it is."""

    START = "start"
    STOP = "stop"
    KEEP = "keep"
    IDLE = "idle"


@dataclass(frozen=True)
class HostBus:
    """The host session bus that the proxy may use: the address member and its socket path."""

    address: str
    path: str


@dataclass(frozen=True)
class ProxyPlan:
    """One realm's proxy: the host socket, the argv, the realm bind and the exec environment."""

    listen: str
    argv: tuple[str, ...]
    bind: SessionBind
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProxyState:
    """The ginn-launched apps of one realm, and its proxy (None when none runs).

    Every pid is stored with its start time (`Proc`), so a reused pid is never taken for a live app
    or proxy.
    """

    realm: str
    apps: frozenset[Proc] = frozenset()
    proxy: Proc | None = None

    def to_dict(self) -> dict[str, object]:
        """The JSON form that the realm record keeps. Apps are sorted by pid, so text is stable."""
        apps = sorted(self.apps, key=lambda proc: proc.pid)
        return {
            "realm": self.realm,
            "apps": [proc.to_dict() for proc in apps],
            "proxy": None if self.proxy is None else self.proxy.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ProxyState:
        """Read the JSON form back. A malformed value raises, so a bad record starts no proxy."""
        realm, apps, proxy = data.get("realm"), data.get("apps"), data.get("proxy")
        if not isinstance(realm, str) or not isinstance(apps, list):
            raise ValueError("a proxy state needs a realm name and a list of apps")
        records = frozenset(Proc.from_dict(app) for app in apps)
        return cls(realm, records, None if proxy is None else Proc.from_dict(proxy))


def select_host_bus(address: str) -> HostBus:
    """The host session bus for the proxy, from a session address that may list several buses.

    Members are separated by `;`. Any member with an `abstract=` key is refused: a realm that shares
    the host network namespace could connect to the abstract socket directly, past the proxy
    (AS-012). So is any member that is not a `unix:` address, such as a TCP one, which the same
    namespace would reach. The first `unix:path=<absolute>` member is chosen. An address with no
    such member raises ValueError. The caller reports the refusal.
    """
    chosen: HostBus | None = None
    for member in address.split(";"):
        if not member.startswith("unix:"):
            if member.strip():
                raise ValueError(
                    "a session address member that is not a unix socket could reach the bus "
                    "past the proxy (AS-012)"
                )
            continue
        keys = [part.partition("=") for part in member[len("unix:") :].split(",")]
        if any(key == "abstract" for key, _, _ in keys):
            raise ValueError(
                "the session bus has an abstract socket, which a realm could reach (AS-012)"
            )
        if chosen is None:
            for key, _, value in keys:
                if key == "path" and value.startswith("/"):
                    chosen = HostBus(member, value)
                    break
    if chosen is None:
        raise ValueError("the session bus address names no unix socket path (AS-012)")
    return chosen


def realm_bus(uid: int) -> str:
    """The realm-side path of the session bus proxy: `/run/user/<uid>/bus`."""
    return f"{runtime_dir(uid)}/{REALM_BUS}"


def _names(names: Iterable[str]) -> list[str]:
    """Validate D-Bus names for talk or own. A bare wildcard, or a malformed name, raises."""
    checked = []
    for name in names:
        if not _NAME.fullmatch(name):
            raise ValueError(f"not a D-Bus name: {name!r}")
        checked.append(name)
    return checked


def proxy_argv(
    *,
    binary: str,
    bus_address: str,
    listen: str,
    talk: Sequence[str] = TALK_DEFAULT,
    own: Sequence[str] = (),
) -> tuple[str, ...]:
    """The argv that starts xdg-dbus-proxy for one realm (S-05 measured this form).

    `binary` is an absolute path, so the argv never depends on the search path. The proxy is given
    the chosen `unix:path=` member of the host address, and never the other members (see
    `select_host_bus`). The proxy socket must not be the host bus (AS-012). Names are passed as
    separate `--talk=` and `--own=` arguments, never through a shell.
    """
    if not os.path.isabs(binary) or not os.path.isabs(listen):
        raise ValueError("the proxy binary and its socket are absolute paths")
    bus = select_host_bus(bus_address)
    if listen == bus.path:
        raise ValueError("the proxy socket must not be the host session bus (AS-012)")
    return (
        binary,
        bus.address,
        listen,
        "--filter",
        *(f"--talk={name}" for name in _names(talk)),
        *(f"--own={name}" for name in _names(own)),
    )


def realm_plan(
    *,
    bus_address: str,
    listen: str,
    uid: int,
    binary: str,
    talk: Sequence[str] = TALK_DEFAULT,
    own: Sequence[str] = (),
) -> ProxyPlan:
    """The proxy argv, the realm bind of its socket, and the realm's bus address.

    The bind's host is `listen`, the proxy socket. It is never the host bus. The bind is a socket,
    attached after start (a variable grant, ADR-008), so it adds no create-time option.
    """
    argv = proxy_argv(binary=binary, bus_address=bus_address, listen=listen, talk=talk, own=own)
    path = realm_bus(uid)
    return ProxyPlan(
        listen=listen,
        argv=argv,
        bind=SessionBind(host=listen, realm=path, internal=True),
        env={"DBUS_SESSION_BUS_ADDRESS": f"unix:path={path}"},
    )


def reconcile(
    state: ProxyState, *, alive: Callable[[Proc], bool] = Proc.alive
) -> tuple[ProxyState, Action]:
    """Drop the gone apps and a gone proxy, then say what the proxy needs (ADR-007).

    `alive` says whether a process record still runs. The default checks the pid and its start
    time. A proxy runs while an app runs, so START is asked when apps run and no proxy does, and
    STOP when no app runs and a proxy does. On STOP the proxy stays in the returned state, so that
    `stop_proxy` can stop it.
    """
    apps = frozenset(app for app in state.apps if alive(app))
    up = state.proxy is not None and alive(state.proxy)
    if apps and not up:
        action = Action.START
    elif not apps and up:
        action = Action.STOP
    else:
        action = Action.KEEP if up else Action.IDLE
    kept = state.proxy if up else None
    return replace(state, apps=apps, proxy=kept), action


def launched(
    state: ProxyState, app: Proc, *, alive: Callable[[Proc], bool] = Proc.alive
) -> tuple[ProxyState, Action]:
    """Record an app that ginn launched in this realm, then reconcile."""
    return reconcile(replace(state, apps=state.apps | {app}), alive=alive)


def exited(
    state: ProxyState, app: Proc, *, alive: Callable[[Proc], bool] = Proc.alive
) -> tuple[ProxyState, Action]:
    """Forget an app that exited, then reconcile. The last one out stops the proxy."""
    return reconcile(replace(state, apps=state.apps - {app}), alive=alive)


def with_proxy(state: ProxyState, proxy: Proc) -> ProxyState:
    """Record the proxy that a START just started."""
    return replace(state, proxy=proxy)


def stop_proxy(state: ProxyState) -> ProxyState:
    """Stop the proxy that the state names, and its clients, then clear it from the state.

    `stop_group` checks the start time before any signal, so a reused pid is not stopped.
    """
    if state.proxy is not None:
        stop_group(state.proxy)
    return replace(state, proxy=None)


def launcher_for(realm: str, listen: str, log: str) -> Launcher:
    """The real launcher for a realm's proxy. `log` is the helper's stderr file (Owned state)."""

    def failure(detail: str) -> GinnError:
        return make("GINN-E-DBUS-PROXY", realm=realm, detail=detail)

    def launch(argv: Sequence[str]) -> Child:
        return start_ready(argv, listen, log, failure)

    return launch


def start_proxy(plan: ProxyPlan, launch: Launcher) -> Child:
    """Start the proxy for `plan`. A stale socket from a dead proxy is removed first.

    Only a socket is removed. Any other file at the listen path raises, so ginn never deletes a
    file it did not make. The caller records `child.proc` with `with_proxy`.
    """
    try:
        mode = os.lstat(plan.listen).st_mode
    except FileNotFoundError:
        return launch(plan.argv)
    if not stat.S_ISSOCK(mode):
        raise ValueError(f"{plan.listen} exists and is not a socket")
    os.unlink(plan.listen)
    return launch(plan.argv)
