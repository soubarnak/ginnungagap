# SPDX-License-Identifier: Apache-2.0
"""The choke point for mutating operations: order, one audit entry, refusals, crash evidence.

Covers DR-032 (order and start-time audit), DR-033 and FR-123 (realm lock, refused at once),
FR-006 (host integrity before and after), FR-124 and NFR-043 (one audit entry per operation),
FR-122 and NFR-030 (journal evidence after a kill). Host-level runs with real operations come in
M4 to M8.
"""

import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import ginn
from ginn.domain import RealmName
from ginn.errors import GinnError, make
from ginn.ops._mutating import OpEnv, mutating
from ginn.store._time import now_iso
from ginn.store.audit import AuditEntry
from ginn.store.journal import Journal
from ginn.store.locks import realm_lock
from ginn.store.paths import OwnedPaths
from tests.fakes.clock import FakeClock

SRC_DIR = Path(ginn.__file__).resolve().parents[1]
SCOPE = ("etc", "var/db/xbps", "var/service", "usr/local", "opt")
USER = "tester"
ARCH = RealmName("arch")
DEBIAN = RealmName("debian")
OP_ID = re.compile(r"^[0-9a-f]{32}$")

# The child holds the realm lock inside an operation and waits until its stdin closes.
HOLDER_SCRIPT = """
import sys
from pathlib import Path
from ginn.domain import RealmName
from ginn.ops._mutating import OpEnv, mutating
from ginn.store.paths import OwnedPaths

env = OpEnv.for_paths(OwnedPaths.from_env(), "holder", root_dir=Path(sys.argv[1]))
with mutating(env, "rebuild", RealmName("arch")):
    print("ready", flush=True)
    sys.stdin.read()
"""

# The child records a journal step, prints its op_id, then SIGKILLs itself inside the body.
KILLED_SCRIPT = """
import os, signal, sys
from pathlib import Path
from ginn.domain import RealmName
from ginn.ops._mutating import OpEnv, mutating
from ginn.store.paths import OwnedPaths

env = OpEnv.for_paths(OwnedPaths.from_env(), "holder", root_dir=Path(sys.argv[1]))
with mutating(env, "rebuild", RealmName("arch"), {"tier": "basic"}) as m:
    m.step("images-pulled")
    print(m.op_id, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
"""


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    """Owned Paths rooted in tmp_path. Every XDG variable is pinned so no real host path is used."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    yield resolved


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A host root under tmp_path, outside the Owned Paths, so ginn's own writes are not seen."""
    root = tmp_path / "host"
    for scope in SCOPE:
        (root / scope).mkdir(parents=True)
    (root / "etc" / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    return root


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def env(paths: OwnedPaths, host: Path, clock: FakeClock) -> OpEnv:
    return OpEnv.for_paths(paths, USER, root_dir=host, clock=clock)


def _change_hosts(host: Path) -> None:
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")


class _TickingClock:
    """A clock that moves a full second on every reading and counts its readings."""

    def __init__(self, start: float) -> None:
        self._now = start
        self.readings = 0

    def __call__(self) -> float:
        self.readings += 1
        self._now += 1.0
        return self._now


def _realm_entries(env: OpEnv, realm: RealmName) -> list[AuditEntry]:
    """Audit entries of one realm. Host-integrity events have no realm and are not included."""
    return list(env.audit.read(realm=realm))


def _start_child(script: str, host: Path, *args: str) -> subprocess.Popen[str]:
    child_env = os.environ.copy()
    existing = child_env.get("PYTHONPATH")
    child_env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC_DIR), existing]))
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", script, str(host), *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env,
    )


@pytest.mark.fr("DR-032")
def test_lock_precedes_host_check_then_journal_then_body_then_after_check_audit_finish(
    env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    env.integrity.check(env.audit, USER)  # the baseline, so the first operation writes no baseline

    @contextmanager
    def recording_realm_lock(paths: OwnedPaths, name: RealmName, op: str) -> Iterator[None]:
        events.append(f"lock {name.value}")
        with realm_lock(paths, name, op):
            yield
        events.append(f"unlock {name.value}")

    def record(owner: object, method: str, label: str) -> None:
        real: Callable[..., object] = getattr(owner, method)

        def wrapper(*args: object, **kwargs: object) -> object:
            events.append(label)
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, method, wrapper)

    monkeypatch.setattr("ginn.ops._mutating.realm_lock", recording_realm_lock)
    record(env.integrity, "before_op", "before_op")
    record(env.journal, "begin", "journal begin")
    record(env.integrity, "after_op", "after_op")
    record(env.audit, "append", "audit")
    record(env.journal, "finish", "journal finish")

    with mutating(env, "install", ARCH):
        events.append("body")

    assert events == [
        "lock arch",
        "before_op",
        "journal begin",
        "body",
        "after_op",
        "audit",
        "journal finish",
        "unlock arch",
    ]


@pytest.mark.fr("FR-124")
@pytest.mark.fr("NFR-043")
@pytest.mark.fr("DR-050")
@pytest.mark.parametrize(
    ("build", "outcome", "code", "journal_kept"),
    [
        pytest.param(None, "ok", None, False, id="success"),
        pytest.param(
            lambda: make("GINN-E-NAME-TAKEN", name="arch"),
            "error",
            "GINN-E-NAME-TAKEN",
            False,
            id="ginn-error",
        ),
        pytest.param(KeyboardInterrupt, "interrupted", None, True, id="keyboard-interrupt"),
        pytest.param(lambda: RuntimeError("boom"), "error", None, True, id="other-exception"),
    ],
)
def test_each_operation_writes_exactly_one_audit_entry_and_the_body_exception_is_unchanged(
    env: OpEnv,
    paths: OwnedPaths,
    build: Callable[[], BaseException] | None,
    outcome: str,
    code: str | None,
    journal_kept: bool,
) -> None:
    if build is None:
        with mutating(env, "install", ARCH):
            pass
    else:
        raised = build()
        with pytest.raises(BaseException) as info:
            with mutating(env, "install", ARCH):
                raise raised
        assert info.value is raised

    entries = _realm_entries(env, ARCH)
    assert len(entries) == 1
    assert entries[0].op == "install"
    assert entries[0].outcome == outcome
    assert entries[0].error_code == code
    assert OP_ID.match(str(entries[0].params["op_id"]))

    # Recovery's rule: the journal stays unless the audit outcome is "ok" or an error with a code.
    pending = env.journal.pending()
    if journal_kept:
        (kept,) = pending
        assert kept.op == "install"
        assert kept.params["op_id"] == entries[0].params["op_id"]
    else:
        assert pending == []


@pytest.mark.fr("FR-006")
def test_a_host_change_found_after_a_raising_body_is_noted_on_the_exception(
    env: OpEnv, host: Path
) -> None:
    with pytest.raises(RuntimeError, match="boom") as info:
        with mutating(env, "install", ARCH) as mutation:
            _change_hosts(host)
            raise RuntimeError("boom")

    assert [w.code for w in mutation.warnings] == ["GINN-W-HOST-CHANGED"]
    notes = "\n".join(info.value.__notes__)
    assert "GINN-W-HOST-CHANGED" in notes
    assert "/etc/hosts" in notes


@pytest.mark.fr("DR-032")
@pytest.mark.fr("FR-122")
def test_journal_and_audit_share_one_start_reading_when_the_clock_moves_on_every_call(
    paths: OwnedPaths, host: Path
) -> None:
    ticking = _TickingClock(start=1_800_000_000.0)
    env = OpEnv.for_paths(paths, USER, root_dir=host, clock=ticking)
    with mutating(env, "install", ARCH):
        (pending,) = env.journal.pending()

    (entry,) = _realm_entries(env, ARCH)
    assert ticking.readings > 1, (
        "the clock must move between the readings for this to test anything"
    )
    assert entry.ts == "2027-01-15T08:00:01.000Z"  # the first reading, taken at entry
    assert pending.started == entry.ts


@pytest.mark.fr("DR-032")
def test_audit_timestamp_is_the_start_of_the_operation_not_its_end(
    env: OpEnv, clock: FakeClock
) -> None:
    start = clock()
    with mutating(env, "rebuild", ARCH):
        clock.advance(30)

    (entry,) = _realm_entries(env, ARCH)
    assert entry.ts == now_iso(lambda: start)
    assert entry.ts != now_iso(clock)


@pytest.mark.fr("DR-032")
@pytest.mark.fr("FR-122")
def test_journal_start_and_audit_timestamp_are_the_same_instant(
    env: OpEnv, clock: FakeClock
) -> None:
    start = clock()
    with mutating(env, "install", ARCH):
        (pending,) = env.journal.pending()
        clock.advance(5)

    (entry,) = _realm_entries(env, ARCH)
    assert pending.started == entry.ts == now_iso(lambda: start)


@pytest.mark.fr("DR-032")
def test_overlapping_operations_on_different_realms_read_back_in_start_order(
    env: OpEnv, clock: FakeClock, paths: OwnedPaths
) -> None:
    with mutating(env, "install", ARCH):  # A starts
        clock.advance(1)
        with mutating(env, "install", DEBIAN):  # B starts and finishes inside A
            clock.advance(1)
        clock.advance(1)  # A finishes last

    # The file holds completion order: B was appended before A.
    raw = [json.loads(line) for line in paths.audit_log.read_text(encoding="utf-8").splitlines()]
    assert [line["realm"] for line in raw if line["realm"] is not None] == ["debian", "arch"]
    # read() returns start order: A, then B.
    assert [e.realm for e in env.audit.read() if e.realm is not None] == ["arch", "debian"]


@pytest.mark.fr("FR-124")
def test_audit_entry_carries_user_realm_op_params_and_the_permission_diff(env: OpEnv) -> None:
    with mutating(env, "tier", ARCH, {"tier": "basic"}) as mutation:
        mutation.set_permissions(
            before={"tier": "basic", "shared": []},
            after={"tier": "development", "shared": []},
        )
        mutation.add_params(restart_required=True)

    (entry,) = _realm_entries(env, ARCH)
    assert entry.user == USER
    assert entry.realm == "arch"
    assert entry.op == "tier"
    assert entry.before == {"tier": "basic", "shared": []}
    assert entry.after == {"tier": "development", "shared": []}
    assert entry.params == {
        "tier": "basic",
        "restart_required": True,
        "op_id": mutation.op_id,
    }


@pytest.mark.fr("DR-050")
def test_each_operation_gets_its_own_op_id(env: OpEnv) -> None:
    with mutating(env, "install", ARCH) as first:
        pass
    with mutating(env, "install", ARCH) as second:
        pass
    assert OP_ID.match(first.op_id)
    assert OP_ID.match(second.op_id)
    assert first.op_id != second.op_id


@pytest.mark.fr("FR-123")
@pytest.mark.fr("DR-033")
def test_nested_attempt_on_the_same_realm_is_refused_naming_the_running_operation(
    env: OpEnv,
) -> None:
    with mutating(env, "rebuild", ARCH):
        ran: list[bool] = []
        with pytest.raises(GinnError) as info:
            with mutating(env, "install", ARCH):
                ran.append(True)
    assert str(info.value) == "GINN-E-REALM-BUSY: rebuild in progress"
    assert info.value.exit_code == 75
    assert ran == []


@pytest.mark.fr("DR-033")
def test_different_realms_do_not_block_each_other(env: OpEnv) -> None:
    with mutating(env, "rebuild", ARCH):
        with mutating(env, "install", DEBIAN):
            pass
    assert len(_realm_entries(env, ARCH)) == 1
    assert len(_realm_entries(env, DEBIAN)) == 1


@pytest.mark.fr("FR-123")
@pytest.mark.fr("DR-033")
def test_second_process_is_refused_at_once_and_writes_no_entry_for_the_refused_attempt(
    env: OpEnv, host: Path
) -> None:
    holder = _start_child(HOLDER_SCRIPT, host)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready", holder.stderr and holder.stderr.read()

        ran: list[bool] = []
        with pytest.raises(GinnError) as info:
            with mutating(env, "install", ARCH):
                ran.append(True)
        assert str(info.value) == "GINN-E-REALM-BUSY: rebuild in progress"
        assert ran == []
        assert _realm_entries(env, ARCH) == []
    finally:
        if holder.stdin is not None and not holder.stdin.closed:
            holder.stdin.close()
        holder.wait(timeout=30)

    assert holder.returncode == 0, holder.stderr and holder.stderr.read()
    (entry,) = _realm_entries(env, ARCH)
    assert entry.op == "rebuild"
    assert entry.outcome == "ok"
    assert env.journal.pending() == []


@pytest.mark.fr("FR-006")
def test_unacknowledged_host_change_blocks_entry_without_journal_or_operation_entry(
    env: OpEnv, host: Path, paths: OwnedPaths
) -> None:
    with mutating(env, "install", ARCH):
        pass
    _change_hosts(host)

    ran: list[bool] = []
    with pytest.raises(GinnError) as info:
        with mutating(env, "install", ARCH):
            ran.append(True)

    assert info.value.code == "GINN-E-HOST-UNACKED"
    assert info.value.details["paths"] == ["/etc/hosts"]
    assert ran == []
    assert env.journal.pending() == []
    assert not (paths.journal / "arch.json").exists()
    assert len(_realm_entries(env, ARCH)) == 1  # only the first, clean operation

    host_events = [entry for entry in env.audit.read() if entry.realm is None]
    assert any(entry.op == "integrity" for entry in host_events)


@pytest.mark.fr("FR-006")
def test_host_change_during_the_body_warns_leaves_it_pending_and_blocks_until_acknowledged(
    env: OpEnv, host: Path
) -> None:
    with mutating(env, "install", ARCH) as mutation:
        _change_hosts(host)

    assert [warning.code for warning in mutation.warnings] == ["GINN-W-HOST-CHANGED"]
    assert "/etc/hosts" in mutation.warnings[0].message
    assert env.integrity.pending() == ["/etc/hosts"]
    (entry,) = _realm_entries(env, ARCH)
    assert entry.outcome == "ok"

    with pytest.raises(GinnError) as info:
        with mutating(env, "install", ARCH):
            pass
    assert info.value.code == "GINN-E-HOST-UNACKED"

    env.integrity.acknowledge(env.audit, USER)
    with mutating(env, "install", ARCH):
        pass
    assert len(_realm_entries(env, ARCH)) == 2


@pytest.mark.fr("FR-006")
def test_failing_after_check_still_audits_and_finishes_the_journal(
    env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_after(*_args: object) -> None:
        raise OSError("host capture failed")

    monkeypatch.setattr(env.integrity, "after_op", broken_after)

    with pytest.raises(OSError, match="host capture failed"):
        with mutating(env, "install", ARCH):
            pass

    (entry,) = _realm_entries(env, ARCH)
    assert entry.outcome == "ok"
    assert entry.params["integrity_after"] == "OSError"
    assert env.journal.pending() == []


@pytest.mark.fr("NFR-030")
def test_failed_audit_append_after_a_failed_body_keeps_the_journal_and_adds_a_note(
    env: OpEnv, paths: OwnedPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full_disk(_entry: AuditEntry) -> None:
        raise OSError("disk full")

    env.integrity.check(env.audit, USER)  # the baseline is written before the disk fills
    monkeypatch.setattr(env.audit, "append", full_disk)

    with pytest.raises(RuntimeError, match="boom") as info:
        with mutating(env, "install", ARCH):
            raise RuntimeError("boom")

    assert any("OSError" in note for note in info.value.__notes__)
    (pending,) = Journal(paths).pending()
    assert pending.op == "install"
    assert pending.realm == "arch"


@pytest.mark.fr("NFR-030")
def test_failed_audit_append_after_a_successful_body_raises_and_keeps_the_journal(
    env: OpEnv, paths: OwnedPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full_disk(_entry: AuditEntry) -> None:
        raise OSError("disk full")

    env.integrity.check(env.audit, USER)  # the baseline is written before the disk fills
    monkeypatch.setattr(env.audit, "append", full_disk)

    with pytest.raises(OSError, match="disk full"):
        with mutating(env, "install", ARCH):
            pass

    assert len(Journal(paths).pending()) == 1


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-122")
def test_a_killed_body_leaves_a_journal_entry_with_its_op_id_and_no_audit_entry(
    env: OpEnv, paths: OwnedPaths, host: Path
) -> None:
    child = _start_child(KILLED_SCRIPT, host)
    try:
        assert child.stdout is not None
        op_id = child.stdout.readline().strip()
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)

    assert child.returncode == -signal.SIGKILL, child.stderr and child.stderr.read()
    assert OP_ID.match(op_id)

    (pending,) = env.journal.pending()
    assert pending.op == "rebuild"
    assert pending.realm == "arch"
    assert pending.step == "images-pulled"
    assert pending.params == {"tier": "basic", "op_id": op_id}
    assert [entry.op for entry in env.journal.orphans()] == ["rebuild"]
    assert _realm_entries(env, ARCH) == []

    # The kernel released the realm lock with the process, so the next attempt reaches the
    # journal check and is refused there, not by the lock. The interrupted entry is kept.
    with pytest.raises(GinnError) as info:
        with mutating(env, "install", ARCH):
            pass
    assert info.value.code == "GINN-E-RECOVERY-PENDING"
    assert len(env.journal.pending()) == 1
    assert (paths.journal / "arch.json").exists()
