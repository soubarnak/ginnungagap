# SPDX-License-Identifier: Apache-2.0
"""Generated SSH agent frame streams through native/ginn-agent-filter (FR-067 AC-1, NFR-SEC-007).

Every case runs the compiled helper against the fake agent in tests/fakes. The model below is
the one the design states: a request of type 11 or 13 is forwarded, and every other message gets
one failure. The fixed cases are in tests/unit/test_native_agent_filter.py.
"""

from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from tests.fakes.fake_ssh_agent import (  # noqa: E402
    FAILURE,
    REPLY_FOR,
    REQUEST_IDENTITIES,
    SIGN_REQUEST,
    FakeAgent,
    build_filter,
    encode_frame,
    exchange,
    read_frame,
    start_filter,
    stop_filter,
)

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "native" / "ginn-agent-filter.c"
WAIT_S = 10.0
ALLOWED_REPLIES = {FAILURE, 12, 14}
ALLOWED_REQUESTS = {REQUEST_IDENTITIES, SIGN_REQUEST}

pytestmark = [pytest.mark.fr("FR-067"), pytest.mark.fr("NFR-SEC-007")]

KINDS = st.one_of(
    st.sampled_from([REQUEST_IDENTITIES, SIGN_REQUEST]),
    st.sampled_from([5, 6, 12, 14, 17, 18, 19, 20, 21, 22, 23, 25, 26, 27]),
    st.integers(min_value=0, max_value=255),
)
FRAMES = st.lists(st.tuples(KINDS, st.binary(max_size=300)), min_size=1, max_size=12)
CUTS = st.lists(st.integers(min_value=1, max_value=40), max_size=30)


Message = tuple[int, bytes]


def pieces(data: bytes, cuts: list[int]) -> Iterator[bytes]:
    """`data` cut into pieces of the given sizes, with the rest in one last piece."""
    pos = 0
    for size in cuts:
        if pos >= len(data):
            break
        yield data[pos : pos + size]
        pos += size
    if pos < len(data):
        yield data[pos:]


def model(frames: list[Message]) -> tuple[list[Message], list[Message]]:
    """What the agent should see, and what the client should read back, for these frames."""
    forwarded: list[tuple[int, bytes]] = []
    replies: list[tuple[int, bytes]] = []
    for kind, payload in frames:
        if kind in ALLOWED_REQUESTS:
            forwarded.append((kind, payload))
            replies.append((REPLY_FOR[kind], payload))
        else:
            replies.append((FAILURE, b""))
    return forwarded, replies


def send_in_pieces(listen: Path, wire: bytes, cuts: list[int], expect: int) -> list[Message]:
    """Send `wire` cut into pieces of the given sizes, then read up to `expect` replies."""
    replies: list[Message] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(WAIT_S)
        client.connect(str(listen))
        for piece in pieces(wire, cuts):
            client.sendall(piece)
        for _ in range(expect):
            frame = read_frame(client)
            if frame is None:
                break
            replies.append(frame)
    return replies


@pytest.fixture(scope="module")
def helper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    return build_filter(tmp_path_factory.mktemp("agentframing"), SOURCE, cc)


@pytest.fixture(scope="module")
def running(
    helper: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[Path, FakeAgent]]:
    """One filter and one fake agent for every generated case in this module."""
    base = tmp_path_factory.mktemp("agentsock")
    upstream = base / "agent.sock"
    listen = base / "filter.sock"
    with FakeAgent(upstream) as agent:
        proc = start_filter(helper, upstream, listen)
        try:
            yield listen, agent
        finally:
            stop_filter(proc)


@given(FRAMES, CUTS)
def test_only_list_and_sign_reach_the_agent_and_each_message_gets_one_reply(
    running: tuple[Path, FakeAgent], frames: list[tuple[int, bytes]], cuts: list[int]
) -> None:
    """Whatever the framing, the agent sees exactly the allowed requests, in order, unchanged."""
    listen, agent = running
    forwarded, expected = model(frames)
    agent.received.clear()
    wire = b"".join(encode_frame(kind, payload) for kind, payload in frames)
    replies = send_in_pieces(listen, wire, cuts, len(frames))
    assert replies == expected
    assert agent.received == forwarded


@given(st.binary(max_size=2000), CUTS)
def test_a_junk_stream_never_gets_a_reply_outside_the_allowlist(
    running: tuple[Path, FakeAgent], junk: bytes, cuts: list[int]
) -> None:
    """Random bytes may close the connection, but no reply type or forwarded request is ever new."""
    listen, agent = running
    agent.received.clear()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(WAIT_S)
        client.connect(str(listen))
        for piece in pieces(junk, cuts):
            client.sendall(piece)
        client.shutdown(socket.SHUT_WR)
        seen: list[Message] = []
        for _ in range(200):
            frame = read_frame(client)
            if frame is None:
                break
            seen.append(frame)
    assert all(kind in ALLOWED_REPLIES for kind, _ in seen)
    assert all(kind in ALLOWED_REQUESTS for kind, _ in agent.received)
    agent.received.clear()
    assert exchange(listen, encode_frame(REQUEST_IDENTITIES), 1) == [(12, b"")]
    assert agent.received == [(REQUEST_IDENTITIES, b"")]


@given(st.lists(st.integers(min_value=0, max_value=255), min_size=1, max_size=40))
def test_a_refused_request_is_answered_by_the_failure_type_only(
    running: tuple[Path, FakeAgent], kinds: list[int]
) -> None:
    """Requests of other types, one per frame, get failures and never reach the agent."""
    listen, agent = running
    refused = [k for k in kinds if k not in ALLOWED_REQUESTS]
    agent.received.clear()
    wire = b"".join(encode_frame(k) for k in refused)
    replies = send_in_pieces(listen, wire, [], len(refused))
    assert replies == [(FAILURE, b"")] * len(refused)
    assert agent.received == []
