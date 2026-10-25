# SPDX-License-Identifier: Apache-2.0
"""The helper processes of the security modules (FR-067, FR-068, AS-012): the process record, the
group stop, and the start that detaches, runs in `/`, and keeps its stderr in a log.

These tests start real processes that they own: `sleep` sessions from tests/fakes/fake_helpers.py,
`/bin/sh` scripts, and the compiled filter against FakeAgent. None of them touches a host agent or
a host bus.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from ginn.errors import GinnError
from ginn.security import _proc
from ginn.security._proc import Proc, signal_group, stop_group
from tests.fakes.fake_helpers import group_members, sleep_binary, spawn_sleeper
from tests.fakes.fake_ssh_agent import REQUEST_IDENTITIES, FakeAgent, build_filter, encode_frame

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "native" / "ginn-agent-filter.c"
WAIT_S = 10.0

pytestmark = [pytest.mark.fr("FR-067"), pytest.mark.fr("FR-068"), pytest.mark.fr("AS-012")]


def _need_sleep() -> None:
    if sleep_binary() is None:
        pytest.skip("sleep is needed for the process tests")


def _reap(popen: subprocess.Popen[bytes]) -> None:
    """Stop a sleeper the test started, whatever state the test left it in."""
    if popen.poll() is None:
        signal_group(popen.pid, signal.SIGKILL)
    popen.wait(timeout=WAIT_S)


def _wait_until(predicate: Callable[[], bool], timeout: float = WAIT_S) -> bool:
    """Poll `predicate` until it holds or `timeout` passes. The result is whether it held."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.mark.parametrize(
    "data",
    [
        {"pid": True, "start_ticks": 1},
        {"pid": 1, "start_ticks": True},
        {"pid": 0, "start_ticks": 1},
        {"pid": 1, "start_ticks": -1},
        {"pid": 1},
        {"pid": "1", "start_ticks": 1},
        "not an object",
        None,
    ],
)
def test_a_process_record_rejects_bools_negatives_and_missing_fields(data: object) -> None:
    with pytest.raises(ValueError):
        Proc.from_dict(data)


def test_a_process_record_round_trips_through_json() -> None:
    record = Proc(4242, 99)
    assert Proc.from_dict(json.loads(json.dumps(record.to_dict()))) == record


def test_a_live_record_is_alive_and_a_reused_pid_with_another_start_time_is_not() -> None:
    _need_sleep()
    sleeper = spawn_sleeper()
    try:
        live = Proc.of(sleeper.pid)
        assert live is not None and live.alive()
        assert not Proc(live.pid, live.start_ticks + 1).alive()
    finally:
        _reap(sleeper)


def test_stop_group_never_signals_a_reused_pid() -> None:
    """The record names our sleeper's pid with another start time: the sleeper must survive."""
    _need_sleep()
    sleeper = spawn_sleeper()
    try:
        live = Proc.of(sleeper.pid)
        assert live is not None
        stop_group(Proc(live.pid, live.start_ticks + 1))
        assert sleeper.poll() is None
    finally:
        _reap(sleeper)


def test_stop_group_stops_a_live_helper_and_every_process_in_its_group() -> None:
    """A shell that backgrounds a sleep leads a group with two members. One stop ends both."""
    _need_sleep()
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("sh is needed for the group test")
    leader = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [shell, "-c", "sleep 600 & echo $!; wait"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert leader.stdout is not None
        grandchild = int(leader.stdout.readline())
        assert grandchild in group_members(leader.pid)
        record = Proc.of(leader.pid)
        assert record is not None
        stop_group(record)
        assert _wait_until(lambda: not group_members(leader.pid))
        assert not record.alive()
    finally:
        _reap(leader)


def test_a_dead_record_and_a_missing_group_are_no_error() -> None:
    _need_sleep()
    sleeper = spawn_sleeper()
    record = Proc.of(sleeper.pid)
    assert record is not None
    signal_group(sleeper.pid, signal.SIGKILL)
    sleeper.wait(timeout=WAIT_S)
    stop_group(record)
    signal_group(999_999_999, signal.SIGTERM)


def test_a_helper_started_detached_runs_in_root_and_its_last_log_line_names_the_cause(
    tmp_path: Path,
) -> None:
    """The helper's working directory is `/`, and its stderr is the log file, not a pipe."""
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("sh is needed for the start test")
    script = tmp_path / "pwd.sh"
    script.write_text(f"#!{shell}\npwd >&2\nexit 3\n", encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "helper.log"

    def failure(detail: str) -> GinnError:
        return GinnError("GINN-E-AGENT-FILTER", detail, "retry", 1, {"detail": detail})

    with pytest.raises(GinnError) as caught:
        _proc.start_ready([str(script)], str(tmp_path / "never.sock"), str(log), failure)
    assert caught.value.message.endswith(": /")


def test_a_log_path_that_is_not_absolute_is_refused(tmp_path: Path) -> None:
    def failure(detail: str) -> GinnError:
        return GinnError("GINN-E-AGENT-FILTER", detail, "retry", 1, {"detail": detail})

    with pytest.raises(ValueError, match="absolute"):
        _proc.start_ready(["/bin/false"], str(tmp_path / "s.sock"), "relative.log", failure)


def test_stop_ends_the_clients_that_are_still_connected(tmp_path: Path) -> None:
    """A client that connected before the stop gets no forwarding after it: the group is gone.

    The helper is the compiled filter, and the client is connected and waiting when the stop comes.
    """
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    helper = build_filter(tmp_path, SOURCE, cc)
    upstream = tmp_path / "agent.sock"
    listen = tmp_path / "filter.sock"
    with FakeAgent(upstream) as agent:
        child = _proc.start_ready(
            [str(helper), str(upstream), str(listen)],
            str(listen),
            str(tmp_path / "filter.log"),
            lambda detail: GinnError("GINN-E-AGENT-FILTER", detail, "retry", 1, {"detail": detail}),
        )
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(WAIT_S)
        try:
            client.connect(str(listen))
            group = child.proc.pid
            # the leader, and the handler it forked for this client, share the group
            assert _wait_until(lambda: len(group_members(group)) >= 2)
            child.stop()
            assert _wait_until(lambda: not group_members(group))
            try:
                client.sendall(encode_frame(REQUEST_IDENTITIES))
            except OSError:
                pass
            try:
                data = client.recv(5)
            except OSError:
                data = b""
            assert data == b""
        finally:
            client.close()
            # A failed assertion above must not leave the filter and its handler running.
            child.stop()
        assert agent.received == []
    assert not child.running()


def test_stop_is_safe_on_a_helper_that_already_exited() -> None:
    _need_sleep()
    sleeper = spawn_sleeper()
    record = Proc.of(sleeper.pid)
    assert record is not None
    sleeper.kill()
    sleeper.wait(timeout=WAIT_S)
    stop_group(record)
    assert not record.alive()
