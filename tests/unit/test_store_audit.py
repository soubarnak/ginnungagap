# SPDX-License-Identifier: Apache-2.0
"""Audit Log: append, rotation, ordering, completeness, append-only (FR-124, FR-125, DR-019)."""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ginn
from ginn.domain import RealmName
from ginn.errors import GinnError
from ginn.store.audit import DEFAULT_KEEP, DEFAULT_MAX_BYTES, AuditEntry, AuditLog
from ginn.store.paths import OwnedPaths
from tests.fakes.clock import FakeClock

WRITER_SCRIPT = """
import sys
from ginn.store.audit import AuditEntry, AuditLog
from ginn.store.paths import OwnedPaths

count, max_bytes, keep, tag = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
log = AuditLog(OwnedPaths.from_env(), max_bytes=max_bytes, keep=keep)
for index in range(count):
    log.append(AuditEntry(user="tester", realm="arch", op=f"{tag}-{index:04d}", outcome="ok"))
"""


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
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


def _entry(op: str, realm: str | None = "arch", ts: str | None = None) -> AuditEntry:
    return AuditEntry(user="soubarna", realm=realm, op=op, outcome="ok", ts=ts)


def _ops(
    log: AuditLog,
    realm: RealmName | None = None,
    since: datetime | None = None,
) -> list[str]:
    return [e.op for e in log.read(realm=realm, since=since)]


def _rotated_names(paths: OwnedPaths) -> list[str]:
    return sorted(p.name for p in paths.state.glob("audit.log.*"))


@pytest.mark.fr("FR-124")
def test_append_writes_one_line_with_the_dr050_fields_and_a_utc_millisecond_timestamp(
    paths: OwnedPaths,
) -> None:
    clock = FakeClock(start=1_800_000_000.25)
    log = AuditLog(paths, clock=clock)
    stored = log.append(
        AuditEntry(
            user="soubarna",
            realm="arch",
            op="tier-change",
            outcome="ok",
            params={"tier": "development"},
            before={"tier": "basic"},
            after={"tier": "development"},
        )
    )

    lines = paths.audit_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert set(data) == {
        "ts",
        "user",
        "realm",
        "op",
        "params",
        "before",
        "after",
        "outcome",
        "error_code",
    }
    assert data["ts"] == "2027-01-15T08:00:00.250Z"
    assert data["before"] == {"tier": "basic"}
    assert data["after"] == {"tier": "development"}
    assert data["error_code"] is None
    assert stored.ts == data["ts"]


@pytest.mark.fr("FR-124")
def test_append_is_flushed_to_disk_before_it_returns(
    paths: OwnedPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    real_fsync = os.fsync

    def counting_fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    log = AuditLog(paths, clock=FakeClock())
    log.append(_entry("install"))
    assert len(synced) == 2  # the file, then the directory that now holds it (DR-035)
    log.append(_entry("export"))
    assert len(synced) == 3  # a later append syncs the file only


@pytest.mark.fr("DR-050")
def test_audit_files_are_created_with_mode_0600(paths: OwnedPaths) -> None:
    log = AuditLog(paths, clock=FakeClock(), max_bytes=200, keep=2)
    for index in range(6):
        log.append(_entry(f"op-{index}"))
    for name in ["audit.log", *_rotated_names(paths)]:
        assert (paths.state / name).stat().st_mode & 0o777 == 0o600, name


@pytest.mark.fr("DR-050")
def test_the_audit_log_has_no_delete_or_rewrite_api() -> None:
    public = {name for name in dir(AuditLog) if not name.startswith("_")}
    assert public == {"append", "read"}


@pytest.mark.fr("DR-019")
def test_default_threshold_and_retention_are_10_mib_and_five_files() -> None:
    assert DEFAULT_MAX_BYTES == 10 * 1024 * 1024
    assert DEFAULT_KEEP == 5


@pytest.mark.fr("DR-019")
def test_no_rotation_below_the_threshold(paths: OwnedPaths) -> None:
    log = AuditLog(paths, clock=FakeClock())
    for index in range(3):
        log.append(_entry(f"op-{index}"))
    assert _rotated_names(paths) == []


@pytest.mark.fr("DR-019")
def test_rotation_shifts_files_and_keeps_only_the_five_most_recent(paths: OwnedPaths) -> None:
    log = AuditLog(paths, clock=FakeClock(), max_bytes=300, keep=5)
    appended = [f"op-{index:03d}" for index in range(60)]
    for op in appended:
        log.append(_entry(op))

    assert _rotated_names(paths) == [f"audit.log.{i}" for i in range(1, 6)]
    kept = _ops(log)
    assert kept == appended[-len(kept) :]
    assert kept[0] != appended[0]  # the oldest entries were dropped
    assert kept[-1] == appended[-1]


@pytest.mark.fr("NFR-043")
@pytest.mark.fr("FR-124")
def test_entry_count_equals_append_count_when_no_file_is_dropped(paths: OwnedPaths) -> None:
    log = AuditLog(paths, clock=FakeClock(), max_bytes=300, keep=5)
    appended = [f"op-{index:03d}" for index in range(10)]
    for op in appended:
        log.append(_entry(op))
    assert len(_rotated_names(paths)) >= 3, "the test must actually rotate"
    assert _ops(log) == appended


@pytest.mark.fr("FR-125")
@pytest.mark.fr("DR-032")
def test_read_is_chronological_across_rotated_files_and_the_current_one(
    paths: OwnedPaths,
) -> None:
    clock = FakeClock()
    log = AuditLog(paths, clock=clock, max_bytes=250, keep=5)
    for index in range(12):
        clock.advance(1.0)
        log.append(_entry(f"op-{index:02d}"))

    entries = list(log.read())
    assert [e.op for e in entries] == [f"op-{i:02d}" for i in range(12)]
    stamps = [datetime.fromisoformat(e.ts or "").astimezone(UTC) for e in entries]
    assert stamps == sorted(stamps)


@pytest.mark.fr("DR-032")
def test_read_orders_by_start_time_when_the_file_order_differs(paths: OwnedPaths) -> None:
    """The file holds completion order. The entry that started first is read first."""
    log = AuditLog(paths, clock=FakeClock(), max_bytes=100, keep=5)
    log.append(_entry("finished-first", ts="2027-01-15T08:00:02.000Z"))  # started second
    log.append(_entry("finished-second", ts="2027-01-15T08:00:01.000Z"))  # started first
    assert _rotated_names(paths) == ["audit.log.1"], "the two entries must sit in two files"

    assert _ops(log) == ["finished-second", "finished-first"]
    assert _ops(log, realm=RealmName("arch")) == ["finished-second", "finished-first"]


@pytest.mark.fr("DR-032")
def test_entries_with_equal_start_times_keep_their_file_order(paths: OwnedPaths) -> None:
    log = AuditLog(paths, clock=FakeClock(), max_bytes=100, keep=5)
    stamp = "2027-01-15T08:00:00.000Z"
    for op in ("first", "second", "third"):
        log.append(_entry(op, ts=stamp))
    assert len(_rotated_names(paths)) >= 2, "equal stamps must span files for this test"

    assert _ops(log) == ["first", "second", "third"]


@pytest.mark.fr("FR-125")
def test_read_shows_only_the_requested_realm_in_order(paths: OwnedPaths) -> None:
    clock = FakeClock()
    log = AuditLog(paths, clock=clock, max_bytes=400, keep=5)
    log.append(_entry("create", realm="arch"))
    clock.advance(1.0)
    log.append(_entry("create", realm="debian"))
    clock.advance(1.0)
    log.append(_entry("tier-change", realm="arch"))
    clock.advance(1.0)
    log.append(_entry("integrity-ack", realm=None))

    assert _ops(log, realm=RealmName("arch")) == ["create", "tier-change"]
    assert _ops(log, realm=RealmName("debian")) == ["create"]
    assert _ops(log) == ["create", "create", "tier-change", "integrity-ack"]


@pytest.mark.fr("FR-125")
def test_read_since_keeps_entries_at_or_after_the_moment(paths: OwnedPaths) -> None:
    clock = FakeClock(start=1_800_000_000.0)
    log = AuditLog(paths, clock=clock)
    for index in range(4):
        clock.advance(60.0)
        log.append(_entry(f"op-{index}"))

    cutoff = datetime(2027, 1, 15, 8, 2, 0, tzinfo=UTC)
    assert _ops(log, since=cutoff) == ["op-1", "op-2", "op-3"]
    with pytest.raises(ValueError, match="timezone"):
        list(log.read(since=datetime(2027, 1, 15, 8, 2, 0)))


@pytest.mark.fr("FR-124")
def test_torn_last_line_is_skipped_but_corruption_elsewhere_is_an_error(
    paths: OwnedPaths,
) -> None:
    log = AuditLog(paths, clock=FakeClock())
    log.append(_entry("install"))
    with paths.audit_log.open("ab") as handle:
        handle.write(b'{"ts": "2027-01-15T08:00:00.000Z", "us')  # crash mid-write, no newline
    assert _ops(log) == ["install"]

    paths.audit_log.write_bytes(b"not json\n" + paths.audit_log.read_bytes())
    with pytest.raises(GinnError) as excinfo:
        list(log.read())
    assert excinfo.value.code == "GINN-E-INTERNAL"
    assert excinfo.value.details["line"] == 1


TORN = b'{"ts": "2027-01-15T08:00:00.000Z", "us'  # a record cut short by a crash, no newline


@pytest.mark.fr("FR-124")
@pytest.mark.fr("NFR-030")
def test_append_after_a_torn_tail_cuts_the_torn_bytes_and_keeps_the_log_readable(
    paths: OwnedPaths,
) -> None:
    log = AuditLog(paths, clock=FakeClock())
    log.append(_entry("install"))
    with paths.audit_log.open("ab") as handle:
        handle.write(TORN)

    log.append(_entry("export"))

    assert _ops(log) == ["install", "export"]
    raw = paths.audit_log.read_bytes()
    assert TORN not in raw
    assert raw.endswith(b"\n")
    assert [json.loads(line)["op"] for line in raw.splitlines()] == ["install", "export"]


@pytest.mark.fr("NFR-030")
@pytest.mark.parametrize("torn_bytes", [1, 4095, 4096, 4097, 12_300])
def test_torn_tails_longer_than_one_read_are_cut_back_to_the_last_newline(
    paths: OwnedPaths, torn_bytes: int
) -> None:
    log = AuditLog(paths, clock=FakeClock())
    log.append(_entry("install"))
    log.append(_entry("export"))
    complete = paths.audit_log.read_bytes()
    with paths.audit_log.open("ab") as handle:
        handle.write(b"a" * torn_bytes)

    log.append(_entry("tier-change"))

    assert _ops(log) == ["install", "export", "tier-change"]
    assert paths.audit_log.read_bytes().startswith(complete)


@pytest.mark.fr("NFR-030")
def test_a_log_holding_only_a_torn_tail_is_emptied_before_the_append(paths: OwnedPaths) -> None:
    paths.audit_log.write_bytes(TORN)
    log = AuditLog(paths, clock=FakeClock())
    assert _ops(log) == []

    log.append(_entry("install"))

    assert _ops(log) == ["install"]
    assert paths.audit_log.read_bytes().count(b"\n") == 1


@pytest.mark.fr("DR-019")
@pytest.mark.fr("NFR-030")
def test_rotation_still_happens_after_a_torn_tail_is_cut(paths: OwnedPaths) -> None:
    log = AuditLog(paths, clock=FakeClock(), max_bytes=150, keep=3)
    log.append(_entry("op-000"))
    full = paths.audit_log.stat().st_size
    assert full >= 150, "one entry must reach the threshold"
    with paths.audit_log.open("ab") as handle:
        handle.write(TORN)

    log.append(_entry("op-001"))

    assert _rotated_names(paths) == ["audit.log.1"]
    rotated = (paths.state / "audit.log.1").read_bytes()
    assert rotated.endswith(b"\n")
    assert TORN not in rotated, "the torn bytes were cut before the file was rotated"
    assert _ops(log) == ["op-000", "op-001"]


@pytest.mark.fr("DR-035")
def test_creating_the_audit_log_fsyncs_its_directory_once(
    paths: OwnedPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushed: list[Path] = []

    def record(directory: Path) -> None:
        flushed.append(directory)

    monkeypatch.setattr("ginn.store.audit.fsync_dir", record)
    log = AuditLog(paths, clock=FakeClock())
    log.append(_entry("install"))
    log.append(_entry("export"))
    assert flushed == [paths.state]


@pytest.mark.fr("NFR-043")
def test_concurrent_writers_lose_no_entries(paths: OwnedPaths) -> None:
    """Two processes append at once with rotation on. Each entry must appear exactly once."""
    count = 40
    env = os.environ.copy()
    src = str(Path(ginn.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH")]))
    writers = [
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", WRITER_SCRIPT, str(count), "2000", "50", tag],
            env=env,
        )
        for tag in ("alpha", "beta")
    ]
    for writer in writers:
        assert writer.wait(timeout=120) == 0

    log = AuditLog(paths, clock=FakeClock(), max_bytes=2000, keep=50)
    assert len(_rotated_names(paths)) >= 2, "the test must actually rotate"
    ops = _ops(log)
    assert len(ops) == 2 * count
    assert len(set(ops)) == 2 * count
    for tag in ("alpha", "beta"):
        own = [op for op in ops if op.startswith(tag)]
        assert own == [f"{tag}-{i:04d}" for i in range(count)]


@pytest.mark.fr("FR-124")
def test_an_explicit_timestamp_is_kept_and_a_missing_one_is_stamped(paths: OwnedPaths) -> None:
    log = AuditLog(paths, clock=FakeClock(start=1_800_000_000.25))
    kept = log.append(_entry("keep", ts="2026-10-12T09:30:00.000Z"))
    stamped = log.append(_entry("stamp"))
    assert kept.ts == "2026-10-12T09:30:00.000Z"
    assert stamped.ts == "2027-01-15T08:00:00.250Z"
