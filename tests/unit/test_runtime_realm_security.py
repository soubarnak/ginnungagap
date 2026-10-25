# SPDX-License-Identifier: Apache-2.0
"""The security grants of a realm's session: the Agent Proxy, the session bus and the GPU (FR-062
AC-1, FR-067, FR-068, FR-069).

Covers runtime/realm_security.py and the `extras` of runtime/env.py `session_env`. Every host read
is a fake: the file types, the gpg extra socket, the bus binary and the environment are passed in.
Nothing here starts a process or reads the host.
"""

import stat
from pathlib import Path

import pytest

from ginn.domain import Tier
from ginn.runtime.env import SessionExtras, session_env
from ginn.runtime.mounts import SessionBind, Skipped
from ginn.runtime.realm_security import (
    SecurityHooks,
    agent_enabled,
    bus_listen,
    bus_plan,
    session_extras,
    ssh_listen,
    stored_gpg_extra,
)
from ginn.security import dbus_filter
from ginn.security.nvidia import LIB_DIR

HOME = Path("/home/alice")
UID = 1000
OWNED = Path("/home/alice/.local/share/ginn")
RUNTIME = Path("/run/ginn/1000")
"""ginn's own runtime root (ADR-004), where the host sockets listen."""
GPG_EXTRA = "/run/user/1000/gnupg/d.abc/S.gpg-agent.extra"
BUS = "unix:path=/run/user/1000/bus"
BINARY = "/usr/bin/xdg-dbus-proxy"


def _hooks(**overrides: object) -> SecurityHooks:
    """Hooks for a host where every path is a socket and the gpg extra socket is GPG_EXTRA."""
    values: dict[str, object] = {
        "dbus_binary": BINARY,
        "gpg_extra": lambda: GPG_EXTRA,
        "file_type": lambda _path: stat.S_IFSOCK,
    }
    values.update(overrides)
    return SecurityHooks(**values)  # type: ignore[arg-type]


def _host(**overrides: str) -> dict[str, str]:
    env = {
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": BUS,
        "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    }
    env.update(overrides)
    return env


def _binds(extras: SessionExtras) -> list[str]:
    return sorted(bind.realm for bind in extras.binds)


def _extras(
    tier: Tier,
    *,
    host: dict[str, str] | None = None,
    hooks: SecurityHooks | None = None,
    gpu: bool = False,
) -> SessionExtras:
    """The session grants of a realm of `tier`, with the agent toggle that the tier gives it."""
    return session_extras(
        _host() if host is None else host,
        agent_on=agent_enabled(tier, frozenset()),
        uid=UID,
        realm="lab",
        runtime=RUNTIME,
        hooks=_hooks() if hooks is None else hooks,
        gpu=gpu,
    )


@pytest.mark.fr("FR-068")
def test_a_basic_realm_gets_the_session_bus_and_no_agent_grant() -> None:
    extras = _extras(Tier.BASIC)
    assert _binds(extras) == ["/run/user/1000/bus"]
    assert extras.env == {"DBUS_SESSION_BUS_ADDRESS": BUS}


@pytest.mark.fr("FR-067")
def test_a_development_realm_gets_the_filter_socket_and_the_gpg_socket() -> None:
    extras = _extras(Tier.DEVELOPMENT)
    assert _binds(extras) == [
        "/run/user/1000/bus",
        "/run/user/1000/ginn-ssh-agent",
        "/run/user/1000/gnupg/S.gpg-agent",
    ]
    assert extras.env["SSH_AUTH_SOCK"] == "/run/user/1000/ginn-ssh-agent"


@pytest.mark.fr("FR-067")
def test_the_host_sockets_listen_under_the_ginn_runtime_root_at_their_fixed_names() -> None:
    """ADR-004 and ADR-008: ginn's own sockets sit under its runtime root, as ginn's own."""
    assert ssh_listen(RUNTIME) == "/run/ginn/1000/agent/ssh.sock"
    assert bus_listen(RUNTIME, "lab") == "/run/ginn/1000/proxy/lab/bus.sock"
    assert ssh_listen(RUNTIME).startswith(f"{RUNTIME}/")


@pytest.mark.fr("NFR-004")
def test_a_basic_realm_runs_no_gpg_lookup() -> None:
    def never() -> str | None:
        raise AssertionError("the gpg extra socket is resolved for Development only")

    _extras(Tier.BASIC, hooks=_hooks(gpg_extra=never))


@pytest.mark.fr("FR-068")
def test_an_abstract_bus_address_grants_no_bus_at_all() -> None:
    """AS-012: an address with an abstract socket could be reached past the proxy, so none is."""
    host = _host(DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus;unix:abstract=/tmp/x")
    extras = _extras(Tier.BASIC, host=host)
    assert "DBUS_SESSION_BUS_ADDRESS" not in extras.env
    assert extras.binds == ()


@pytest.mark.fr("AS-012")
def test_an_abstract_bus_address_is_a_skip_with_the_as012_reason() -> None:
    """The refusal is recorded like any other skipped grant, so `ginn status` shows it (FR-064)."""
    host = _host(DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus,abstract=/tmp/x")
    plan = bus_plan(host, uid=UID, realm="lab", runtime=RUNTIME, hooks=_hooks())
    assert isinstance(plan, Skipped)
    assert plan.kind == "bus"
    assert "AS-012" in plan.reason
    extras = _extras(Tier.BASIC, host=host)
    assert extras.skipped == (plan,)
    assert any("AS-012" in skip.message() for skip in extras.skipped)


@pytest.mark.fr("FR-068")
def test_a_path_bus_address_is_not_a_skip() -> None:
    plan = bus_plan(_host(), uid=UID, realm="lab", runtime=RUNTIME, hooks=_hooks())
    assert isinstance(plan, dbus_filter.ProxyPlan)
    assert _extras(Tier.BASIC).skipped == ()


@pytest.mark.fr("FR-068")
def test_a_host_without_a_proxy_binary_grants_no_bus() -> None:
    plan = bus_plan(_host(), uid=UID, realm="lab", runtime=RUNTIME, hooks=_hooks(dbus_binary="xdg"))
    assert plan is None


@pytest.mark.fr("FR-068")
def test_a_host_with_no_session_address_grants_no_bus() -> None:
    host = _host()
    del host["DBUS_SESSION_BUS_ADDRESS"]
    assert bus_plan(host, uid=UID, realm="lab", runtime=RUNTIME, hooks=_hooks()) is None


@pytest.mark.fr("FR-069")
def test_the_gpu_grant_names_the_ginn_libraries_only_when_the_cdi_grant_is_active() -> None:
    active = _extras(Tier.BASIC, gpu=True)
    inactive = _extras(Tier.BASIC)
    assert active.env["LD_LIBRARY_PATH"] == LIB_DIR == "/usr/lib/ginn-nvidia"
    assert "LD_LIBRARY_PATH" not in inactive.env


@pytest.mark.fr("FR-067")
def test_no_host_agent_means_no_agent_bind_and_a_skip_that_says_why() -> None:
    host = _host()
    del host["SSH_AUTH_SOCK"]
    extras = _extras(Tier.DEVELOPMENT, host=host)
    assert "/run/user/1000/ginn-ssh-agent" not in _binds(extras)
    assert "SSH_AUTH_SOCK" not in extras.env
    assert [skip.reason for skip in extras.skipped] == ["SSH_AUTH_SOCK is not set"]


@pytest.mark.fr("FR-062")
def test_session_env_merges_the_extra_binds_after_the_session_binds() -> None:
    extras = SessionExtras(binds=(SessionBind(host="/run/user/1000/x", realm="/run/user/1000/x"),))
    session = session_env(
        {"XDG_RUNTIME_DIR": "/run/user/1000"},
        home=HOME,
        uid=UID,
        owned_paths=[OWNED],
        extras=extras,
    )
    assert session.binds[-1] == SessionBind(host="/run/user/1000/x", realm="/run/user/1000/x")


@pytest.mark.fr("NFR-SEC-005")
def test_session_env_refuses_an_extra_variable_outside_the_allowlist() -> None:
    extras = SessionExtras(env={"PATH": "/opt/evil"})
    with pytest.raises(ValueError, match="may not set PATH"):
        session_env({}, home=HOME, uid=UID, owned_paths=[OWNED], extras=extras)


@pytest.mark.fr("FR-068")
def test_bus_plan_binds_the_proxy_socket_and_never_the_host_bus() -> None:
    plan = bus_plan(_host(), uid=UID, realm="lab", runtime=RUNTIME, hooks=_hooks())
    assert plan is not None
    assert plan.bind.host == "/run/ginn/1000/proxy/lab/bus.sock"
    assert plan.bind.realm == "/run/user/1000/bus"
    assert "/run/user/1000/bus" != plan.listen
    assert plan.argv[0] == BINARY


@pytest.mark.fr("NFR-004")
def test_an_enter_reads_the_stored_gpg_socket_and_runs_no_lookup() -> None:
    def never() -> str | None:
        raise AssertionError("gpgconf runs at the start only")

    lookup = stored_gpg_extra(_hooks(gpg_extra=never), known=True, stored=GPG_EXTRA)
    extras = session_extras(
        _host(),
        agent_on=True,
        uid=UID,
        realm="lab",
        runtime=RUNTIME,
        hooks=_hooks(gpg_extra=never),
        gpu=False,
        gpg_extra=lookup,
    )
    assert "/run/user/1000/gnupg/S.gpg-agent" in _binds(extras)


@pytest.mark.fr("NFR-004")
def test_a_record_that_predates_the_stored_socket_resolves_it_on_enter() -> None:
    calls: list[str] = []

    def lookup() -> str | None:
        calls.append("gpgconf")
        return GPG_EXTRA

    lookup_for_old = stored_gpg_extra(_hooks(gpg_extra=lookup), known=False, stored=None)
    assert lookup_for_old() == GPG_EXTRA
    assert calls == ["gpgconf"]
