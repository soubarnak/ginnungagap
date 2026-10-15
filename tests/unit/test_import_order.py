# SPDX-License-Identifier: Apache-2.0
"""Circular imports, caught by importing each module alone in its own clean interpreter.

Tests in one session share an interpreter, so the first test that touches a package fixes the
import order for every later test, and a cycle between two packages can stay hidden that way.
Here each listed module gets a child process of its own in which it is the only ginn import. The
child runs in isolated mode (-I), which drops PYTHONPATH and the user site, so the source root
travels as an argument and the child inserts it into sys.path before importing anything. A cycle
that breaks the import makes the child exit non-zero, and its stderr becomes the failure text.

The command-line entry point reaches the same code in another order. Its command table loads
hostsetup, and hostsetup loads services.exec_relay, which is why the entry point and the status
command are probed alongside the operations and services modules.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BUDGET_S = 120

TARGETS = (
    "ginn.ops._mutating",
    "ginn.services.exec_relay",
    "ginn.services._exec_lock",
    "ginn.podman.cli",
    "ginn.ops.recovery",
    "ginn.cli.commands.status",
    "ginn.__main__",
)

# Run by the child interpreter. Its argv after -c is [source root, dotted module name].
PROBE = "\n".join(
    (
        "import importlib, sys",
        "sys.path.insert(0, sys.argv[1])",
        "importlib.import_module(sys.argv[2])",
    )
)


@pytest.fixture
def source_root() -> Path:
    return Path(__file__).resolve().parents[2] / "src"


def _import_alone(module: str, root: Path) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-I", "-c", PROBE, str(root), module]
    return subprocess.run(  # noqa: S603 -- argv list, no shell
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        timeout=BUDGET_S,
    )


@pytest.fixture(params=TARGETS)
def clean_import(
    request: pytest.FixtureRequest, source_root: Path
) -> subprocess.CompletedProcess[str]:
    return _import_alone(request.param, source_root)


@pytest.mark.fr("FR-123")
def test_target_loads_when_it_is_the_first_import_of_its_process(
    clean_import: subprocess.CompletedProcess[str],
) -> None:
    if clean_import.returncode:
        pytest.fail(f"child exited {clean_import.returncode}:\n{clean_import.stderr}")
