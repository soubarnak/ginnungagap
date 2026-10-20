# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811
"""Recovery reconciles the Install Manifest of an interrupted install or remove (T8.3 residual).

An install or remove that is killed before its `installed` or `removed` step may have changed
packages. Recovery reads the realm's explicit list, read-only, when the container runs, and the
manifest follows it for the journaled names (FR-040, FR-041). A stopped container, an unreadable
list or invalid journal names keep the manifest, with the reason in the outcome. A second run
changes nothing (FR-122). Fixtures and stubs come from test_ops_recovery.py.
"""

from dataclasses import replace
from typing import Any

import pytest

from ginn.domain import (
    InstallMethod,
    InstallSource,
    ManifestEntry,
    RealmName,
    RealmState,
)
from ginn.ops._mutating import OpEnv
from ginn.ops.recovery import OUTCOME_RECOVERED, recover
from ginn.podman.models import ContainerState
from ginn.store.audit import AuditLog
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmStore
from tests.unit.test_ops_recovery import (  # noqa: F401 - the fixtures are used by name
    _dead,
    _only,
    _Podman,
    _record,
    _save,
    _write_journal,
    env,
    paths,
)

REASON_NOT_RUNNING = "the container is not running, so the Install Manifest is left as it is"
REASON_UNREADABLE = "the package list cannot be read, so the Install Manifest is left as it is"


def _entry_of(name: str) -> ManifestEntry:
    return ManifestEntry(
        name=name,
        source=InstallSource.REPO,
        method=InstallMethod.GINN,
    )


def _with_manifest(paths: OwnedPaths, names: list[str]) -> None:
    record = _record(state=RealmState.RUNNING)
    _save(paths, replace(record, manifest=[_entry_of(n) for n in names]))


def _manifest(paths: OwnedPaths) -> list[str]:
    return [e.name for e in RealmStore(paths).load(RealmName("arch")).manifest]


def _journal(paths: OwnedPaths, op: str, step: str, names: list[str]) -> None:
    params: dict[str, Any] = {"packages": names, "aur": False}
    _write_journal(paths, _dead(op=op, step=step, params=params))


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
@pytest.mark.parametrize("step", ["begin", "exec-acked", "refreshed"])
def test_an_install_killed_before_its_step_records_the_names_the_realm_lists(
    paths: OwnedPaths, env: OpEnv, step: str
) -> None:
    _with_manifest(paths, [])
    _journal(paths, "install", step, ["htop", "vim"])
    podman = _Podman(ContainerState.RUNNING, listing="htop\nbase")
    outcome = _only(recover(env, podman))
    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest(paths) == ["htop"]
    assert "the Install Manifest follows the realm's explicit packages" in outcome.reason


@pytest.mark.fr("FR-041")
@pytest.mark.fr("FR-122")
@pytest.mark.parametrize("step", ["checked", "exec-acked"])
def test_a_remove_killed_before_its_step_drops_the_names_the_realm_no_longer_lists(
    paths: OwnedPaths, env: OpEnv, step: str
) -> None:
    _with_manifest(paths, ["htop", "vim"])
    _journal(paths, "remove", step, ["htop", "vim"])
    podman = _Podman(ContainerState.RUNNING, listing="vim\nbase")
    outcome = _only(recover(env, podman))
    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest(paths) == ["vim"]


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_a_begin_step_with_nothing_changed_leaves_the_manifest_and_writes_no_record(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _with_manifest(paths, ["vim"])
    before = RealmStore(paths).load(RealmName("arch"))
    _journal(paths, "install", "begin", ["htop"])
    _only(recover(env, _Podman(ContainerState.RUNNING, listing="vim")))
    assert RealmStore(paths).load(RealmName("arch")) == before


@pytest.mark.fr("FR-041")
@pytest.mark.fr("FR-122")
def test_a_stopped_realm_keeps_the_manifest_and_the_reason_says_so(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _with_manifest(paths, ["htop"])
    _journal(paths, "remove", "checked", ["htop"])
    podman = _Podman(ContainerState.EXITED, listing="")
    outcome = _only(recover(env, podman))
    assert _manifest(paths) == ["htop"]
    assert REASON_NOT_RUNNING in outcome.reason
    assert podman.execs == []


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_a_list_that_fails_keeps_the_manifest_and_settles_the_entry(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _with_manifest(paths, [])
    _journal(paths, "install", "begin", ["htop"])
    podman = _Podman(ContainerState.RUNNING, listing="htop", exec_exit=1)
    outcome = _only(recover(env, podman))
    assert outcome.outcome == OUTCOME_RECOVERED
    assert _manifest(paths) == []
    assert REASON_UNREADABLE in outcome.reason
    assert not list(paths.journal.glob("*.json"))


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_the_list_is_read_only_and_no_package_is_changed(paths: OwnedPaths, env: OpEnv) -> None:
    _with_manifest(paths, ["htop"])
    _journal(paths, "remove", "checked", ["htop"])
    podman = _Podman(ContainerState.RUNNING, listing="")
    _only(recover(env, podman))
    assert len(podman.execs) == 1
    assert podman.execs[0][1] == ("pacman", "--query", "--explicit", "--quiet")


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_recovery_run_twice_gives_the_same_manifest_and_one_audit_entry(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _with_manifest(paths, [])
    _journal(paths, "install", "exec-acked", ["htop"])
    podman = _Podman(ContainerState.RUNNING, listing="htop")
    _only(recover(env, podman))
    first = RealmStore(paths).load(RealmName("arch"))
    assert recover(env, podman) == []
    assert RealmStore(paths).load(RealmName("arch")) == first
    assert len([e for e in AuditLog(paths).read() if e.op == "install"]) == 1


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_a_crash_after_the_manifest_write_is_replayed_to_the_same_manifest(
    paths: OwnedPaths, env: OpEnv
) -> None:
    """The record holds the reconciled manifest and the journal file is still there: the replay
    reads the same list and finds nothing to change."""
    _with_manifest(paths, ["htop"])
    _journal(paths, "install", "exec-acked", ["htop"])
    podman = _Podman(ContainerState.RUNNING, listing="htop")
    before = RealmStore(paths).load(RealmName("arch")).manifest
    _only(recover(env, podman))
    assert RealmStore(paths).load(RealmName("arch")).manifest == before


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_an_invalid_name_in_the_journal_is_not_listed_or_recorded(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _with_manifest(paths, [])
    _journal(paths, "install", "begin", ["-bad name"])
    podman = _Podman(ContainerState.RUNNING, listing="htop")
    outcome = _only(recover(env, podman))
    assert _manifest(paths) == []
    assert "names no valid package list" in outcome.reason
    assert podman.execs == []


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-122")
def test_an_aur_install_at_upgrading_on_a_stopped_realm_says_it_recorded_without_a_check(
    paths: OwnedPaths, env: OpEnv
) -> None:
    _with_manifest(paths, [])
    params = {"packages": ["yay-bin"], "aur": True, "added": []}
    _write_journal(paths, _dead(op="install", step="upgrading", params=params))
    outcome = _only(recover(env, _Podman(ContainerState.EXITED)))
    assert _manifest(paths) == ["yay-bin"]
    assert "recorded without a check, since the realm is not running" in outcome.reason


@pytest.mark.fr("FR-041")
@pytest.mark.fr("FR-122")
def test_a_remove_killed_at_begin_keeps_the_manifest_whatever_the_list_says(
    paths: OwnedPaths, env: OpEnv
) -> None:
    """The dry run may be all that ran, and the list does not name aliases or groups."""
    _with_manifest(paths, ["vim", "htop"])
    _journal(paths, "remove", "begin", ["vim"])
    podman = _Podman(ContainerState.RUNNING, listing="vim-enhanced\nhtop")
    outcome = _only(recover(env, podman))
    assert _manifest(paths) == ["vim", "htop"]
    assert "the removal may not have started" in outcome.reason
    assert podman.execs == []


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
@pytest.mark.parametrize(
    ("source", "method"),
    [(InstallSource.AUR, InstallMethod.GINN), (InstallSource.REPO, InstallMethod.NATIVE)],
)
@pytest.mark.parametrize("step", ["begin", "exec-acked", "refreshed"])
def test_an_install_keeps_the_source_of_an_entry_that_exists(
    paths: OwnedPaths, env: OpEnv, source: InstallSource, method: InstallMethod, step: str
) -> None:
    record = _record(state=RealmState.RUNNING)
    entry = ManifestEntry(name="htop", source=source, method=method)
    _save(paths, replace(record, manifest=[entry]))
    _journal(paths, "install", step, ["htop", "vim"])
    _only(recover(env, _Podman(ContainerState.RUNNING, listing="htop\nvim")))
    stored = {e.name: e for e in RealmStore(paths).load(RealmName("arch")).manifest}
    assert stored["htop"].source is source
    assert stored["htop"].method is method
    assert stored["vim"].source is InstallSource.REPO
