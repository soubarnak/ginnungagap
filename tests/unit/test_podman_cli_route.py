# SPDX-License-Identifier: Apache-2.0
"""Which route an exec takes: the direct podman client or the supervisor relay (ADR-014, FR-049).

A services-on target outside the supervisor leaf goes through the relay, and everything else runs
the direct route as before. The two routes send the same argv tail, and only the pass-through values
travel in the relay request, never on a command line (NFR-SEC-005). No real podman runs: a stub
script stands in for it, and the relay is a fake listener from test_exec_relay_client.
"""

from __future__ import annotations

import inspect
import os
import shutil
import signal
import socket
import stat
from pathlib import Path
from typing import Any, Final

import pytest

from ginn.errors import GinnError
from ginn.podman.adapter import ExecTarget, Podman
from ginn.podman.cli import PodmanCli
from ginn.runtime import exec_relay
from ginn.runtime.attach import attach_at_start
from tests.unit.test_exec_relay_client import _ack, _Listener, _sock_dir
from tests.unit.test_podman_cli import configured_paths

CID: Final = "c" * 64
REALM: Final = "tst-route"
LEAF_OFF: Final = "0::/user.slice/user-1000.slice/session-1.scope\n"


def _stub(tmp_path: Path, log: Path) -> Path:
    """A podman stand-in that logs its argv, prints one line and exits 0 (it never runs podman)."""
    script = tmp_path / "podman"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\necho ran\nexit 0\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _cli(tmp_path: Path, binary: Path, cgroup: str, runtime: Path | None = None) -> PodmanCli:
    """The explicit constructor over configured Owned Paths. The runtime root may be short."""
    paths = configured_paths(tmp_path)
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text(cgroup.format(uid=os.getuid()), encoding="utf-8")
    return PodmanCli(
        paths.podman_root,
        paths.podman_runroot,
        tmpdir=paths.podman_tmpdir,
        image_tmp=paths.image_tmp,
        containers_conf=paths.podman_containers_conf,
        storage_conf=paths.podman_storage_conf,
        runtime_root=runtime or paths.runtime,
        binary=str(binary),
        cgroup_file=cgroup_file,
    )


def _target(*, services: bool, mutating: bool = False) -> ExecTarget:
    return ExecTarget(realm=REALM, services=services, mutating=mutating)


def _logged(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.fr("FR-082")
def test_a_services_off_exec_runs_direct_and_never_touches_the_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "argv.log"
    cli = _cli(tmp_path, _stub(tmp_path, log), LEAF_OFF)

    def no_relay(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a services-off exec must not open the relay")

    monkeypatch.setattr(exec_relay, "open_exchange", no_relay)
    assert cli.exec(CID, ["true"], target=_target(services=False)).stdout == "ran\n"
    assert len(_logged(log)) == 1


@pytest.mark.fr("FR-049")
def test_a_services_on_exec_outside_the_leaf_is_sent_to_the_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "argv.log"
    cli = _cli(tmp_path, _stub(tmp_path, log), LEAF_OFF)
    sent: list[exec_relay.Request] = []

    def record(_path: Path, request: exec_relay.Request, *_args: Any, **_kw: Any) -> None:
        sent.append(request)
        raise GinnError("GINN-E-RELAY-UNAVAILABLE", "stopped here for the test", "", 1)

    monkeypatch.setattr(exec_relay, "open_exchange", record)
    with pytest.raises(GinnError):
        cli.exec(CID, ["true"], target=_target(services=True, mutating=True))
    assert _logged(log) == []
    assert len(sent) == 1
    assert sent[0].realm == REALM
    assert sent[0].container_id == CID
    assert sent[0].mutating is True
    assert sent[0].captured is True


@pytest.mark.fr("FR-049")
def test_a_services_on_exec_inside_the_supervisor_leaf_runs_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "argv.log"
    cli = _cli(tmp_path, _stub(tmp_path, log), "0::/ginn/{uid}/supervisor\n")

    def no_relay(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a process in the leaf enters the realm itself")

    monkeypatch.setattr(exec_relay, "open_exchange", no_relay)
    assert cli.exec(CID, ["true"], target=_target(services=True)).stdout == "ran\n"
    assert len(_logged(log)) == 1


@pytest.mark.fr("FR-049")
def test_the_relay_request_carries_the_direct_route_tail_and_its_pass_through_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "argv.log"
    binary = _stub(tmp_path, log)
    monkeypatch.setenv("LANG", "C.UTF-8")
    direct = _cli(tmp_path, binary, LEAF_OFF)
    relayed = _cli(tmp_path, binary, LEAF_OFF)
    options: dict[str, Any] = {
        "user": "0:0",
        "env": {"GINN_SAMPLE": "1"},
        "workdir": "/home/realm",
        "pass_env": ("LANG",),
    }
    direct.exec(CID, ["ls", "-l"], target=_target(services=False), **options)
    direct_argv = direct.history[-1]

    sent: list[exec_relay.Request] = []

    def record(_path: Path, request: exec_relay.Request, *_args: Any, **_kw: Any) -> None:
        sent.append(request)
        raise GinnError("GINN-E-RELAY-UNAVAILABLE", "stopped here for the test", "", 1)

    monkeypatch.setattr(exec_relay, "open_exchange", record)
    with pytest.raises(GinnError):
        relayed.exec(CID, ["ls", "-l"], target=_target(services=True), **options)
    request = sent[0]
    assert list(request.tail) == direct_argv[len(direct.global_argv()) :]
    assert request.passed == ("LANG=C.UTF-8",)


@pytest.mark.fr("NFR-SEC-005")
def test_a_pass_through_value_is_in_the_request_and_never_on_a_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://user:hunter2@proxy.example:3128")
    cli = _cli(tmp_path, _stub(tmp_path, tmp_path / "argv.log"), LEAF_OFF)
    sent: list[exec_relay.Request] = []

    def record(_path: Path, request: exec_relay.Request, *_args: Any, **_kw: Any) -> None:
        sent.append(request)
        raise GinnError("GINN-E-RELAY-UNAVAILABLE", "stopped here for the test", "", 1)

    monkeypatch.setattr(exec_relay, "open_exchange", record)
    with pytest.raises(GinnError):
        cli.exec(
            CID,
            ["true"],
            target=_target(services=True),
            pass_env=("HTTPS_PROXY",),
        )
    request = sent[0]
    assert request.passed == ("HTTPS_PROXY=http://user:hunter2@proxy.example:3128",)
    assert "hunter2" not in " ".join(request.tail)
    assert "--env" in request.tail and "HTTPS_PROXY" in request.tail


@pytest.mark.fr("FR-049")
def test_a_pass_through_name_absent_from_the_environment_is_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    cli = _cli(tmp_path, _stub(tmp_path, tmp_path / "argv.log"), LEAF_OFF)
    sent: list[exec_relay.Request] = []

    def record(_path: Path, request: exec_relay.Request, *_args: Any, **_kw: Any) -> None:
        sent.append(request)
        raise GinnError("GINN-E-RELAY-UNAVAILABLE", "stopped here for the test", "", 1)

    monkeypatch.setattr(exec_relay, "open_exchange", record)
    with pytest.raises(GinnError):
        cli.exec(CID, ["true"], target=_target(services=True), pass_env=("HTTPS_PROXY",))
    assert sent[0].passed == ()


@pytest.mark.fr("FR-048")
def test_the_direct_interactive_route_reports_its_pid_through_on_spawn_and_on_ack(
    tmp_path: Path,
) -> None:
    cli = _cli(tmp_path, _stub(tmp_path, tmp_path / "argv.log"), LEAF_OFF)
    spawned: list[int] = []
    acked: list[int] = []
    status = cli.exec_interactive(
        CID,
        ["true"],
        target=_target(services=False),
        on_spawn=spawned.append,
        on_ack=acked.append,
    )
    assert status == 0
    assert len(spawned) == 1 and spawned == acked
    assert spawned[0] > 0


@pytest.mark.fr("FR-049")
def test_the_direct_captured_route_reports_its_pid_through_on_ack(tmp_path: Path) -> None:
    cli = _cli(tmp_path, _stub(tmp_path, tmp_path / "argv.log"), LEAF_OFF)
    acked: list[int] = []
    result = cli.exec(CID, ["true"], target=_target(services=False), on_ack=acked.append)
    assert result.stdout == "ran\n"
    assert len(acked) == 1 and acked[0] > 0


@pytest.mark.fr("FR-048")
def test_the_relay_interactive_route_hands_its_pid_to_the_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _cli(tmp_path, _stub(tmp_path, tmp_path / "argv.log"), LEAF_OFF)
    events: list[str] = []

    def fake_run_attached(_path: Path, request: exec_relay.Request, **kwargs: Any) -> int:
        events.append(f"tty={request.tty} interactive={request.interactive}")
        kwargs["on_spawn"](4242)
        kwargs["on_ack"](4242)
        kwargs["on_exit"]()
        return 5

    monkeypatch.setattr(exec_relay, "run_attached", fake_run_attached)
    status = cli.exec_interactive(
        CID,
        ["sh"],
        target=_target(services=True),
        tty=True,
        interactive=True,
        on_spawn=lambda pid: events.append(f"spawn {pid}"),
        on_ack=lambda pid: events.append(f"ack {pid}"),
        on_exit=lambda: events.append("exit"),
    )
    assert status == 5
    assert events == ["tty=True interactive=True", "spawn 4242", "ack 4242", "exit"]


@pytest.mark.fr("FR-044")
def test_a_bound_on_the_relay_route_stops_the_run_and_raises_the_direct_error(
    tmp_path: Path,
) -> None:
    runtime = _sock_dir()
    signals: list[bytes] = []

    def respond(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        conn.send(_ack(321))
        signals.append(conn.recv(2))
        conn.send(bytes([exec_relay.EXIT, 1, signal.SIGTERM]))

    listener = _Listener(runtime / exec_relay.SOCKET_NAME, respond).start()
    try:
        cli = _cli(tmp_path, _stub(tmp_path, tmp_path / "argv.log"), LEAF_OFF, runtime=runtime)
        with pytest.raises(GinnError) as caught:
            cli.exec(CID, ["sleep", "9"], target=_target(services=True), timeout_s=0.2)
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
    listener.join()
    assert caught.value.code == "GINN-E-INTERNAL"
    assert caught.value.details["reason"] == "timed out after 0.2 s"
    assert signals == [bytes([exec_relay.SIG, signal.SIGTERM])]


@pytest.mark.fr("FR-049")
def test_every_exec_entry_point_requires_a_target_keyword() -> None:
    for method in (
        PodmanCli.exec,
        PodmanCli.exec_interactive,
        Podman.exec,
        Podman.exec_interactive,
    ):
        parameter = inspect.signature(method).parameters["target"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


@pytest.mark.fr("FR-061")
def test_attach_at_start_requires_a_target_keyword() -> None:
    parameter = inspect.signature(attach_at_start).parameters["target"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.fr("FR-049")
def test_an_unbounded_exec_with_on_ack_decodes_its_output_as_run_does(tmp_path: Path) -> None:
    """The on_ack route runs through the bounded reader, but an unbounded exec decodes as `_run`
    does (replace), and a bounded one keeps the exact bytes (surrogateescape) (ADR-014)."""
    script = tmp_path / "podman"
    script.write_text("#!/bin/sh\nprintf '\\377\\n'\nexit 0\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    cli = _cli(tmp_path, script, LEAF_OFF)
    acked: list[int] = []
    with_ack = cli.exec(CID, ["true"], target=_target(services=False), on_ack=acked.append)
    plain = cli.exec(CID, ["true"], target=_target(services=False))
    bounded = cli.exec(CID, ["true"], target=_target(services=False), timeout_s=30)
    assert len(acked) == 1
    assert with_ack.stdout == plain.stdout == "\ufffd\n"
    assert bounded.stdout == "\udcff\n"
