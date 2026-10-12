# SPDX-License-Identifier: Apache-2.0
"""Integrity state: baseline, pending change, audited checks and acknowledgement (FR-006, FR-124).

The AC-1 analogue runs at unit level: a file under /etc is changed between the before and after
checks of a mutating operation. The host-level run needs `ginn install` on a Void host (M5). The
NFR-011 timing proof is here too: a before and after pair on 5000 files, within one second. The
host-level timing is T10.4.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ginn.errors import GinnError
from ginn.integrity import state as state_module
from ginn.integrity.state import LOCK_FILE, MAX_AUDIT_PATHS, IntegrityState
from ginn.store.audit import AuditLog
from ginn.store.paths import OwnedPaths
from tests.fakes.clock import FakeClock

SCOPE = ("etc", "var/db/xbps", "var/service", "usr/local", "opt")
USER = "tester"
CHANGED = "10.0.0.1 changed\n"
BASELINE = "baseline.json"
PENDING = "pending.json"


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    """Owned Paths rooted in tmp_path. Every XDG variable is pinned so no real host path is used."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    yield OwnedPaths.from_env(uid=os.getuid())


@pytest.fixture
def host(tmp_path: Path) -> Path:
    root = tmp_path / "host"
    for scope in SCOPE:
        (root / scope).mkdir(parents=True)
    (root / "etc" / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    return root


@pytest.fixture
def audit(paths: OwnedPaths) -> AuditLog:
    return AuditLog(paths, clock=FakeClock())


@pytest.fixture
def state(paths: OwnedPaths, host: Path) -> IntegrityState:
    return IntegrityState(paths, root_dir=host, clock=FakeClock())


def _change_hosts(host: Path) -> None:
    (host / "etc" / "hosts").write_text(CHANGED, encoding="utf-8")


def _other_root(paths: OwnedPaths, tmp_path: Path) -> IntegrityState:
    """A state for a second host root with the five scope directories, sharing the same paths."""
    other_root = tmp_path / "other"
    for scope in SCOPE:
        (other_root / scope).mkdir(parents=True, exist_ok=True)
    return IntegrityState(paths, root_dir=other_root, clock=FakeClock())


def _entries(audit: AuditLog) -> list[Any]:
    return list(audit.read())


def _changes(audit: AuditLog) -> list[Any]:
    return [e for e in _entries(audit) if e.op == "integrity" and e.outcome.startswith("GINN-W")]


@pytest.mark.fr("FR-006")
def test_a_change_during_an_operation_warns_pends_audits_and_blocks_until_acknowledged(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    before = state.before_op(audit, USER)
    assert state.baseline() is not None

    _change_hosts(host)
    warning = state.after_op(before, audit, USER)

    assert warning is not None
    assert warning.code == "GINN-W-HOST-CHANGED"
    assert "/etc/hosts" in warning.message
    assert state.pending() == ["/etc/hosts"]

    entries = _entries(audit)
    assert [entry.op for entry in entries] == ["integrity", "integrity"]
    change = entries[-1]
    assert change.user == USER
    assert change.realm is None
    assert change.outcome == "GINN-W-HOST-CHANGED: 1 path(s) changed"
    assert change.params["changed"]["paths"] == ["/etc/hosts"]
    assert change.params["against"] == "before-operation"
    assert change.before == {"digest": before.digest}
    assert change.after is not None and change.after["digest"] != before.digest

    with pytest.raises(GinnError) as refused_check:
        state.require_acknowledged()
    assert refused_check.value.code == "GINN-E-HOST-UNACKED"
    assert refused_check.value.details["paths"] == ["/etc/hosts"]

    with pytest.raises(GinnError) as refused_op:
        state.before_op(audit, USER)
    assert refused_op.value.code == "GINN-E-HOST-UNACKED"

    assert state.acknowledge(audit, USER) == ["/etc/hosts"]
    assert state.pending() is None
    state.require_acknowledged()
    assert _entries(audit)[-1].op == "integrity-ack"

    state.before_op(audit, USER)


@pytest.mark.fr("FR-006")
def test_a_change_made_between_two_operations_is_refused_against_the_baseline(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    state.before_op(audit, USER)
    _change_hosts(host)

    with pytest.raises(GinnError) as refused:
        state.before_op(audit, USER)

    assert refused.value.code == "GINN-E-HOST-UNACKED"
    assert refused.value.details["paths"] == ["/etc/hosts"]
    assert "/etc/hosts" in refused.value.message
    assert state.pending() == ["/etc/hosts"]
    (change,) = _changes(audit)
    assert change.params["against"] == "baseline"


@pytest.mark.fr("FR-006")
def test_the_refusal_names_the_paths_and_caps_the_list_at_twenty(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    state.before_op(audit, USER)
    for index in range(25):
        (host / "etc" / f"extra{index:02d}").write_text("x", encoding="utf-8")

    with pytest.raises(GinnError) as refused:
        state.before_op(audit, USER)

    changed = refused.value.details["paths"]
    assert isinstance(changed, list)
    assert len(changed) == 26  # the 25 new files and /etc, whose entries changed
    assert "/etc/extra00" in refused.value.message
    assert "/etc/extra18" in refused.value.message
    assert "/etc/extra24" not in refused.value.message
    assert f"and {len(changed) - 20} more" in refused.value.message


@pytest.mark.fr("FR-006")
def test_an_unchanged_host_writes_no_pending_file_and_no_audit_entry(
    state: IntegrityState, audit: AuditLog
) -> None:
    before = state.before_op(audit, USER)
    assert state.after_op(before, audit, USER) is None
    assert state.check(audit, USER) == []
    assert state.pending() is None
    assert len(_entries(audit)) == 1


@pytest.mark.fr("FR-006")
def test_check_reports_a_change_and_audits_it_only_once_while_it_stays_pending(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    assert state.check(audit, USER) == []
    _change_hosts(host)

    assert state.check(audit, USER) == ["/etc/hosts"]
    assert state.check(audit, USER) == ["/etc/hosts"]

    assert state.pending() == ["/etc/hosts"]
    assert len(_entries(audit)) == 2
    assert _changes(audit)[0].params["against"] == "baseline"


@pytest.mark.fr("FR-006")
def test_check_records_a_further_change_when_the_pending_list_grows(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    state.check(audit, USER)
    _change_hosts(host)
    state.check(audit, USER)

    (host / "etc" / "resolv.conf").write_text("nameserver 10.0.0.2\n", encoding="utf-8")
    changed = state.check(audit, USER)

    assert "/etc/resolv.conf" in changed
    assert state.pending() == changed
    assert len(_entries(audit)) == 3


@pytest.mark.fr("FR-006")
def test_acknowledging_a_clean_host_is_audited_and_changes_nothing(
    state: IntegrityState, audit: AuditLog
) -> None:
    state.before_op(audit, USER)
    assert state.acknowledge(audit, USER) == []
    assert state.pending() is None
    entry = _entries(audit)[-1]
    assert entry.op == "integrity-ack"
    assert entry.params["changed"]["count"] == 0


@pytest.mark.fr("FR-006")
def test_acknowledge_refuses_a_change_wider_than_the_pending_list_and_relists_it(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    state.check(audit, USER)
    _change_hosts(host)
    assert state.check(audit, USER) == ["/etc/hosts"]

    (host / "etc" / "resolv.conf").write_text("nameserver 10.0.0.2\n", encoding="utf-8")
    with pytest.raises(GinnError) as refused:
        state.acknowledge(audit, USER)

    assert refused.value.code == "GINN-E-HOST-UNACKED"
    listed = refused.value.details["paths"]
    assert isinstance(listed, list)
    assert "/etc/hosts" in listed
    assert "/etc/resolv.conf" in listed
    assert "/etc/resolv.conf" in refused.value.message
    assert state.pending() == listed

    with pytest.raises(GinnError):
        state.before_op(audit, USER)  # still blocked: nothing was accepted

    accepted = state.acknowledge(audit, USER)
    assert accepted == listed
    assert state.pending() is None
    state.before_op(audit, USER)


@pytest.mark.fr("FR-006")
def test_acknowledge_refuses_a_changed_host_when_no_change_was_ever_listed(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    state.check(audit, USER)
    _change_hosts(host)

    with pytest.raises(GinnError) as refused:
        state.acknowledge(audit, USER)

    assert refused.value.details["paths"] == ["/etc/hosts"]
    assert state.pending() == ["/etc/hosts"]
    assert state.acknowledge(audit, USER) == ["/etc/hosts"]


@pytest.mark.fr("FR-006")
def test_acknowledge_accepts_exactly_the_listed_paths_and_audits_them(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    state.check(audit, USER)
    _change_hosts(host)
    listed = state.check(audit, USER)

    assert state.acknowledge(audit, USER) == listed
    entry = _entries(audit)[-1]
    assert entry.op == "integrity-ack"
    assert entry.params["was_pending"]["paths"] == listed
    assert entry.params["changed"]["paths"] == listed


@pytest.mark.fr("FR-006")
def test_acknowledge_treats_an_unreadable_baseline_as_absent_audits_it_and_recaptures(
    state: IntegrityState, audit: AuditLog, paths: OwnedPaths, host: Path
) -> None:
    state.check(audit, USER)
    (paths.integrity / "baseline.json").write_text("not json", encoding="utf-8")

    report = state.acknowledge_report(audit, USER)

    assert list(report.discarded) == ["baseline.json"]
    assert report.created is True
    assert report.changed == []
    entry = _entries(audit)[-1]
    assert entry.op == "integrity-ack"
    assert entry.outcome == "acknowledged; state discarded and rewritten: baseline.json"
    assert entry.params["discarded"] == ["baseline.json"]
    recaptured = state.baseline()
    assert recaptured is not None
    assert recaptured.digest == state.capture().digest


@pytest.mark.fr("FR-006")
def test_acknowledge_treats_an_unreadable_pending_file_as_absent(
    state: IntegrityState, audit: AuditLog, paths: OwnedPaths, host: Path
) -> None:
    state.check(audit, USER)
    (paths.integrity / "pending.json").write_text("{", encoding="utf-8")

    report = state.acknowledge_report(audit, USER)

    assert list(report.discarded) == ["pending.json"]
    assert report.changed == []
    assert state.pending() is None

    # With the host changed, the unreadable list is no longer trusted, so the change is relisted.
    _change_hosts(host)
    (paths.integrity / "pending.json").write_text("{", encoding="utf-8")
    with pytest.raises(GinnError) as refused:
        state.acknowledge(audit, USER)
    assert refused.value.details["paths"] == ["/etc/hosts"]
    assert state.acknowledge(audit, USER) == ["/etc/hosts"]


@pytest.mark.fr("FR-006")
def test_a_corrupt_baseline_stops_a_check_and_the_remedy_names_the_file(
    state: IntegrityState, audit: AuditLog, paths: OwnedPaths
) -> None:
    paths.integrity.mkdir(mode=0o700, parents=True)
    (paths.integrity / "baseline.json").write_text(
        '{"schema_version": 1, "root": "/", "entries": {"/etc/hosts": ["file"]}}',
        encoding="utf-8",
    )

    with pytest.raises(GinnError) as corrupt:
        state.check(audit, USER)

    assert corrupt.value.code == "GINN-E-INTERNAL"
    assert corrupt.value.exit_code == 70
    assert corrupt.value.details["path"] == str(paths.integrity / "baseline.json")
    assert "baseline.json" in corrupt.value.remedy
    assert "ginn integrity --ack" in corrupt.value.remedy


@pytest.mark.fr("FR-006")
def test_a_corrupt_pending_file_blocks_an_operation_and_the_remedy_names_it(
    state: IntegrityState, audit: AuditLog, paths: OwnedPaths
) -> None:
    state.before_op(audit, USER)
    paths.integrity.mkdir(mode=0o700, exist_ok=True)
    (paths.integrity / "pending.json").write_text("[1, 2", encoding="utf-8")

    with pytest.raises(GinnError) as corrupt:
        state.before_op(audit, USER)

    assert corrupt.value.code == "GINN-E-INTERNAL"
    assert "pending.json" in corrupt.value.remedy


@pytest.mark.fr("FR-006")
def test_a_baseline_under_another_root_refuses_check_and_operations_with_a_file_remedy(
    state: IntegrityState, audit: AuditLog, paths: OwnedPaths, host: Path, tmp_path: Path
) -> None:
    state.check(audit, USER)
    other = _other_root(paths, tmp_path)

    with pytest.raises(GinnError) as check_refused:
        other.check(audit, USER)
    with pytest.raises(GinnError) as op_refused:
        other.before_op(audit, USER)

    for refused in (check_refused, op_refused):
        assert refused.value.code == "GINN-E-USAGE"
        assert refused.value.exit_code == 2
        assert str(host) in refused.value.message
        assert str(paths.integrity / "baseline.json") in refused.value.remedy
        assert "ginn integrity --ack" in refused.value.remedy
    assert check_refused.value.details["recorded_root"] == str(host)


@pytest.mark.fr("FR-006")
def test_acknowledge_discards_a_baseline_under_another_root_audits_why_and_recaptures(
    state: IntegrityState, audit: AuditLog, paths: OwnedPaths, host: Path, tmp_path: Path
) -> None:
    state.check(audit, USER)
    other = _other_root(paths, tmp_path)

    report = other.acknowledge_report(audit, USER)

    assert list(report.discarded) == ["baseline.json"]
    assert str(host) in report.discarded["baseline.json"]
    assert report.changed == []
    entry = _entries(audit)[-1]
    assert entry.op == "integrity-ack"
    assert entry.outcome == "acknowledged; state discarded and rewritten: baseline.json"
    assert entry.params["discarded"] == ["baseline.json"]
    assert str(host) in entry.params["discard_reasons"]["baseline.json"]
    assert other.check(audit, USER) == []
    with pytest.raises(GinnError) as back:
        state.check(audit, USER)
    assert back.value.code == "GINN-E-USAGE"


@pytest.mark.fr("FR-006")
def test_a_pending_file_that_outlived_its_baseline_is_kept_and_blocks_until_acknowledged(
    state: IntegrityState, audit: AuditLog, host: Path, paths: OwnedPaths
) -> None:
    state.check(audit, USER)
    _change_hosts(host)
    assert state.check(audit, USER) == ["/etc/hosts"]
    (paths.integrity / "baseline.json").unlink()

    report = state.check_report(audit, USER)

    assert report.created is True
    assert report.pending == ["/etc/hosts"]
    with pytest.raises(GinnError) as refused:
        state.before_op(audit, USER)
    assert refused.value.code == "GINN-E-HOST-UNACKED"
    assert refused.value.details["paths"] == ["/etc/hosts"]

    assert state.acknowledge(audit, USER) == []
    assert state.pending() is None
    state.before_op(audit, USER)


@pytest.mark.fr("FR-006")
def test_the_baseline_writer_is_private_so_callers_reach_it_through_the_locked_methods() -> None:
    assert not hasattr(IntegrityState, "set_baseline")
    assert callable(IntegrityState._set_baseline)  # noqa: SLF001 - the test pins the private name


@pytest.mark.fr("FR-006")
def test_the_baseline_records_its_root(
    state: IntegrityState, audit: AuditLog, paths: OwnedPaths, host: Path
) -> None:
    state.check(audit, USER)
    data = json.loads((paths.integrity / BASELINE).read_text(encoding="utf-8"))
    assert data["root"] == str(host)


@pytest.mark.fr("FR-006")
def test_a_crash_after_the_audit_entry_leaves_a_duplicate_on_rerun_not_a_missing_entry(
    state: IntegrityState,
    audit: AuditLog,
    host: Path,
    paths: OwnedPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state.before_op(audit, USER)
    _change_hosts(host)
    real_write = state_module.write_json_atomic

    def disk_full(path: Path, obj: object) -> None:
        if path.name == "pending.json":
            raise OSError("disk full")
        real_write(path, obj)

    monkeypatch.setattr(state_module, "write_json_atomic", disk_full)
    with pytest.raises(OSError, match="disk full"):
        state.before_op(audit, USER)

    assert len(_changes(audit)) == 1  # the event is audited before its state is written
    assert state.pending() is None

    monkeypatch.setattr(state_module, "write_json_atomic", real_write)
    assert state.check(audit, USER) == ["/etc/hosts"]
    assert state.pending() == ["/etc/hosts"]
    assert len(_changes(audit)) == 2  # the rerun records the same change again


@pytest.mark.fr("FR-006")
def test_a_crash_during_acknowledge_keeps_the_old_baseline_and_a_rerun_completes_it(
    state: IntegrityState,
    audit: AuditLog,
    host: Path,
    paths: OwnedPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state.check(audit, USER)
    _change_hosts(host)
    state.check(audit, USER)
    stored = state.baseline()
    assert stored is not None
    old_digest = stored.digest
    real_write = state_module.write_json_atomic

    def disk_full(path: Path, obj: object) -> None:
        if path.name == "baseline.json":
            raise OSError("disk full")
        real_write(path, obj)

    monkeypatch.setattr(state_module, "write_json_atomic", disk_full)
    with pytest.raises(OSError, match="disk full"):
        state.acknowledge(audit, USER)

    assert [e.op for e in _entries(audit)].count("integrity-ack") == 1
    kept = state.baseline()
    assert kept is not None and kept.digest == old_digest
    assert state.pending() == ["/etc/hosts"]

    monkeypatch.setattr(state_module, "write_json_atomic", real_write)
    assert state.acknowledge(audit, USER) == ["/etc/hosts"]
    assert state.pending() is None
    assert [e.op for e in _entries(audit)].count("integrity-ack") == 2


@pytest.mark.fr("FR-006")
def test_a_second_writer_waits_for_the_integrity_lock_and_is_never_refused(
    state: IntegrityState, audit: AuditLog, paths: OwnedPaths
) -> None:
    state.check(audit, USER)
    holder = os.open(paths.integrity / LOCK_FILE, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    outcome: list[object] = []

    def writer() -> None:
        try:
            outcome.append(state.check(audit, USER))
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=writer)
    try:
        thread.start()
        thread.join(0.3)
        assert thread.is_alive(), "the writer ran while another holder had the lock"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    thread.join(5)

    assert not thread.is_alive()
    assert outcome == [[]]


@pytest.mark.fr("FR-006")
def test_the_baseline_pending_and_lock_files_are_private_in_a_0700_directory(
    state: IntegrityState, audit: AuditLog, host: Path, paths: OwnedPaths
) -> None:
    state.before_op(audit, USER)
    _change_hosts(host)
    state.check(audit, USER)

    assert paths.integrity.stat().st_mode & 0o777 == 0o700
    for name in (BASELINE, PENDING, LOCK_FILE):
        assert (paths.integrity / name).stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in paths.integrity.iterdir()) == sorted(
        [BASELINE, PENDING, LOCK_FILE]
    )


@pytest.mark.fr("FR-006")
def test_the_audit_params_list_at_most_200_paths_and_say_when_they_are_cut(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    state.check(audit, USER)
    for index in range(MAX_AUDIT_PATHS + 5):
        (host / "etc" / f"extra{index:03d}").write_text("x", encoding="utf-8")

    changed = state.check(audit, USER)

    assert len(changed) > MAX_AUDIT_PATHS
    recorded = _entries(audit)[-1].params["changed"]
    assert recorded["count"] == len(changed)
    assert recorded["paths"] == changed[:MAX_AUDIT_PATHS]
    assert recorded["truncated"] is True
    assert state.pending() == changed


@pytest.mark.fr("FR-006")
@pytest.mark.fr("NFR-011")
def test_a_before_and_after_pair_on_five_thousand_files_takes_at_most_one_second(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    for index in range(5000):
        directory = host / "usr" / "local" / f"d{index % 50:02d}"
        directory.mkdir(exist_ok=True)
        (directory / f"f{index:05d}").write_text("x", encoding="utf-8")
    state.before_op(audit, USER)  # creates the baseline; not timed

    start = time.perf_counter()
    before = state.before_op(audit, USER)
    warning = state.after_op(before, audit, USER)
    elapsed = time.perf_counter() - start

    assert warning is None
    assert elapsed <= 1.0, f"before_op and after_op took {elapsed:.3f} s"


@pytest.mark.fr("FR-006")
def test_a_change_in_a_timed_pair_is_still_reported(
    state: IntegrityState, audit: AuditLog, host: Path
) -> None:
    before = state.before_op(audit, USER)
    _change_hosts(host)

    warning = state.after_op(before, audit, USER)

    assert warning is not None
    assert state.pending() == ["/etc/hosts"]
