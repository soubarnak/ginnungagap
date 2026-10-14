# SPDX-License-Identifier: Apache-2.0
"""The client of the exec relay: one exec request from the CLI to the per-user listener (ADR-014).

A services-on realm's processes live in the supervisor leaf, so a process of the user's session
cannot move into them (ADR-014, F2). PodmanCli sends such an exec here instead, unless the caller is
already inside the leaf. The request carries the argv tail that PodmanCli builds for the direct
route, the realm, the container id and the pass-through values. The worker checks the request and
runs podman exec in the leaf (native/ginn-exec-worker.c, native/ginn-exec-run.c).

The frame layout is native/ginn-exec-frame.h, which governs. The constants below repeat it, and
tests/unit/test_exec_relay_client.py reads the header and checks them.

One request runs in this order. Connect to `<runtime root>/exec.sock`. A refused connection runs
`sv up` on the `_exec` service once, then the socket is polled for UP_WAIT_S seconds. Send REQ with
the descriptors. Wait ACK_WAIT_S seconds for ACK, which carries the podman client's pid. Then wait
for EXIT, and forward the caller's signals to the worker as SIG messages until it arrives. The
timeouts of a captured run belong to PodmanCli, which reads the pipes. The relay has no timers.

Only the standard library is used here. services/exec_relay.py imports PodmanCli, so the services
modules are imported inside the functions that need them, as runtime/mounts.py does.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import struct
import termios
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ginn.errors import GinnError, make

SOCKET_NAME: Final = "exec.sock"
"""The listener's socket under the runtime root. It equals services.exec_relay.SOCKET_NAME."""
PROC_CGROUP: Final = Path("/proc/self/cgroup")
"""The cgroup file that decides whether the caller is inside the supervisor leaf (ADR-014)."""

REQ: Final = 0x01
ACK: Final = 0x02
REJECT: Final = 0x03
SIG: Final = 0x04
EXIT: Final = 0x05
EXIT_STATUS: Final = 0
EXIT_SIGNAL: Final = 1
VERSION: Final = 1
FLAG_TTY: Final = 1
FLAG_INTERACTIVE: Final = 2
FLAG_MUTATING: Final = 4
FLAG_CAPTURED: Final = 8
CID_LEN: Final = 64
MAX_RECORD: Final = 65536
MAX_ITEMS: Final = 256
MAX_VALUE: Final = 4096
REJECT_REJECTED: Final = 1
REJECT_LOCKED: Final = 2
REJECT_BUSY: Final = 3
WORKER_CAP: Final = 32
"""The listener's cap on running workers (ADR-014). The BUSY message names it."""
ACK_WAIT_S: Final = 5
"""How long the client waits for ACK before GINN-E-RELAY-TIMEOUT (ADR-014)."""
KILL_WAIT_S: Final = 5
"""How long a stop waits after SIGTERM before SIGKILL (ADR-014, the bound of a captured run)."""
UP_WAIT_S: Final = 10
NOT_SUPERVISED_YET: Final = "unable to open supervise/ok"
"""What sv reports for a service directory that runsvdir has not scanned yet (ADR-014)."""
"""How long the client polls the socket after `sv up` before GINN-E-RELAY-UNAVAILABLE (ADR-014)."""
CONNECT_POLL_S: Final = 0.1
_SUPERVISOR_LEAF: Final = "/ginn/{uid}/supervisor"
_HEX: Final = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class Request:
    """One exec request as the client sends it (ADR-014, Wire protocol).

    `tail` is the argv after the fixed prefix that the worker adds: `exec`, the options, `--`, the
    container id and the command. It is the same tail the direct route runs. `passed` holds the
    NAME=VALUE pairs of the pass-through names, read from the environment podman would see.
    `tty`, `interactive` and `mutating` are the flags of the REQ, and `tty` and `interactive` must
    also appear in the tail, which PodmanCli guarantees by building both from the same arguments.
    """

    realm: str
    container_id: str
    tail: tuple[str, ...]
    passed: tuple[str, ...]
    tty: bool
    interactive: bool
    mutating: bool
    captured: bool


def in_supervisor_leaf(cgroup_file: Path = PROC_CGROUP) -> bool:
    """True when the caller runs inside `/ginn/<uid>/supervisor` or below it (ADR-014, Decision 1).

    Read from the unified `0::` line of /proc/self/cgroup. A process in the leaf enters a realm
    directly, as the supervisor's own processes always did. An unreadable file answers False, so
    the request goes through the relay, which is the safe route from any process of the uid.
    """
    leaf = _SUPERVISOR_LEAF.format(uid=os.getuid())
    try:
        text = cgroup_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            return fields[2] == leaf or fields[2].startswith(leaf + "/")
    return False


def encode(request: Request) -> bytes:
    """The REQ record of `request` (ADR-014, Wire protocol). Pure.

    The limits of the worker are checked here, so a request that it would refuse is not sent: a
    container id of 64 lowercase hex characters, at most 256 items in each list, a pass-through
    value of at most 4 KiB, and a record of at most 64 KiB. A request outside them raises
    GINN-E-RELAY-REJECTED before anything is sent.
    """
    if len(request.container_id) != CID_LEN or not _HEX.issuperset(request.container_id):
        raise _rejected(request.realm, "the container id is not 64 lowercase hex characters")
    flags = (
        (FLAG_TTY if request.tty else 0)
        | (FLAG_INTERACTIVE if request.interactive else 0)
        | (FLAG_MUTATING if request.mutating else 0)
        | (FLAG_CAPTURED if request.captured else 0)
    )
    body = bytearray((REQ, VERSION, flags, 5 if request.captured else 3))
    body += _text(request.realm, request.realm)
    body += request.container_id.encode("ascii")
    for items in (request.passed, request.tail):
        if len(items) > MAX_ITEMS:
            raise _rejected(request.realm, f"more than {MAX_ITEMS} items in one request")
        body += struct.pack(">H", len(items))
        for item in items:
            body += _text(item, request.realm)
    for item in request.passed:
        if len(item.partition("=")[2].encode("utf-8", "surrogateescape")) > MAX_VALUE:
            raise _rejected(request.realm, f"a pass-through value is longer than {MAX_VALUE} bytes")
    if len(body) > MAX_RECORD:
        raise _rejected(request.realm, f"the request is larger than {MAX_RECORD} bytes")
    return bytes(body)


def _text(value: str, realm: str) -> bytes:
    """A string as the wire has it: a u16 byte length and the bytes, with no NUL (ADR-014)."""
    raw = value.encode("utf-8", "surrogateescape")
    if b"\0" in raw or len(raw) > 0xFFFF:
        raise _rejected(realm, "a request string holds a NUL or is too long")
    return struct.pack(">H", len(raw)) + raw


class Exchange:
    """The relay side of one request, from its ACK to its EXIT (ADR-014).

    `pid` is the podman client's pid, as the worker reports it. The client never signals that pid
    itself: `signal` sends a SIG message, and the worker sends the signal to podman. `returncode` is
    set once EXIT has arrived, with the Popen convention: a signal is negative.
    """

    def __init__(self, sock: socket.socket, realm: str, pid: int) -> None:
        self._sock: socket.socket | None = sock
        self._realm = realm
        self.pid = pid
        self.returncode: int | None = None

    def signal(self, signum: int) -> None:
        """Send one SIG message. A worker that has already left needs no signal: no error."""
        if self._sock is None or self.returncode is not None:
            return
        try:
            self._sock.send(bytes((SIG, signum)))
        except OSError:
            return

    def wait(self, timeout: float | None) -> int | None:
        """The exit status once EXIT arrives, or None when `timeout` seconds pass first.

        EOF or a broken frame before EXIT is GINN-E-RELAY-LOST, because the exit status is then
        unknown. Once the status is known, every later call returns it.
        """
        if self.returncode is not None:
            return self.returncode
        if self._sock is None:
            raise _lost(self._realm)
        try:
            if not _readable(self._sock, timeout):
                return None
            record = self._sock.recv(MAX_RECORD)
        except OSError as exc:
            raise _lost(self._realm) from exc
        if len(record) != 3 or record[0] != EXIT or record[1] not in (EXIT_STATUS, EXIT_SIGNAL):
            raise _lost(self._realm)
        self.returncode = record[2] if record[1] == EXIT_STATUS else -record[2]
        return self.returncode

    def stop(self) -> None:
        """Stop the run after a bound or an error: SIGTERM, then SIGKILL after KILL_WAIT_S.

        This is the bound of a captured run (ADR-014). A relay that is gone is not an error here,
        because the caller already has its own error.
        """
        if self.returncode is not None:
            return
        self.signal(signal.SIGTERM)
        try:
            if self.wait(KILL_WAIT_S) is None:
                self.signal(signal.SIGKILL)
                self.wait(None)
        except GinnError as exc:
            if exc.code != "GINN-E-RELAY-LOST":
                raise

    def close(self) -> None:
        """Close the socket. The run is not stopped by this (see `stop`)."""
        if self._sock is not None:
            self._sock.close()
            self._sock = None


def open_exchange(
    socket_path: Path,
    request: Request,
    descriptors: Sequence[int],
    *,
    service: Path | None,
) -> Exchange:
    """Send REQ with `descriptors` and return the exchange once ACK has arrived (ADR-014).

    `descriptors` are 0, 1 and 2 for a run with the caller's stdio. A captured run sends 0, then
    the write ends of the stdout and stderr pipes, then their read ends. `service` is the `_exec`
    service directory, which `sv up` starts when the socket refuses; None means no `sv up`. The
    caller closes its copies of the pipe write ends after this returns, because the worker holds
    its own copies from the send (ADR-014).

    A REJECT maps to GINN-E-RELAY-REJECTED, GINN-E-REALM-BUSY (LOCKED) or GINN-E-RELAY-BUSY. No
    ACK within ACK_WAIT_S is GINN-E-RELAY-TIMEOUT. EOF before ACK is GINN-E-RELAY-LOST.
    """
    record = encode(request)
    sock = _connect(socket_path, request.realm, service)
    kept = False
    try:
        try:
            socket.send_fds(sock, [record], list(descriptors))
        except OSError as exc:
            raise _lost(request.realm) from exc
        try:
            if not _readable(sock, ACK_WAIT_S):
                raise make("GINN-E-RELAY-TIMEOUT", realm=request.realm, seconds=ACK_WAIT_S)
            reply = sock.recv(MAX_RECORD)
        except OSError as exc:
            raise _lost(request.realm) from exc
        if not reply:
            raise _lost(request.realm)
        if reply[0] == REJECT and len(reply) >= 4:
            raise _refused(request, reply)
        if reply[0] != ACK or len(reply) != 5:
            raise _lost(request.realm)
        (pid,) = struct.unpack(">I", reply[1:5])
        kept = True
        return Exchange(sock, request.realm, pid)
    finally:
        # Every path that does not hand the socket to the exchange closes it, a KeyboardInterrupt
        # included (ADR-014).
        if not kept:
            sock.close()


def _readable(sock: socket.socket, timeout: float | None) -> bool:
    """True when `sock` has data or a hangup within `timeout` seconds (None waits without a limit).

    poll(2) is used, not select(2): select cannot watch a descriptor of 1024 or more, and a process
    with many files open can hold one (ADR-014).
    """
    poller = select.poll()
    poller.register(sock.fileno(), select.POLLIN | select.POLLHUP | select.POLLERR)
    milliseconds = None if timeout is None else max(int(timeout * 1000), 0)
    return bool(poller.poll(milliseconds))


def run_attached(
    socket_path: Path,
    request: Request,
    *,
    service: Path | None,
    on_spawn: Callable[[int], None] | None,
    on_ack: Callable[[int], None] | None,
    on_exit: Callable[[], None] | None,
) -> int:
    """Run an interactive request with the caller's stdio and return its status (FR-048, FR-049).

    The caller's descriptors 0, 1 and 2 go to the worker, so podman takes the terminal itself. On
    a TTY request the caller's termios is saved and restored on every exit path (ADR-014). The
    caller's SIGINT, SIGTERM and SIGHUP are forwarded as SIG messages, and SIGWINCH too on a TTY
    request, so the caller's handlers are replaced while the request runs (CL-27).

    The callbacks run inside that forwarding block. `on_spawn` and `on_ack` receive the podman pid
    as soon as the worker acknowledges it. `on_exit` runs once the pid is reaped, and on every
    error path after the client started, so a caller's signal handler never reaches a pid that has
    already ended (enter.py `_orderly_signals`).
    """
    with tty_state() if request.tty else nullcontext():
        exchange = open_exchange(socket_path, request, (0, 1, 2), service=service)
        reaped = False
        try:
            # The error path is inside the forwarding block too: a handler restored before on_exit
            # could signal a pid that has not been reaped yet.
            with forwarding(exchange, tty=request.tty):
                try:
                    if on_spawn is not None:
                        on_spawn(exchange.pid)
                    if on_ack is not None:
                        on_ack(exchange.pid)
                    status = exchange.wait(None)
                    if status is None:  # no timeout was given, so this is only a type guard
                        raise _lost(request.realm)
                    reaped = True
                    if on_exit is not None:
                        on_exit()
                except BaseException:
                    exchange.stop()
                    if on_exit is not None and not reaped:
                        on_exit()
                    raise
        finally:
            exchange.close()
    return status


@contextmanager
def tty_state() -> Iterator[None]:
    """Save the terminal's termios and restore it on every exit (ADR-014, Terminal handling).

    A TTY request hands the terminal to podman, which sets raw mode. A crashed client does not
    restore it, so the caller does. Without a terminal on fd 0 nothing is saved.
    """
    if not os.isatty(0):
        yield
        return
    saved = termios.tcgetattr(0)
    try:
        yield
    finally:
        try:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
        except termios.error:
            pass


@contextmanager
def forwarding(exchange: Exchange, *, tty: bool) -> Iterator[None]:
    """Forward the caller's signals to the worker as SIG messages while the exchange runs (ADR-014).

    Only the main thread may set a handler, so any other thread runs without forwarding. The
    previous handlers are restored when the block ends. SIGWINCH is forwarded only on a TTY request.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    signums = [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]
    if tty:
        signums.append(signal.SIGWINCH)

    def forward(signum: int, _frame: object) -> None:
        exchange.signal(signum)

    saved = {signum: signal.getsignal(signum) for signum in signums}
    for signum in signums:
        signal.signal(signum, forward)
    try:
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler if handler is not None else signal.SIG_DFL)


def _connect(socket_path: Path, realm: str, service: Path | None) -> socket.socket:
    """Connect to the listener. A refusal runs `sv up`, then the socket is polled (ADR-014).

    A fresh `_exec` directory is not supervised until runsvdir scans its directory. Then sv cannot
    open supervise/ok, and `_revive` reports that as "not yet": the poll goes on, and `sv up` runs
    once more halfway through the wait. Any other failure of `sv up` is GINN-E-RELAY-UNAVAILABLE.
    """
    try:
        return _dial(socket_path)
    except OSError as exc:
        if service is None or not _refused_socket(exc):
            raise _unavailable(realm, socket_path, exc) from exc
    started = time.monotonic()
    deadline = started + UP_WAIT_S
    retry_at = started + UP_WAIT_S / 2
    pending_retry = _revive(realm, service)
    while True:
        try:
            return _dial(socket_path)
        except OSError as exc:
            now = time.monotonic()
            if now >= deadline:
                raise _unavailable(realm, socket_path, exc) from exc
            if pending_retry and now >= retry_at:
                pending_retry = _revive(realm, service)
            time.sleep(CONNECT_POLL_S)


def _dial(socket_path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
    try:
        sock.connect(os.fspath(socket_path))
    except OSError:
        sock.close()
        raise
    return sock


def _refused_socket(exc: OSError) -> bool:
    """A listener that is not running: no socket file, or a socket nobody listens on."""
    return isinstance(exc, (ConnectionRefusedError, FileNotFoundError))


def _revive(realm: str, service: Path) -> bool:
    """Run `sv up` on the relay service (ADR-014). True when the service is not supervised yet.

    The start is services.exec_relay.up_exec_service, imported here because that module imports
    PodmanCli. sv's "unable to open supervise/ok" means runsvdir has not scanned the directory yet,
    so the caller keeps polling the socket and returns True. Any other failure is
    GINN-E-RELAY-UNAVAILABLE with sv's own detail.
    """
    from ginn.services.exec_relay import up_exec_service

    try:
        up_exec_service(service)
    except GinnError as err:
        detail = str(err.details.get("detail", err.message))
        if NOT_SUPERVISED_YET in detail:
            return True
        raise make("GINN-E-RELAY-UNAVAILABLE", realm=realm, detail=detail) from err
    return False


def _refused(request: Request, reply: bytes) -> GinnError:
    """The error of a REJECT frame: u8 code, then the reason as a u16-length string (ADR-014)."""
    code = reply[1]
    size = struct.unpack(">H", reply[2:4])[0]
    reason = reply[4 : 4 + size].decode("utf-8", "replace") or "no reason given"
    if code == REJECT_LOCKED:
        return make("GINN-E-REALM-BUSY", op=f"exec of realm '{request.realm}' ({reason})")
    if code == REJECT_BUSY:
        return make("GINN-E-RELAY-BUSY", realm=request.realm, limit=WORKER_CAP)
    if code == REJECT_REJECTED:
        return _rejected(request.realm, reason)
    return _lost(request.realm)


def _rejected(realm: str, reason: str) -> GinnError:
    return make("GINN-E-RELAY-REJECTED", realm=realm, reason=reason)


def _lost(realm: str) -> GinnError:
    return make("GINN-E-RELAY-LOST", realm=realm)


def _unavailable(realm: str, socket_path: Path, exc: OSError) -> GinnError:
    detail = f"no listener at {socket_path}: {exc.strerror or exc}"
    return make("GINN-E-RELAY-UNAVAILABLE", realm=realm, detail=detail)


__all__ = [
    "ACK",
    "CID_LEN",
    "EXIT",
    "PROC_CGROUP",
    "REQ",
    "SIG",
    "SOCKET_NAME",
    "Exchange",
    "Request",
    "encode",
    "in_supervisor_leaf",
    "open_exchange",
    "run_attached",
]
