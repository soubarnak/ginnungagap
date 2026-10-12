# SPDX-License-Identifier: Apache-2.0
"""Session guard for FR-004: no test may run an xbps-* command.

The autouse session fixture puts tests/shims/xbps-fail first on PATH and points
GINN_XBPS_SHIM_LOG at an empty file in the session temporary directory. Each shim call appends a
line to that file and exits 97. pytest_sessionfinish fails the run if the file is not empty.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SHIM_DIR = Path(__file__).resolve().parent / "tests" / "shims" / "xbps-fail"
LOG_ENV = "GINN_XBPS_SHIM_LOG"
_LOG_KEY = pytest.StashKey[Path]()


@pytest.fixture(scope="session", autouse=True)
def xbps_shim(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """Install the shim for the whole session and yield the path of its call log."""
    log = tmp_path_factory.mktemp("xbps-shim") / "xbps-calls.log"
    log.touch()
    request.config.stash[_LOG_KEY] = log
    path = os.pathsep.join(part for part in (str(SHIM_DIR), os.environ.get("PATH", "")) if part)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(LOG_ENV, str(log))
        patch.setenv("PATH", path)
        yield log


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Fail the session when any xbps-* command was invoked, and print the recorded calls."""
    log = session.config.stash.get(_LOG_KEY, None)
    if log is None or not log.exists():
        return
    calls = [line for line in log.read_text(encoding="utf-8").splitlines() if line]
    if not calls:
        return
    session.exitstatus = 1
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    lines = [f"FR-004: {len(calls)} xbps-* call(s) recorded in {log}", *calls]
    if reporter is None:
        sys.stderr.write("\n".join(lines) + "\n")
        return
    reporter.write_sep("=", "xbps shim recorded calls", red=True)
    for line in lines:
        reporter.write_line(line, red=True)
