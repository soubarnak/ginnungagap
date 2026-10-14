# SPDX-License-Identifier: Apache-2.0
"""The sv control of a services-on realm's runit service (FR-081, AS-028).

services/supervisor.py runs `sv` through its argv. Here `sv` is a fake script in tmp_path, written
with the interpreter's absolute path. It records every argv in a log, and it prints and exits with
the canned reply that the test wrote for the verb. No runsv, runsvdir or real sv runs, and no
podman. The clock is a FakeClock, and the wait for runsv to supervise a directory advances it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import RealmName
from ginn.errors import GinnError
from ginn.services import supervisor
from ginn.services.runit_user import service_dir
from ginn.services.supervisor import RunitStatus, SupervisorHooks
from ginn.store.paths import OwnedPaths
from tests.fakes.clock import FakeClock

NAME = RealmName("lab")

pytestmark = [pytest.mark.fr("FR-081")]

_FAKE_SV = """#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
with open(os.path.join(here, "replies.json"), encoding="utf-8") as fh:
    replies = json.load(fh)
verb = args[2] if args[:1] == ["-w"] else args[0]
reply = replies.get(verb, {{"code": 0, "out": "", "err": ""}})
if reply.get("sleep"):
    time.sleep(reply["sleep"])
sys.stdout.write(reply.get("out", ""))
sys.stderr.write(reply.get("err", ""))
sys.exit(reply.get("code", 0))
"""


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OwnedPaths:
    """Owned Paths under tmp_path, with the runtime directory that the supervisor leaves."""
    root = tmp_path
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    (root / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(root / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    return resolved


class FakeSv:
    """A fake `sv` in its own directory: `script` is the executable, `calls()` the argv log."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.script = directory / "sv"
        self.script.write_text(_FAKE_SV.format(python=sys.executable), encoding="utf-8")
        self.script.chmod(0o755)
        self.replies: dict[str, dict[str, Any]] = {}
        self._write_replies()

    def reply(self, verb: str, **reply: Any) -> None:
        self.replies[verb] = reply
        self._write_replies()

    def _write_replies(self) -> None:
        (self.directory / "replies.json").write_text(json.dumps(self.replies), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        log = self.directory / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def hooks(self, clock: FakeClock, sleep: Any = None) -> SupervisorHooks:
        return SupervisorHooks(
            sv=str(self.script),
            clock=clock,
            sleep=sleep if sleep is not None else clock.advance,
        )


@pytest.fixture
def fake_sv(tmp_path: Path) -> FakeSv:
    return FakeSv(tmp_path / "bin")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(1_800_000_000.0)


def _supervise(paths: OwnedPaths) -> Path:
    """Make runsv's supervise/ok FIFO for the realm's directory, as runsv does at its start."""
    directory = service_dir(paths, NAME)
    (directory / "supervise").mkdir(parents=True, exist_ok=True)
    fifo = directory / "supervise" / "ok"
    if not fifo.exists():
        os.mkfifo(fifo)
    return fifo


def _service(paths: OwnedPaths) -> Path:
    directory = service_dir(paths, NAME)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "run: /x/lab: (pid 4321) 12s\n",
            RunitStatus("run", pid=4321, seconds=12),
        ),
        (
            "run: /x/lab: (pid 4321) 3s; run: /x/lab/log: (pid 4320) 3s\n",
            RunitStatus("run", pid=4321, seconds=3),
        ),
        (
            "down: /x/lab: 0s, normally up\n",
            RunitStatus("down", seconds=0, normally_up=True),
        ),
        (
            "down: /x/lab: 7s\n",
            RunitStatus("down", seconds=7),
        ),
        (
            "down: /x/lab: 0s, normally up, want up\n",
            RunitStatus("down", seconds=0, normally_up=True, want_up=True),
        ),
        (
            "down: /x/lab: 3s, want down\n",
            RunitStatus("down", seconds=3),
        ),
        (
            "finish: /x/lab: (pid 99) 1s\n",
            RunitStatus("finish", pid=99, seconds=1),
        ),
        ("", RunitStatus("unknown")),
        ("warning: /x/lab: unable to open supervise/ok\n", RunitStatus("unknown")),
    ],
)
def test_the_status_line_of_sv_is_read_into_a_typed_status(
    text: str, expected: RunitStatus
) -> None:
    assert supervisor.parse_status(text) == expected


@pytest.mark.parametrize(
    ("status", "described"),
    [
        (RunitStatus("run", pid=4321, seconds=12), "run, pid 4321, 12 s"),
        (RunitStatus("down", seconds=0, normally_up=True), "down, 0 s"),
        (RunitStatus("unknown"), "unknown"),
    ],
)
def test_the_status_is_described_for_ginn_status(status: RunitStatus, described: str) -> None:
    assert status.describe() == described


@pytest.mark.fr("FR-081")
def test_status_runs_sv_status_on_the_absolute_directory_and_reads_the_line(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock
) -> None:
    _service(paths)
    fake_sv.reply("status", out="run: /x/lab: (pid 4321) 2s\n")

    status = supervisor.status(paths, NAME, hooks=fake_sv.hooks(clock))

    assert status == RunitStatus("run", pid=4321, seconds=2)
    assert fake_sv.calls() == [["status", str(service_dir(paths, NAME))]]


@pytest.mark.fr("FR-081")
def test_a_directory_that_runsv_does_not_supervise_is_supervisor_down(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock
) -> None:
    _service(paths)
    fake_sv.reply(
        "status",
        code=1,
        err="warning: /x/lab: unable to open supervise/ok: file does not exist\n",
    )
    with pytest.raises(GinnError) as caught:
        supervisor.status(paths, NAME, hooks=fake_sv.hooks(clock))
    assert caught.value.code == "GINN-E-SUPERVISOR-DOWN"
    assert "supervise/ok" in caught.value.message


@pytest.mark.fr("FR-081")
def test_up_waits_for_runsvdir_to_start_runsv_before_it_runs_sv(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock
) -> None:
    _service(paths)
    fifo_after = 3
    seen_before_ok: list[list[list[str]]] = []

    def runsvdir_scan(seconds: float) -> None:
        clock.advance(seconds)
        seen_before_ok.append(fake_sv.calls())
        if len(seen_before_ok) == fifo_after:
            _supervise(paths)

    fake_sv.reply("up", out="ok: run: /x/lab: (pid 4321) 1s\n")

    supervisor.up(paths, NAME, hooks=fake_sv.hooks(clock, sleep=runsvdir_scan))

    assert seen_before_ok == [[], [], []]
    assert fake_sv.calls() == [
        ["-w", str(supervisor.UP_WAIT_S), "up", str(service_dir(paths, NAME))]
    ]


@pytest.mark.fr("FR-081")
def test_up_gives_up_after_the_bounded_wait_when_runsvdir_never_starts_runsv(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock
) -> None:
    _service(paths)
    start = clock()

    with pytest.raises(GinnError) as caught:
        supervisor.up(paths, NAME, hooks=fake_sv.hooks(clock))

    assert caught.value.code == "GINN-E-SUPERVISOR-DOWN"
    assert "within 10 s" in caught.value.message
    assert clock() - start >= supervisor.SUPERVISE_WAIT_S
    assert fake_sv.calls() == []


@pytest.mark.fr("FR-081")
def test_up_without_the_runtime_directory_is_supervisor_down_at_once(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock
) -> None:
    _service(paths)
    shutil.rmtree(paths.runtime)

    def no_sleep(seconds: float) -> None:  # pragma: no cover - the wait must not start
        raise AssertionError("no wait without a runtime directory")

    with pytest.raises(GinnError) as caught:
        supervisor.up(paths, NAME, hooks=fake_sv.hooks(clock, sleep=no_sleep))
    assert caught.value.code == "GINN-E-SUPERVISOR-DOWN"
    assert "runtime directory" in caught.value.message
    assert fake_sv.calls() == []


@pytest.mark.fr("FR-081")
def test_up_starts_the_service_even_when_its_down_file_exists(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock
) -> None:
    """`sv up` is the `u` control, so the `down` file (DR-012) does not stop the start."""
    _service(paths)
    (service_dir(paths, NAME) / "down").write_bytes(b"")
    _supervise(paths)
    fake_sv.reply("up", out="ok: run: /x/lab: (pid 9) 0s\n")

    supervisor.up(paths, NAME, hooks=fake_sv.hooks(clock))

    assert (service_dir(paths, NAME) / "down").exists()
    assert fake_sv.calls()[0][2] == "up"


@pytest.mark.fr("FR-081")
def test_a_down_that_sv_reports_as_timeout_is_a_service_timeout(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock
) -> None:
    _service(paths)
    _supervise(paths)
    fake_sv.reply(
        "down",
        code=1,
        out="timeout: run: /x/lab: (pid 4) 5s, want down, got TERM\n",
    )

    with pytest.raises(GinnError) as caught:
        supervisor.down(paths, NAME, hooks=fake_sv.hooks(clock))

    assert caught.value.code == "GINN-E-SERVICE-TIMEOUT"
    assert caught.value.details["action"] == "stop"
    assert caught.value.details["seconds"] == supervisor.DOWN_WAIT_S
    assert "want down" in caught.value.details["detail"]


@pytest.mark.fr("FR-081")
def test_a_down_that_sv_cannot_finish_is_cut_off_and_reported_as_a_timeout(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service(paths)
    _supervise(paths)
    monkeypatch.setattr(supervisor, "COMMAND_MARGIN_S", 0)
    fake_sv.reply("down", sleep=30)
    hooks = SupervisorHooks(sv=str(fake_sv.script), clock=clock, down_wait_s=1)

    with pytest.raises(GinnError) as caught:
        supervisor.down(paths, NAME, hooks=hooks)

    assert caught.value.code == "GINN-E-SERVICE-TIMEOUT"


@pytest.mark.fr("FR-081")
def test_up_passes_the_wait_and_the_absolute_directory_to_sv(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock
) -> None:
    _service(paths)
    _supervise(paths)
    fake_sv.reply("up", out="ok: run: /x/lab: (pid 3) 0s\n")
    hooks = SupervisorHooks(sv=str(fake_sv.script), clock=clock, up_wait_s=4)

    supervisor.up(paths, NAME, hooks=hooks)

    assert fake_sv.calls() == [["-w", "4", "up", str(service_dir(paths, NAME))]]


@pytest.mark.fr("FR-081")
def test_sv_that_is_not_installed_is_a_control_error_not_a_proof_of_a_gone_runsv(
    paths: OwnedPaths, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runsv may still run the realm when sv is missing, so no caller may read this as gone."""
    _service(paths)
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: None)
    monkeypatch.setattr(supervisor, "SV_FALLBACKS", ())

    with pytest.raises(GinnError) as caught:
        supervisor.status(paths, NAME, hooks=SupervisorHooks(clock=clock))
    assert caught.value.code == "GINN-E-SERVICE-CONTROL"
    assert "not installed" in caught.value.message


@pytest.mark.fr("FR-081")
def test_an_sv_that_cannot_be_executed_is_a_control_error(
    paths: OwnedPaths, clock: FakeClock, tmp_path: Path
) -> None:
    _service(paths)
    missing = tmp_path / "no-such-dir" / "sv"

    with pytest.raises(GinnError) as caught:
        supervisor.status(paths, NAME, hooks=SupervisorHooks(sv=str(missing), clock=clock))
    assert caught.value.code == "GINN-E-SERVICE-CONTROL"
    assert "sv cannot run" in caught.value.message


@pytest.mark.fr("FR-081")
@pytest.mark.parametrize(
    "gone",
    [
        "fail: /x/lab: runsv not running\n",
        "warning: /x/lab: unable to open supervise/ok: file does not exist\n",
        "fail: /x/lab: unable to change to service directory: file does not exist\n",
    ],
)
@pytest.mark.parametrize("verb", ["up", "down"])
def test_output_that_proves_runsv_is_gone_is_supervisor_down(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock, verb: str, gone: str
) -> None:
    _service(paths)
    _supervise(paths)
    fake_sv.reply(verb, code=1, err=gone)

    action = supervisor.up if verb == "up" else supervisor.down
    with pytest.raises(GinnError) as caught:
        action(paths, NAME, hooks=fake_sv.hooks(clock))
    assert caught.value.code == "GINN-E-SUPERVISOR-DOWN"


@pytest.mark.fr("FR-081")
@pytest.mark.parametrize(
    "failure",
    [
        "warning: /x/lab: unable to open supervise/control: file does not exist\n",
        "fail: /x/lab: unable to read supervise/status\n",
    ],
)
@pytest.mark.parametrize("verb", ["up", "down"])
def test_a_failure_that_does_not_prove_runsv_is_gone_is_a_control_error(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock, verb: str, failure: str
) -> None:
    """runsv may still be alive (its control FIFO cannot be opened), so the caller must not fall
    back to podman. The error is GINN-E-SERVICE-CONTROL, never GINN-E-SUPERVISOR-DOWN."""
    _service(paths)
    _supervise(paths)
    fake_sv.reply(verb, code=1, err=failure)

    action = supervisor.up if verb == "up" else supervisor.down
    with pytest.raises(GinnError) as caught:
        action(paths, NAME, hooks=fake_sv.hooks(clock))
    assert caught.value.code == "GINN-E-SERVICE-CONTROL"
    assert caught.value.details["action"] == ("start" if verb == "up" else "stop")


@pytest.mark.fr("FR-081")
def test_a_status_failure_that_does_not_prove_runsv_is_gone_is_a_control_error(
    paths: OwnedPaths, fake_sv: FakeSv, clock: FakeClock
) -> None:
    _service(paths)
    fake_sv.reply("status", code=1, err="fail: /x/lab: unable to read supervise/status\n")

    with pytest.raises(GinnError) as caught:
        supervisor.status(paths, NAME, hooks=fake_sv.hooks(clock))
    assert caught.value.code == "GINN-E-SERVICE-CONTROL"


@pytest.mark.fr("FR-081")
def test_is_supervised_is_true_only_with_the_ok_fifo(paths: OwnedPaths) -> None:
    _service(paths)
    assert supervisor.is_supervised(paths, NAME) is False
    _supervise(paths)
    assert supervisor.is_supervised(paths, NAME) is True
