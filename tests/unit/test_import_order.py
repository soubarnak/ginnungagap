# SPDX-License-Identifier: Apache-2.0
"""Each module imports first, in a fresh interpreter, so no import cycle hides behind test order.

Most tests import ginn.ops before ginn.services, and a cycle between the two can hide in that order.
The CLI does not: `ginn` loads the command table first, which loads hostsetup, which loads
services.exec_relay. Each module here is imported alone, in a new interpreter, with `-I` (isolated:
no PYTHONPATH, no user site). The command puts the source tree on sys.path itself, because -I
ignores PYTHONPATH. A cycle that breaks the import fails the test with the interpreter's traceback.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
MODULES = (
    "ginn.ops._mutating",
    "ginn.services.exec_relay",
    "ginn.services._exec_lock",
    "ginn.podman.cli",
    "ginn.ops.recovery",
    "ginn.cli.commands.status",
    "ginn.__main__",
)


@pytest.mark.fr("FR-123")
@pytest.mark.parametrize("module", MODULES)
def test_a_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    code = f"import sys; sys.path.insert(0, {str(SRC)!r}); import {module}"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
