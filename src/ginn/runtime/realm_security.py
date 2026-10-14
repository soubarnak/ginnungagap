# SPDX-License-Identifier: Apache-2.0
"""The security grants of one realm, as session binds and exec variables (FR-062 AC-1, FR-067,
FR-068, FR-069, ADR-008, ADR-012).

This module turns the Agent Proxy (security/agent.py), the session D-Bus filter (security/
dbus_filter.py) and the NVIDIA grant (security/nvidia.py) into the inputs of a session.
`session_extras` returns a `SessionExtras` for runtime/env.py `session_env`, so the start, the
enter, the launch and the re-attach all read the same grants. Nothing here starts a process. The
filter and the proxy are started by ops/security_ops.py, which also keeps their state.

Where the sockets live. The filter socket is `<runtime>/agent/ssh.sock`, and a realm's proxy
socket is `<runtime>/proxy/<realm>/bus.sock`, where `<runtime>` is ginn's own runtime root,
`/run/ginn/<uid>` (ADR-004). That root is checked before use, mode 0700 and owned by the user, so no
socket depends on the caller's environment. These are ginn's own sockets, so attach admits them
through the typed internal grant (ADR-008, CL-14). The host upstream, SSH_AUTH_SOCK and
DBUS_SESSION_BUS_ADDRESS, still come from the session. The realm sees the sockets at the fixed names
of ADR-008, under `/run/user/<uid>` inside the realm.

Missing helpers. The filter helper is installed at `/usr/libexec/ginn/ginn-agent-filter`. The D-Bus
proxy is `xdg-dbus-proxy` on PATH. A bus address that AS-012 refuses, or a missing proxy binary,
grants no bus: the realm then has no session bus, which fails closed.
"""

import os
import shutil
import subprocess  # noqa: S404 - gpgconf is run with a fixed argv list, never through a shell
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ginn.domain.enums import Tier
from ginn.domain.permissions import Toggle, effective_toggles
from ginn.runtime.env import FileType, SessionExtras
from ginn.runtime.mounts import SessionBind, Skipped
from ginn.security import agent, dbus_filter
from ginn.security._proc import Launcher, host_file_type
from ginn.security.nvidia import LIB_DIR, HostProbe

DEFAULT_AGENT_HELPER: Final = "/usr/libexec/ginn/ginn-agent-filter"
"""The installed filter helper (native/Makefile, LIBEXECDIR)."""
AGENT_DIR: Final = "agent"
"""The filter's directory under the ginn runtime root."""
SSH_SOCKET_NAME: Final = "ssh.sock"
PROXY_DIR: Final = "proxy"
"""The proxy directory under the ginn runtime root: one subdirectory per realm."""
BUS_SOCKET_NAME: Final = "bus.sock"
GPG_EXTRA_TIMEOUT_S: Final = 5.0
BUS_NAME: Final = "D-Bus session bus"
"""The name of the session bus in a skip message (FR-068, AS-012)."""


def gpg_extra_host() -> str | None:
    """The host gpg-agent extra socket from `gpgconf`, or None. The argv is a list, not a shell."""
    tool = shutil.which("gpgconf")
    if tool is None:
        return None
    argv = [tool, *agent.GPGCONF_ARGV[1:]]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell (NFR-SEC-005)
            argv, capture_output=True, text=True, timeout=GPG_EXTRA_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return agent.extra_socket_from(result.stdout)


@dataclass(frozen=True)
class SecurityHooks:
    """The seams of the security helpers. The defaults are the installed helpers and the host.

    `agent_launch` and `proxy_launch` replace the real launchers in tests, so no process starts.
    `gpg_extra` resolves the gpg extra socket. `nvidia_probe` is None for the real host (OsProbe).
    """

    agent_helper: str = DEFAULT_AGENT_HELPER
    dbus_binary: str | None = None
    gpg_extra: Callable[[], str | None] = gpg_extra_host
    file_type: FileType = host_file_type
    agent_launch: Launcher | None = None
    proxy_launch: Launcher | None = None
    nvidia_probe: HostProbe | None = None


def agent_enabled(tier: Tier, toggles: frozenset[Toggle]) -> bool:
    """True when the realm has the Agent Proxy: the `agent` toggle (FR-067, Development holds it).

    Only a Development realm has the toggle by its tier, so only it gets the filter (FR-067).
    """
    return Toggle.AGENT in effective_toggles(tier, toggles)


def ssh_listen(runtime: Path) -> str:
    """The host path of the filter socket, under the ginn runtime root (ADR-004, CL-14)."""
    return f"{runtime}/{AGENT_DIR}/{SSH_SOCKET_NAME}"


def bus_listen(runtime: Path, realm: str) -> str:
    """The host path of a realm's proxy socket, under the ginn runtime root (ADR-004, CL-14)."""
    return f"{runtime}/{PROXY_DIR}/{realm}/{BUS_SOCKET_NAME}"


def proxy_binary(hooks: SecurityHooks) -> str | None:
    """The absolute path of xdg-dbus-proxy, or None when the host has none."""
    found = hooks.dbus_binary or shutil.which(dbus_filter.PROXY_NAME)
    return found if found is not None and os.path.isabs(found) else None


def bus_plan(
    host_env: Mapping[str, str],
    *,
    uid: int,
    realm: str,
    runtime: Path,
    hooks: SecurityHooks,
) -> dbus_filter.ProxyPlan | Skipped | None:
    """The proxy plan of a realm, a Skipped for a refused bus, or None when there is no bus to plan.

    None is returned when the host has no session address or no proxy binary. A refused address
    (AS-012, for example one with an abstract socket) returns a Skipped with its reason, so the
    realm gets no bus and the refusal is recorded like any other skipped grant. The plan serves the
    bind (attached once the proxy listens) and the launch (`start_proxy`).
    """
    address = host_env.get("DBUS_SESSION_BUS_ADDRESS", "")
    binary = proxy_binary(hooks)
    if not address or binary is None:
        return None
    try:
        return dbus_filter.realm_plan(
            bus_address=address,
            listen=bus_listen(runtime, realm),
            uid=uid,
            binary=binary,
        )
    except ValueError as err:
        return Skipped("bus", BUS_NAME, str(err))


def stored_gpg_extra(
    hooks: SecurityHooks, *, known: bool, stored: str | None
) -> Callable[[], str | None]:
    """The gpg extra socket of a realm: the value its last start stored, or a lookup (NFR-004).

    A record that knows the socket (`known`) answers with the stored value and runs no `gpgconf`.
    A record that predates it resolves the socket with `hooks.gpg_extra`, which is the fallback.
    """
    if known:
        return lambda: stored
    return hooks.gpg_extra


def session_extras(
    host_env: Mapping[str, str],
    *,
    agent_on: bool,
    uid: int,
    realm: str,
    runtime: Path,
    hooks: SecurityHooks,
    gpu: bool,
    gpg_extra: Callable[[], str | None] | None = None,
) -> SessionExtras:
    """The security grants of one realm's session (see the module docstring).

    `agent_on` is the agent toggle of the realm (FR-067): the filter binds are only for a realm
    that has it. The session bus is for every realm (FR-068). `gpu` is True when the realm was
    created with the NVIDIA CDI grant, so the exec environment names the ginn libraries (ADR-012).
    The gpg extra socket comes from `gpg_extra`, which a caller gives from the record (see
    `stored_gpg_extra`), and from `hooks.gpg_extra` when it gives none. It is asked for only by an
    agent realm, so a realm without the toggle runs no `gpgconf`.
    """
    binds: list[SessionBind] = []
    env: dict[str, str] = {}
    skipped: list[Skipped] = []
    if agent_on:
        lookup = hooks.gpg_extra if gpg_extra is None else gpg_extra
        plan = agent.plan_realm(
            host_env,
            uid=uid,
            helper=hooks.agent_helper,
            listen=ssh_listen(runtime),
            gpg_extra=lookup(),
            file_type=hooks.file_type,
        )
        binds += plan.binds
        env.update(plan.env)
        skipped += plan.skipped
    bus = bus_plan(host_env, uid=uid, realm=realm, runtime=runtime, hooks=hooks)
    if isinstance(bus, Skipped):
        skipped.append(bus)
    elif bus is not None:
        binds.append(bus.bind)
        env.update(bus.env)
    if gpu:
        env["LD_LIBRARY_PATH"] = LIB_DIR
    return SessionExtras(binds=tuple(binds), env=env, skipped=tuple(skipped))


__all__ = [
    "DEFAULT_AGENT_HELPER",
    "SecurityHooks",
    "agent_enabled",
    "bus_listen",
    "bus_plan",
    "gpg_extra_host",
    "session_extras",
    "ssh_listen",
    "stored_gpg_extra",
]
