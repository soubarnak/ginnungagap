# SPDX-License-Identifier: Apache-2.0
"""The session D-Bus filter (FR-068, AS-012): the host bus choice, the proxy argv, the realm bind,
the lazy per-realm lifecycle over process records, and the stale-socket rule.

No xdg-dbus-proxy and no host session bus is started. The launcher is a fake, except where a test
says so: `/bin/sh` scripts for the failure path, and a `sleep` session for the stop.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Sequence
from pathlib import Path

import pytest

from ginn.errors import GinnError
from ginn.runtime.mounts import SessionBind
from ginn.security import dbus_filter as df
from ginn.security._proc import Child, Launcher, Proc
from tests.fakes.fake_helpers import sleep_binary, spawn_sleeper

UID = 1000
BUS = "unix:path=/run/user/1000/bus"
HOST_BUS = "/run/user/1000/bus"
PROXY = "/usr/bin/xdg-dbus-proxy"
REALM_BUS = "/run/user/1000/bus"
LISTEN = "/run/ginn/1000/dbus/dev.sock"

pytestmark = [pytest.mark.fr("FR-068"), pytest.mark.fr("AS-012")]


class Alive:
    """The process records that run in a test. Any other record has exited."""

    def __init__(self, *up: Proc) -> None:
        self.up = set(up)

    def __call__(self, proc: Proc) -> bool:
        return proc in self.up


def _argv(**extra: object) -> tuple[str, ...]:
    options: dict[str, object] = {"binary": PROXY, "bus_address": BUS, "listen": LISTEN}
    options.update(extra)
    return df.proxy_argv(**options)  # type: ignore[arg-type]


def test_the_proxy_argv_is_the_measured_form_with_the_basic_allowlist() -> None:
    """S-05 ran this form: the bus address, the socket, --filter, then the two talk names."""
    assert _argv() == (
        PROXY,
        BUS,
        LISTEN,
        "--filter",
        "--talk=org.freedesktop.Notifications",
        "--talk=org.freedesktop.portal.*",
    )


@pytest.mark.fr("FR-068")
def test_the_basic_allowlist_does_not_name_the_secret_service() -> None:
    """AC-1 in its static form: a Basic realm's proxy never allows the Secret Service name."""
    assert not any("secrets" in name or "keyring" in name for name in df.TALK_DEFAULT)
    assert not any("secrets" in arg for arg in _argv())


@pytest.mark.fr("FR-068")
def test_custom_names_widen_the_allowlist_only_when_the_caller_passes_them() -> None:
    argv = _argv(talk=[*df.TALK_DEFAULT, "org.freedesktop.secrets"])
    assert "--talk=org.freedesktop.secrets" in argv
    assert "--talk=org.freedesktop.secrets" not in _argv()


def test_own_names_are_separate_flags() -> None:
    argv = _argv(own=["org.example.App"])
    assert argv[-1] == "--own=org.example.App"


@pytest.mark.parametrize(
    "name",
    ["*", "org", "org.freedesktop.*.more", "bad name", "x;rm -rf /", "--talk=x", "", "org..x"],
)
def test_a_malformed_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="D-Bus name"):
        _argv(talk=[name])


@pytest.mark.parametrize(
    "address",
    [
        "tcp:host=localhost,port=1",
        "unix:path=relative/bus",
        "unix:guid=abc",
        "unix:",
        "",
    ],
)
def test_an_address_with_no_absolute_unix_path_is_refused(address: str) -> None:
    with pytest.raises(ValueError):
        df.select_host_bus(address)


def test_the_host_bus_is_the_path_member_of_the_address() -> None:
    bus = df.select_host_bus(BUS)
    assert bus == df.HostBus(address=BUS, path=HOST_BUS)


@pytest.mark.fr("FR-068")
@pytest.mark.parametrize(
    "address",
    [
        "unix:abstract=/tmp/dbus-XXXX,guid=0123",
        "unix:path=/run/user/1000/bus;unix:abstract=/tmp/dbus-XXXX",
        "unix:path=/run/user/1000/bus,abstract=/tmp/dbus-XXXX",
        "unix:abstract=",
    ],
)
def test_an_abstract_bus_is_refused_because_a_realm_could_reach_it_directly(address: str) -> None:
    """A realm on the host network namespace can connect to an abstract socket past the proxy."""
    with pytest.raises(ValueError, match="abstract"):
        df.select_host_bus(address)
    with pytest.raises(ValueError, match="abstract"):
        _argv(bus_address=address)


@pytest.mark.fr("FR-068")
def test_a_list_with_a_non_unix_member_is_refused_because_the_realm_shares_the_network() -> None:
    """A `;` list may name several buses, but a TCP member is reachable past the proxy (AS-012)."""
    address = "tcp:host=localhost,port=1;unix:path=/run/user/1000/bus"
    with pytest.raises(ValueError, match="AS-012"):
        df.select_host_bus(address)
    with pytest.raises(ValueError, match="AS-012"):
        _argv(bus_address=address)


@pytest.mark.fr("FR-068")
def test_the_first_of_several_path_members_is_chosen_not_a_joined_string() -> None:
    """Regression: a `;` list must not parse into one garbage path (`/a;unix:path=/b`)."""
    bus = df.select_host_bus("unix:path=/run/user/1000/bus;unix:path=/tmp/other-bus")
    assert bus.path == HOST_BUS
    assert bus.address == BUS


def test_the_proxy_socket_is_never_the_host_bus() -> None:
    with pytest.raises(ValueError, match="AS-012"):
        _argv(listen=HOST_BUS)


def test_the_binary_and_the_socket_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _argv(binary="xdg-dbus-proxy")
    with pytest.raises(ValueError, match="absolute"):
        _argv(listen="dev.sock")


def test_the_realm_binds_the_proxy_socket_at_the_standard_path_and_never_the_host_bus() -> None:
    plan = df.realm_plan(bus_address=BUS, listen=LISTEN, uid=UID, binary=PROXY)
    assert plan.bind == SessionBind(
        host=LISTEN, realm=REALM_BUS, mode="rw", is_dir=False, internal=True
    )
    assert plan.bind.host != HOST_BUS
    assert plan.env == {"DBUS_SESSION_BUS_ADDRESS": f"unix:path={REALM_BUS}"}


APP_A = Proc(100, 1000)
APP_B = Proc(101, 1001)
PROXY_P = Proc(500, 5000)


def test_an_idle_realm_has_no_proxy_and_asks_for_none() -> None:
    """NFR-002, ADR-007: a realm with no ginn-launched app runs no proxy."""
    state, action = df.reconcile(df.ProxyState("dev"), alive=Alive())
    assert action is df.Action.IDLE
    assert state.proxy is None


def test_the_first_launch_starts_one_proxy_and_the_next_launch_reuses_it() -> None:
    state, action = df.launched(df.ProxyState("dev"), APP_A, alive=Alive(APP_A))
    assert action is df.Action.START
    state = df.with_proxy(state, PROXY_P)
    state, action = df.launched(state, APP_B, alive=Alive(APP_A, APP_B, PROXY_P))
    assert action is df.Action.KEEP
    assert state.apps == frozenset({APP_A, APP_B})
    assert state.proxy == PROXY_P


def test_the_last_app_to_exit_stops_the_proxy() -> None:
    state = df.ProxyState("dev", frozenset({APP_A, APP_B}), PROXY_P)
    state, action = df.exited(state, APP_A, alive=Alive(APP_B, PROXY_P))
    assert action is df.Action.KEEP
    state, action = df.exited(state, APP_B, alive=Alive(PROXY_P))
    assert action is df.Action.STOP
    assert state.proxy == PROXY_P  # kept until stop_proxy has signalled it
    assert state.apps == frozenset()


def test_a_launch_that_died_without_its_exit_leaves_no_proxy_running() -> None:
    """The launch that recorded app A died before its exit. The next reconcile stops the proxy."""
    state, action = df.reconcile(
        df.ProxyState("dev", frozenset({APP_A}), PROXY_P), alive=Alive(PROXY_P)
    )
    assert action is df.Action.STOP
    assert state.apps == frozenset()


def test_a_dead_proxy_is_started_again_while_an_app_runs() -> None:
    state, action = df.reconcile(
        df.ProxyState("dev", frozenset({APP_A}), PROXY_P), alive=Alive(APP_A)
    )
    assert action is df.Action.START
    assert state.proxy is None


def test_a_reused_app_pid_is_not_taken_for_the_app() -> None:
    """Pid 100 with another start time is a different process: the app is gone."""
    reused = Proc(APP_A.pid, APP_A.start_ticks + 99)
    state, action = df.reconcile(df.ProxyState("dev", frozenset({APP_A})), alive=Alive(reused))
    assert action is df.Action.IDLE
    assert state.apps == frozenset()


def test_the_state_round_trips_through_its_json_form_with_start_times() -> None:
    state = df.ProxyState("dev", frozenset({APP_B, APP_A}), PROXY_P)
    text = json.dumps(state.to_dict(), sort_keys=True)
    assert json.loads(text)["apps"] == [APP_A.to_dict(), APP_B.to_dict()]
    assert df.ProxyState.from_dict(json.loads(text)) == state


@pytest.mark.parametrize(
    "data",
    [
        {"apps": []},
        {"realm": "dev", "apps": "1"},
        {"realm": "dev", "apps": [{"pid": 0, "start_ticks": 1}]},
        {"realm": "dev", "apps": [{"pid": True, "start_ticks": 1}]},
        {"realm": "dev", "apps": [{"pid": 2, "start_ticks": True}]},
        {"realm": "dev", "apps": [{"pid": 2, "start_ticks": -1}]},
        {"realm": "dev", "apps": [], "proxy": {"pid": 5}},
        {"realm": "dev", "apps": [], "proxy": "5"},
    ],
)
def test_a_malformed_state_record_raises(data: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        df.ProxyState.from_dict(data)


@pytest.mark.fr("FR-068")
def test_stop_proxy_stops_the_running_proxy_and_clears_the_record() -> None:
    if sleep_binary() is None:
        pytest.skip("sleep is needed for the stop test")
    sleeper = spawn_sleeper()
    try:
        record = Proc.of(sleeper.pid)
        assert record is not None
        state = df.stop_proxy(df.ProxyState("dev", frozenset(), record))
        assert state.proxy is None
        assert not record.alive()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait(timeout=10)


class FakeChild:
    """A child over a fixed record. It runs no process."""

    def __init__(self, pid: int) -> None:
        self.proc = Proc(pid, 7)

    def running(self) -> bool:
        return True

    def stop(self) -> None:
        return None


class FakeLauncher:
    """A launcher that starts no process. Each start is recorded with its argv."""

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> Child:
        self.argvs.append(tuple(argv))
        return FakeChild(700 + len(self.argvs))


def _plan(listen: Path) -> df.ProxyPlan:
    return df.realm_plan(bus_address=BUS, listen=str(listen), uid=UID, binary=PROXY)


def test_start_proxy_removes_a_stale_socket_and_starts_with_the_plan_argv(tmp_path: Path) -> None:
    listen = tmp_path / "dev.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(listen))
    stale.close()
    launcher = FakeLauncher()
    child = df.start_proxy(_plan(listen), launcher)
    assert child.proc.pid == 701
    assert launcher.argvs == [_argv(listen=str(listen))]
    assert not listen.exists()


def test_start_proxy_refuses_a_regular_file_and_leaves_it_alone(tmp_path: Path) -> None:
    listen = tmp_path / "dev.sock"
    listen.write_text("not ours", encoding="utf-8")
    launcher = FakeLauncher()
    with pytest.raises(ValueError, match="not a socket"):
        df.start_proxy(_plan(listen), launcher)
    assert launcher.argvs == []
    assert listen.read_text(encoding="utf-8") == "not ours"


def test_start_proxy_with_no_stale_file_just_starts(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    df.start_proxy(_plan(tmp_path / "fresh.sock"), launcher)
    assert len(launcher.argvs) == 1


def test_a_proxy_that_cannot_start_raises_the_dbus_error_with_its_last_log_line(
    tmp_path: Path,
) -> None:
    """The helper's stderr goes to the log file. Its last line joins the detail (Owned state)."""
    if not Path("/bin/sh").exists():
        pytest.skip("/bin/sh is needed for the failing proxy")
    script = tmp_path / "fail.sh"
    script.write_text("#!/bin/sh\necho 'bus refused the name' >&2\nexit 3\n", encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "dev.log"
    launch = df.launcher_for("dev", str(tmp_path / "dev.sock"), str(log))
    with pytest.raises(GinnError) as caught:
        launch([str(script)])
    assert caught.value.code == "GINN-E-DBUS-PROXY"
    assert caught.value.details["realm"] == "dev"
    assert "exited before it listened" in caught.value.message
    assert "bus refused the name" in caught.value.message
    assert log.stat().st_mode & 0o777 == 0o600


def test_a_log_that_cannot_be_opened_is_the_dbus_error_not_an_os_error(tmp_path: Path) -> None:
    launch: Launcher = df.launcher_for(
        "dev", str(tmp_path / "dev.sock"), str(tmp_path / "no/x.log")
    )
    with pytest.raises(GinnError) as caught:
        launch(["/bin/false"])
    assert caught.value.code == "GINN-E-DBUS-PROXY"
    assert "cannot open the helper log" in caught.value.message
