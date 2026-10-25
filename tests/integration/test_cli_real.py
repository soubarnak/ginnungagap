# SPDX-License-Identifier: Apache-2.0
"""Host smoke of the realm commands through the CLI entry point (T4.11, IR-010, IR-011).

Run with `GINN_INTEGRATION=1 pytest tests/integration/test_cli_real.py -q` on a Void host with
rootless Podman. The commands are `ginn.__main__.main`, which builds the production objects through
`ginn.cli._context`. Only the seams of the host gate are replaced, as the gate does (T4.9):
`integrate` is False, so the create runs no package step, the helper is the built one, and the
mount roots of the user are hidden. The owned paths, the adapter and the session are the gate's
own storage, under the temp base of conftest.py. Nothing touches ~/.local/share/containers.

The smoke runs one realm through create, start, status, exec, enter, stop, list and delete. Each
step's duration is written to GINN_T49_RESULTS when that variable is set (conftest.py).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator

import pytest

from ginn.__main__ import main
from ginn.cli import _context
from tests.integration.conftest import DISTRO, Host, p95
from tests.integration.test_lifecycle_real import (  # noqa: F401 - the mount-root hide is autouse
    _gate_exists,
    _hide_real_mount_roots,
)

REALM = "clismoke"


@contextlib.contextmanager
def _stdin_is_devnull() -> Iterator[None]:
    """Give the shell EOF on fd 0, so a login shell without a terminal exits at once."""
    saved = os.dup(0)
    try:
        with open(os.devnull, "rb") as devnull:
            os.dup2(devnull.fileno(), 0)
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)


@pytest.fixture
def cli_host(host: Host, monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    """The environment and the seams of the CLI, pointed at the gate's storage."""
    for name, value in {
        "HOME": str(host.host_home),
        "XDG_DATA_HOME": str(host.base / "data"),
        "XDG_CONFIG_HOME": str(host.base / "config"),
        "XDG_STATE_HOME": str(host.base / "state"),
        "GINN_RUNTIME_DIR": str(host.xdg),
        "XDG_RUNTIME_DIR": str(host.session),
    }.items():
        monkeypatch.setenv(name, value)
    seams = _context.Seams(
        root_dir=host.root_dir,
        helper=host.helper,
        integrate=False,  # Gate 3: no refresh and no package step (CreateHooks.integrate)
        exists=_gate_exists,
    )
    monkeypatch.setattr(_context, "seams", lambda: seams)
    yield host


def _timed(host: Host, step: str, call: object) -> object:
    """Run one step and write its duration (MEASURED) to the gate's results file."""
    started = time.perf_counter()
    result = call()  # type: ignore[operator]
    host.results.record(f"cli-{step}", "MEASURED", seconds=round(time.perf_counter() - started, 3))
    return result


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-016")
@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-020")
@pytest.mark.fr("FR-042")
@pytest.mark.fr("FR-048")
@pytest.mark.fr("FR-049")
@pytest.mark.fr("IR-010")
@pytest.mark.fr("IR-011")
def test_cli_smoke_one_realm_end_to_end(cli_host: Host, capfd: pytest.CaptureFixture[str]) -> None:
    """create, start, status --json, exec --root, exec status 7, enter, stop, list, delete."""
    host = cli_host
    try:
        assert _timed(host, "create", lambda: main(["create", DISTRO, "--name", REALM])) == 0
        capfd.readouterr()

        assert _timed(host, "start", lambda: main(["start", REALM])) == 0
        capfd.readouterr()

        assert _timed(host, "status", lambda: main(["--json", "status", REALM])) == 0
        status = json.loads(capfd.readouterr().out)
        assert status["name"] == REALM
        assert status["state"] == "running"
        assert status["container_state"] == "running"
        assert status["state_mismatch"] is False

        assert (
            _timed(host, "exec-root", lambda: main(["exec", "--root", REALM, "--", "echo", "ok"]))
            == 0
        )
        assert capfd.readouterr().out == "ok\n"

        # FR-049 AC-1: the inner command's status is the command's exit status (IR-011).
        exit_seven = ["sh", "-c", "exit 7"]
        assert _timed(host, "exec-status", lambda: main(["exec", REALM, "--", *exit_seven])) == 7
        capfd.readouterr()

        # The CLI path of one exec, in process: argv, recovery scan, realm look and podman exec.
        # Interpreter start-up is not in it. NFR-004 is measured at the enter path (T4.9).
        timings: list[float] = []
        for _ in range(100):
            started = time.perf_counter()
            assert main(["exec", REALM, "--", "true"]) == 0
            timings.append(time.perf_counter() - started)
        capfd.readouterr()
        host.results.record(
            "cli-exec-100",
            "MEASURED",
            runs=len(timings),
            p95=round(p95(timings), 4),
            mean=round(sum(timings) / len(timings), 4),
            max=round(max(timings), 4),
        )

        with _stdin_is_devnull():
            assert _timed(host, "enter", lambda: main(["enter", REALM])) == 0
        capfd.readouterr()

        assert _timed(host, "stop", lambda: main(["stop", REALM])) == 0
        capfd.readouterr()

        assert _timed(host, "list", lambda: main(["--json", "list"])) == 0
        listed = json.loads(capfd.readouterr().out)["realms"]
        assert [(row["name"], row["state"]) for row in listed] == [(REALM, "stopped")]

        assert _timed(host, "delete", lambda: main(["delete", REALM])) == 0
        assert "Realm Home kept" in capfd.readouterr().out
        assert main(["status", REALM]) == 3
        capfd.readouterr()
    finally:
        main(["delete", REALM, "--yes"])
        capfd.readouterr()
