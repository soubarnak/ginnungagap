# SPDX-License-Identifier: Apache-2.0
"""Per-realm and host-wide operation locks: refused at once, never queued (FR-123, DR-033)."""

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import ginn
from ginn.domain import RealmName
from ginn.errors import GinnError
from ginn.store.locks import global_lock, realm_lock
from ginn.store.paths import OwnedPaths

SRC_DIR = Path(ginn.__file__).resolve().parents[1]

# The holder takes the lock, reports "ready", and keeps it until its stdin closes.
HOLDER_SCRIPT = """
import sys
from ginn.domain import RealmName
from ginn.store.locks import realm_lock
from ginn.store.paths import OwnedPaths

paths = OwnedPaths.from_env()
with realm_lock(paths, RealmName(sys.argv[1]), sys.argv[2]):
    print("ready", flush=True)
    sys.stdin.read()
"""

BUSY_MAX_SECONDS = 0.1


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    """Owned Paths rooted in tmp_path. Every XDG variable is pinned so no real host path is used."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Host Setup leaves the runtime directory 0700 and owned by the user; from_env checks that.
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    yield resolved


def _start_holder(name: str, op: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC_DIR), env.get("PYTHONPATH")]))
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", HOLDER_SCRIPT, name, op],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _stop_holder(holder: subprocess.Popen[str]) -> None:
    if holder.stdin is not None and not holder.stdin.closed:
        holder.stdin.close()
    holder.wait(timeout=30)


@pytest.mark.fr("FR-123")
@pytest.mark.fr("DR-033")
def test_second_process_is_refused_at_once_naming_the_running_operation(paths: OwnedPaths) -> None:
    holder = _start_holder("arch", "rebuild")
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready", holder.stderr and holder.stderr.read()

        started = time.perf_counter()
        with pytest.raises(GinnError) as excinfo:
            with realm_lock(paths, RealmName("arch"), "install"):
                pass
        elapsed = time.perf_counter() - started
    finally:
        _stop_holder(holder)

    assert excinfo.value.code == "GINN-E-REALM-BUSY"
    assert str(excinfo.value) == "GINN-E-REALM-BUSY: rebuild in progress"
    assert excinfo.value.exit_code == 75
    assert elapsed < BUSY_MAX_SECONDS, f"refusal took {elapsed:.3f}s; it must not wait"


@pytest.mark.fr("FR-123")
def test_lock_is_free_again_once_the_holding_process_exits(paths: OwnedPaths) -> None:
    holder = _start_holder("arch", "rebuild")
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "ready"
    _stop_holder(holder)

    with realm_lock(paths, RealmName("arch"), "install"):
        pass


@pytest.mark.fr("FR-123")
@pytest.mark.fr("DR-033")
def test_nested_attempt_in_one_process_is_refused_too(paths: OwnedPaths) -> None:
    arch = RealmName("arch")
    with realm_lock(paths, arch, "rebuild"):
        started = time.perf_counter()
        with pytest.raises(GinnError) as excinfo:
            with realm_lock(paths, arch, "update"):
                pass
        elapsed = time.perf_counter() - started
    assert str(excinfo.value) == "GINN-E-REALM-BUSY: rebuild in progress"
    assert elapsed < BUSY_MAX_SECONDS


@pytest.mark.fr("DR-033")
def test_different_realms_do_not_block_each_other(paths: OwnedPaths) -> None:
    with realm_lock(paths, RealmName("arch"), "rebuild"):
        with realm_lock(paths, RealmName("debian"), "rebuild"):
            pass


@pytest.mark.fr("DR-033")
def test_holder_record_names_the_operation_and_pid_while_held(paths: OwnedPaths) -> None:
    with realm_lock(paths, RealmName("arch"), "rebuild"):
        record = json.loads((paths.locks / "arch.lock").read_text(encoding="utf-8"))
    assert record["op"] == "rebuild"
    assert record["pid"] == os.getpid()
    assert record["started"].endswith("Z")


@pytest.mark.fr("DR-033")
def test_release_truncates_the_record_so_no_stale_op_is_reported(paths: OwnedPaths) -> None:
    with realm_lock(paths, RealmName("arch"), "rebuild"):
        pass
    assert (paths.locks / "arch.lock").read_text(encoding="utf-8") == ""
    with realm_lock(paths, RealmName("arch"), "install"):
        pass


@pytest.mark.fr("DR-033")
def test_realm_named_global_does_not_collide_with_the_host_lock(paths: OwnedPaths) -> None:
    with global_lock(paths, "integrity ack"):
        with realm_lock(paths, RealmName("global"), "rebuild"):
            pass


@pytest.mark.fr("FR-123")
def test_host_wide_lock_refuses_a_second_host_operation(paths: OwnedPaths) -> None:
    with global_lock(paths, "host setup"):
        with pytest.raises(GinnError) as excinfo:
            with global_lock(paths, "integrity ack"):
                pass
    assert str(excinfo.value) == "GINN-E-REALM-BUSY: host setup in progress"
