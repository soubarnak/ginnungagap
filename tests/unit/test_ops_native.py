# SPDX-License-Identifier: Apache-2.0
"""The explicit-package diff of a `--root` session (T5.2, ops/native.py, FR-042 AC-1, DR-007).

The fake answers `list-explicit` in order, so the diff sees the packages before and after the
session. No test runs a package manager. Covered: a new package is recorded as native, nothing is
written when nothing is new, every failure is a warning and the exec status comes back unchanged,
a ginn entry keeps its method, and a held realm lock is a warning.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ginn.distros import Verb, load
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
from ginn.errors import Warning as GinnWarning
from ginn.ops._mutating import OpEnv
from ginn.ops.native import OP_RECORD_NATIVE, UNRECORDED_CODE, root_session_recorder
from ginn.podman.adapter import CreateSpec
from ginn.podman.models import ExecResult
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.store.locks import realm_lock
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from tests.fakes.clock import FakeClock
from tests.fakes.fake_podman import FakePodman

DIGEST = "registry.example/arch@sha256:" + "ab" * 32
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
REALM = "arch"
LIST = tuple(load("arch").argv(Verb.LIST_EXPLICIT))


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
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
def env(paths: OwnedPaths, tmp_path: Path) -> OpEnv:
    return OpEnv.for_paths(paths, "alice", root_dir=tmp_path, clock=FakeClock())


@pytest.fixture
def fake() -> FakePodman:
    return FakePodman()


def _running(env: OpEnv, fake: FakePodman, manifest: list[ManifestEntry] | None = None) -> str:
    """A running arch realm: its record, its container, and the container id."""
    labels = {LABEL_REALM: REALM, LABEL_DISTRO: "arch", LABEL_GENERATION: "1"}
    spec = CreateSpec(
        name=f"ginn-{REALM}-g1", image_digest_ref=DIGEST, network="host", labels=labels
    )
    cid = fake.create(spec)
    fake.start(cid)
    settings = RealmSettings(
        name=RealmName(REALM),
        distro=DistroId("arch"),
        tier=Tier.BASIC,
        services=False,
        autostart=False,
        everything=False,
    )
    record = RealmRecord(
        settings=settings,
        state=RealmState.RUNNING,
        previous_state=None,
        image_ref=DIGEST,
        image_digest=DIGEST.partition("@")[2],
        container_id=cid,
        generation=1,
        created_at=T0,
        updated_at=T0,
        manifest=list(manifest or []),
    )
    (env.paths.homes / REALM).mkdir(parents=True, exist_ok=True)
    RealmStore(env.paths, env.clock).save(record)
    fake.calls.clear()
    fake.exec_calls.clear()
    return cid


def _stored(env: OpEnv) -> RealmRecord:
    return RealmStore(env.paths, env.clock).load(RealmName(REALM))


def _audit(env: OpEnv) -> list[Any]:
    return [entry for entry in env.audit.read() if entry.op == OP_RECORD_NATIVE]


def _answer_list(fake: FakePodman, *answers: str) -> None:
    """Queue `list-explicit` answers: one per call, in order (the diff reads twice)."""
    fake.exec_sequences[LIST] = [ExecResult(0, text, "") for text in answers]


def _session(env: OpEnv, fake: FakePodman, status: int = 0) -> tuple[int, list[GinnWarning]]:
    """Run the recorder around a run that returns `status`. Return the status and the notices."""
    notices: list[GinnWarning] = []
    record = _stored(env)
    recorder = root_session_recorder(env, fake, notify=notices.append)
    result = recorder(record, record.container_id or "", lambda: status)
    return result, notices


@pytest.mark.fr("FR-042")
@pytest.mark.fr("DR-007")
def test_a_package_the_root_session_installs_is_recorded_as_native(
    env: OpEnv, fake: FakePodman
) -> None:
    _running(env, fake)
    _answer_list(fake, "vim\n", "htop\nvim\n")
    status, notices = _session(env, fake)
    assert status == 0
    assert notices == []
    assert _stored(env).manifest == [
        ManifestEntry(name="htop", source=InstallSource.REPO, method=InstallMethod.NATIVE)
    ]
    (entry,) = _audit(env)
    assert entry.outcome == "ok"
    assert entry.params["packages"] == ["htop"]


@pytest.mark.fr("FR-042")
@pytest.mark.fr("FR-124")
def test_the_two_reads_run_as_root_under_the_locale(env: OpEnv, fake: FakePodman) -> None:
    _running(env, fake)
    _answer_list(fake, "vim\n", "vim\n")
    _session(env, fake)
    reads = [call for call in fake.exec_calls if call.argv == LIST]
    assert len(reads) == 2
    for call in reads:
        assert call.user == "0:0"
        assert call.env == {"LC_ALL": "C.UTF-8"}


@pytest.mark.fr("FR-042")
@pytest.mark.fr("FR-124")
def test_nothing_new_writes_nothing_and_takes_no_lock(env: OpEnv, fake: FakePodman) -> None:
    _running(env, fake)
    _answer_list(fake, "vim\n", "vim\n")
    status, notices = _session(env, fake)
    assert (status, notices) == (0, [])
    assert _stored(env).manifest == []
    assert _audit(env) == []


@pytest.mark.fr("FR-042")
def test_the_exec_status_comes_back_unchanged_even_when_recording_fails(
    env: OpEnv, fake: FakePodman
) -> None:
    _running(env, fake)
    _answer_list(fake, "vim\n", "vim\n")
    status, notices = _session(env, fake, status=7)
    assert status == 7
    assert notices == []


@pytest.mark.fr("FR-042")
@pytest.mark.fr("IR-011")
def test_a_failed_read_before_the_session_is_a_warning_and_nothing_is_recorded(
    env: OpEnv, fake: FakePodman
) -> None:
    _running(env, fake)
    fake.exec_sequences[LIST] = [ExecResult(1, "", "error: database is locked")]
    status, notices = _session(env, fake, status=3)
    assert status == 3
    (notice,) = notices
    assert notice.code == UNRECORDED_CODE
    assert "before the session" in notice.message
    assert "GINN-E-PKG-MANAGER" in notice.message
    assert _stored(env).manifest == []


@pytest.mark.fr("FR-042")
def test_a_failed_read_after_the_session_is_a_warning_and_nothing_is_recorded(
    env: OpEnv, fake: FakePodman
) -> None:
    _running(env, fake)
    fake.exec_sequences[LIST] = [
        ExecResult(0, "vim\n", ""),
        ExecResult(1, "", "error: database is locked"),
    ]
    status, notices = _session(env, fake)
    assert status == 0
    (notice,) = notices
    assert notice.code == UNRECORDED_CODE
    assert "after the session" in notice.message
    assert _stored(env).manifest == []


@pytest.mark.fr("FR-042")
@pytest.mark.fr("NFR-SEC-005")
def test_a_name_that_is_not_a_package_name_is_not_recorded_and_is_reported(
    env: OpEnv, fake: FakePodman
) -> None:
    _running(env, fake)
    _answer_list(fake, "vim\n", "vim\nhtop\nGimp*\n")
    _status, notices = _session(env, fake)
    assert _stored(env).manifest == [
        ManifestEntry(name="htop", source=InstallSource.REPO, method=InstallMethod.NATIVE)
    ]
    (notice,) = notices
    assert "Gimp*" in notice.message


@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-040")
def test_a_package_installed_by_ginn_keeps_the_ginn_method(env: OpEnv, fake: FakePodman) -> None:
    manifest = [ManifestEntry(name="htop", source=InstallSource.REPO, method=InstallMethod.GINN)]
    _running(env, fake, manifest)
    _answer_list(fake, "vim\n", "vim\nhtop\n")
    _session(env, fake)
    assert _stored(env).manifest == manifest


@pytest.mark.fr("FR-123")
@pytest.mark.fr("FR-042")
def test_a_held_realm_lock_is_a_warning_and_the_session_status_is_kept(
    env: OpEnv, fake: FakePodman
) -> None:
    _running(env, fake)
    _answer_list(fake, "vim\n", "vim\nhtop\n")
    with realm_lock(env.paths, RealmName(REALM), "install"):
        status, notices = _session(env, fake, status=0)
    assert status == 0
    (notice,) = notices
    assert notice.code == UNRECORDED_CODE
    assert "GINN-E-REALM-BUSY" in notice.message
    assert _stored(env).manifest == []


@pytest.mark.fr("NFR-040")
def test_a_control_character_in_a_rejected_name_is_written_escaped_in_the_warning(
    env: OpEnv, fake: FakePodman
) -> None:
    _running(env, fake)
    _answer_list(fake, "vim\n", "vim\nevil\x1b[31m\nhtop\n")
    _status, notices = _session(env, fake)
    assert _stored(env).manifest == [
        ManifestEntry(name="htop", source=InstallSource.REPO, method=InstallMethod.NATIVE)
    ]
    (notice,) = notices
    assert "\x1b" not in notice.message
    assert "evil\\x1b[31m" in notice.message


@pytest.mark.fr("FR-042")
@pytest.mark.fr("NFR-040")
def test_a_diff_over_the_name_cap_is_a_warning_and_writes_nothing(
    env: OpEnv, fake: FakePodman
) -> None:
    _running(env, fake)
    _answer_list(fake, "vim\n", "\n".join(f"p{index}" for index in range(5001)))
    status, notices = _session(env, fake)
    assert status == 0
    (notice,) = notices
    assert notice.code == UNRECORDED_CODE
    assert "packages after the session are unknown" in notice.message
    assert "more than 5000" in notice.message
    assert _stored(env).manifest == []
    assert _audit(env) == []


@pytest.mark.fr("FR-042")
@pytest.mark.fr("DR-020")
def test_a_refusal_raised_by_the_command_comes_out_of_the_session_with_no_warning(
    env: OpEnv, fake: FakePodman
) -> None:
    """A stopped realm is refused by enter, inside the exec. The error comes out of the session, so
    the diff writes no warning and records nothing for a command that never ran."""
    cid = _running(env, fake)
    notices: list[GinnWarning] = []
    recorder = root_session_recorder(env, fake, notify=notices.append)

    def refused() -> int:
        raise make(
            "GINN-E-REALM-STATE", realm=REALM, state="stopped", action="exec", allowed="running"
        )

    with pytest.raises(GinnError) as info:
        recorder(_stored(env), cid, refused)

    assert info.value.code == "GINN-E-REALM-STATE"
    assert notices == []
    assert _audit(env) == []


def _refused_session(env: OpEnv, fake: FakePodman) -> tuple[GinnError, list[GinnWarning]]:
    """Run the recorder around a command that enter refuses as a stopped realm. Return the error
    that came out and the notices made."""
    cid = _running(env, fake)
    notices: list[GinnWarning] = []
    recorder = root_session_recorder(env, fake, notify=notices.append)

    def refused() -> int:
        raise make(
            "GINN-E-REALM-STATE", realm=REALM, state="stopped", action="exec", allowed="running"
        )

    with pytest.raises(GinnError) as info:
        recorder(_stored(env), cid, refused)
    return info.value, notices


@pytest.mark.fr("FR-042")
@pytest.mark.fr("DR-020")
def test_a_refusal_after_a_readable_before_list_says_the_packages_may_be_unrecorded(
    env: OpEnv, fake: FakePodman
) -> None:
    """The command ran, so the packages it installed may be unrecorded. The note says so, and it
    names the remedy of GINN-W-MANIFEST-UNRECORDED. The refusal still comes out, with no warning."""
    error, notices = _refused_session(env, fake)

    assert error.code == "GINN-E-REALM-STATE"
    (note,) = getattr(error, "__notes__", [])
    assert "may be unrecorded in the Install Manifest" in note
    assert f"run 'ginn install {REALM} PACKAGE'" in note
    assert notices == []


@pytest.mark.fr("FR-042")
@pytest.mark.fr("DR-020")
def test_a_refusal_after_an_unreadable_before_list_gets_no_note(
    env: OpEnv, fake: FakePodman
) -> None:
    """The before-read failed, so no diff could run and nothing is claimed about the packages."""
    fake.exec_results[LIST] = ExecResult(1, "", "")

    error, notices = _refused_session(env, fake)

    assert error.code == "GINN-E-REALM-STATE"
    assert getattr(error, "__notes__", []) == []
    assert notices == []
