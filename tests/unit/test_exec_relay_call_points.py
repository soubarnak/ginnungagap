# SPDX-License-Identifier: Apache-2.0
"""The call points of the exec relay in the operations (ADR-014, FR-084, FR-122).

A journaled exec records `exec-acked` once the podman client has started (FR-122). The relay is
synced after each change of the realm states that decide whether a services-on realm needs it:
REBUILDING and the rebuild swap, and UPDATING. A failed sync is GINN-W-RELAY-SYNC, and the operation
keeps its result. The rebuild call points are tested in test_ops_rebuild.py. Every test here
runs on FakePodman, through the world fixture.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from typing import Any

import pytest

from ginn.distros import Verb, load
from ginn.domain import RealmName, RealmState
from ginn.errors import GinnError
from ginn.ops._mutating import OpEnv
from ginn.ops.lifecycle import HostUser, LifecycleHooks
from ginn.ops.packages import PackageHooks, install_packages, remove_packages
from ginn.ops.update import update_realm
from ginn.packages.aur import _Realm, query_argv, restore
from ginn.packages.manager import name_batches
from ginn.podman.adapter import ExecTarget
from ginn.podman.models import ExecResult
from ginn.runtime import exec_relay
from ginn.runtime.realm_security import SecurityHooks
from ginn.store.journal import Journal
from ginn.store.records import RealmStore
from tests.fakes.clock import FakeClock
from tests.unit.realm_cli_support import World, make_realm, stored

DEB = "deb"
SYNC = "GINN-W-RELAY-SYNC"


def _env(world: World) -> OpEnv:
    return OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=FakeClock())


def _session(world: World) -> HostUser:
    return HostUser(uid=os.getuid(), host_home=world.home, host_env={})


def _hooks(world: World) -> PackageHooks:
    lifecycle = LifecycleHooks(
        exists=lambda p: p.startswith(str(world.tmp)) and os.path.exists(p),
        mountinfo=world.tmp / "mountinfo",
        security=SecurityHooks(gpg_extra=lambda: None),
    )
    return PackageHooks(lifecycle=lifecycle, sleep=lambda _s: None)


@pytest.fixture
def journal_steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every journal step name, in the order the operations record them (FR-122)."""
    steps: list[str] = []
    original: Callable[..., Any] = Journal.step

    def recording(self: Journal, entry: Any, name: str, params: Any = None, **kwargs: Any) -> Any:
        steps.append(name)
        return original(self, entry, name, params, **kwargs)

    monkeypatch.setattr(Journal, "step", recording)
    return steps


@pytest.fixture
def sync_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Replaces the relay sync of the update module. Each call records the realm's state."""
    calls: list[str] = []

    def record(paths: Any, *_args: Any, **_kwargs: Any) -> None:
        calls.append(RealmStore(paths).load(RealmName(DEB)).state.value)

    monkeypatch.setattr("ginn.ops.update.sync_exec_service", record)
    yield calls


@pytest.mark.fr("FR-122")
def test_an_install_records_exec_acked_once_the_client_started_and_before_installed(
    world: World, journal_steps: list[str]
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    install_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    assert "exec-acked" in journal_steps
    assert journal_steps.index("exec-acked") < journal_steps.index("installed")


@pytest.mark.fr("FR-084")
def test_an_update_syncs_the_relay_while_the_realm_is_updating(
    world: World, sync_calls: list[str]
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    update_realm(
        _env(world), world.fake, RealmName(DEB), session=_session(world), hooks=_hooks(world)
    )
    assert sync_calls == [RealmState.UPDATING.value]
    assert stored(world, DEB).state is RealmState.RUNNING


@pytest.mark.fr("FR-084")
def test_a_failed_relay_sync_during_an_update_is_a_warning_and_the_update_succeeds(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.EIO, "the sv directory is unreadable")

    monkeypatch.setattr("ginn.ops.update.sync_exec_service", broken)
    warnings: list[Any] = []
    update_realm(
        _env(world),
        world.fake,
        RealmName(DEB),
        session=_session(world),
        hooks=_hooks(world),
        warnings=warnings,
    )
    assert stored(world, DEB).state is RealmState.RUNNING
    assert [w.code for w in warnings] == [SYNC]


ARCH = "arch"
CID = "c" * 64
NAMES = [f"lib-{index:04d}" for index in range(300)]


def _services(world: World, name: str) -> None:
    """Mark the stored realm as services-on at create, so its execs take the relay route."""
    store = RealmStore(world.paths)
    store.save(replace(store.load(RealmName(name)), services_at_create=True))


def _relay_fits(argv: Sequence[str], container: str) -> None:
    """The exec of `argv` encodes for the relay: the worker would take the request (ADR-014)."""
    tail = ("exec", "--env", "LC_ALL=C.UTF-8", "--user", "0:0", "--", container, *argv)
    exec_relay.encode(
        exec_relay.Request(
            realm="tst",
            container_id=container,
            tail=tail,
            passed=(),
            tty=False,
            interactive=False,
            mutating=True,
            captured=True,
        )
    )


def _names_per_call(calls: list[Any], verb_word: str) -> list[list[str]]:
    """The package names of each exec that runs `verb_word`, in order."""
    return [
        [word for word in call.argv if word in NAMES] for call in calls if verb_word in call.argv
    ]


@pytest.mark.fr("FR-049")
def test_the_relay_route_splits_names_into_calls_the_relay_can_carry() -> None:
    target = ExecTarget(realm="tst", services=True, mutating=True)
    batches = name_batches(NAMES, target)
    assert [len(batch) for batch in batches] == [200, 100]
    assert [name for batch in batches for name in batch] == NAMES


@pytest.mark.fr("FR-049")
def test_the_relay_route_also_splits_by_the_bytes_of_the_names() -> None:
    long_names = [f"{index:03d}" + "x" * 1000 for index in range(100)]
    batches = name_batches(long_names, ExecTarget(realm="tst", services=True, mutating=True))
    assert len(batches) > 1
    assert all(sum(len(name) + 1 for name in batch) <= 40 * 1024 for batch in batches)
    assert [name for batch in batches for name in batch] == long_names


@pytest.mark.fr("FR-049")
def test_the_direct_route_keeps_one_call_whatever_the_count() -> None:
    assert name_batches(NAMES, ExecTarget(realm="tst", services=False, mutating=True)) == [NAMES]
    assert name_batches([], ExecTarget(realm="tst", services=True, mutating=True)) == [[]]


@pytest.mark.fr("NFR-SEC-005")
def test_the_largest_relay_batch_fits_the_relay_request_for_every_distribution() -> None:
    longest = ["x" * 64 + f"{index:03d}" for index in range(200)]
    for distro in ("arch", "debian", "fedora", "kali"):
        definition = load(distro)
        for verb in (Verb.INSTALL, Verb.REMOVE):
            _relay_fits(definition.argv(verb, longest), CID)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-122")
def test_an_install_of_three_hundred_names_on_the_relay_route_runs_relay_sized_calls(
    world: World,
) -> None:
    record = make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    _services(world, DEB)
    install_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        NAMES,
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    installs = [call for call in world.fake.exec_calls if "install" in call.argv]
    assert [len(call) for call in _names_per_call(installs, "install")] == [200, 100]
    for call in installs:
        assert call.target is not None and call.target.services is True
        _relay_fits(call.argv, record.container_id or CID)


@pytest.mark.fr("FR-040")
def test_an_install_of_three_hundred_names_on_the_direct_route_is_one_call(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    install_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        NAMES,
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    installs = [call for call in world.fake.exec_calls if "install" in call.argv]
    assert [len(call) for call in _names_per_call(installs, "install")] == [300]
    assert all(call.target is not None and call.target.services is False for call in installs)


@pytest.mark.fr("FR-041")
def test_a_removal_of_three_hundred_names_on_the_relay_route_runs_relay_sized_calls(
    world: World,
) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    _services(world, ARCH)
    remove_packages(
        _env(world),
        world.fake,
        RealmName(ARCH),
        NAMES,
        session=_session(world),
        hooks=_hooks(world),
    )
    removals = [call for call in world.fake.exec_calls if "--remove" in call.argv]
    assert [len(call) for call in _names_per_call(removals, "--remove")] == [200, 100]
    for call in removals:
        assert call.target is not None and call.target.services is True


@pytest.mark.fr("FR-122")
def test_a_refresh_before_an_install_records_exec_acked_before_refreshed(
    world: World, journal_steps: list[str]
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    install_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    assert "refreshed" in journal_steps
    first_ack = journal_steps.index("exec-acked")
    assert first_ack < journal_steps.index("refreshed")


@pytest.mark.fr("FR-122")
def test_a_removal_records_exec_acked_before_removed(
    world: World, journal_steps: list[str]
) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    remove_packages(
        _env(world),
        world.fake,
        RealmName(ARCH),
        ["htop"],
        session=_session(world),
        hooks=_hooks(world),
    )
    assert "removed" in journal_steps
    assert journal_steps.index("exec-acked") < journal_steps.index("removed")


@pytest.mark.fr("FR-084")
def test_a_failed_update_syncs_the_relay_after_it_leaves_updating_and_a_failed_sync_is_a_note(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    states: list[str] = []

    def broken(paths: Any, *_args: Any, **_kwargs: Any) -> None:
        states.append(RealmStore(paths).load(RealmName(DEB)).state.value)
        raise OSError(errno.EIO, "the sv directory is unreadable")

    monkeypatch.setattr("ginn.ops.update.sync_exec_service", broken)
    upgrade = tuple(load("debian").argv(Verb.UPGRADE))
    world.fake.exec_results[upgrade] = ExecResult(1, "", "E: a broken upgrade\n")
    with pytest.raises(GinnError) as caught:
        update_realm(
            _env(world), world.fake, RealmName(DEB), session=_session(world), hooks=_hooks(world)
        )
    assert states == [RealmState.UPDATING.value, RealmState.RUNNING.value]
    assert any("GINN-W-RELAY-SYNC" in note for note in caught.value.__notes__)
    assert stored(world, DEB).state is RealmState.RUNNING


@pytest.mark.fr("FR-041")
def test_a_removal_of_three_hundred_names_on_the_direct_route_is_one_call(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    remove_packages(
        _env(world),
        world.fake,
        RealmName(ARCH),
        NAMES,
        session=_session(world),
        hooks=_hooks(world),
    )
    removals = [call for call in world.fake.exec_calls if "--remove" in call.argv]
    assert [len(batch) for batch in _names_per_call(removals, "--remove")] == [300]
    assert all(call.target is not None and call.target.services is False for call in removals)


@pytest.mark.fr("FR-041")
def test_a_refused_batch_needing_a_later_batch_removed_first_is_retried_after_it(
    world: World,
) -> None:
    """A is in batch 1 and needed by B, which is in batch 2. Batch 1 is refused first, batch 2
    removes B, and batch 1 succeeds on the next pass. Before each later batch the names are filtered
    against the explicit list, so no name already gone is passed (ADR-014, T7.10 review)."""
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    _services(world, ARCH)
    arch = load("arch")
    batch1, batch2 = NAMES[:200], NAMES[200:]
    world.fake.exec_sequences[tuple(arch.argv(Verb.REMOVE, batch1))] = [
        ExecResult(1, "", "error: breaks dependency: B needs A\n"),
        ExecResult(0, "", ""),
    ]
    world.fake.exec_sequences[tuple(arch.argv(Verb.LIST_EXPLICIT))] = [
        ExecResult(0, "\n".join(NAMES), ""),  # read once before the removal
        ExecResult(0, "\n".join(NAMES), ""),  # before batch 1
        ExecResult(0, "\n".join(NAMES), ""),  # before batch 2
        ExecResult(0, "\n".join(batch1), ""),  # before the retry of batch 1: B is gone
    ]
    remove_packages(
        _env(world),
        world.fake,
        RealmName(ARCH),
        NAMES,
        session=_session(world),
        hooks=_hooks(world),
    )
    removals = [call for call in world.fake.exec_calls if "--remove" in call.argv]
    assert [[w for w in call.argv if w in NAMES] for call in removals] == [batch1, batch2, batch1]


@pytest.mark.fr("FR-043")
def test_an_aur_restore_removes_three_hundred_names_in_relay_sized_calls(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    cid = stored(world, ARCH).container_id or CID
    world.fake.exec_results[tuple(query_argv())] = ExecResult(0, "\n".join(NAMES), "")
    run = _Realm(
        world.fake,
        cid,
        load("arch"),
        ARCH,
        None,
        "1000:1000",
        ExecTarget(realm=ARCH, services=True, mutating=True),
    )
    restore(run, frozenset())
    removals = [call for call in world.fake.exec_calls if "--remove" in call.argv]
    assert [len([w for w in call.argv if w in NAMES]) for call in removals] == [200, 100]
    for call in removals:
        assert call.target is not None and call.target.services is True


@pytest.mark.fr("FR-041")
def test_a_failed_presence_read_removes_unfiltered_in_relay_sized_calls(world: World) -> None:
    """The first presence read is refused (over the caps, or unreadable). The removal goes on
    without the filter, so both batches are passed, and the removal does not fail for the read."""
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    _services(world, ARCH)
    arch = load("arch")
    world.fake.exec_sequences[tuple(arch.argv(Verb.LIST_EXPLICIT))] = [
        ExecResult(1, "", "error: the database is locked\n"),
    ]
    remove_packages(
        _env(world),
        world.fake,
        RealmName(ARCH),
        NAMES,
        session=_session(world),
        hooks=_hooks(world),
    )
    removals = [call for call in world.fake.exec_calls if "--remove" in call.argv]
    assert [[w for w in call.argv if w in NAMES] for call in removals] == [NAMES[:200], NAMES[200:]]


@pytest.mark.fr("FR-043")
def test_an_aur_restore_whose_presence_read_fails_removes_unfiltered(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    cid = stored(world, ARCH).container_id or CID
    world.fake.exec_sequences[tuple(query_argv())] = [
        ExecResult(0, "\n".join(NAMES), ""),
        ExecResult(1, "", "error: the database is locked\n"),
    ]
    run = _Realm(
        world.fake,
        cid,
        load("arch"),
        ARCH,
        None,
        "1000:1000",
        ExecTarget(realm=ARCH, services=True, mutating=True),
    )
    restore(run, frozenset())
    removals = [call for call in world.fake.exec_calls if "--remove" in call.argv]
    assert [len([w for w in call.argv if w in NAMES]) for call in removals] == [200, 100]
