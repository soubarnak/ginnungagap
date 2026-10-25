# SPDX-License-Identifier: Apache-2.0
"""The idle watcher of a services-off realm (FR-018, NFR-002, runtime/idle.py).

`arm` spawns ginn-idle with the argv its usage line names, records the watcher, and starts nothing
twice for one init. Most tests replace the spawn with a recorder, so no process starts. The
detached-spawn test runs a fake helper (a Python script with the interpreter's absolute path) that
records its argv, its session and its stderr, and waits for a stop file. Every process a test starts
is killed and reaped at teardown, and the test checks that it is gone. No podman and no real
ginn-idle runs here.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import RealmName
from ginn.errors import Warning as GinnWarning
from ginn.runtime.idle import (
    IdleHooks,
    IdleRecord,
    arm,
    idle_stopped,
    log_path,
    read_record,
    record_path,
    remove,
)
from ginn.store.journal import process_start_ticks
from ginn.store.paths import OwnedPaths

NAME = RealmName("lab")
CONTAINER = "ginn-lab-g1"
PODMAN_ARGV = ["/usr/bin/podman", "--root", "/data/podman", "--runroot", "/run/ginn/podman"]
INIT_PID = 123

pytestmark = [pytest.mark.fr("FR-018")]  # the memory budget (NFR-002) is not proven here

_FAKE_HELPER = """#!{python}
import json, os, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "argv.json"), "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
with open(os.path.join(here, "session.txt"), "w", encoding="utf-8") as fh:
    fh.write(str(os.getsid(0)))
sys.stderr.write("boom from the fake helper\\n")
sys.stderr.flush()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
deadline = time.monotonic() + 30
while time.monotonic() < deadline and not os.path.exists(os.path.join(here, "stop")):
    time.sleep(0.05)
"""


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OwnedPaths:
    """Owned Paths under tmp_path, with the runtime directory pinned there."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    return resolved


@pytest.fixture
def helper(tmp_path: Path) -> Path:
    """An executable file where ginn-idle would be. The recorder spawn never runs it."""
    path = tmp_path / "ginn-idle"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class Recorder:
    """The spawn seam: records each argv and answers with the pid it was given."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.calls: list[tuple[list[str], Path]] = []
        self.error: OSError | None = None

    def __call__(self, argv: Any, log: Path) -> int:
        self.calls.append((list(argv), log))
        if self.error is not None:
            raise self.error
        return self.pid


def _hooks(
    helper: Path, spawn: Any, ticks: dict[int, int | None] | None = None, **kw: Any
) -> IdleHooks:
    table = {INIT_PID: 900, 4242: 555} if ticks is None else ticks
    return IdleHooks(
        helper=str(helper),
        grace_s=kw.get("grace_s", 60),
        spawn=spawn,
        start_ticks=lambda pid: table.get(pid),
    )


def _arm(paths: OwnedPaths, hooks: IdleHooks, *, init: int = INIT_PID) -> GinnWarning | None:
    return arm(
        paths,
        NAME,
        container_id=CONTAINER,
        init_pid=init,
        podman_argv=PODMAN_ARGV,
        hooks=hooks,
    )


def _dead_pid() -> int:
    """The pid of a process that has exited and been reaped, so /proc has no entry for it."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 - fixed argv, no shell
    proc.wait()
    return proc.pid


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def test_the_record_round_trips_and_a_bad_record_reads_as_none(paths: OwnedPaths) -> None:
    record = IdleRecord(
        watcher_pid=4242, watcher_start=555, container_id=CONTAINER, init_pid=123, init_start=900
    )
    path = record_path(paths, NAME)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps(record.__dict__), encoding="utf-8")
    assert read_record(paths, NAME) == record

    path.write_text("[1, 2]", encoding="utf-8")
    assert read_record(paths, NAME) is None
    path.write_text("{not json", encoding="utf-8")
    assert read_record(paths, NAME) is None
    path.unlink()
    assert read_record(paths, NAME) is None


@pytest.mark.fr("FR-018")
def test_arm_spawns_the_helper_with_the_init_grace_podman_and_container_in_its_usage_order(
    paths: OwnedPaths, helper: Path
) -> None:
    recorder = Recorder()

    assert _arm(paths, _hooks(helper, recorder)) is None

    ((argv, log),) = recorder.calls
    assert argv == [
        str(helper),
        str(INIT_PID),
        "60",
        *PODMAN_ARGV,
        "--",
        CONTAINER,
    ]
    assert log == paths.runtime / "idle" / "lab.log"
    assert read_record(paths, NAME) == IdleRecord(4242, 555, CONTAINER, INIT_PID, 900)


@pytest.mark.fr("FR-018")
def test_the_grace_in_the_argv_is_the_hook_value(paths: OwnedPaths, helper: Path) -> None:
    recorder = Recorder()

    _arm(paths, _hooks(helper, recorder, grace_s=5))

    assert recorder.calls[0][0][2] == "5"


@pytest.mark.fr("FR-018")
def test_a_second_arm_for_the_same_init_while_the_watcher_runs_spawns_nothing(
    paths: OwnedPaths, helper: Path
) -> None:
    """The watcher is this test process, which runs with its own start time, so it is alive."""
    me = os.getpid()
    my_start = process_start_ticks(me)
    assert my_start is not None
    recorder = Recorder(pid=me)
    hooks = _hooks(helper, recorder, ticks={INIT_PID: 900, me: my_start})

    assert _arm(paths, hooks) is None
    assert _arm(paths, hooks) is None

    assert len(recorder.calls) == 1, "a second start of the same init spawns no duplicate"


@pytest.mark.fr("FR-018")
def test_a_record_whose_watcher_is_gone_is_replaced(paths: OwnedPaths, helper: Path) -> None:
    dead = _dead_pid()
    path = record_path(paths, NAME)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "watcher_pid": dead,
                "watcher_start": 1,
                "container_id": CONTAINER,
                "init_pid": INIT_PID,
                "init_start": 900,
            }
        ),
        encoding="utf-8",
    )
    recorder = Recorder()

    assert _arm(paths, _hooks(helper, recorder)) is None

    assert len(recorder.calls) == 1, "a stale record is replaced by a new watcher"
    assert read_record(paths, NAME) == IdleRecord(4242, 555, CONTAINER, INIT_PID, 900)


@pytest.mark.fr("FR-018")
def test_a_reused_watcher_pid_is_treated_as_gone_by_its_start_time(
    paths: OwnedPaths, helper: Path
) -> None:
    """The pid is alive, but with another start time it is a different process (pid reuse)."""
    me = os.getpid()
    my_start = process_start_ticks(me)
    assert my_start is not None
    path = record_path(paths, NAME)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "watcher_pid": me,
                "watcher_start": my_start + 1,
                "container_id": CONTAINER,
                "init_pid": INIT_PID,
                "init_start": 900,
            }
        ),
        encoding="utf-8",
    )
    recorder = Recorder()

    _arm(paths, _hooks(helper, recorder, ticks={INIT_PID: 900, 4242: 555}))

    assert len(recorder.calls) == 1


@pytest.mark.fr("FR-018")
def test_a_new_container_init_gets_a_new_watcher_even_while_the_old_one_runs(
    paths: OwnedPaths, helper: Path
) -> None:
    """The old watcher exits by itself when its init is gone, so the new init is armed at once."""
    me = os.getpid()
    my_start = process_start_ticks(me)
    assert my_start is not None
    recorder = Recorder(pid=me)
    first = _hooks(helper, recorder, ticks={INIT_PID: 900, me: my_start})
    _arm(paths, first)
    second = _hooks(helper, recorder, ticks={777: 1000, me: my_start})

    _arm(paths, second, init=777)

    assert len(recorder.calls) == 2
    assert read_record(paths, NAME) == IdleRecord(me, my_start, CONTAINER, 777, 1000)


@pytest.mark.fr("FR-018")
def test_a_missing_helper_is_a_warning_and_nothing_is_spawned(
    paths: OwnedPaths, tmp_path: Path
) -> None:
    recorder = Recorder()
    hooks = _hooks(tmp_path / "absent" / "ginn-idle", recorder)

    notice = _arm(paths, hooks)

    assert notice is not None and notice.code == "GINN-W-IDLE-STOP"
    assert "is not installed" in notice.message
    assert recorder.calls == []
    assert read_record(paths, NAME) is None


@pytest.mark.fr("FR-018")
def test_a_helper_that_is_not_executable_is_a_warning(paths: OwnedPaths, helper: Path) -> None:
    helper.chmod(0o644)
    recorder = Recorder()

    notice = _arm(paths, _hooks(helper, recorder))

    assert notice is not None and notice.code == "GINN-W-IDLE-STOP"
    assert recorder.calls == []


@pytest.mark.fr("FR-018")
def test_a_relative_podman_path_is_a_warning_because_the_helper_refuses_it(
    paths: OwnedPaths, helper: Path
) -> None:
    recorder = Recorder()

    notice = arm(
        paths,
        NAME,
        container_id=CONTAINER,
        init_pid=INIT_PID,
        podman_argv=["podman", "--root", "/data"],
        hooks=_hooks(helper, recorder),
    )

    assert notice is not None and notice.code == "GINN-W-IDLE-STOP"
    assert "not an absolute path" in notice.message
    assert recorder.calls == []


@pytest.mark.fr("FR-018")
def test_an_init_that_is_not_running_is_a_warning(paths: OwnedPaths, helper: Path) -> None:
    recorder = Recorder()

    notice = _arm(paths, _hooks(helper, recorder, ticks={4242: 555}))

    assert notice is not None and notice.code == "GINN-W-IDLE-STOP"
    assert "is not running" in notice.message
    assert recorder.calls == []


@pytest.mark.fr("FR-018")
def test_a_spawn_that_fails_is_a_warning_not_an_error(paths: OwnedPaths, helper: Path) -> None:
    recorder = Recorder()
    recorder.error = OSError(errno.ENOEXEC, "Exec format error")

    notice = _arm(paths, _hooks(helper, recorder))

    assert notice is not None and notice.code == "GINN-W-IDLE-STOP"
    assert "did not start" in notice.message
    assert read_record(paths, NAME) is None


@pytest.mark.fr("FR-018")
def test_a_watcher_that_exits_at_once_is_a_warning_and_is_not_recorded(
    paths: OwnedPaths, helper: Path
) -> None:
    recorder = Recorder(pid=999999)  # no such process: its start time is None

    notice = _arm(paths, _hooks(helper, recorder))

    assert notice is not None and notice.code == "GINN-W-IDLE-STOP"
    assert "exited at once" in notice.message
    assert read_record(paths, NAME) is None


@pytest.mark.fr("FR-018")
def test_the_real_spawn_detaches_the_watcher_and_sends_its_stderr_to_the_log(
    paths: OwnedPaths, tmp_path: Path, fake_helper_pids: list[int]
) -> None:
    """A real process: a session of its own, stdin and stdout on /dev/null, stderr in the log."""
    script_dir = tmp_path / "fake-helper"
    script_dir.mkdir()
    fake = script_dir / "ginn-idle"
    fake.write_text(_FAKE_HELPER.format(python=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    me = os.getpid()
    hooks = IdleHooks(
        helper=str(fake),
        grace_s=60,
        start_ticks=lambda pid: process_start_ticks(pid) if pid == me else 321,
    )

    notice = arm(
        paths,
        NAME,
        container_id=CONTAINER,
        init_pid=me,
        podman_argv=PODMAN_ARGV,
        hooks=hooks,
    )
    record = read_record(paths, NAME)
    assert record is not None
    fake_helper_pids.append(record.watcher_pid)

    assert notice is None
    _wait_until(lambda: (script_dir / "argv.json").exists())
    assert json.loads((script_dir / "argv.json").read_text(encoding="utf-8")) == [
        str(me),
        "60",
        *PODMAN_ARGV,
        "--",
        CONTAINER,
    ]
    _wait_until(lambda: (script_dir / "session.txt").exists())
    assert int((script_dir / "session.txt").read_text(encoding="utf-8")) != os.getsid(0)
    log = paths.runtime / "idle" / "lab.log"
    _wait_until(lambda: log.exists() and "boom from the fake helper" in log.read_text("utf-8"))


def _wait_until(done: Any) -> None:
    deadline = time.monotonic() + 10
    while not done():
        assert time.monotonic() < deadline, "the fake helper did not reach its marker"
        time.sleep(0.02)


@pytest.fixture
def fake_helper_pids() -> Iterator[list[int]]:
    """The watchers the detached test started. Each is stopped, then reaped, at teardown."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    for pid in pids:
        assert _gone(pid), f"the fake helper {pid} is still running after teardown"


STOP = "ginn-idle: no user process for 60 s; stopping {cid}\n"
FAILED = "ginn-idle: podman stop failed; init 123 still runs\n"


def _evidence(
    paths: OwnedPaths,
    *,
    text: str | None,
    container: str = CONTAINER,
    watcher_pid: int = 999_999,
    watcher_start: int = 1,
) -> None:
    """A watcher record for the realm, and its log (None writes no log)."""
    record = IdleRecord(watcher_pid, watcher_start, container, INIT_PID, 900)
    directory = paths.runtime / "idle"
    directory.mkdir(mode=0o700, exist_ok=True)
    write = {
        "watcher_pid": record.watcher_pid,
        "watcher_start": record.watcher_start,
        "container_id": record.container_id,
        "init_pid": record.init_pid,
        "init_start": record.init_start,
    }
    record_path(paths, NAME).write_text(json.dumps(write), encoding="utf-8")
    if text is not None:
        log_path(paths, NAME).write_text(text, encoding="utf-8")


@pytest.mark.fr("FR-018")
@pytest.mark.parametrize(
    ("log", "container_running", "record_container", "expected"),
    [
        (STOP.format(cid=CONTAINER), False, CONTAINER, True),
        ("armed\ncancelled\n" + STOP.format(cid=CONTAINER) + "tail line\n", False, CONTAINER, True),
        (STOP.format(cid=CONTAINER) + FAILED, False, CONTAINER, False),
        (FAILED, False, CONTAINER, False),
        (STOP.format(cid=CONTAINER), True, CONTAINER, False),
        (STOP.format(cid="other-container"), False, CONTAINER, False),
        (STOP.format(cid=CONTAINER), False, "other-container", False),
        ("armed\n", False, CONTAINER, False),
        (None, False, CONTAINER, False),
    ],
)
def test_an_idle_stop_needs_the_stopping_line_no_failure_line_a_stopped_container_and_its_record(
    paths: OwnedPaths,
    log: str | None,
    container_running: bool,
    record_container: str,
    expected: bool,
) -> None:
    _evidence(paths, text=log, container=record_container)

    answer = idle_stopped(paths, NAME, container_id=CONTAINER, container_running=container_running)

    assert answer is expected


@pytest.mark.fr("FR-018")
def test_no_record_means_no_idle_stop_whatever_the_log_says(paths: OwnedPaths) -> None:
    directory = paths.runtime / "idle"
    directory.mkdir(mode=0o700, exist_ok=True)
    log_path(paths, NAME).write_text(STOP.format(cid=CONTAINER), encoding="utf-8")

    assert idle_stopped(paths, NAME, container_id=CONTAINER, container_running=False) is False


@pytest.mark.fr("FR-018")
def test_a_reused_watcher_pid_does_not_change_the_answer(paths: OwnedPaths) -> None:
    """The answer reads the record and the log, not the watcher's pid. A pid that another process
    now holds, alive with another start time, leaves the answer as it was."""
    me = os.getpid()
    start = process_start_ticks(me)
    assert start is not None
    _evidence(paths, text=STOP.format(cid=CONTAINER), watcher_pid=me, watcher_start=start + 1)

    assert idle_stopped(paths, NAME, container_id=CONTAINER, container_running=False) is True


@pytest.mark.fr("FR-018")
def test_each_arm_starts_a_new_log_so_it_describes_only_the_current_watcher(
    paths: OwnedPaths, tmp_path: Path, fake_helper_pids: list[int]
) -> None:
    """The first watcher keeps running with its own log, which is no longer the realm's log. The
    second arm names its log afresh, so the old line is not there."""
    script_dir = tmp_path / "tagger"
    script_dir.mkdir()
    script = script_dir / "ginn-idle"
    script.write_text(_TAG_HELPER.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    hooks = IdleHooks(
        helper=str(script),
        grace_s=60,
        start_ticks=lambda pid: 900 if pid in (111, 222) else process_start_ticks(pid),
    )
    log = log_path(paths, NAME)

    assert _arm_with(paths, hooks, 111) is None
    fake_helper_pids.append(read_record(paths, NAME).watcher_pid)  # type: ignore[union-attr]
    _wait_until(lambda: _text_of(log).startswith("tag 111"))

    assert _arm_with(paths, hooks, 222) is None
    fake_helper_pids.append(read_record(paths, NAME).watcher_pid)  # type: ignore[union-attr]
    _wait_until(lambda: _text_of(log).startswith("tag 222"))

    assert _text_of(log) == "tag 222\n"


_TAG_HELPER = """#!{python}
import sys, time
sys.stderr.write("tag " + sys.argv[1] + "\\n")
sys.stderr.flush()
time.sleep(30)
"""


def _arm_with(paths: OwnedPaths, hooks: IdleHooks, init: int) -> GinnWarning | None:
    return arm(
        paths,
        NAME,
        container_id=CONTAINER,
        init_pid=init,
        podman_argv=PODMAN_ARGV,
        hooks=hooks,
    )


def _text_of(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@pytest.mark.fr("FR-018")
def test_remove_deletes_the_record_and_the_log_and_is_a_no_op_when_they_are_absent(
    paths: OwnedPaths,
) -> None:
    _evidence(paths, text=STOP.format(cid=CONTAINER))

    remove(paths, NAME)

    assert not record_path(paths, NAME).exists()
    assert not log_path(paths, NAME).exists()
    remove(paths, NAME)
