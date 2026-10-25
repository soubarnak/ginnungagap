# SPDX-License-Identifier: Apache-2.0
"""`ginn _service-run` and `ginn _service-finish`, the hidden commands of a services-on realm.

The runit `run` script runs the first, and the `finish` script the second (FR-081). The pinned host
of realm_cli_support runs the ops against FakePodman. os.execv is replaced by a recorder that
raises, so no process is replaced, and the back-off sleep is a list. No podman, runsv or ginn-wait
runs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.domain import RealmName, RealmState
from ginn.ops.service import ServiceHooks
from ginn.podman.adapter import CreateSpec
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.services.runit_user import conmon_pidfile, crash_log, service_dir, write_service_dir
from ginn.services.supervisor import Result, SupervisorHooks
from ginn.store.records import RealmStore
from tests.unit.realm_cli_support import DIGEST, World, record_of

GLOBAL_ARGV = [
    "/usr/bin/podman",
    "--remote=false",
    "--root",
    "/data/podman",
    "--runroot",
    "/run/ginn/1000/podman",
    "--tmpdir",
    "/run/ginn/1000/podman-tmp",
    "--runtime",
    "crun",
]
REALM = RealmName("lab")
UP_STATUS = "run: /x/lab: (pid 4321) 2s\n"
"""What `sv status` says of a realm that runs or that a crash restarted: no `want down` (CL-24)."""

pytestmark = [pytest.mark.fr("FR-081"), pytest.mark.fr("AS-028")]


def _sv_says_up(argv: Sequence[str], timeout: float) -> Result:
    """The sv control of the finish: `sv status` answers UP_STATUS, and no sv process runs."""
    return Result(0, UP_STATUS, "")


@pytest.fixture
def helper(tmp_path: Path) -> Path:
    path = tmp_path / "ginn-wait"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def execs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The argv of each os.execv, recorded. The stand-in ends the command with SystemExit, which
    main() does not turn into an internal error, as a real exec would never return."""
    argvs: list[list[str]] = []

    def recorder(_path: str, argv: list[str]) -> None:
        argvs.append(list(argv))
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", recorder)
    return argvs


@pytest.fixture
def sleeps(world: World, helper: Path, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Wire the helper path, the global podman flags and the back-off recorder."""
    recorded: list[float] = []
    base = _context.seams()
    monkeypatch.setattr(_context, "seams", lambda: replace(base, wait_helper=str(helper)))
    monkeypatch.setattr(_context, "podman_global", lambda _podman: list(GLOBAL_ARGV))
    monkeypatch.setattr(
        _context,
        "service_hooks",
        lambda: ServiceHooks(
            lifecycle=_context.lifecycle_hooks(),
            wait_helper=str(helper),
            sleep=recorded.append,
            supervisor=SupervisorHooks(sv="/usr/bin/sv", run=_sv_says_up),
        ),
    )

    return recorded


def _services_realm(world: World, state: RealmState, *, running: bool) -> str:
    if not service_dir(world.paths, REALM).exists():
        write_service_dir(world.paths, REALM, "/usr/bin/ginn")
    labels = {LABEL_REALM: "lab", LABEL_DISTRO: "debian", LABEL_GENERATION: "1"}
    spec = CreateSpec(name="ginn-lab-g1", image_digest_ref=DIGEST, network="host", labels=labels)
    cid = world.fake.create(spec)
    if running:
        world.fake.start(cid)
    (world.paths.homes / "lab").mkdir(parents=True, exist_ok=True)
    RealmStore(world.paths).save(record_of("lab", state, cid, services=True))
    world.fake.calls.clear()
    return cid


@pytest.mark.fr("NFR-033")
def test_service_run_starts_the_realm_and_execs_ginn_wait_on_its_conmon(
    world: World, helper: Path, sleeps: list[float], execs: list[list[str]]
) -> None:
    cid = _services_realm(world, RealmState.STOPPED, running=False)

    with pytest.raises(SystemExit):
        main(["_service-run", "lab"])

    pidfile = conmon_pidfile(world.paths, REALM)
    assert execs == [[str(helper), str(pidfile), *GLOBAL_ARGV, "--", cid]]
    assert RealmStore(world.paths).load(REALM).state is RealmState.RUNNING
    assert "start" in world.fake.ops()


@pytest.mark.fr("AS-028")
def test_service_finish_takes_the_runsv_arguments_with_a_negative_exit_code(
    world: World, sleeps: list[float]
) -> None:
    _services_realm(world, RealmState.RUNNING, running=True)

    assert main(["_service-finish", "lab", "-1", "9"]) == 0

    assert sleeps == [5.0], "a crash waits the restart delay before runsv restarts run"
    assert crash_log(world.paths, REALM).exists()
    assert RealmStore(world.paths).load(REALM).state is RealmState.RUNNING


@pytest.mark.fr("NFR-033")
def test_service_finish_with_exit_zero_records_the_realm_stopped(
    world: World, sleeps: list[float]
) -> None:
    _services_realm(world, RealmState.RUNNING, running=True)

    assert main(["_service-finish", "lab", "0", "0"]) == 0

    assert sleeps == []
    assert RealmStore(world.paths).load(REALM).state is RealmState.STOPPED


@pytest.mark.fr("FR-081")
def test_a_failed_start_makes_service_run_exit_non_zero_and_execs_nothing(
    world: World, sleeps: list[float], execs: list[list[str]]
) -> None:
    _services_realm(world, RealmState.STOPPED, running=False)
    world.fake.fail_ops = {"start": "GINN-E-INTERNAL"}

    assert main(["_service-run", "lab"]) != 0

    assert execs == [], "ginn-wait is not run for a start that failed"
    assert RealmStore(world.paths).load(REALM).state is RealmState.STOPPED


@pytest.mark.fr("FR-081")
@pytest.mark.fr("NFR-033")
def test_five_failed_starts_within_sixty_seconds_give_the_realm_up_through_the_cli(
    world: World, sleeps: list[float], execs: list[list[str]]
) -> None:
    _services_realm(world, RealmState.STOPPED, running=False)

    codes = []
    for _ in range(5):
        world.fake.fail_ops = {"start": "GINN-E-INTERNAL"}  # a fault is taken once
        assert main(["_service-run", "lab"]) != 0
        codes.append(main(["_service-finish", "lab", "1", "0"]))

    assert codes == [0] * 5
    assert sleeps == [5.0] * 4, "the fifth crash does not wait: runsv is told to stop"
    assert RealmStore(world.paths).load(REALM).state is RealmState.FAILED
    assert not crash_log(world.paths, REALM).exists()
    assert (service_dir(world.paths, REALM) / "down").is_file()


def test_the_service_commands_are_hidden_from_the_top_level_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--help"]) == 0
    assert "_service" not in capsys.readouterr().out


@pytest.mark.fr("FR-081")
@pytest.mark.parametrize("stopped_exit", [255, 125])
def test_a_stop_in_the_owner_wait_makes_service_run_exit_70_and_counts_as_a_crash(
    world: World,
    sleeps: list[float],
    execs: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stopped_exit: int,
) -> None:
    """The init is killed while `_service-run` waits for the runtime dir's owner. The start fails
    with GINN-E-INTERNAL, and the CLI exits 70. runsv then reports that end to `_service-finish`,
    which counts it as a crash, since no `down` mark is set (FR-081, AS-028)."""
    world.fake.stopped_exec_exit = stopped_exit
    _services_realm(world, RealmState.STOPPED, running=False)
    real_exec = world.fake.exec

    def exec_after_the_init_is_killed(container_id: str, argv: list[str], **kwargs: Any) -> Any:
        if tuple(argv[:4]) == ("stat", "-c", "%u", "--"):
            world.fake.stop(container_id, 0)
        return real_exec(container_id, argv, **kwargs)

    monkeypatch.setattr(world.fake, "exec", exec_after_the_init_is_killed)

    assert main(["--json", "_service-run", "lab"]) == 70
    # The details tell the GONE error from the last-resort boundary, which carries none.
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "GINN-E-INTERNAL"
    assert error["exit_code"] == 70
    assert error["details"]["detail"] == "the realm is not running after start"
    assert error["details"]["state"] == "exited"
    assert execs == [], "ginn-wait is not run for a start that failed"
    assert RealmStore(world.paths).load(REALM).state is RealmState.STOPPED

    assert main(["_service-finish", "lab", "70", "0"]) == 0
    assert crash_log(world.paths, REALM).exists()
    assert sleeps == [5.0], "a crash waits the restart delay before runsv restarts run"
