# SPDX-License-Identifier: Apache-2.0
"""The exec relay client: the REQ frame, the ACK, REJECT, SIG and EXIT exchange (ADR-014, FR-049).

The client is tested against a small listener that speaks the frame format of
native/ginn-exec-frame.h, and once against the real helpers: the relay listener, the worker and the
run helper, built from native/ with the C compiler, with a stub podman. No real podman runs.
"""

from __future__ import annotations

import fcntl
import os
import re
import resource
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from ginn.errors import GinnError
from ginn.podman.adapter import ExecTarget
from ginn.podman.cli import PodmanCli
from ginn.runtime import exec_relay
from ginn.runtime.exec_relay import Request
from ginn.services import exec_relay as service_relay
from tests.unit.test_podman_cli import configured_paths

REPO: Final = Path(__file__).resolve().parents[2]
NATIVE: Final = REPO / "native"
HEADER: Final = NATIVE / "ginn-exec-frame.h"
WAIT_S: Final = 15.0
CID: Final = "b" * 64
REALM: Final = "tst-exec"
TAIL: Final = ("exec", "--", CID, "true")
HELPERS: Final = ("ginn-exec-relay", "ginn-exec-worker", "ginn-exec-run", "ginn-exec-kill")
_WARNINGS: Final = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2"]


def _request(**changes: object) -> Request:
    base: dict[str, object] = {
        "realm": REALM,
        "container_id": CID,
        "tail": TAIL,
        "passed": (),
        "tty": False,
        "interactive": False,
        "mutating": False,
        "captured": False,
    }
    base.update(changes)
    return Request(**base)  # type: ignore[arg-type]


def _s16(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _sock_dir() -> Path:
    """A short directory for a socket: AF_UNIX paths are at most 108 bytes."""
    shm = Path("/dev/shm")  # noqa: S108 - a short directory for a socket, not a shared file
    base = shm if shm.is_dir() else Path(tempfile.gettempdir())
    return Path(tempfile.mkdtemp(prefix="ginn-rc-", dir=base))


def _read_u16(buf: bytes, at: int) -> tuple[int, int]:
    return struct.unpack(">H", buf[at : at + 2])[0], at + 2


def _parse_req(buf: bytes) -> dict[str, object]:
    """Decode a REQ record by the layout of native/ginn-exec-frame.h (ADR-014, Wire protocol)."""
    assert buf[0] == exec_relay.REQ
    version, flags, nfds = buf[1], buf[2], buf[3]
    at = 4
    size, at = _read_u16(buf, at)
    realm = buf[at : at + size].decode()
    at += size
    cid = buf[at : at + 64].decode()
    at += 64
    lists: list[list[str]] = []
    for _ in range(2):
        count, at = _read_u16(buf, at)
        items: list[str] = []
        for _ in range(count):
            size, at = _read_u16(buf, at)
            items.append(buf[at : at + size].decode())
            at += size
        lists.append(items)
    assert at == len(buf)
    return {
        "version": version,
        "flags": flags,
        "nfds": nfds,
        "realm": realm,
        "cid": cid,
        "passed": lists[0],
        "tail": lists[1],
    }


class _Listener:
    """A one-connection listener that answers each request with `respond` (ADR-014).

    `respond(conn, record, fds)` gets the REQ bytes and the descriptors that came with them. The
    descriptors are closed after it returns. `error` holds what the thread raised, if anything.
    """

    def __init__(self, path: Path, respond: Callable[[socket.socket, bytes, list[int]], None]):
        self.path = path
        self.respond = respond
        self.error: BaseException | None = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(str(path))
        self.listener.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> _Listener:
        self.thread.start()
        return self

    def _serve(self) -> None:
        try:
            conn, _ = self.listener.accept()
            try:
                record, fds, _flags, _addr = socket.recv_fds(conn, exec_relay.MAX_RECORD, 8)
                try:
                    self.respond(conn, record, fds)
                finally:
                    for fd in fds:
                        os.close(fd)
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001 - the test reads it after join
            self.error = exc
        finally:
            self.listener.close()

    def join(self) -> None:
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive()
        if self.error is not None:
            raise self.error


def _ack(pid: int) -> bytes:
    return bytes([exec_relay.ACK]) + struct.pack(">I", pid)


def _reject(code: int, reason: str) -> bytes:
    return bytes([exec_relay.REJECT, code]) + _s16(reason)


def _exit(kind: int, value: int) -> bytes:
    return bytes([exec_relay.EXIT, kind, value])


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    path = _sock_dir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.mark.fr("NFR-SEC-005")
def test_the_constants_are_the_values_of_the_frame_header() -> None:
    """The header governs (ADR-014). The client's constants must match its #define lines."""
    defines = dict(re.findall(r"#define GINN_EXEC_(\w+) (\w+)", HEADER.read_text()))
    values = {name: int(text, 0) for name, text in defines.items() if text[:1].isdigit()}
    assert values["REQ"] == exec_relay.REQ
    assert values["ACK"] == exec_relay.ACK
    assert values["REJECT"] == exec_relay.REJECT
    assert values["SIG"] == exec_relay.SIG
    assert values["EXIT"] == exec_relay.EXIT
    assert values["VERSION"] == exec_relay.VERSION
    assert values["FL_TTY"] == exec_relay.FLAG_TTY
    assert values["FL_INTERACTIVE"] == exec_relay.FLAG_INTERACTIVE
    assert values["FL_MUTATING"] == exec_relay.FLAG_MUTATING
    assert values["FL_CAPTURED"] == exec_relay.FLAG_CAPTURED
    assert values["CID_LEN"] == exec_relay.CID_LEN
    assert values["MAX_RECORD"] == exec_relay.MAX_RECORD
    assert values["MAX_ITEMS"] == exec_relay.MAX_ITEMS
    assert values["MAX_VALUE"] == exec_relay.MAX_VALUE
    assert values["REJ_REJECTED"] == exec_relay.REJECT_REJECTED
    assert values["REJ_LOCKED"] == exec_relay.REJECT_LOCKED
    assert values["REJ_BUSY"] == exec_relay.REJECT_BUSY


@pytest.mark.fr("FR-049")
def test_the_socket_name_is_the_one_the_service_listens_on() -> None:
    assert exec_relay.SOCKET_NAME == service_relay.SOCKET_NAME


@pytest.mark.fr("FR-049")
def test_a_request_is_encoded_in_the_header_layout_with_its_flags_and_fd_count() -> None:
    request = _request(
        passed=("LANG=C.UTF-8",),
        tty=True,
        interactive=True,
        mutating=True,
        captured=True,
        tail=("exec", "--tty", "--interactive", "--", CID, "true"),
    )
    decoded = _parse_req(exec_relay.encode(request))
    assert decoded == {
        "version": 1,
        "flags": 1 | 2 | 4 | 8,
        "nfds": 5,
        "realm": REALM,
        "cid": CID,
        "passed": ["LANG=C.UTF-8"],
        "tail": ["exec", "--tty", "--interactive", "--", CID, "true"],
    }
    plain = _parse_req(exec_relay.encode(_request()))
    assert plain["flags"] == 0
    assert plain["nfds"] == 3


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    "change",
    [
        {"container_id": "B" * 64},
        {"container_id": "b" * 63},
        {"passed": tuple(f"V{i}=x" for i in range(257))},
        {"tail": ("exec",) + ("x",) * 257},
        {"passed": ("BIG=" + "x" * 4097,)},
        {"tail": ("exec", "a" * 40000, "b" * 40000, "--", CID)},
    ],
)
def test_a_request_the_worker_would_refuse_is_not_encoded(change: dict[str, object]) -> None:
    with pytest.raises(GinnError) as caught:
        exec_relay.encode(_request(**change))
    assert caught.value.code == "GINN-E-RELAY-REJECTED"


@pytest.mark.fr("NFR-SEC-005")
def test_a_long_argument_that_is_not_a_pass_through_value_is_encoded() -> None:
    tail = ("exec", "--", CID, "x" * 5000)
    assert _parse_req(exec_relay.encode(_request(tail=tail)))["tail"] == list(tail)


@pytest.mark.fr("FR-049")
@pytest.mark.parametrize(
    ("cgroup", "expected"),
    [
        ("0::/ginn/{uid}/supervisor\n", True),
        ("0::/ginn/{uid}/supervisor/_service-run\n", True),
        ("0::/ginn/{uid}/supervisorX\n", False),
        ("0::/ginn/{uid}/realm-one\n", False),
        ("0::/user.slice/user-{uid}.slice/session-2.scope\n", False),
        ("1:name=systemd:/ginn/{uid}/supervisor\n0::/user.slice\n", False),
    ],
)
def test_the_supervisor_leaf_is_read_from_the_unified_cgroup_line(
    tmp_path: Path, cgroup: str, expected: bool
) -> None:
    path = tmp_path / "cgroup"
    path.write_text(cgroup.format(uid=os.getuid()), encoding="utf-8")
    assert exec_relay.in_supervisor_leaf(path) is expected


@pytest.mark.fr("FR-049")
def test_an_unreadable_cgroup_file_routes_through_the_relay(tmp_path: Path) -> None:
    assert exec_relay.in_supervisor_leaf(tmp_path / "missing") is False


@pytest.mark.fr("FR-049")
def test_the_ack_pid_and_the_exit_status_come_back_and_the_record_is_what_was_encoded(
    sock_dir: Path,
) -> None:
    path = sock_dir / "exec.sock"
    seen: dict[str, object] = {}

    def respond(conn: socket.socket, record: bytes, fds: list[int]) -> None:
        seen["record"] = record
        seen["fds"] = len(fds)
        conn.send(_ack(4321))
        conn.send(_exit(0, 7))

    listener = _Listener(path, respond).start()
    request = _request()
    exchange = exec_relay.open_exchange(path, request, (0, 1, 2), service=None)
    try:
        assert exchange.pid == 4321
        assert exchange.wait(WAIT_S) == 7
        assert exchange.wait(0) == 7
    finally:
        exchange.close()
    listener.join()
    assert seen["record"] == exec_relay.encode(request)
    assert seen["fds"] == 3


@pytest.mark.fr("FR-049")
def test_a_signal_exit_is_negative_as_popen_reports_it(sock_dir: Path) -> None:
    path = sock_dir / "exec.sock"

    def respond(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        conn.send(_ack(9))
        conn.send(_exit(1, signal.SIGKILL))

    listener = _Listener(path, respond).start()
    exchange = exec_relay.open_exchange(path, _request(), (0, 1, 2), service=None)
    try:
        assert exchange.wait(WAIT_S) == -signal.SIGKILL
    finally:
        exchange.close()
    listener.join()


@pytest.mark.fr("FR-049")
@pytest.mark.parametrize(
    ("code", "error"),
    [
        (exec_relay.REJECT_REJECTED, "GINN-E-RELAY-REJECTED"),
        (exec_relay.REJECT_LOCKED, "GINN-E-REALM-BUSY"),
        (exec_relay.REJECT_BUSY, "GINN-E-RELAY-BUSY"),
    ],
)
def test_a_reject_maps_to_the_error_of_its_code(sock_dir: Path, code: int, error: str) -> None:
    path = sock_dir / "exec.sock"

    def respond(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        conn.send(_reject(code, "held by run 1 podman 2"))

    listener = _Listener(path, respond).start()
    with pytest.raises(GinnError) as caught:
        exec_relay.open_exchange(path, _request(), (0, 1, 2), service=None)
    listener.join()
    assert caught.value.code == error


@pytest.mark.fr("FR-123")
def test_a_locked_reject_names_the_holder_in_the_message(sock_dir: Path) -> None:
    path = sock_dir / "exec.sock"

    def respond(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        conn.send(_reject(exec_relay.REJECT_LOCKED, "held by run 1 podman 2"))

    listener = _Listener(path, respond).start()
    with pytest.raises(GinnError) as caught:
        exec_relay.open_exchange(path, _request(), (0, 1, 2), service=None)
    listener.join()
    assert "held by run 1 podman 2" in caught.value.message


@pytest.mark.fr("FR-049")
def test_no_ack_within_the_bound_is_a_timeout(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exec_relay, "ACK_WAIT_S", 0.2)
    path = sock_dir / "exec.sock"

    def respond(_conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        time.sleep(1.0)

    listener = _Listener(path, respond).start()
    with pytest.raises(GinnError) as caught:
        exec_relay.open_exchange(path, _request(), (0, 1, 2), service=None)
    listener.join()
    assert caught.value.code == "GINN-E-RELAY-TIMEOUT"


@pytest.mark.fr("FR-049")
def test_the_worker_leaving_before_ack_or_exit_is_lost(sock_dir: Path) -> None:
    path = sock_dir / "exec.sock"
    listener = _Listener(path, lambda _c, _r, _f: None).start()
    with pytest.raises(GinnError) as caught:
        exec_relay.open_exchange(path, _request(), (0, 1, 2), service=None)
    listener.join()
    assert caught.value.code == "GINN-E-RELAY-LOST"

    def ack_then_leave(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        conn.send(_ack(5))

    path2 = sock_dir / "exec2.sock"
    listener = _Listener(path2, ack_then_leave).start()
    exchange = exec_relay.open_exchange(path2, _request(), (0, 1, 2), service=None)
    try:
        with pytest.raises(GinnError) as lost:
            exchange.wait(WAIT_S)
    finally:
        exchange.close()
    listener.join()
    assert lost.value.code == "GINN-E-RELAY-LOST"


@pytest.mark.fr("FR-049")
def test_a_malformed_exit_frame_is_lost(sock_dir: Path) -> None:
    path = sock_dir / "exec.sock"

    def respond(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        conn.send(_ack(5))
        conn.send(bytes([exec_relay.EXIT, 0]))

    listener = _Listener(path, respond).start()
    exchange = exec_relay.open_exchange(path, _request(), (0, 1, 2), service=None)
    try:
        with pytest.raises(GinnError) as lost:
            exchange.wait(WAIT_S)
    finally:
        exchange.close()
    listener.join()
    assert lost.value.code == "GINN-E-RELAY-LOST"


@pytest.mark.fr("FR-049")
def test_a_refused_socket_without_a_service_is_unavailable_at_once(sock_dir: Path) -> None:
    with pytest.raises(GinnError) as caught:
        exec_relay.open_exchange(sock_dir / "absent.sock", _request(), (0, 1, 2), service=None)
    assert caught.value.code == "GINN-E-RELAY-UNAVAILABLE"
    assert "no listener" in caught.value.message


@pytest.mark.fr("FR-084")
def test_a_refused_socket_runs_sv_up_once_and_then_connects(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = sock_dir / "exec.sock"
    service = sock_dir / "_exec"
    calls: list[tuple[str, Path]] = []
    listeners: list[_Listener] = []

    def revive(realm: str, service_dir: Path) -> None:
        calls.append((realm, service_dir))

        def respond(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
            conn.send(_ack(77))
            conn.send(_exit(0, 0))

        listeners.append(_Listener(path, respond).start())

    monkeypatch.setattr(exec_relay, "_revive", revive)
    exchange = exec_relay.open_exchange(path, _request(), (0, 1, 2), service=service)
    try:
        assert exchange.pid == 77
        assert exchange.wait(WAIT_S) == 0
    finally:
        exchange.close()
    listeners[0].join()
    assert calls == [(REALM, service)]


@pytest.mark.fr("FR-084")
def test_a_failed_sv_up_is_unavailable_with_the_sv_detail(
    sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sv = tmp_path / "sv"  # not under /dev/shm, which may be mounted noexec
    sv.write_text("#!/bin/sh\necho 'sv: fail: boom' >&2\nexit 1\n", encoding="utf-8")
    sv.chmod(sv.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr("shutil.which", lambda name: str(sv))
    with pytest.raises(GinnError) as caught:
        exec_relay.open_exchange(
            sock_dir / "absent.sock", _request(), (0, 1, 2), service=sock_dir / "_exec"
        )
    assert caught.value.code == "GINN-E-RELAY-UNAVAILABLE"
    assert "boom" in caught.value.message


@pytest.mark.fr("FR-048")
def test_signals_are_forwarded_as_sig_messages_and_the_handlers_are_restored(
    sock_dir: Path,
) -> None:
    path = sock_dir / "exec.sock"
    received: list[bytes] = []

    def respond(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        conn.send(_ack(12))
        received.append(conn.recv(2))
        conn.send(_exit(0, 0))

    listener = _Listener(path, respond).start()
    before = signal.getsignal(signal.SIGINT)
    timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        status = exec_relay.run_attached(
            path,
            _request(interactive=True),
            service=None,
            on_spawn=None,
            on_ack=None,
            on_exit=None,
        )
    finally:
        timer.cancel()
    listener.join()
    assert status == 0
    assert received == [bytes([exec_relay.SIG, signal.SIGINT])]
    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.fr("FR-049")
def test_on_ack_then_on_exit_run_once_each_and_on_exit_runs_after_the_error(
    sock_dir: Path,
) -> None:
    path = sock_dir / "exec.sock"

    def respond(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        conn.send(_ack(33))
        conn.send(_exit(0, 4))

    events: list[str] = []
    listener = _Listener(path, respond).start()
    status = exec_relay.run_attached(
        path,
        _request(interactive=True),
        service=None,
        on_spawn=lambda pid: events.append(f"spawn {pid}"),
        on_ack=lambda pid: events.append(f"ack {pid}"),
        on_exit=lambda: events.append("exit"),
    )
    listener.join()
    assert status == 4
    assert events == ["spawn 33", "ack 33", "exit"]


@pytest.mark.fr("FR-122")
def test_on_ack_runs_before_the_exit_is_read(sock_dir: Path) -> None:
    """The worker sends EXIT only after on_ack has run, so an exec is journaled as started before
    it can end (ADR-014, exec-acked). The listener waits for the callback before it answers."""
    path = sock_dir / "exec.sock"
    acked = threading.Event()
    seen_before_exit: list[bool] = []

    def respond(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        conn.send(_ack(8))
        seen_before_exit.append(acked.wait(WAIT_S))
        conn.send(_exit(0, 0))

    listener = _Listener(path, respond).start()
    status = exec_relay.run_attached(
        path,
        _request(interactive=True),
        service=None,
        on_spawn=None,
        on_ack=lambda _pid: acked.set(),
        on_exit=None,
    )
    listener.join()
    assert status == 0
    assert seen_before_exit == [True]


@pytest.mark.fr("FR-048")
def test_the_termios_of_the_terminal_is_restored_on_an_error_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = ["termios-state"]
    restored: list[object] = []
    monkeypatch.setattr(exec_relay.os, "isatty", lambda fd: fd == 0)
    monkeypatch.setattr(exec_relay.termios, "tcgetattr", lambda fd: saved)
    monkeypatch.setattr(
        exec_relay.termios, "tcsetattr", lambda fd, when, state: restored.append(state)
    )
    with pytest.raises(RuntimeError), exec_relay.tty_state():
        raise RuntimeError("the client crashed")
    assert restored == [saved]


@pytest.mark.fr("FR-048")
def test_without_a_terminal_on_fd_0_no_termios_is_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exec_relay.os, "isatty", lambda fd: False)

    def unexpected(*_args: object) -> None:
        raise AssertionError("termios must not be read without a terminal")

    monkeypatch.setattr(exec_relay.termios, "tcgetattr", unexpected)
    with exec_relay.tty_state():
        pass


def _build_helpers(out: Path) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    for name in HELPERS:
        run = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                cc,
                *_WARNINGS,
                "-D_FORTIFY_SOURCE=2",
                "-o",
                str(out / name),
                str(NATIVE / f"{name}.c"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, run.stderr
    return out


@pytest.fixture(scope="module")
def helpers(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build_helpers(tmp_path_factory.mktemp("exec-client-bin"))


def _stub_podman(path: Path, log: Path) -> Path:
    """A podman stand-in: it logs its argv, prints a line on each stream, and exits with 3."""
    path.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\necho "out line"\necho "err line" >&2\nexit 3\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.fr("FR-049")
def test_a_captured_exec_runs_through_the_real_helpers_and_returns_the_output(
    helpers: Path, tmp_path: Path, sock_dir: Path
) -> None:
    log = tmp_path / "podman.log"
    stub = _stub_podman(tmp_path / "podman", log)
    runtime = sock_dir / "rt"
    runtime.mkdir(mode=0o700)
    relay = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(helpers / "ginn-exec-relay"), str(runtime), str(stub)],
        stdout=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + WAIT_S
        while not (runtime / "exec.sock").exists():
            assert time.monotonic() < deadline, "the relay did not bind its socket"
            time.sleep(0.05)
        paths = configured_paths(tmp_path)
        cli = PodmanCli(
            paths.podman_root,
            paths.podman_runroot,
            tmpdir=paths.podman_tmpdir,
            image_tmp=paths.image_tmp,
            containers_conf=paths.podman_containers_conf,
            storage_conf=paths.podman_storage_conf,
            runtime_root=runtime,
            binary=str(stub),
            cgroup_file=tmp_path / "not-in-leaf",
        )
        cli.cgroup_file.write_text("0::/user.slice\n", encoding="utf-8")
        acked: list[int] = []
        result = cli.exec(
            CID,
            ["true"],
            target=ExecTarget(realm=REALM, services=True, mutating=True),
            on_ack=acked.append,
        )
    finally:
        relay.terminate()
        relay.wait(WAIT_S)
    assert result.exit_code == 3
    assert result.stdout == "out line\n"
    assert result.stderr == "err line\n"
    assert len(acked) == 1 and acked[0] > 0
    assert "exec" in log.read_text(encoding="utf-8")


def _acked_and_exited(status: int) -> Callable[[socket.socket, bytes, list[int]], None]:
    def respond(conn: socket.socket, _record: bytes, _fds: list[int]) -> None:
        conn.send(_ack(9))
        conn.send(_exit(0, status))

    return respond


@pytest.mark.fr("FR-049")
def test_readiness_is_polled_so_a_descriptor_above_the_select_limit_is_watched() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < 1200:
        target = 4096 if hard == resource.RLIM_INFINITY else min(hard, 4096)
        if target < 1200:
            pytest.skip("the descriptor limit is too low for a descriptor above 1024")
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    try:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        high = fcntl.fcntl(left.fileno(), fcntl.F_DUPFD, 1100)
        moved = socket.socket(fileno=high)
        left.close()
        with pytest.raises(ValueError):
            _select_refuses(moved)
        assert exec_relay._readable(moved, 0.0) is False
        right.send(b"x")
        assert exec_relay._readable(moved, 1.0) is True
        moved.close()
        right.close()
    finally:
        if soft < 1200:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def _select_refuses(sock: socket.socket) -> None:
    """select(2) refuses a descriptor of 1024 or more, which is why the client polls (ADR-014)."""
    import select

    select.select([sock], [], [], 0)


@pytest.mark.fr("FR-049")
def test_an_interrupt_while_waiting_for_the_ack_closes_the_socket(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = sock_dir / "exec.sock"
    listener = _Listener(path, lambda _conn, _record, _fds: None).start()
    opened: list[socket.socket] = []
    connect = exec_relay._connect

    def recording(*args: Any, **kwargs: Any) -> socket.socket:
        sock = connect(*args, **kwargs)
        opened.append(sock)
        return sock

    def interrupted(_sock: socket.socket, _timeout: float | None) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(exec_relay, "_connect", recording)
    monkeypatch.setattr(exec_relay, "_readable", interrupted)
    with pytest.raises(KeyboardInterrupt):
        exec_relay.open_exchange(path, _request(), (0, 1, 2), service=None)
    listener.join()
    assert len(opened) == 1
    assert opened[0].fileno() == -1, "the socket is closed on every path that keeps no exchange"


@pytest.mark.fr("FR-048")
def test_the_callbacks_run_while_the_caller_signal_forwarding_is_installed(
    sock_dir: Path,
) -> None:
    path = sock_dir / "exec.sock"
    sentinel = lambda _signum, _frame: None  # noqa: E731 - a handler the caller installed
    previous = signal.signal(signal.SIGTERM, sentinel)
    seen: list[tuple[str, bool]] = []

    def installed() -> bool:
        return signal.getsignal(signal.SIGTERM) is not sentinel

    try:
        listener = _Listener(path, _acked_and_exited(7)).start()
        status = exec_relay.run_attached(
            path,
            _request(interactive=True),
            service=None,
            on_spawn=lambda _pid: seen.append(("spawn", installed())),
            on_ack=lambda _pid: seen.append(("ack", installed())),
            on_exit=lambda: seen.append(("exit", installed())),
        )
        listener.join()
        assert status == 7
        assert seen == [("spawn", True), ("ack", True), ("exit", True)]
        assert signal.getsignal(signal.SIGTERM) is sentinel
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.fr("FR-048")
def test_a_failing_spawn_callback_runs_on_exit_once_inside_the_forwarding_block(
    sock_dir: Path,
) -> None:
    path = sock_dir / "exec.sock"
    sentinel = lambda _signum, _frame: None  # noqa: E731 - a handler the caller installed
    previous = signal.signal(signal.SIGTERM, sentinel)
    exits: list[bool] = []

    def refuse(_pid: int) -> None:
        raise RuntimeError("the caller failed after the client started")

    def installed() -> bool:
        return signal.getsignal(signal.SIGTERM) is not sentinel

    try:
        listener = _Listener(path, _acked_and_exited(0)).start()
        with pytest.raises(RuntimeError):
            exec_relay.run_attached(
                path,
                _request(interactive=True),
                service=None,
                on_spawn=refuse,
                on_ack=None,
                on_exit=lambda: exits.append(installed()),
            )
        listener.join()
        assert exits == [True], "on_exit runs once, while the forwarding is still installed"
        assert signal.getsignal(signal.SIGTERM) is sentinel
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.fr("NFR-SEC-005")
def test_a_pass_through_secret_reaches_the_podman_environment_and_no_command_line(
    helpers: Path, tmp_path: Path, sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "hunter2-proxy-secret"  # noqa: S105 - a test value, not a credential
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:{secret}@proxy.example:3128")
    seen = tmp_path / "seen"
    seen.mkdir()
    stub = tmp_path / "podman"
    stub.write_text(
        "#!/bin/sh\n"
        f"tr '\\0' ' ' < /proc/$$/cmdline > {seen}/podman-cmdline\n"
        f"tr '\\0' '\\n' < /proc/$$/environ > {seen}/podman-environ\n"
        f"tr '\\0' ' ' < /proc/$PPID/cmdline > {seen}/helper-cmdline\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    runtime = sock_dir / "rt"
    runtime.mkdir(mode=0o700)
    relay = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(helpers / "ginn-exec-relay"), str(runtime), str(stub)],
        stdout=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + WAIT_S
        while not (runtime / "exec.sock").exists():
            assert time.monotonic() < deadline, "the relay did not bind its socket"
            time.sleep(0.05)
        paths = configured_paths(tmp_path)
        cli = PodmanCli(
            paths.podman_root,
            paths.podman_runroot,
            tmpdir=paths.podman_tmpdir,
            image_tmp=paths.image_tmp,
            containers_conf=paths.podman_containers_conf,
            storage_conf=paths.podman_storage_conf,
            runtime_root=runtime,
            binary=str(stub),
            cgroup_file=tmp_path / "cgroup",
        )
        cli.cgroup_file.write_text("0::/user.slice\n", encoding="utf-8")
        cli.exec(
            CID,
            ["true"],
            target=ExecTarget(realm=REALM, services=True, mutating=False),
            pass_env=("HTTPS_PROXY",),
        )
        relay_cmdline = Path(f"/proc/{relay.pid}/cmdline").read_bytes().decode()
    finally:
        relay.terminate()
        relay.wait(WAIT_S)
    assert secret in (seen / "podman-environ").read_text(encoding="utf-8")
    assert secret not in (seen / "podman-cmdline").read_text(encoding="utf-8")
    assert secret not in (seen / "helper-cmdline").read_text(encoding="utf-8")
    assert secret not in relay_cmdline
    assert secret not in " ".join(cli.history[-1])


@pytest.mark.fr("FR-084")
def test_a_service_runsvdir_has_not_scanned_yet_is_polled_and_started_again_mid_wait(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sv cannot open supervise/ok until runsvdir scans a fresh directory. The client keeps
    polling the socket, runs `sv up` again halfway through the wait, and connects once the
    listener is up."""
    from ginn.errors import make
    from ginn.services.exec_relay import EXEC_SERVICE

    monkeypatch.setattr(exec_relay, "UP_WAIT_S", 2)
    path = sock_dir / "exec.sock"
    calls: list[Path] = []
    listeners: list[_Listener] = []

    def not_yet(service: Path) -> None:
        calls.append(service)
        if len(calls) == 1:
            raise make(
                "GINN-E-SERVICE-CONTROL",
                realm=EXEC_SERVICE,
                action="up",
                detail="fail: _exec: unable to open supervise/ok: file does not exist",
            )
        listeners.append(_Listener(path, _acked_and_exited(0)).start())

    monkeypatch.setattr("ginn.services.exec_relay.up_exec_service", not_yet)
    exchange = exec_relay.open_exchange(path, _request(), (0, 1, 2), service=sock_dir / "_exec")
    try:
        assert exchange.wait(WAIT_S) == 0
    finally:
        exchange.close()
    listeners[0].join()
    assert len(calls) == 2


@pytest.mark.fr("FR-084")
def test_a_service_that_never_becomes_supervised_is_unavailable_at_the_bound(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ginn.errors import make
    from ginn.services.exec_relay import EXEC_SERVICE

    monkeypatch.setattr(exec_relay, "UP_WAIT_S", 1)

    def never(_service: Path) -> None:
        raise make(
            "GINN-E-SERVICE-CONTROL",
            realm=EXEC_SERVICE,
            action="up",
            detail="fail: _exec: unable to open supervise/ok: file does not exist",
        )

    monkeypatch.setattr("ginn.services.exec_relay.up_exec_service", never)
    with pytest.raises(GinnError) as caught:
        exec_relay.open_exchange(
            sock_dir / "absent.sock", _request(), (0, 1, 2), service=sock_dir / "_exec"
        )
    assert caught.value.code == "GINN-E-RELAY-UNAVAILABLE"
