# SPDX-License-Identifier: Apache-2.0
"""The relay's socket and lock directory are refused as bind sources (DR-010, ADR-014).

runtime/mounts.py admits any socket under the runtime root as an internal bind (ADR-008). The relay
socket, `exec.sock`, and the lock directory `exec/` are ginn's own too, but a bind of either would
give a container the relay's control, so both are refused by name. `recheck` is the check that an
internal grant passes again before it is attached, and the plan uses the same refusal.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ginn.runtime.mounts import INTERNAL_KIND, recheck

RELAY_REFUSAL = "ADR-014"
USER_ROOT = "/run/user/1000/agent"


def _socket(path: Path) -> str:
    """A socket node, made without binding it, so the path stays short (AF_UNIX limit)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mknod(path, 0o600 | stat.S_IFSOCK)
    return str(path)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home" / "alice"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    path = tmp_path / "run" / "ginn" / str(os.getuid())
    path.mkdir(parents=True, mode=0o700)
    return path


def _refusal(home: Path, runtime: Path, host: str) -> str | None:
    return recheck(
        INTERNAL_KIND,
        host,
        USER_ROOT,
        "rw",
        home=home,
        owned_paths=(),
        runtime_root=runtime,
        env={},
    )


@pytest.mark.fr("DR-010")
def test_the_relay_socket_is_refused_as_a_bind_source(home: Path, runtime: Path) -> None:
    sock = _socket(runtime / "exec.sock")
    reason = _refusal(home, runtime, sock)
    assert reason is not None
    assert RELAY_REFUSAL in reason


@pytest.mark.fr("DR-010")
def test_a_socket_in_the_relay_lock_directory_is_refused(home: Path, runtime: Path) -> None:
    sock = _socket(runtime / "exec" / "lab.sock")
    reason = _refusal(home, runtime, sock)
    assert reason is not None
    assert RELAY_REFUSAL in reason


@pytest.mark.fr("DR-010")
def test_the_lock_directory_itself_is_refused_by_name(home: Path, runtime: Path) -> None:
    lock_dir = runtime / "exec"
    lock_dir.mkdir()
    reason = _refusal(home, runtime, str(lock_dir))
    assert reason is not None
    assert RELAY_REFUSAL in reason


@pytest.mark.fr("DR-010")
def test_an_ordinary_socket_under_the_runtime_root_is_still_admitted(
    home: Path, runtime: Path
) -> None:
    sock = _socket(runtime / "agent" / "ssh.sock")
    assert _refusal(home, runtime, sock) is None


@pytest.mark.fr("DR-010")
def test_a_relay_named_socket_outside_the_runtime_root_is_refused_by_the_root_rule(
    home: Path, runtime: Path, tmp_path: Path
) -> None:
    sock = _socket(tmp_path / "elsewhere" / "exec.sock")
    reason = _refusal(home, runtime, sock)
    assert reason is not None
    assert "runtime root" in reason
