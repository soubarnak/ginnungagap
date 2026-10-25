# SPDX-License-Identifier: Apache-2.0
"""Session guards: FR-004 (no xbps-* command runs in a test) and the IDs of `fr` markers (NFR-061).

FR-004. The autouse session fixture puts tests/shims/xbps-fail first on PATH and points
GINN_XBPS_SHIM_LOG_DIR at a fresh, empty session directory. Each shim call writes one file into
that directory and exits 97. pytest_sessionfinish fails the run when the directory is missing or
holds any file.

Limits of the shim, which are kept on purpose:
- A call by absolute path (/usr/bin/xbps-install) skips PATH and is not logged here. The strace
  of M10 (T10.3) covers that case.
- The fixture also writes the directory into tests/shims/xbps-fail/.logdir, so a child process that
  clears its environment still logs. The file is removed when the session ends.
- pytest-xdist is not used. Each worker would set up its own session and write the same .logdir,
  so workers and concurrent sessions in one checkout would race for it. Give each worker its own
  file before enabling xdist.

NFR-061. pytest_collection_modifyitems checks every `fr` marker against the SRS, using the parser
in tools/traceability.py. That file is loaded by path because tools/ is not a package.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent
SHIM_DIR = REPO / "tests" / "shims" / "xbps-fail"
LOGDIR_FILE = SHIM_DIR / ".logdir"
LOG_DIR_ENV = "GINN_XBPS_SHIM_LOG_DIR"
TRACEABILITY = REPO / "tools" / "traceability.py"
SRS = REPO / "GINN-SRS-001-v1.0.md"
_LOG_DIR_KEY = pytest.StashKey[Path]()


def _load_traceability() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ginn_traceability", TRACEABILITY)
    if spec is None or spec.loader is None:
        raise pytest.UsageError(f"cannot load {TRACEABILITY}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session", autouse=True)
def xbps_shim(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """Install the shim for the whole session and yield the session log directory."""
    log_dir = tmp_path_factory.mktemp("xbps-shim")
    request.config.stash[_LOG_DIR_KEY] = log_dir
    LOGDIR_FILE.write_text(f"{log_dir}\n", encoding="utf-8")
    path = os.pathsep.join(part for part in (str(SHIM_DIR), os.environ.get("PATH", "")) if part)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(LOG_DIR_ENV, str(log_dir))
        patch.setenv("PATH", path)
        yield log_dir


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Fail the run when the session log directory is missing or holds a recorded call."""
    log_dir = session.config.stash.get(_LOG_DIR_KEY, None)
    LOGDIR_FILE.unlink(missing_ok=True)
    if log_dir is None:
        # The session fixture never set up, so no test ran and none could reach a shim.
        return
    if log_dir.is_dir():
        calls = sorted(log_dir.iterdir())
        if not calls:
            return
        lines = [f"FR-004: {len(calls)} xbps-* call(s) recorded in {log_dir}"]
        lines += [call.read_text(encoding="utf-8").strip() for call in calls]
    else:
        lines = [f"FR-004: xbps shim log directory {log_dir} is missing; calls cannot be audited"]
    if session.exitstatus == 0:
        session.exitstatus = 1
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter is None:
        sys.stderr.write("\n".join(lines) + "\n")
        return
    reporter.write_sep("=", "xbps shim recorded calls", red=True)
    for line in lines:
        reporter.write_line(line, red=True)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Fail collection when an `fr` marker is not a string ID that the SRS defines (NFR-061)."""
    tr = _load_traceability()
    try:
        reqs: dict[str, Any] = tr.parse_srs(SRS)
    except (OSError, ValueError) as exc:
        raise pytest.UsageError(f"cannot read the SRS for fr markers: {exc}") from exc
    problems: list[str] = []
    for item in items:
        for mark in item.iter_markers("fr"):
            if len(mark.args) != 1 or not isinstance(mark.args[0], str) or mark.kwargs:
                problems.append(f"{item.nodeid}: pytest.mark.fr takes one string ID")
                continue
            problem = tr.id_problem(mark.args[0], reqs)
            if problem is not None:
                problems.append(f"{item.nodeid}: {problem}")
    if problems:
        raise pytest.UsageError("invalid fr marker(s):\n  " + "\n  ".join(problems))
