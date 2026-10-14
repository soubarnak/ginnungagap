# SPDX-License-Identifier: Apache-2.0
"""`ginn _service-run` and `ginn _service-finish`, the hidden commands of a services-on realm.

The runit `run` script runs the first, and the `finish` script the second (FR-081). The pinned host
of realm_cli_support runs the ops against FakePodman. os.execv is replaced by a recorder that
raises, so no process is replaced, and the back-off sleep is a list. No podman, runsv or ginn-wait
runs.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from ginn.domain import RealmName, RealmState
from ginn.ops.service import ServiceHooks
from ginn.podman.adapter import CreateSpec
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.services.runit_user import conmon_pidfile, crash_log
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

pytestmark = [pytest.mark.fr("FR-081"), pytest.mark.fr("AS-028")]


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
            lifecycle=_context.lifecycle_hooks(), wait_helper=str(helper), sleep=recorded.append
        ),
    )

    return recorded


def _services_realm(world: World, state: RealmState, *, running: bool) -> str:
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


def test_the_service_commands_are_hidden_from_the_top_level_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--help"]) == 0
    assert "_service" not in capsys.readouterr().out
