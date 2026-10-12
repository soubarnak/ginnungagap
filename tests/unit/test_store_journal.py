# SPDX-License-Identifier: Apache-2.0
"""Operation journal: crash evidence, orphan detection and pid reuse (FR-122, NFR-030)."""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from ginn.domain import RealmName
from ginn.errors import GinnError
from ginn.store.journal import Journal, JournalEntry, is_pid_alive, process_start_ticks
from ginn.store.paths import OwnedPaths
from ginn.store.records import write_json_atomic


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    resolved = OwnedPaths.from_env(uid=1000)
    resolved.ensure()
    yield resolved


def _dead_pid() -> int:
    """Return the pid of a process that has exited and been reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def _write_entry(paths: OwnedPaths, realm: str, entry: JournalEntry) -> None:
    write_json_atomic(paths.journal / f"{realm}.json", entry.to_json())


@pytest.mark.fr("FR-122")
def test_begin_records_op_realm_pid_start_time_and_params(paths: OwnedPaths) -> None:
    journal = Journal(paths)
    entry = journal.begin("create", RealmName("debian"), {"distro": "debian", "tier": "basic"})

    stored = json.loads((paths.journal / "debian.json").read_text(encoding="utf-8"))
    assert stored["op"] == "create"
    assert stored["realm"] == "debian"
    assert stored["pid"] == os.getpid()
    assert stored["step"] == "begin"
    assert stored["params"] == {"distro": "debian", "tier": "basic"}
    assert stored["started"].endswith("Z")
    assert stored["pid_start"] == process_start_ticks(os.getpid())
    assert (paths.journal / "debian.json").stat().st_mode & 0o777 == 0o600
    assert journal.pending() == [entry]


@pytest.mark.fr("NFR-030")
def test_step_updates_the_journal_entry_in_place(paths: OwnedPaths) -> None:
    journal = Journal(paths)
    entry = journal.begin("rebuild", RealmName("arch"))
    advanced = journal.step(entry, "image-pulled")

    assert advanced.step == "image-pulled"
    assert journal.pending() == [advanced]
    assert not (paths.journal / "arch.tmp").exists()


@pytest.mark.fr("NFR-030")
def test_finish_removes_the_entry_and_a_second_finish_is_harmless(paths: OwnedPaths) -> None:
    journal = Journal(paths)
    entry = journal.begin("delete", RealmName("arch"))
    journal.finish(entry)
    assert journal.pending() == []
    assert not (paths.journal / "arch.json").exists()
    journal.finish(entry)


@pytest.mark.fr("FR-122")
def test_begin_refuses_to_overwrite_an_interrupted_entry(paths: OwnedPaths) -> None:
    """A create killed mid-way leaves a journal whose process is gone: recovery is pending."""
    dead = _dead_pid()
    _write_entry(
        paths,
        "debian",
        JournalEntry(
            op="create",
            realm="debian",
            pid=dead,
            started="2026-10-12T09:00:00.000Z",
            pid_start=None,
            step="download",
            params={},
        ),
    )
    before = (paths.journal / "debian.json").read_bytes()
    with pytest.raises(GinnError) as excinfo:
        Journal(paths).begin("rebuild", RealmName("debian"))
    err = excinfo.value
    assert err.code == "GINN-E-RECOVERY-PENDING"
    assert err.exit_code == 4
    assert err.details == {"realm": "debian", "op": "create", "pid": dead}
    assert "ginn create --resume debian" in err.remedy
    assert "ginn delete debian" in err.remedy
    assert (paths.journal / "debian.json").read_bytes() == before


@pytest.mark.fr("FR-122")
def test_begin_over_a_same_process_entry_is_also_refused(paths: OwnedPaths) -> None:
    journal = Journal(paths)
    journal.begin("create", RealmName("debian"))
    with pytest.raises(GinnError) as excinfo:
        journal.begin("rebuild", RealmName("debian"))
    assert excinfo.value.code == "GINN-E-RECOVERY-PENDING"
    assert excinfo.value.details["op"] == "create"
    stored = json.loads((paths.journal / "debian.json").read_text(encoding="utf-8"))
    assert stored["op"] == "create"


@pytest.mark.fr("FR-122")
def test_entry_of_a_dead_process_is_an_orphan(paths: OwnedPaths) -> None:
    dead = _dead_pid()
    journal = Journal(paths)
    _write_entry(
        paths,
        "debian",
        JournalEntry(
            op="create",
            realm="debian",
            pid=dead,
            started="2026-10-12T09:00:00.000Z",
            pid_start=None,
            step="download",
            params={},
        ),
    )
    orphans = journal.orphans()
    assert [e.realm for e in orphans] == ["debian"]
    assert orphans[0].op == "create"
    assert orphans[0].step == "download"
    assert is_pid_alive(dead) is False


@pytest.mark.fr("FR-122")
def test_entry_of_a_live_process_is_not_an_orphan(paths: OwnedPaths) -> None:
    journal = Journal(paths)
    journal.begin("rebuild", RealmName("arch"))
    assert journal.orphans() == []


@pytest.mark.fr("NFR-030")
def test_a_reused_pid_with_another_start_time_counts_as_orphaned(paths: OwnedPaths) -> None:
    """The pid is alive but its start time differs from the one recorded: another process."""
    real_start = process_start_ticks(os.getpid())
    assert real_start is not None
    _write_entry(
        paths,
        "arch",
        JournalEntry(
            op="update",
            realm="arch",
            pid=os.getpid(),
            started="2026-10-12T09:00:00.000Z",
            pid_start=real_start + 1,
            step="begin",
            params={},
        ),
    )
    assert is_pid_alive(os.getpid(), real_start) is True
    assert is_pid_alive(os.getpid(), real_start + 1) is False
    assert [e.realm for e in Journal(paths).orphans()] == ["arch"]


@pytest.mark.fr("FR-122")
def test_zombie_process_counts_as_dead(paths: OwnedPaths) -> None:
    """Exited but not yet reaped: waitid with WNOWAIT observes the exit and leaves a zombie."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        assert is_pid_alive(proc.pid) is False
    finally:
        proc.wait(timeout=30)


@pytest.mark.fr("FR-122")
def test_pid_zero_and_negative_are_never_alive() -> None:
    assert is_pid_alive(0) is False
    assert is_pid_alive(-1) is False


@pytest.mark.fr("NFR-030")
def test_pending_lists_every_realm_sorted_and_flags_corrupt_entries(paths: OwnedPaths) -> None:
    journal = Journal(paths)
    journal.begin("install", RealmName("zeta"))
    journal.begin("install", RealmName("alpha"))
    assert [e.realm for e in journal.pending()] == ["alpha", "zeta"]

    (paths.journal / "broken.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(GinnError) as excinfo:
        journal.pending()
    assert excinfo.value.code == "GINN-E-INTERNAL"
