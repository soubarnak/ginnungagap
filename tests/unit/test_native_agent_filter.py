# SPDX-License-Identifier: Apache-2.0
"""native/ginn-agent-filter.c: it builds clean, stays within 200 lines, validates its argv, and
passes only the SSH agent requests a Development realm needs (FR-067 AC-1, NFR-SEC-007).

The fixed cases here run the compiled helper against the fake agent in tests/fakes. Nothing here
starts ssh-agent or touches the host agent. The property test over generated frames is in
tests/property/test_agent_framing.py.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from tests.fakes.fake_ssh_agent import (
    FAILURE,
    REQUEST_IDENTITIES,
    SIGN_REQUEST,
    FakeAgent,
    build_filter,
    encode_frame,
    exchange,
    start_filter,
    stop_filter,
)

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "native"
SOURCE = NATIVE / "ginn-agent-filter.c"
LINE_LIMIT = 200
MAX_BODY = 256 * 1024
WAIT_S = 10.0

pytestmark = [pytest.mark.fr("FR-067"), pytest.mark.fr("NFR-SEC-007")]

# The draft-miller-ssh-agent message types that change keys or the agent's state (AC-1).
KEY_CHANGING = [17, 18, 19, 20, 21, 22, 23, 25, 26, 27]


@pytest.fixture(scope="module")
def helper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    return build_filter(tmp_path_factory.mktemp("agentfilter"), SOURCE, cc)


@pytest.fixture
def stack(helper: Path, tmp_path: Path) -> tuple[Path, FakeAgent, subprocess.Popen[bytes]]:
    """A running filter in front of a fake agent, both under a short temporary path."""
    upstream = tmp_path / "agent.sock"
    listen = tmp_path / "filter.sock"
    with FakeAgent(upstream) as agent:
        proc = start_filter(helper, upstream, listen)
        try:
            yield listen, agent, proc
        finally:
            stop_filter(proc)


def test_source_stays_within_the_line_limit() -> None:
    lines = SOURCE.read_text(encoding="utf-8").count("\n")
    assert lines <= LINE_LIMIT, f"{SOURCE.name} has {lines} lines; the limit is {LINE_LIMIT}"


def test_the_makefile_check_target_builds_it_with_gcc_and_clang() -> None:
    make = shutil.which("make")
    if make is None or shutil.which("clang") is None or shutil.which("gcc") is None:
        pytest.skip("make, gcc and clang are needed for the Makefile gate")
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [make, "-C", str(NATIVE), "check"], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "ginn-agent-filter.c" in run.stdout


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--once"],
        ["/u"],
        ["relative", "/l"],
        ["/u", "relative"],
        ["/u/../x", "/l"],
        ["/u", "/l/.."],
        ["/u\nx", "/l"],
        ["--bogus", "/u", "/l"],
        ["/u", "/l", "extra"],
    ],
)
def test_bad_argv_exits_2_with_usage(helper: Path, args: list[str]) -> None:
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), *args], capture_output=True, text=True, check=False, timeout=WAIT_S
    )
    assert run.returncode == 2, run.stderr
    assert "usage: ginn-agent-filter" in run.stderr


def test_an_existing_listen_path_is_an_error_and_is_not_removed(
    helper: Path, tmp_path: Path
) -> None:
    listen = tmp_path / "filter.sock"
    listen.write_text("not a socket", encoding="utf-8")
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), "/nonexistent/agent.sock", str(listen)],
        capture_output=True,
        text=True,
        check=False,
        timeout=WAIT_S,
    )
    assert run.returncode == 1
    assert "bind" in run.stderr
    assert listen.read_text(encoding="utf-8") == "not a socket"


@pytest.mark.fr("FR-067")
def test_the_listen_socket_is_owner_only(helper: Path, tmp_path: Path) -> None:
    upstream = tmp_path / "agent.sock"
    listen = tmp_path / "filter.sock"
    with FakeAgent(upstream):
        proc = start_filter(helper, upstream, listen)
        try:
            assert listen.stat().st_mode & 0o077 == 0
        finally:
            stop_filter(proc)


@pytest.mark.fr("FR-067")
def test_identities_request_is_forwarded_and_its_answer_returned(
    stack: tuple[Path, FakeAgent, subprocess.Popen[bytes]],
) -> None:
    listen, agent, _proc = stack
    replies = exchange(listen, encode_frame(REQUEST_IDENTITIES), 1)
    assert replies == [(12, b"")]
    assert agent.received == [(REQUEST_IDENTITIES, b"")]


@pytest.mark.fr("FR-067")
def test_sign_request_is_forwarded_with_its_payload(
    stack: tuple[Path, FakeAgent, subprocess.Popen[bytes]],
) -> None:
    listen, agent, _proc = stack
    replies = exchange(listen, encode_frame(SIGN_REQUEST, b"data"), 1)
    assert replies == [(14, b"data")]
    assert agent.received == [(SIGN_REQUEST, b"data")]


@pytest.mark.fr("FR-067")
def test_remove_all_identities_is_refused_and_never_reaches_the_agent(
    stack: tuple[Path, FakeAgent, subprocess.Popen[bytes]],
) -> None:
    """`ssh-add -D` sends type 19. The filter answers failure, and the agent sees nothing."""
    listen, agent, _proc = stack
    replies = exchange(listen, encode_frame(19), 1)
    assert replies == [(FAILURE, b"")]
    assert agent.received == []


@pytest.mark.fr("FR-067")
@pytest.mark.parametrize("kind", KEY_CHANGING)
def test_every_key_changing_or_extension_request_is_refused(
    stack: tuple[Path, FakeAgent, subprocess.Popen[bytes]], kind: int
) -> None:
    listen, agent, _proc = stack
    replies = exchange(listen, encode_frame(kind, b"key material"), 1)
    assert replies == [(FAILURE, b"")]
    assert agent.received == []


@pytest.mark.fr("FR-067")
def test_a_refused_request_does_not_end_the_connection(
    stack: tuple[Path, FakeAgent, subprocess.Popen[bytes]],
) -> None:
    listen, agent, _proc = stack
    wire = encode_frame(22) + encode_frame(REQUEST_IDENTITIES) + encode_frame(18, b"x")
    replies = exchange(listen, wire, 3)
    assert replies == [(FAILURE, b""), (12, b""), (FAILURE, b"")]
    assert agent.received == [(REQUEST_IDENTITIES, b"")]


@pytest.mark.fr("FR-067")
def test_a_frame_that_arrives_in_pieces_is_reassembled(
    stack: tuple[Path, FakeAgent, subprocess.Popen[bytes]],
) -> None:
    listen, agent, _proc = stack
    wire = encode_frame(SIGN_REQUEST, b"piecewise")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(WAIT_S)
        client.connect(str(listen))
        for i in range(len(wire)):
            client.sendall(wire[i : i + 1])
            time.sleep(0.001)
        head = client.recv(4)
        assert int.from_bytes(head, "big") == len(b"\x0epiecewise")
        body = b""
        while len(body) < len(b"\x0epiecewise"):
            body += client.recv(64)
    assert body == b"\x0epiecewise"
    assert agent.received == [(SIGN_REQUEST, b"piecewise")]


@pytest.mark.fr("FR-067")
def test_a_zero_length_frame_gets_a_failure_and_the_connection_closes(
    stack: tuple[Path, FakeAgent, subprocess.Popen[bytes]],
) -> None:
    listen, agent, _proc = stack
    assert exchange(listen, b"\x00\x00\x00\x00", 2) == [(FAILURE, b"")]
    assert agent.received == []


@pytest.mark.fr("FR-067")
def test_an_oversize_length_is_refused_before_any_body_is_read(
    stack: tuple[Path, FakeAgent, subprocess.Popen[bytes]],
) -> None:
    """A hostile length of 2 GiB gets a failure and a closed connection. No body is read."""
    listen, agent, proc = stack
    assert exchange(listen, b"\x7f\xff\xff\xff" + b"\x0b", 2) == [(FAILURE, b"")]
    assert agent.received == []
    assert proc.poll() is None  # the helper keeps listening for the next client
    assert exchange(listen, encode_frame(REQUEST_IDENTITIES), 1) == [(12, b"")]


@pytest.mark.fr("FR-067")
def test_a_length_one_over_the_limit_is_refused(
    stack: tuple[Path, FakeAgent, subprocess.Popen[bytes]],
) -> None:
    listen, agent, _proc = stack
    wire = (MAX_BODY + 1).to_bytes(4, "big") + b"\x0b"
    assert exchange(listen, wire, 2) == [(FAILURE, b"")]
    assert agent.received == []


@pytest.mark.fr("FR-067")
def test_a_body_at_the_limit_is_forwarded(
    stack: tuple[Path, FakeAgent, subprocess.Popen[bytes]],
) -> None:
    listen, agent, _proc = stack
    payload = b"s" * (MAX_BODY - 1)
    replies = exchange(listen, encode_frame(SIGN_REQUEST, payload), 1)
    assert replies == [(14, payload)]
    assert agent.received == [(SIGN_REQUEST, payload)]


@pytest.mark.fr("FR-067")
def test_a_reply_outside_the_allowlist_becomes_a_failure(helper: Path, tmp_path: Path) -> None:
    """An agent that answers with a key-adding reply type gives the client a failure only."""
    upstream = tmp_path / "agent.sock"
    listen = tmp_path / "filter.sock"
    with FakeAgent(upstream, bad_reply=17):
        proc = start_filter(helper, upstream, listen)
        try:
            assert exchange(listen, encode_frame(REQUEST_IDENTITIES), 1) == [(FAILURE, b"")]
        finally:
            stop_filter(proc)


@pytest.mark.fr("FR-067")
def test_a_missing_upstream_agent_gets_a_failure(helper: Path, tmp_path: Path) -> None:
    upstream = tmp_path / "no-agent.sock"
    listen = tmp_path / "filter.sock"
    proc = start_filter(helper, upstream, listen)
    try:
        assert exchange(listen, encode_frame(REQUEST_IDENTITIES), 1) == [(FAILURE, b"")]
    finally:
        stop_filter(proc)


@pytest.mark.fr("FR-067")
def test_once_serves_one_client_and_exits_zero(helper: Path, tmp_path: Path) -> None:
    upstream = tmp_path / "agent.sock"
    listen = tmp_path / "filter.sock"
    with FakeAgent(upstream):
        proc = start_filter(helper, upstream, listen, once=True)
        try:
            assert exchange(listen, encode_frame(REQUEST_IDENTITIES), 1) == [(12, b"")]
            assert proc.wait(timeout=WAIT_S) == 0
        finally:
            stop_filter(proc)
