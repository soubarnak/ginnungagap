# SPDX-License-Identifier: Apache-2.0
"""A fake ssh-agent socket and SSH agent frame helpers for the agent filter tests (FR-067).

`FakeAgent` serves the agent protocol on a unix socket from a thread. It records every request
frame it receives, so a test can check what reached the agent. Replies are canned: a sign request
gets a sign response, a list request gets an identities answer, and any other request gets a
failure. `bad_reply` makes every reply carry another type, so a test can check the filter's
reply allowlist.

A frame is a 4-byte big-endian length, then a body whose first byte is the message type (the
draft-miller-ssh-agent framing). The helpers below encode and decode that form and nothing else.
"""

from __future__ import annotations

import os
import select
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Final

Frame = tuple[int, bytes]
"""A message: its type and its payload, which is the body after the type byte."""

REQUEST_IDENTITIES: Final = 11
SIGN_REQUEST: Final = 13
FAILURE: Final = 5
REPLY_FOR: Final = {REQUEST_IDENTITIES: 12, SIGN_REQUEST: 14}
"""The reply type to each request type that the fake agent answers."""
WAIT_S: Final = 10.0


def encode_frame(kind: int, payload: bytes = b"") -> bytes:
    """The wire form of one message: the length of type and payload, then both."""
    body = bytes([kind]) + payload
    return len(body).to_bytes(4, "big") + body


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket) -> Frame | None:
    """The next message on `sock`. None at end of stream, or on a zero length."""
    head = _recv_exact(sock, 4)
    if head is None:
        return None
    length = int.from_bytes(head, "big")
    if length == 0:
        return None
    body = _recv_exact(sock, length)
    if body is None:
        return None
    return body[0], body[1:]


class FakeAgent:
    """An ssh-agent stand-in on `path`. Use it as a context manager, or call start and stop."""

    def __init__(self, path: Path, *, bad_reply: int | None = None) -> None:
        self.path = path
        self.bad_reply = bad_reply
        self.received: list[Frame] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(8)
        self._server.settimeout(0.05)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> FakeAgent:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=WAIT_S)
        self._server.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            with conn:
                self._serve(conn)

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(WAIT_S)
        while True:
            frame = read_frame(conn)
            if frame is None:
                return
            kind, payload = frame
            self.received.append(frame)
            if kind in REPLY_FOR:
                rtype = REPLY_FOR[kind] if self.bad_reply is None else self.bad_reply
                conn.sendall(encode_frame(rtype, payload))
            else:
                conn.sendall(encode_frame(FAILURE))


def read_line(proc: subprocess.Popen[bytes], timeout: float = WAIT_S) -> str:
    """The next stdout line of `proc`, or an empty string at a timeout or at end of stream."""
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    buf = b""
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        ready, _, _ = select.select([proc.stdout.fileno()], [], [], left)
        if not ready:
            break
        chunk = os.read(proc.stdout.fileno(), 1)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8").rstrip("\n")


def start_filter(
    helper: Path, upstream: Path, listen: Path, *, once: bool = False
) -> subprocess.Popen[bytes]:
    """Start the compiled filter and wait for its `listening` line. Fails the caller when none."""
    argv = [str(helper), *(["--once"] if once else []), str(upstream), str(listen)]
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    line = read_line(proc)
    if not line.startswith("listening "):
        proc.kill()
        _out, err = proc.communicate(timeout=WAIT_S)
        raise AssertionError(f"filter did not listen: {line!r} {err!r}")
    return proc


def stop_filter(proc: subprocess.Popen[bytes]) -> None:
    """Stop a filter that loops until it is killed."""
    if proc.poll() is None:
        proc.terminate()
    proc.communicate(timeout=WAIT_S)


def exchange(listen: Path, wire: bytes, expect: int) -> list[Frame]:
    """Send `wire` on one new connection and return up to `expect` replies, in order."""
    replies: list[Frame] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(WAIT_S)
        client.connect(str(listen))
        client.sendall(wire)
        for _ in range(expect):
            frame = read_frame(client)
            if frame is None:
                break
            replies.append(frame)
    return replies


def build_filter(out_dir: Path, source: Path, cc: str) -> Path:
    """Compile the helper with the Makefile's warnings and -Werror. Fails on a compile error."""
    out = out_dir / "ginn-agent-filter"
    built = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2", "-o", str(out), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        raise AssertionError(built.stderr)
    return out
