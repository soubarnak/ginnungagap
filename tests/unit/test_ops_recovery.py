# SPDX-License-Identifier: Apache-2.0
"""Recovery of interrupted operations: dead, live and damaged journal entries (T2.8, FR-122).

Covers ops/recovery.py against the real journal, audit log and record stores under tmp_path. The
podman side is a stub that answers inspect and ps for one container and records stops. No test runs
podman, sudo or a host command.

A killed operation is modelled by writing the journal file it would have left, with the pid and
start time of a process that has exited. A live operation is modelled by this process's own pid. The
pid checks are the same ones that the real journal uses (store/journal.py).
"""

import errno
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ginn.domain import (
    DistroId,
    InstallMethod,
    InstallSource,
    ManifestEntry,
    RealmName,
    RealmSettings,
    RealmState,
    Tier,
)
from ginn.errors import GinnError, make
from ginn.ops._mutating import OpEnv, is_finished
from ginn.ops.recovery import (
    _REMOVE_TIMEOUT_S,
    OUTCOME_BLOCKED,
    OUTCOME_BROKEN,
    OUTCOME_BUSY,
    OUTCOME_CORRUPT,
    OUTCOME_FINISHED,
    OUTCOME_LIVE,
    OUTCOME_RECOVERED,
    RecoveryOutcome,
    find_pending,
    recover,
)
from ginn.ops.security_ops import agent_state_path, read_agent_record
from ginn.packages.aur import query_argv
from ginn.packages.manager import MAX_OUTPUT_CHARS
from ginn.podman.models import ContainerInfo, ContainerState, ExecResult
from ginn.runtime.spec import LABEL_GENERATION, LABEL_REALM
from ginn.security import agent
from ginn.security._proc import Proc
from ginn.services.runit_user import service_dir
from ginn.store import journal as journal_module
from ginn.store.audit import AuditEntry, AuditLog
from ginn.store.journal import PARAMS_OVERFLOW, Journal, JournalEntry, process_start_ticks
from ginn.store.locks import realm_lock
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore, write_json_atomic
from tests.fakes.clock import FakeClock

OP_ID = "0123456789abcdef0123456789abcdef"
OTHER_OP_ID = "fedcba9876543210fedcba9876543210"
STARTED = "2026-10-13T08:00:00.000Z"
CREATED = datetime(2026, 10, 13, 7, 0, tzinfo=UTC)
ARCH = DistroId.parse("arch")
CID = "c0ffee"


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


@pytest.fixture
def env(paths: OwnedPaths, tmp_path: Path) -> OpEnv:
    return OpEnv.for_paths(paths, "tester", root_dir=tmp_path / "host", clock=FakeClock())


def _info(cid: str, state: ContainerState) -> ContainerInfo:
    return ContainerInfo(
        id=cid,
        name="ginn-test",
        image="docker.io/library/archlinux@sha256:abc",
        state=state,
        pid=0,
        exit_code=0,
        started_at="",
        finished_at="",
    )


class _Podman:
    """Answers inspect and ps for one container, and records stops and execs. `container` None:
    absent. `exec_exit` is the status of every exec (the AUR build-only removal, FR-043)."""

    def __init__(
        self,
        container: ContainerState | None = None,
        *,
        inspect_fails: bool = False,
        ps_fails: bool = False,
        exec_exit: int = 0,
    ) -> None:
        self.container = container
        self.inspect_fails = inspect_fails
        self.ps_fails = ps_fails
        self.exec_exit = exec_exit
        self.stops: list[str] = []
        self.execs: list[tuple[str, tuple[str, ...], str | None]] = []

    def exec(
        self,
        container_id: str,
        argv: Any,
        *,
        user: str | None = None,
        **_: Any,
    ) -> ExecResult:
        self.execs.append((container_id, tuple(argv), user))
        return ExecResult(self.exec_exit, "", "")

    def inspect(self, container_id: str) -> ContainerInfo:
        if self.inspect_fails or self.container is None:
            raise make("GINN-E-INTERNAL")
        return _info(container_id, self.container)

    def ps(self, label_filter: Any = None) -> list[ContainerInfo]:
        if self.ps_fails:
            raise make("GINN-E-INTERNAL")
        return [] if self.container is None else [_info(CID, self.container)]

    def stop(self, container_id: str, timeout_s: int) -> None:
        self.stops.append(container_id)
        if self.container is ContainerState.RUNNING:
            self.container = ContainerState.EXITED


def _dead_process() -> tuple[int, int]:
    """The pid and start time of a process that has exited and been reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    start = process_start_ticks(proc.pid)
    proc.wait()
    assert start is not None
    return proc.pid, start


def _record(
    name: str = "arch",
    *,
    state: RealmState = RealmState.CREATING,
    previous_state: RealmState | None = None,
    container_id: str | None = CID,
) -> RealmRecord:
    settings = RealmSettings(
        name=RealmName(name),
        distro=ARCH,
        tier=Tier.BASIC,
        services=False,
        autostart=False,
        everything=False,
    )
    return RealmRecord(
        settings=settings,
        state=state,
        previous_state=previous_state,
        image_ref="docker.io/library/archlinux@sha256:abc",
        image_digest="sha256:abc",
        container_id=container_id,
        generation=1,
        created_at=CREATED,
        updated_at=CREATED,
    )


def _save(paths: OwnedPaths, record: RealmRecord) -> None:
    RealmStore(paths).save(record)


def _op_id_for(realm: str) -> str:
    """Every operation has its own op_id (ops/_mutating.py), so each realm gets its own."""
    return OP_ID if realm == "arch" else OTHER_OP_ID


def _entry(
    op: str = "create",
    realm: str = "arch",
    *,
    step: str = "pulled",
    pid: int,
    pid_start: int | None = None,
    params: dict[str, Any] | None = None,
) -> JournalEntry:
    return JournalEntry(
        op=op,
        realm=realm,
        pid=pid,
        started=STARTED,
        pid_start=pid_start,
        step=step,
        params={"op_id": _op_id_for(realm), **(params or {})},
    )


def _write_journal(paths: OwnedPaths, entry: JournalEntry) -> Path:
    paths.journal.mkdir(parents=True, exist_ok=True)
    path = paths.journal / f"{entry.realm}.json"
    write_json_atomic(path, entry.to_json())
    return path


def _dead(
    op: str = "create",
    realm: str = "arch",
    *,
    step: str = "pulled",
    params: dict[str, Any] | None = None,
) -> JournalEntry:
    pid, start = _dead_process()
    return _entry(op, realm, step=step, pid=pid, pid_start=start, params=params)


def _audit(paths: OwnedPaths) -> list[AuditEntry]:
    return list(AuditLog(paths).read())


def _with_op_id(paths: OwnedPaths, op_id: str) -> list[AuditEntry]:
    return [e for e in _audit(paths) if e.params.get("op_id") == op_id]


def _recovers(paths: OwnedPaths, op_id: str) -> list[AuditEntry]:
    return [e for e in _audit(paths) if e.params.get("recovers") == op_id]


def _only(outcomes: list[RecoveryOutcome]) -> RecoveryOutcome:
    assert len(outcomes) == 1, outcomes
    return outcomes[0]


def _state(paths: OwnedPaths, name: str = "arch") -> RealmState:
    return RealmStore(paths).load(RealmName(name)).state


@pytest.mark.fr("FR-122")
@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-124")
@pytest.mark.fr("NFR-043")
def test_a_dead_create_is_marked_broken_with_one_audit_entry_and_no_error_code(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead(step="pulled"))

    outcome = _only(recover(env, _Podman()))

    assert outcome.outcome == OUTCOME_BROKEN
    assert outcome.op == "create"
    assert outcome.op_id == OP_ID
    assert "partial artifacts are left" in outcome.reason
    assert _state(paths) is RealmState.BROKEN
    assert not journal.exists()
    [entry] = _audit(paths)
    assert (entry.op, entry.outcome, entry.error_code) == ("create", "broken", None)
    assert (entry.realm, entry.user, entry.ts) == ("arch", "tester", STARTED)
    assert entry.params["op_id"] == OP_ID
    assert entry.params["step"] == "pulled"
    assert "partial artifacts are left" in entry.params["reason"]


@pytest.mark.fr("FR-122")
@pytest.mark.fr("NFR-030")
def test_a_live_entry_is_never_touched(paths: OwnedPaths, env: OpEnv) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    own = os.getpid()
    journal = _write_journal(paths, _entry(pid=own, pid_start=process_start_ticks(own)))
    before = journal.read_bytes()

    outcome = _only(recover(env, _Podman()))

    assert outcome.outcome == OUTCOME_LIVE
    assert journal.read_bytes() == before
    assert _state(paths) is RealmState.CREATING
    assert _audit(paths) == []


@pytest.mark.fr("FR-122")
def test_a_reused_pid_with_another_start_time_counts_as_dead(paths: OwnedPaths, env: OpEnv) -> None:
    """This process's pid, but a start time that is not its own: the entry belonged to another."""
    _save(paths, _record(state=RealmState.CREATING))
    _write_journal(paths, _entry(pid=os.getpid(), pid_start=1))

    outcome = _only(recover(env, _Podman()))

    assert outcome.outcome == OUTCOME_BROKEN
    assert _state(paths) is RealmState.BROKEN


def _hostile(kind: str, bad: Path, tmp_path: Path) -> str:
    """Write a damaged journal file of `kind` at `bad`. Return a fragment of its expected reason."""
    if kind == "not-json":
        bad.write_bytes(b"{not json")
        return "not valid JSON"
    if kind == "not-object":
        bad.write_bytes(b"[1, 2]\n")
        return "not a JSON object"
    if kind == "wrong-schema":
        write_json_atomic(bad, {**_dead(realm="arch").to_json(), "schema_version": 99})
        return "schema_version"
    if kind == "name-mismatch":
        write_json_atomic(bad, _dead(realm="debian").to_json())
        return "names realm"
    if kind == "symlink":
        target = tmp_path / "elsewhere.json"
        write_json_atomic(target, _dead(realm="arch").to_json())
        bad.symlink_to(target)
        return "symbolic link"
    if kind == "fifo":
        os.mkfifo(bad)
        return "not a regular file"
    if kind == "nested-unclosed":
        bad.write_bytes(b"[" * 50_000)
        return "not valid JSON"
    if kind == "nested-huge":
        bad.write_bytes(b"[" * 200_000)
        return "larger than"
    if kind == "oversized":
        bad.write_bytes(b'{"schema_version": 1, "pad": "' + b"x" * 70_000 + b'"}')
        return "larger than"
    if kind == "naive-started":
        data = _dead(realm="arch").to_json()
        write_json_atomic(bad, {**data, "started": "2026-10-13T08:00:00"})
        return "no timezone"
    if kind == "deep-params":
        nested: Any = "leaf"
        for _ in range(40):
            nested = {"n": nested}
        write_json_atomic(bad, _dead(realm="arch", params={"deep": nested}).to_json())
        return "nested deeper than"
    raise AssertionError(kind)


@pytest.mark.fr("FR-122")
@pytest.mark.parametrize(
    "kind",
    [
        "not-json",
        "not-object",
        "wrong-schema",
        "name-mismatch",
        "symlink",
        "fifo",
        "nested-unclosed",
        "nested-huge",
        "oversized",
        "naive-started",
        "deep-params",
    ],
)
def test_a_damaged_journal_file_is_reported_with_a_remedy_and_kept_while_others_recover(
    paths: OwnedPaths, env: OpEnv, tmp_path: Path, kind: str
) -> None:
    _save(paths, _record("debian", state=RealmState.CREATING))
    _save(paths, _record("arch", state=RealmState.CREATING))
    _write_journal(paths, _dead(realm="debian"))
    bad = paths.journal / "arch.json"
    expected = _hostile(kind, bad, tmp_path)

    outcomes = recover(env, _Podman())

    by_realm = {o.realm: o for o in outcomes}
    assert by_realm["arch"].outcome == OUTCOME_CORRUPT
    assert expected in by_realm["arch"].reason
    assert str(bad) in by_realm["arch"].reason
    assert "rm " in by_realm["arch"].reason
    assert by_realm["debian"].outcome == OUTCOME_BROKEN
    assert os.path.lexists(bad)
    assert _state(paths, "debian") is RealmState.BROKEN
    assert _state(paths, "arch") is RealmState.CREATING
    assert [e.realm for e in _audit(paths)] == ["debian"]


@pytest.mark.fr("FR-122")
@pytest.mark.parametrize(
    ("error", "fragment"),
    [(RecursionError("deep"), "nested too deeply"), (MemoryError(), "too large to parse")],
)
def test_a_parse_that_recurses_or_exhausts_memory_is_reported_not_raised(
    paths: OwnedPaths,
    env: OpEnv,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    fragment: str,
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead())

    def fails(text: str) -> Any:
        raise error

    monkeypatch.setattr(journal_module, "json", SimpleNamespace(loads=fails))

    outcome = _only(recover(env, _Podman(None)))

    assert outcome.outcome == OUTCOME_CORRUPT
    assert fragment in outcome.reason
    assert journal.exists()
    assert _state(paths) is RealmState.CREATING


@pytest.mark.fr("FR-122")
def test_a_damaged_file_blocks_a_new_operation_and_names_its_path(paths: OwnedPaths) -> None:
    paths.journal.mkdir(parents=True, exist_ok=True)
    bad = paths.journal / "arch.json"
    bad.write_bytes(b"{not json")

    with pytest.raises(GinnError) as refused:
        Journal(paths).begin("install", RealmName("arch"))

    assert refused.value.code == "GINN-E-INTERNAL"
    assert refused.value.details["path"] == str(bad)
    remedy = refused.value.details["remedy"]
    assert str(bad) in remedy
    assert f"rm {bad}" in remedy
    assert bad.read_bytes() == b"{not json"


@pytest.mark.fr("FR-122")
def test_a_readable_entry_blocks_a_new_operation_as_recovery_pending(paths: OwnedPaths) -> None:
    _write_journal(paths, _dead(op="install", realm="arch"))

    with pytest.raises(GinnError) as refused:
        Journal(paths).begin("create", RealmName("arch"))

    assert refused.value.code == "GINN-E-RECOVERY-PENDING"
    assert refused.value.details["op"] == "install"


@pytest.mark.fr("FR-122")
@pytest.mark.fr("NFR-030")
@pytest.mark.fr("NFR-043")
def test_a_second_run_finds_nothing_and_writes_no_second_entry(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    _write_journal(paths, _dead())
    assert len(recover(env, _Podman())) == 1
    before = _audit(paths)

    assert recover(env, _Podman()) == []
    assert _audit(paths) == before
    assert _state(paths) is RealmState.BROKEN


@pytest.mark.fr("DR-033")
@pytest.mark.fr("FR-122")
def test_a_held_realm_lock_makes_the_entry_busy_and_leaves_it_alone(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead())

    with realm_lock(paths, RealmName("arch"), "rebuild"):
        outcome = _only(recover(env, _Podman()))

    assert outcome.outcome == OUTCOME_BUSY
    assert "rebuild in progress" in outcome.reason
    assert journal.exists()
    assert _state(paths) is RealmState.CREATING
    assert _audit(paths) == []


@pytest.mark.fr("DR-033")
@pytest.mark.fr("FR-122")
def test_an_entry_replaced_by_a_live_one_while_another_operation_holds_the_lock_is_busy(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the scan and the lock, a new live operation takes the realm and its entry."""
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead())
    live_pid = os.getpid()
    live = _entry(op="install", pid=live_pid, pid_start=process_start_ticks(live_pid))
    real_scan = env.journal.scan
    stack = ExitStack()

    def scan_then_replace() -> list[Any]:
        files = real_scan()
        stack.enter_context(realm_lock(paths, RealmName("arch"), "install"))
        write_json_atomic(journal, live.to_json())
        return files

    monkeypatch.setattr(env.journal, "scan", scan_then_replace)
    try:
        outcome = _only(recover(env, _Podman()))
    finally:
        stack.close()

    assert outcome.outcome == OUTCOME_BUSY
    assert "install in progress" in outcome.reason
    assert json.loads(journal.read_text(encoding="utf-8")) == live.to_json()
    assert _state(paths) is RealmState.CREATING
    assert _audit(paths) == []


@pytest.mark.fr("FR-122")
def test_a_file_removed_between_the_scan_and_the_lock_changes_nothing(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead())
    real_scan = env.journal.scan

    def scan_then_remove() -> list[Any]:
        files = real_scan()
        journal.unlink()
        return files

    monkeypatch.setattr(env.journal, "scan", scan_then_remove)

    outcome = _only(recover(env, _Podman()))

    assert outcome.outcome == OUTCOME_FINISHED
    assert "already removed" in outcome.reason
    assert _state(paths) is RealmState.CREATING
    assert _audit(paths) == []


@pytest.mark.fr("FR-122")
@pytest.mark.parametrize(
    ("step", "record_state", "container", "expected", "stops"),
    [
        # Before the final record save: the container is stopped, the record stays stopped.
        ("attached", RealmState.STOPPED, ContainerState.RUNNING, RealmState.STOPPED, [CID]),
        ("grants-reset", RealmState.STOPPED, ContainerState.EXITED, RealmState.STOPPED, []),
        # A record saved running is a completed start, even at an earlier step (item 3).
        ("started", RealmState.RUNNING, ContainerState.RUNNING, RealmState.RUNNING, []),
        ("started", RealmState.RUNNING, ContainerState.EXITED, RealmState.STOPPED, []),
        ("started", RealmState.FAILED, ContainerState.RUNNING, RealmState.STOPPED, [CID]),
        # At the final step the record was saved running, so it is matched to podman.
        ("running", RealmState.RUNNING, ContainerState.RUNNING, RealmState.RUNNING, []),
        ("running", RealmState.RUNNING, ContainerState.EXITED, RealmState.STOPPED, []),
        ("running", RealmState.STOPPED, ContainerState.RUNNING, RealmState.RUNNING, []),
        ("running", RealmState.RUNNING, None, RealmState.STOPPED, []),
    ],
)
def test_an_interrupted_start_stops_the_container_before_the_record_is_saved_running(
    paths: OwnedPaths,
    env: OpEnv,
    step: str,
    record_state: RealmState,
    container: ContainerState | None,
    expected: RealmState,
    stops: list[str],
) -> None:
    _save(paths, _record(state=record_state))
    _write_journal(paths, _dead(op="start", step=step))
    podman = _Podman(container)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert podman.stops == stops
    assert _state(paths) is expected
    [entry] = _audit(paths)
    assert (entry.op, entry.outcome, entry.error_code) == ("start", "recovered", None)
    assert entry.params["step"] == step


@pytest.mark.fr("FR-122")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    ("record_state", "container", "expected"),
    [
        (RealmState.RUNNING, ContainerState.EXITED, RealmState.STOPPED),
        (RealmState.RUNNING, ContainerState.RUNNING, RealmState.RUNNING),
        (RealmState.STOPPED, ContainerState.RUNNING, RealmState.RUNNING),
    ],
)
def test_an_interrupted_stop_is_matched_to_what_podman_reports(
    paths: OwnedPaths,
    env: OpEnv,
    record_state: RealmState,
    container: ContainerState,
    expected: RealmState,
) -> None:
    _save(paths, _record(state=record_state))
    _write_journal(paths, _dead(op="stop", step="begin"))
    podman = _Podman(container)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert podman.stops == []
    assert _state(paths) is expected


@pytest.mark.fr("FR-122")
def test_podman_inspect_failure_asks_ps_and_is_blocked_when_ps_fails_too(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _dead(op="stop", step="begin"))

    outcome = _only(recover(env, _Podman(ContainerState.EXITED, inspect_fails=True, ps_fails=True)))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert journal.exists()
    assert _state(paths) is RealmState.RUNNING
    assert _audit(paths) == []


@pytest.mark.fr("FR-122")
def test_podman_listing_without_the_container_means_it_is_stopped(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    _write_journal(paths, _dead(op="stop", step="begin"))

    outcome = _only(recover(env, _Podman(None, inspect_fails=True)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _state(paths) is RealmState.STOPPED


@pytest.mark.fr("FR-122")
def test_podman_listing_the_container_as_running_is_taken_from_the_listing(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.STOPPED))
    _write_journal(paths, _dead(op="stop", step="begin"))

    outcome = _only(recover(env, _Podman(ContainerState.RUNNING, inspect_fails=True)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _state(paths) is RealmState.RUNNING


@pytest.mark.fr("FR-122")
@pytest.mark.fr("DR-020")
def test_an_interrupted_delete_is_left_deleting_for_ginn_delete_to_finish(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.DELETING))
    journal = _write_journal(paths, _dead(op="delete", step="begin"))

    outcome = _only(recover(env, _Podman(ContainerState.EXITED)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert outcome.reason == "left deleting; run 'ginn delete' to finish"
    assert _state(paths) is RealmState.DELETING
    assert not journal.exists()
    [entry] = _audit(paths)
    assert (entry.op, entry.outcome) == ("delete", "recovered")


@pytest.mark.fr("FR-122")
def test_an_interrupted_install_on_a_stopped_realm_is_matched_to_podman(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.STOPPED))
    journal = _write_journal(paths, _dead(op="install", step="packages"))

    outcome = _only(recover(env, _Podman(ContainerState.EXITED)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _state(paths) is RealmState.STOPPED
    assert not journal.exists()
    [entry] = _audit(paths)
    assert (entry.op, entry.outcome, entry.params["step"]) == ("install", "recovered", "packages")


@pytest.mark.fr("FR-122")
def test_an_interrupted_install_on_a_creating_record_is_impossible_and_blocked(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead(op="install", step="packages"))

    outcome = _only(recover(env, _Podman(None)))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "cannot leave a realm in state creating" in outcome.reason
    assert journal.exists()
    assert _state(paths) is RealmState.CREATING
    assert _audit(paths) == []


@pytest.mark.fr("FR-122")
@pytest.mark.fr("DR-020")
def test_an_interrupted_update_is_marked_broken_and_a_stopped_one_is_matched_to_podman(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record("arch", state=RealmState.UPDATING, previous_state=RealmState.STOPPED))
    _save(paths, _record("debian", state=RealmState.STOPPED))
    _write_journal(paths, _dead(op="update", realm="arch", step="pulled"))
    _write_journal(paths, _dead(op="update", realm="debian", step="pulled"))

    outcomes = {o.realm: o for o in recover(env, _Podman(ContainerState.EXITED))}

    assert outcomes["arch"].outcome == OUTCOME_BROKEN
    assert _state(paths, "arch") is RealmState.BROKEN
    assert outcomes["debian"].outcome == OUTCOME_RECOVERED
    assert _state(paths, "debian") is RealmState.STOPPED


@pytest.mark.fr("FR-122")
def test_an_unknown_operation_on_a_running_realm_is_matched_to_podman(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    _write_journal(paths, _dead(op="frobnicate", step="begin"))

    outcome = _only(recover(env, _Podman(ContainerState.EXITED)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert outcome.op == "frobnicate"
    assert _state(paths) is RealmState.STOPPED


@pytest.mark.fr("FR-122")
def test_an_unknown_operation_on_a_creating_record_is_impossible_and_blocked(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead(op="frobnicate", step="begin"))

    outcome = _only(recover(env, _Podman(None)))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert journal.exists()
    assert _state(paths) is RealmState.CREATING


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-124")
def test_a_create_whose_record_already_reads_stopped_is_recovered_without_a_state_change(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.STOPPED))
    _write_journal(paths, _dead(step="stopped"))

    outcome = _only(recover(env, _Podman(ContainerState.EXITED)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _state(paths) is RealmState.STOPPED
    [entry] = _audit(paths)
    assert (entry.op, entry.outcome, entry.error_code) == ("create", "recovered", None)


@pytest.mark.fr("FR-122")
def test_a_create_in_a_state_it_cannot_leave_is_blocked(paths: OwnedPaths, env: OpEnv) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _dead(step="attached"))

    outcome = _only(recover(env, _Podman(ContainerState.RUNNING)))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert journal.exists()
    assert _state(paths) is RealmState.RUNNING


@pytest.mark.fr("FR-122")
@pytest.mark.fr("DR-034")
def test_an_interrupted_rebuild_returns_to_its_previous_state(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.REBUILDING, previous_state=RealmState.RUNNING))
    _write_journal(paths, _dead(op="rebuild", step="homed"))

    outcome = _only(recover(env, _Podman(ContainerState.RUNNING)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert "DR-034" in outcome.reason
    assert _state(paths) is RealmState.RUNNING


@pytest.mark.fr("FR-122")
def test_a_missing_record_means_nothing_to_revert_and_the_entry_is_settled(
    paths: OwnedPaths, env: OpEnv
) -> None:
    journal = _write_journal(paths, _dead(step="begin"))

    outcome = _only(recover(env, _Podman()))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert "no realm record exists" in outcome.reason
    assert not journal.exists()
    assert [e.outcome for e in _audit(paths)] == ["recovered"]


@pytest.mark.fr("FR-122")
def test_a_corrupt_realm_record_blocks_recovery_and_keeps_the_journal(
    paths: OwnedPaths, env: OpEnv
) -> None:
    paths.realms.mkdir(mode=0o700, parents=True, exist_ok=True)
    (paths.realms / "arch.json").write_bytes(b"garbage")
    journal = _write_journal(paths, _dead())

    outcome = _only(recover(env, _Podman()))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert outcome.reason.startswith("GINN-E-INTERNAL")
    assert journal.exists()
    assert _audit(paths) == []


@pytest.mark.fr("FR-124")
@pytest.mark.fr("NFR-043")
@pytest.mark.parametrize(
    ("audit_outcome", "error_code", "finished"),
    [
        ("ok", None, True),
        ("error", "GINN-E-NETWORK", True),
        ("recovered", None, True),
        ("broken", None, True),
        ("error", None, False),
        ("interrupted", None, False),
    ],
)
def test_an_existing_entry_is_never_written_again_and_only_an_unfinished_one_gets_a_recover_entry(
    paths: OwnedPaths,
    env: OpEnv,
    audit_outcome: str,
    error_code: str | None,
    finished: bool,
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    AuditLog(paths).append(
        AuditEntry(
            user="tester",
            realm="arch",
            op="create",
            outcome=audit_outcome,
            params={"op_id": OP_ID},
            error_code=error_code,
            ts=STARTED,
        )
    )
    journal = _write_journal(paths, _dead())
    before = _with_op_id(paths, OP_ID)

    outcome = _only(recover(env, _Podman(None)))

    assert not journal.exists()
    assert _with_op_id(paths, OP_ID) == before
    if finished:
        assert outcome.outcome == OUTCOME_FINISHED
        assert _recovers(paths, OP_ID) == []
        assert _state(paths) is RealmState.CREATING
        assert len(_audit(paths)) == 1
    else:
        assert outcome.outcome == OUTCOME_BROKEN
        [recovery] = _recovers(paths, OP_ID)
        assert recovery.op == "recover"
        assert recovery.outcome == "broken"
        assert recovery.params["op_id"] != OP_ID
        assert recovery.ts != STARTED
        assert _state(paths) is RealmState.BROKEN


@pytest.mark.fr("FR-124")
@pytest.mark.fr("NFR-030")
@pytest.mark.fr("NFR-043")
def test_a_crash_after_the_audit_entry_writes_no_second_entry_for_the_operation(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry and the record are written, then the process dies before the journal removal."""
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead())

    def die(entry: JournalEntry) -> None:
        raise RuntimeError("killed after the audit append")

    monkeypatch.setattr(env.journal, "finish", die)
    with pytest.raises(RuntimeError):
        recover(env, _Podman(None))
    assert journal.exists()
    assert len(_with_op_id(paths, OP_ID)) == 1

    monkeypatch.undo()
    outcome = _only(recover(env, _Podman(None)))

    assert outcome.outcome == OUTCOME_FINISHED
    assert not journal.exists()
    assert len(_audit(paths)) == 1
    assert _state(paths) is RealmState.BROKEN


@pytest.mark.fr("FR-124")
@pytest.mark.fr("NFR-043")
def test_an_operation_already_logged_by_a_recover_entry_only_loses_its_journal_file(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    log = AuditLog(paths)
    log.append(
        AuditEntry(
            user="tester",
            realm="arch",
            op="create",
            outcome="interrupted",
            params={"op_id": OP_ID},
            ts=STARTED,
        )
    )
    log.append(
        AuditEntry(
            user="tester",
            realm="arch",
            op="recover",
            outcome="broken",
            params={"op_id": "f" * 32, "recovers": OP_ID},
        )
    )
    journal = _write_journal(paths, _dead())
    before = _audit(paths)

    outcome = _only(recover(env, _Podman(None)))

    assert outcome.outcome == OUTCOME_FINISHED
    assert "already names this operation" in outcome.reason
    assert _audit(paths) == before
    assert not journal.exists()
    assert _state(paths) is RealmState.CREATING


@pytest.mark.fr("FR-122")
@pytest.mark.fr("NFR-043")
def test_a_damaged_audit_line_with_an_op_id_is_skipped_with_a_warning(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    _write_journal(paths, _dead())
    paths.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.audit_log.write_bytes(b'{"op_id": "zz", not json\nnot json at all\n')
    AuditLog(paths).append(
        AuditEntry(user="tester", realm="debian", op="create", outcome="ok", ts=STARTED)
    )

    outcome = _only(recover(env, _Podman(None)))

    assert outcome.outcome == OUTCOME_BROKEN
    assert len(outcome.warnings) == 1
    assert outcome.warnings[0].endswith("audit.log:1")
    assert _state(paths) is RealmState.BROKEN


@pytest.mark.fr("FR-122")
@pytest.mark.fr("NFR-043")
def test_the_audit_log_is_read_once_per_call_and_not_at_all_when_nothing_is_pending(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(paths, _record("arch", state=RealmState.CREATING))
    _save(paths, _record("debian", state=RealmState.CREATING))
    _write_journal(paths, _dead(realm="arch"))
    _write_journal(paths, _dead(realm="debian"))
    real_index = env.audit.index_by_op_id
    calls: list[int] = []

    def counting(op_id: str | None = None) -> Any:
        if op_id is None:
            calls.append(1)
        return real_index(op_id)

    monkeypatch.setattr(env.audit, "index_by_op_id", counting)
    assert len(recover(env, _Podman(None))) == 2
    assert len(calls) == 1

    calls.clear()
    assert recover(env, _Podman(None)) == []
    assert calls == []


@pytest.mark.fr("FR-122")
def test_an_unreadable_audit_log_blocks_every_dead_entry_and_keeps_the_journal(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead())

    def unreadable() -> Any:
        raise OSError(errno.EIO, "audit.log cannot be read")

    monkeypatch.setattr(env.audit, "index_by_op_id", unreadable)

    outcome = _only(recover(env, _Podman(None)))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "cannot be read" in outcome.reason
    assert journal.exists()
    assert _state(paths) is RealmState.CREATING


@pytest.mark.fr("FR-122")
def test_an_os_error_blocks_one_realm_and_the_other_realms_still_recover(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(paths, _record("arch", state=RealmState.CREATING))
    _save(paths, _record("debian", state=RealmState.CREATING))
    arch = _write_journal(paths, _dead(realm="arch"))
    _write_journal(paths, _dead(realm="debian"))
    real_load = env.journal.load

    def load_failing_for_arch(name: RealmName) -> Any:
        if name.value == "arch":
            raise OSError(errno.EIO, "disk error")
        return real_load(name)

    monkeypatch.setattr(env.journal, "load", load_failing_for_arch)

    outcomes = {o.realm: o for o in recover(env, _Podman(None))}

    assert outcomes["arch"].outcome == OUTCOME_BLOCKED
    assert arch.exists()
    assert outcomes["debian"].outcome == OUTCOME_BROKEN
    assert _state(paths, "debian") is RealmState.BROKEN


@pytest.mark.fr("FR-122")
def test_a_runtime_error_propagates_and_is_never_reported_as_a_settled_state(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    _write_journal(paths, _dead())

    def bug(name: RealmName) -> Any:
        raise RuntimeError("a bug, not a failure to recover")

    monkeypatch.setattr(env.journal, "load", bug)

    with pytest.raises(RuntimeError):
        recover(env, _Podman(None))


@pytest.mark.fr("FR-124")
@pytest.mark.parametrize(
    ("outcome", "error_code", "counts"),
    [
        ("ok", None, True),
        ("error", "GINN-E-NETWORK", True),
        ("recovered", None, True),
        ("broken", None, True),
        ("error", None, False),
        ("interrupted", None, False),
    ],
)
def test_is_finished_counts_ok_errors_with_a_code_and_the_two_recovery_outcomes(
    outcome: str, error_code: str | None, counts: bool
) -> None:
    assert is_finished(outcome, error_code) is counts


@pytest.mark.fr("FR-122")
def test_find_pending_lists_every_journal_file_by_name_and_an_empty_directory_gives_none(
    paths: OwnedPaths, env: OpEnv
) -> None:
    assert find_pending(env) == []
    own = os.getpid()
    _write_journal(paths, _dead(realm="debian"))
    _write_journal(paths, _entry(realm="arch", pid=own, pid_start=process_start_ticks(own)))
    (paths.journal / "fedora.json").write_bytes(b"{")

    files = find_pending(env)

    assert [f.realm for f in files] == ["arch", "debian", "fedora"]
    assert files[0].is_live() is True
    assert files[1].is_live() is False
    assert files[2].entry is None and files[2].problem


@pytest.mark.fr("DR-020")
@pytest.mark.fr("FR-122")
def test_a_broken_record_with_a_dead_update_is_settled_as_broken_with_no_state_write(
    paths: OwnedPaths, env: OpEnv
) -> None:
    """A crash between the record write (broken) and the audit append."""
    _save(paths, _record(state=RealmState.BROKEN))
    journal = _write_journal(paths, _dead(op="update", step="pulled"))

    outcome = _only(recover(env, _Podman(None)))

    assert outcome.outcome == OUTCOME_BROKEN
    assert not journal.exists()
    assert _state(paths) is RealmState.BROKEN
    [entry] = _audit(paths)
    assert (entry.op, entry.outcome, entry.error_code) == ("update", "broken", None)


class _StateAtStop(_Podman):
    """_Podman that records the stored state of the realm at each stop (NFR-030 order)."""

    def __init__(self, paths: OwnedPaths, container: ContainerState | None) -> None:
        super().__init__(container)
        self.paths = paths
        self.states_at_stop: list[RealmState] = []

    def stop(self, container_id: str, timeout_s: int) -> None:
        self.states_at_stop.append(_state(self.paths))
        super().stop(container_id, timeout_s)


class _StopFails(_Podman):
    """_Podman whose stop fails, as `podman stop` does when it exits non-zero."""

    def stop(self, container_id: str, timeout_s: int) -> None:
        self.stops.append(container_id)
        raise make("GINN-E-INTERNAL")


def _services_on_control(paths: OwnedPaths, *, is_fifo: bool) -> Path:
    """The runsv control path of the realm `arch`, a FIFO or a directory (runsv(8))."""
    service = service_dir(paths, RealmName("arch"))
    (service / "supervise").mkdir(parents=True)
    control = service / "supervise" / "control"
    if is_fifo:
        os.mkfifo(control)
    else:
        control.mkdir()
    return control


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-122")
@pytest.mark.parametrize(
    ("op", "state", "previous"),
    [
        ("create", RealmState.CREATING, None),
        ("create", RealmState.BROKEN, None),
        ("update", RealmState.UPDATING, RealmState.RUNNING),
        ("update", RealmState.BROKEN, None),
    ],
)
def test_a_dead_create_or_update_stops_its_running_container_before_the_record_reads_broken(
    paths: OwnedPaths,
    env: OpEnv,
    op: str,
    state: RealmState,
    previous: RealmState | None,
) -> None:
    _save(paths, _record(state=state, previous_state=previous))
    _write_journal(paths, _dead(op=op, step="pulled"))
    podman = _StateAtStop(paths, ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BROKEN
    assert podman.stops == [CID]
    assert podman.states_at_stop == [state]
    assert podman.container is ContainerState.EXITED
    assert _state(paths) is RealmState.BROKEN
    [entry] = _audit(paths)
    assert (entry.op, entry.outcome, entry.error_code) == (op, "broken", None)


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-122")
@pytest.mark.parametrize("container", [ContainerState.EXITED, None])
def test_a_dead_update_whose_container_does_not_run_is_marked_broken_with_no_stop(
    paths: OwnedPaths, env: OpEnv, container: ContainerState | None
) -> None:
    _save(paths, _record(state=RealmState.UPDATING, previous_state=RealmState.RUNNING))
    _write_journal(paths, _dead(op="update", step="pulled"))
    podman = _Podman(container)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BROKEN
    assert podman.stops == []
    assert _state(paths) is RealmState.BROKEN


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-081")
def test_a_dead_services_on_update_tells_runsv_down_before_it_stops_the_container(
    paths: OwnedPaths, env: OpEnv
) -> None:
    """runsv restarts a container that exits while it wants the service up, so `d` comes first."""
    base = _record(state=RealmState.UPDATING, previous_state=RealmState.RUNNING)
    record = replace(base, settings=replace(base.settings, services=True), services_at_create=True)
    _save(paths, record)
    _write_journal(paths, _dead(op="update", step="pulled"))
    control = _services_on_control(paths, is_fifo=True)
    reader = os.open(control, os.O_RDONLY | os.O_NONBLOCK)
    try:
        podman = _StateAtStop(paths, ContainerState.RUNNING)
        outcome = _only(recover(env, podman))
        assert os.read(reader, 8) == b"d"
    finally:
        os.close(reader)

    assert outcome.outcome == OUTCOME_BROKEN
    assert podman.stops == [CID]
    assert _state(paths) is RealmState.BROKEN


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-081")
def test_a_services_on_update_whose_runsv_cannot_be_told_down_is_blocked_and_stops_nothing(
    paths: OwnedPaths, env: OpEnv
) -> None:
    base = _record(state=RealmState.UPDATING, previous_state=RealmState.RUNNING)
    record = replace(base, settings=replace(base.settings, services=True), services_at_create=True)
    _save(paths, record)
    journal = _write_journal(paths, _dead(op="update", step="pulled"))
    _services_on_control(paths, is_fifo=False)
    podman = _StateAtStop(paths, ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "could not be told to stop" in outcome.reason
    assert podman.stops == []
    assert _state(paths) is RealmState.UPDATING
    assert journal.exists()
    assert _audit(paths) == []


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-122")
def test_a_failed_stop_before_a_dead_create_is_blocked_and_keeps_the_record_and_journal(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead(op="create", step="pulled"))
    podman = _StopFails(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert outcome.reason.startswith("GINN-E-INTERNAL")
    assert podman.stops == [CID]
    assert _state(paths) is RealmState.CREATING
    assert journal.exists()
    assert _audit(paths) == []


@pytest.mark.fr("NFR-030")
@pytest.mark.fr("FR-122")
def test_a_dead_create_whose_container_cannot_be_probed_is_blocked_and_nothing_is_stopped(
    paths: OwnedPaths, env: OpEnv
) -> None:
    """Neither inspect nor the listing answers, so the container is not known to be stopped."""
    _save(paths, _record(state=RealmState.CREATING))
    journal = _write_journal(paths, _dead(op="create", step="pulled"))
    podman = _Podman(ContainerState.RUNNING, inspect_fails=True, ps_fails=True)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert podman.stops == []
    assert _state(paths) is RealmState.CREATING
    assert journal.exists()
    assert _audit(paths) == []


def _write_audit_after_the_pre_lock_read(
    env: OpEnv, monkeypatch: pytest.MonkeyPatch, entry: AuditEntry
) -> None:
    """Write `entry` after recovery's full audit read, so the pre-lock index cannot see it."""
    real = env.audit.index_by_op_id
    written: list[bool] = []

    def lookup(op_id: str | None = None) -> Any:
        result = real(op_id)
        if op_id is None and not written:
            written.append(True)
            AuditLog(env.paths).append(entry)
        return result

    monkeypatch.setattr(env.audit, "index_by_op_id", lookup)


@pytest.mark.fr("FR-124")
@pytest.mark.fr("NFR-043")
def test_an_entry_written_in_the_window_before_the_lock_is_never_repeated_as_an_original(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier recovery wrote the entry and died before removing the journal file."""
    _save(paths, _record(state=RealmState.CREATING))
    _write_journal(paths, _dead())
    _write_audit_after_the_pre_lock_read(
        env,
        monkeypatch,
        AuditEntry(
            user="tester",
            realm="arch",
            op="create",
            outcome="broken",
            params={"op_id": OP_ID},
            ts=STARTED,
        ),
    )

    outcome = _only(recover(env, _Podman(None)))

    assert outcome.outcome == OUTCOME_FINISHED
    assert len(_with_op_id(paths, OP_ID)) == 1
    assert _recovers(paths, OP_ID) == []
    assert _state(paths) is RealmState.CREATING


@pytest.mark.fr("FR-124")
@pytest.mark.fr("NFR-043")
def test_an_unfinished_entry_written_in_that_window_gets_one_recover_entry_only(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(paths, _record(state=RealmState.CREATING))
    _write_journal(paths, _dead())
    _write_audit_after_the_pre_lock_read(
        env,
        monkeypatch,
        AuditEntry(
            user="tester",
            realm="arch",
            op="create",
            outcome="interrupted",
            params={"op_id": OP_ID},
            ts=STARTED,
        ),
    )

    outcome = _only(recover(env, _Podman(None)))

    assert outcome.outcome == OUTCOME_BROKEN
    assert len(_with_op_id(paths, OP_ID)) == 1
    [recovery] = _recovers(paths, OP_ID)
    assert recovery.outcome == "broken"
    assert _state(paths) is RealmState.BROKEN


# ---- T5.3 review fix: an interrupted install or remove replays the Install Manifest --------

PACKAGES = ["htop", "gimp"]


def _with_manifest(record: RealmRecord, *names: str) -> RealmRecord:
    record.manifest = [
        ManifestEntry(name=name, source=InstallSource.REPO, method=InstallMethod.GINN)
        for name in names
    ]
    return record


def _manifest_names(paths: OwnedPaths) -> list[str]:
    return [entry.name for entry in RealmStore(paths).load(RealmName("arch")).manifest]


@pytest.mark.fr("FR-040")
@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-122")
def test_an_install_interrupted_after_its_package_step_replays_the_manifest_write(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(
        paths, _dead(op="install", step="installed", params={"packages": PACKAGES})
    )

    outcome = _only(recover(env, _Podman(ContainerState.RUNNING)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert "Install Manifest now matches the interrupted install" in outcome.reason
    assert _manifest_names(paths) == ["gimp", "htop"]
    assert _state(paths) is RealmState.RUNNING
    assert not journal.exists()
    [entry] = _audit(paths)
    assert (entry.op, entry.outcome) == ("install", "recovered")


@pytest.mark.fr("FR-040")
@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-122")
def test_an_install_already_past_the_manifest_write_changes_no_manifest(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _with_manifest(_record(state=RealmState.RUNNING), "htop"))
    _write_journal(paths, _dead(op="install", step="recorded", params={"packages": ["htop"]}))

    outcome = _only(recover(env, _Podman(ContainerState.RUNNING)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert "Install Manifest" not in outcome.reason
    assert _manifest_names(paths) == ["htop"]


@pytest.mark.fr("FR-040")
@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-122")
def test_an_install_interrupted_before_its_package_step_leaves_the_manifest_as_it_was(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    _write_journal(paths, _dead(op="install", step="refreshed", params={"packages": PACKAGES}))

    outcome = _only(recover(env, _Podman(ContainerState.RUNNING)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest_names(paths) == []


@pytest.mark.fr("FR-041")
@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-122")
def test_a_remove_interrupted_after_its_package_step_drops_the_removed_entries(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _with_manifest(_record(state=RealmState.RUNNING), "htop", "gimp"))
    _write_journal(paths, _dead(op="remove", step="removed", params={"packages": ["htop"]}))

    outcome = _only(recover(env, _Podman(ContainerState.RUNNING)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert "Install Manifest now matches the interrupted remove" in outcome.reason
    assert _manifest_names(paths) == ["gimp"]


@pytest.mark.fr("FR-040")
@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-122")
def test_an_entry_with_no_package_list_is_blocked_and_its_journal_kept(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(
        paths, _dead(op="install", step="installed", params={"packages": "htop"})
    )

    outcome = _only(recover(env, _Podman(ContainerState.RUNNING)))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "no valid package list" in outcome.reason
    assert journal.exists()
    assert _manifest_names(paths) == []


@pytest.mark.fr("FR-043")
@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-122")
def test_an_aur_install_interrupted_after_its_build_only_removal_replays_aur_entries(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(
        paths, _dead(op="install", step="removed", params={"packages": ["yay-bin"], "aur": True})
    )

    outcome = _only(recover(env, _Podman(ContainerState.RUNNING)))

    assert outcome.outcome == OUTCOME_RECOVERED
    [entry] = RealmStore(paths).load(RealmName("arch")).manifest
    assert (entry.name, entry.source.value, entry.method.value) == ("yay-bin", "aur", "ginn")
    assert not journal.exists()


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_install_interrupted_before_its_removal_leaves_the_manifest_as_it_was(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    params = {"packages": ["yay-bin"], "aur": True, "added": ["git"]}
    _write_journal(paths, _dead(op="install", step="toolchain", params=params))

    outcome = _only(recover(env, _Podman(ContainerState.RUNNING)))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest_names(paths) == []


AUR_ADDED = [
    "fakeroot",
    "gcc",
    "git",
    "go",
    "glibc",
    "isl",
    "libyaml",
    "make",
    "perl",
    "pcre2",
    "yay-bin",
    "zlib",
]
AUR_KEEP = ["glibc", "libyaml", "perl", "yay-bin", "zlib"]
AUR_BUILD_ONLY = ["fakeroot", "gcc", "git", "go", "isl", "make", "pcre2"]


def _aur_entry(step: str, *, params: dict[str, Any] | None = None) -> JournalEntry:
    base = {"packages": ["yay-bin"], "aur": True}
    return _dead(op="install", step=step, params={**base, **(params or {})})


@pytest.mark.fr("FR-043")
@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-122")
def test_an_aur_build_interrupted_at_built_replays_aur_entries_and_removes_the_build_only_set(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(
        paths,
        _aur_entry("built", params={"removable": AUR_BUILD_ONLY}),
    )
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    [entry] = RealmStore(paths).load(RealmName("arch")).manifest
    assert (entry.name, entry.source.value, entry.method.value) == ("yay-bin", "aur", "ginn")
    [(container, argv, user)] = podman.execs
    assert container == CID and user == "0:0"
    assert argv == ("pacman", "--remove", "--noconfirm", *AUR_BUILD_ONLY)
    assert not journal.exists()


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_build_interrupted_before_built_removes_every_added_package_and_keeps_the_manifest(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    _write_journal(paths, _aur_entry("toolchain", params={"added": AUR_ADDED[:-2]}))
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest_names(paths) == []
    [(_, argv, _)] = podman.execs
    assert argv == ("pacman", "--remove", "--noconfirm", *sorted(AUR_ADDED[:-2]))


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_a_build_only_set_with_the_container_stopped_is_reported_left_installed(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    _write_journal(paths, _aur_entry("built", params={"removable": AUR_BUILD_ONLY}))
    podman = _Podman(ContainerState.EXITED)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert podman.execs == []
    assert "left installed, since the realm is not running" in outcome.reason
    assert "fakeroot" in outcome.reason


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_a_build_only_removal_pacman_refuses_is_reported_and_the_entry_is_settled(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _aur_entry("built", params={"removable": AUR_BUILD_ONLY}))
    podman = _Podman(ContainerState.RUNNING, exec_exit=1)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert "left installed, since pacman refused the removal" in outcome.reason
    assert not journal.exists()


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_build_at_removed_replays_the_manifest_and_removes_nothing_more(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    _write_journal(paths, _aur_entry("removed", params={"removable": AUR_BUILD_ONLY}))
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest_names(paths) == ["yay-bin"]
    assert podman.execs == []


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_build_without_its_package_set_is_blocked_and_kept(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _aur_entry("git"))
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "added set is not valid" in outcome.reason
    assert podman.execs == []
    assert journal.exists()
    assert _manifest_names(paths) == []


class _Listing(_Podman):
    """_Podman whose package query answers `listing`, the realm's installed names (FR-043)."""

    def __init__(self, container: ContainerState | None, listing: str, exit_code: int = 0) -> None:
        super().__init__(container)
        self.listing = listing
        self.query_exit = exit_code

    def exec(
        self,
        container_id: str,
        argv: Any,
        *,
        user: str | None = None,
        **kwargs: Any,
    ) -> ExecResult:
        if tuple(argv) == tuple(query_argv()):
            self.execs.append((container_id, tuple(argv), user))
            return ExecResult(self.query_exit, self.listing, "")
        return super().exec(container_id, argv, user=user, **kwargs)


class _LimitedRemoval(_Podman):
    """_Podman whose build-only removal stops at its output limit (FR-043, FR-122). It keeps the
    bounds each exec was given, in `bounds`."""

    def __init__(self, container: ContainerState | None) -> None:
        super().__init__(container)
        self.bounds: list[tuple[object, object, object]] = []

    def exec(
        self,
        container_id: str,
        argv: Any,
        *,
        user: str | None = None,
        **kwargs: Any,
    ) -> ExecResult:
        if tuple(argv)[:2] == ("pacman", "--remove"):
            self.bounds.append(
                (kwargs.get("timeout_s"), kwargs.get("max_output"), kwargs.get("stdout_tail"))
            )
            err = make("GINN-E-INTERNAL")
            err.details["reason"] = "printed more than 1048576 bytes"
            raise err
        return super().exec(container_id, argv, user=user, **kwargs)


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_build_that_overflowed_at_built_removes_nothing_and_records_the_requested_names(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _aur_entry("built", params={PARAMS_OVERFLOW: True}))
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest_names(paths) == ["yay-bin"]
    assert podman.execs == []
    assert "the build-only packages are not known (the journal overflowed)" in outcome.reason
    assert not journal.exists()


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_build_that_overflowed_before_built_names_the_packages_it_does_not_know(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _aur_entry("toolchain", params={PARAMS_OVERFLOW: True}))
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest_names(paths) == []
    assert podman.execs == []
    assert "the packages added so far are not known (the journal overflowed)" in outcome.reason
    assert not journal.exists()


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_build_interrupted_while_upgrading_records_the_requested_names_found_installed(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _aur_entry("upgrading", params={"added": AUR_ADDED}))
    podman = _Listing(ContainerState.RUNNING, "glibc\nperl\nyay-bin\n")

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest_names(paths) == ["yay-bin"]
    [(_, argv, user)] = podman.execs
    assert argv == tuple(query_argv()) and user == "0:0"
    assert "the build-only packages are not removed" in outcome.reason
    assert not journal.exists()


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_upgrading_with_the_realm_stopped_records_the_names_unchecked(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _aur_entry("upgrading", params={"added": AUR_ADDED}))
    podman = _Podman(ContainerState.EXITED)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert podman.execs == []
    # The upgrade may have run, so the requested names are recorded without a check (FR-040).
    assert _manifest_names(paths) == ["yay-bin"]
    [entry] = RealmStore(paths).load(RealmName("arch")).manifest
    assert (entry.source.value, entry.method.value) == ("aur", "ginn")
    assert "recorded without a check, since the realm is not running: yay-bin" in outcome.reason
    assert "not removed" in outcome.reason
    assert not journal.exists()


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_build_whose_upgrading_query_exits_non_zero_keeps_the_entry(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _aur_entry("upgrading", params={"added": AUR_ADDED}))
    podman = _Listing(ContainerState.RUNNING, "", exit_code=1)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "(the query exited with status 1), so the entry is kept" in outcome.reason
    assert [argv for _, argv, _ in podman.execs] == [tuple(query_argv())]
    assert _manifest_names(paths) == []
    assert journal.exists()


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_a_built_set_written_after_an_overflow_clears_the_marker_and_recovery_honours_it(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    paths.journal.mkdir(parents=True, exist_ok=True)
    journal = Journal(paths)
    huge = [f"package-{index:05d}-{'x' * 30}" for index in range(2000)]
    interrupted = _aur_entry("dependencies")
    overflowed = journal.step(interrupted, "toolchain", {"added": huge}, drop=("added",))
    assert overflowed.params.get(PARAMS_OVERFLOW) is True
    built = journal.step(
        overflowed,
        "built",
        {"removable": AUR_BUILD_ONLY},
        drop=("added", PARAMS_OVERFLOW),
    )
    assert PARAMS_OVERFLOW not in built.params
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest_names(paths) == ["yay-bin"]
    [(_, argv, _)] = podman.execs
    assert argv == ("pacman", "--remove", "--noconfirm", *AUR_BUILD_ONLY)
    assert not (paths.journal / "arch.json").exists()


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_build_with_an_invalid_requested_name_is_blocked_and_kept(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(
        paths,
        _aur_entry("built", params={"removable": AUR_BUILD_ONLY, "packages": ["yay bin"]}),
    )
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "no valid package list" in outcome.reason
    assert podman.execs == []
    assert journal.exists()
    assert _manifest_names(paths) == []


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_build_with_an_invalid_added_set_while_upgrading_is_blocked_and_kept(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _aur_entry("upgrading", params={"added": ["bad name"]}))
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "added set is not valid" in outcome.reason
    # The realm is only queried (read-only); nothing is removed.
    assert [argv for _, argv, _ in podman.execs] == [tuple(query_argv())]
    assert journal.exists()


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_a_build_only_removal_that_stops_at_its_limit_is_reported_left_installed(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _aur_entry("built", params={"removable": AUR_BUILD_ONLY}))
    podman = _LimitedRemoval(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert "left installed, since the removal stopped at its limit" in outcome.reason
    assert podman.bounds == [(_REMOVE_TIMEOUT_S, MAX_OUTPUT_CHARS, True)]
    assert not journal.exists()


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-122")
@pytest.mark.parametrize("step", ["listed", "applied"])
def test_an_install_interrupted_during_the_menu_sync_is_settled_and_keeps_its_manifest(
    paths: OwnedPaths, env: OpEnv, step: str
) -> None:
    _save(paths, _with_manifest(_record(state=RealmState.RUNNING), "htop"))
    journal = _write_journal(
        paths, _dead(op="install", step=step, params={"packages": ["htop"], "aur": False})
    )
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest_names(paths) == ["htop"]
    assert podman.execs == []
    assert not journal.exists()


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_install_interrupted_during_the_menu_sync_removes_nothing(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _save(paths, _record(state=RealmState.RUNNING))
    journal = _write_journal(paths, _aur_entry("listed"))
    podman = _Podman(ContainerState.RUNNING)

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest_names(paths) == []
    assert podman.execs == []
    assert not journal.exists()


@pytest.mark.fr("FR-067")
@pytest.mark.fr("FR-122")
def test_an_interrupted_start_leaves_the_filter_it_joined_and_the_filter_stops(
    paths: OwnedPaths, env: OpEnv
) -> None:
    """A start killed after it joined the filter is settled to stopped, and recovery leaves the
    filter (FR-067). With no member left, the filter stops. The helper here is a real child in its
    own session, so only that group is signalled."""
    basic = _record(state=RealmState.STOPPED)
    _save(paths, replace(basic, settings=replace(basic.settings, tier=Tier.DEVELOPMENT)))
    child = subprocess.Popen(  # noqa: S603 - a fixed argv that sleeps
        [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
    )
    try:
        proc = Proc.of(child.pid)
        assert proc is not None
        filter_record = agent.AgentRecord(
            proc=proc,
            upstream="/up",
            listen=str(paths.runtime / "agent" / "ssh.sock"),
            members=("arch",),
        )
        write_json_atomic(agent_state_path(paths), filter_record.to_dict())
        _write_journal(paths, _dead(op="start", step="started"))
        outcome = _only(recover(env, _Podman(ContainerState.EXITED)))
        assert outcome.outcome == OUTCOME_RECOVERED
        assert _state(paths) is RealmState.STOPPED
        stored = read_agent_record(paths)
        assert stored.members == () and stored.proc is None
        assert child.wait(timeout=10) is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


class _Generations:
    """A podman stand-in that lists containers by their labels, as the real `ps` does (DR-034).

    `labels` maps a container id to its labels, and `states` to its state. Stops and removals are
    recorded, so a test can read which containers recovery touched.
    """

    def __init__(
        self, labels: dict[str, dict[str, str]], states: dict[str, ContainerState]
    ) -> None:
        self.labels = labels
        self.states = states
        self.stops: list[str] = []
        self.removed: list[str] = []

    def ps(self, label_filter: Any = None) -> list[ContainerInfo]:
        wanted = dict(label_filter or {})
        return [
            replace(_info(cid, self.states[cid]), labels=dict(labels))
            for cid, labels in self.labels.items()
            if all(labels.get(key) == value for key, value in wanted.items())
        ]

    def inspect(self, container_id: str) -> ContainerInfo:
        if container_id not in self.labels:
            raise make("GINN-E-INTERNAL")
        return _info(container_id, self.states[container_id])

    def stop(self, container_id: str, timeout_s: int) -> None:
        self.stops.append(container_id)
        if self.states[container_id] is ContainerState.RUNNING:
            self.states[container_id] = ContainerState.EXITED

    def rm(self, container_id: str, force: bool) -> None:
        self.removed.append(container_id)
        del self.labels[container_id]
        del self.states[container_id]


def _labelled(realm: str, generation: int) -> dict[str, str]:
    return {LABEL_REALM: realm, LABEL_GENERATION: str(generation)}


@pytest.mark.fr("DR-034")
def test_an_interrupted_rebuild_removes_its_orphan_new_root_and_returns_to_stopped(
    paths: OwnedPaths, env: OpEnv
) -> None:
    record = replace(
        _record(state=RealmState.REBUILDING, previous_state=RealmState.STOPPED), generation=1
    )
    _save(paths, record)
    _write_journal(
        paths,
        _dead(op="rebuild", step="stopped", params={"generation": 1, "new_generation": 2}),
    )
    podman = _Generations(
        {CID: _labelled("arch", 1), "orphan": _labelled("arch", 2)},
        {CID: ContainerState.EXITED, "orphan": ContainerState.RUNNING},
    )

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert "DR-034" in outcome.reason
    assert podman.stops == ["orphan"]
    assert podman.removed == ["orphan"]
    assert CID in podman.labels
    assert _state(paths) is RealmState.STOPPED
    assert RealmStore(paths).load(RealmName("arch")).generation == 1


@pytest.mark.fr("DR-034")
@pytest.mark.fr("FR-122")
def test_a_runit_directory_that_cannot_be_removed_blocks_an_interrupted_rebuild_before_the_swap(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removal of the runit directory that fails with an OSError is a blocked outcome, as the
    other failures of a recovery are. The journal entry is kept and the record does not move."""
    base = _record(state=RealmState.REBUILDING, previous_state=RealmState.STOPPED)
    record = replace(base, generation=1, services_at_create=False, building_generation=2)
    _save(paths, record)
    journal = _write_journal(
        paths,
        _dead(op="rebuild", step="started", params={"generation": 1, "new_generation": 2}),
    )

    def denied(_paths: OwnedPaths, _name: RealmName) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("ginn.ops.recovery.remove_service", denied)
    podman = _Generations(
        {CID: _labelled("arch", 1), "orphan": _labelled("arch", 2)},
        {CID: ContainerState.EXITED, "orphan": ContainerState.RUNNING},
    )

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "runit service" in outcome.reason
    assert journal.exists()
    assert _state(paths) is RealmState.REBUILDING


@pytest.mark.fr("DR-034")
@pytest.mark.fr("FR-122")
def test_a_runit_directory_that_cannot_be_stopped_after_the_swap_blocks_the_entry(
    paths: OwnedPaths, env: OpEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the swap, a `d` that runsv cannot be sent is an OSError too, and it blocks the entry
    the same way. The record names the new generation and stays where the swap left it."""
    record = replace(_record(state=RealmState.STOPPED), generation=2, services_at_create=False)
    _save(paths, record)
    journal = _write_journal(
        paths,
        _dead(op="rebuild", step="swapping", params={"generation": 1, "new_generation": 2}),
    )

    def denied(_directory: Path) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("ginn.ops.recovery.signal_runsv_down", denied)
    podman = _Generations({CID: _labelled("arch", 2)}, {CID: ContainerState.EXITED})

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_BLOCKED
    assert journal.exists()
    assert _state(paths) is RealmState.STOPPED
    assert RealmStore(paths).load(RealmName("arch")).generation == 2


@pytest.mark.fr("DR-034")
@pytest.mark.fr("FR-081")
def test_an_interrupted_services_on_rebuild_tells_runsv_to_stop_its_orphan_before_removing_it(
    paths: OwnedPaths, env: OpEnv
) -> None:
    """The orphan of a services-on rebuild runs under its runit service, which restarts a container
    that exits while runsv wants it up. Recovery writes `d` to runsv's control FIFO first, and then
    removes the orphan and returns the record to stopped (ADR-003)."""
    base = _record(state=RealmState.REBUILDING, previous_state=RealmState.STOPPED)
    record = replace(
        base,
        settings=replace(base.settings, services=True),
        building_container_id="orphan",
        building_generation=2,
    )
    _save(paths, record)
    _write_journal(
        paths,
        _dead(op="rebuild", step="started", params={"generation": 1, "new_generation": 2}),
    )
    service = service_dir(paths, RealmName("arch"))
    (service / "supervise").mkdir(parents=True)
    control = service / "supervise" / "control"
    os.mkfifo(control)
    reader = os.open(control, os.O_RDONLY | os.O_NONBLOCK)
    try:
        podman = _Generations(
            {CID: _labelled("arch", 1), "orphan": _labelled("arch", 2)},
            {CID: ContainerState.EXITED, "orphan": ContainerState.RUNNING},
        )
        outcome = _only(recover(env, podman))
        assert os.read(reader, 8) == b"d"
    finally:
        os.close(reader)

    assert outcome.outcome == OUTCOME_RECOVERED
    assert podman.stops == ["orphan"]
    assert podman.removed == ["orphan"]
    assert _state(paths) is RealmState.STOPPED
    stored = RealmStore(paths).load(RealmName("arch"))
    assert stored.building_container_id is None
    assert stored.building_generation is None


@pytest.mark.fr("DR-034")
def test_a_rebuild_saved_at_the_new_generation_removes_the_old_root_and_settles_by_podman(
    paths: OwnedPaths, env: OpEnv
) -> None:
    record = replace(_record(state=RealmState.STOPPED, container_id="new-root"), generation=2)
    _save(paths, record)
    _write_journal(
        paths,
        _dead(op="rebuild", step="swapping", params={"generation": 1, "new_generation": 2}),
    )
    podman = _Generations(
        {CID: _labelled("arch", 1), "new-root": _labelled("arch", 2)},
        {CID: ContainerState.EXITED, "new-root": ContainerState.EXITED},
    )

    outcome = _only(recover(env, podman))

    assert outcome.outcome == OUTCOME_RECOVERED
    assert podman.removed == [CID]
    assert list(podman.labels) == ["new-root"]
    assert RealmStore(paths).load(RealmName("arch")) == record
