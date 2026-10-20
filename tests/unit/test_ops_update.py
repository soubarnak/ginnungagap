# SPDX-License-Identifier: Apache-2.0
"""`ginn update` and `ginn update --check` in the ops layer (T8.4, ops/update.py, FR-023, FR-024).

Every test runs on FakePodman, through the `world` fixture of tests/unit/realm_cli_support.py. No
package manager runs. Covered: the upgrade argv and the refresh rule (ADR-011), the state an update
moves through (DR-020), the restore of the prior state after a failure, the `broken` marking only
on an inconsistent database (FR-120 AC-2), the FR-120 retry with the 5, 15 and 45 s delays, the
refusals before any side effect, `--all` past a failed realm, and the check of FR-024: its argv,
its results per distribution, its scratch cleanup, and its refusal to start a stopped realm.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from ginn.distros import DistroDef, Verb, load
from ginn.distros.loader import UpdateStatus
from ginn.domain import RealmName, RealmState
from ginn.errors import GinnError
from ginn.export.sync import DESKTOP_LIST
from ginn.ops._mutating import OpEnv
from ginn.ops.lifecycle import HostUser, LifecycleHooks
from ginn.ops.packages import PackageHooks
from ginn.ops.recovery import find_pending
from ginn.ops.update import (
    DISK_PRECHECK_SKIPPED,
    RealmCheck,
    check_realm,
    check_realms,
    update_realm,
    update_realms,
)
from ginn.packages.manager import CheckResult
from ginn.podman.adapter import Podman
from ginn.podman.models import ExecResult
from ginn.runtime.realm_security import SecurityHooks
from tests.fakes.clock import FakeClock
from tests.unit.realm_cli_support import World, make_realm, stored

ARCH = "arch"
DEB = "deb"
FEDORA = "fedora"
KALI = "kali"
TMP = "/tmp/ginn-check-test"  # noqa: S108 - a path inside the realm, as in packages/manager.py
DEBIAN_NETWORK = "E: Failed to fetch http://deb.debian.org/x: Temporary failure resolving 'deb'"
DEBIAN_SIGNATURE = "W: GPG error: NO_PUBKEY 0123456789ABCDEF"
ARCH_SYNC = ("pacman", "--sync", "--refresh", "--dbpath", TMP, "--logfile", "/dev/null")
ARCH_QUERY = ("pacman", "--query", "--upgrades", "--dbpath", TMP)
ARCH_MKDIR = ("mkdir", TMP)
ARCH_LINK = ("ln", "-s", "-T", "/var/lib/pacman/local", f"{TMP}/local")
ARCH_RM = ("rm", "-rf", "--", TMP)


def _env(world: World) -> OpEnv:
    return OpEnv.for_paths(world.paths, "alice", root_dir=world.host, clock=FakeClock())


def _session(world: World) -> HostUser:
    return HostUser(uid=os.getuid(), host_home=world.home, host_env={})


def _hooks(world: World, sleeps: list[float] | None = None) -> PackageHooks:
    """The hooks of one operation. `sleeps` collects each retry wait, when it is given."""
    mountinfo = world.tmp / "mountinfo"
    lifecycle = LifecycleHooks(
        exists=lambda p: p.startswith(str(world.tmp)) and os.path.exists(p),
        mountinfo=mountinfo,
        security=SecurityHooks(gpg_extra=lambda: None),
    )
    collected: list[float] = [] if sleeps is None else sleeps
    return PackageHooks(lifecycle=lifecycle, sleep=collected.append)


def _argv(distro: str, verb: Verb, *packages: str) -> tuple[str, ...]:
    return tuple(load(distro).argv(verb, list(packages)))


def _execs(world: World) -> list[tuple[str, ...]]:
    return [tuple(call.argv) for call in world.fake.exec_calls if call.op == "exec"]


def _audit(env: OpEnv, op: str) -> list[Any]:
    return [entry for entry in env.audit.read() if entry.op == op]


def _upgrade_fails(world: World, distro: str, result: ExecResult) -> None:
    world.fake.exec_results[_argv(distro, Verb.UPGRADE)] = result


@pytest.mark.fr("FR-023")
@pytest.mark.fr("FR-124")
def test_update_runs_refresh_then_dist_upgrade_as_root_and_keeps_the_realm_running(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    env = _env(world)
    record = update_realm(
        env, world.fake, RealmName(DEB), session=_session(world), hooks=_hooks(world)
    )
    # Debian lists `upgrade` in refresh_before, so the lists are refreshed first (ADR-011).
    assert _execs(world) == [
        _argv("debian", Verb.REFRESH),
        _argv("debian", Verb.UPGRADE),
        DESKTOP_LIST,
    ]
    for call in world.fake.exec_calls:
        assert call.user == "0:0"
        if tuple(call.argv) != DESKTOP_LIST:
            assert call.env == {"LC_ALL": "C.UTF-8"}
    assert record.state is RealmState.RUNNING
    assert record.previous_state is None
    (entry,) = _audit(env, "update")
    assert entry.outcome == "ok"
    assert entry.params["distro"] == "debian"


@pytest.mark.fr("FR-023")
def test_arch_update_is_the_single_full_upgrade_with_no_separate_refresh(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    update_realm(
        _env(world), world.fake, RealmName(ARCH), session=_session(world), hooks=_hooks(world)
    )
    (argv, _listing) = _execs(world)
    assert argv == _argv("arch", Verb.UPGRADE)
    assert "--sysupgrade" in argv and "--refresh" in argv


@pytest.mark.fr("FR-023")
@pytest.mark.fr("DR-020")
def test_a_stopped_realm_is_started_for_the_update_and_stopped_again(world: World) -> None:
    make_realm(world, ARCH, RealmState.STOPPED, running=False, distro="arch")
    env = _env(world)
    record = update_realm(
        env, world.fake, RealmName(ARCH), session=_session(world), hooks=_hooks(world)
    )
    assert record.state is RealmState.STOPPED
    ops = [op for op in world.fake.ops() if op in {"start", "stop"}]
    assert ops == ["start", "stop"]
    realm_ops = [e.op for e in env.audit.read() if e.op in {"start", "update", "stop"}]
    assert realm_ops == ["start", "update", "stop"]


@pytest.mark.fr("FR-023")
@pytest.mark.fr("FR-120")
def test_a_failed_upgrade_restores_the_running_state_and_is_audited_with_its_code(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    _upgrade_fails(world, "debian", ExecResult(100, "", "E: Unable to locate package x\n"))
    env = _env(world)
    with pytest.raises(GinnError) as caught:
        update_realm(env, world.fake, RealmName(DEB), session=_session(world), hooks=_hooks(world))
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    record = stored(world, DEB)
    assert record.state is RealmState.RUNNING
    assert record.previous_state is None
    (entry,) = _audit(env, "update")
    assert entry.outcome == "error"
    assert entry.error_code == "GINN-E-PKG-MANAGER"


@pytest.mark.fr("FR-120")
def test_a_failed_upgrade_of_a_stopped_realm_leaves_it_stopped(world: World) -> None:
    make_realm(world, ARCH, RealmState.STOPPED, running=False, distro="arch")
    _upgrade_fails(world, "arch", ExecResult(1, "", "error: failed to commit transaction\n"))
    with pytest.raises(GinnError) as caught:
        update_realm(
            _env(world), world.fake, RealmName(ARCH), session=_session(world), hooks=_hooks(world)
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert stored(world, ARCH).state is RealmState.STOPPED


@pytest.mark.fr("FR-120")
def test_a_network_failure_is_retried_with_the_fr120_delays_then_raised_as_network(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    _upgrade_fails(world, "debian", ExecResult(100, "", DEBIAN_NETWORK))
    sleeps: list[float] = []
    with pytest.raises(GinnError) as caught:
        update_realm(
            _env(world),
            world.fake,
            RealmName(DEB),
            session=_session(world),
            hooks=_hooks(world, sleeps),
        )
    assert caught.value.code == "GINN-E-NETWORK"
    assert sleeps == [5.0, 15.0, 45.0]
    assert _execs(world).count(_argv("debian", Verb.UPGRADE)) == 4
    assert stored(world, DEB).state is RealmState.RUNNING


@pytest.mark.fr("FR-120")
def test_a_network_failure_that_clears_on_a_retry_completes_the_update(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    upgrade = _argv("debian", Verb.UPGRADE)
    world.fake.exec_sequences[upgrade] = [
        ExecResult(100, "", DEBIAN_NETWORK),
        ExecResult(0, "", ""),
    ]
    sleeps: list[float] = []
    env = _env(world)
    record = update_realm(
        env, world.fake, RealmName(DEB), session=_session(world), hooks=_hooks(world, sleeps)
    )
    assert sleeps == [5.0]
    assert record.state is RealmState.RUNNING
    (entry,) = _audit(env, "update")
    assert entry.outcome == "ok"


@pytest.mark.fr("FR-012")
@pytest.mark.fr("FR-120")
def test_a_signature_failure_is_never_retried(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    _upgrade_fails(world, "debian", ExecResult(100, "", DEBIAN_SIGNATURE + DEBIAN_NETWORK))
    sleeps: list[float] = []
    with pytest.raises(GinnError) as caught:
        update_realm(
            _env(world),
            world.fake,
            RealmName(DEB),
            session=_session(world),
            hooks=_hooks(world, sleeps),
        )
    assert caught.value.code == "GINN-E-PKG-SIGNATURE"
    assert sleeps == []


@pytest.mark.fr("FR-120")
def test_an_inconsistent_database_after_a_failed_upgrade_marks_the_realm_broken(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    _upgrade_fails(world, "debian", ExecResult(100, "", "E: dpkg was interrupted\n"))
    seen: list[str] = []

    def inconsistent(_podman: Podman, cid: str, _definition: DistroDef) -> bool:
        seen.append(cid)
        return True

    with pytest.raises(GinnError):
        update_realm(
            _env(world),
            world.fake,
            RealmName(DEB),
            session=_session(world),
            hooks=_hooks(world),
            db_check=inconsistent,
        )
    assert len(seen) == 1
    assert stored(world, DEB).state is RealmState.BROKEN


@pytest.mark.fr("FR-120")
def test_a_consistent_database_after_a_failed_upgrade_restores_the_prior_state(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    _upgrade_fails(world, "debian", ExecResult(100, "", "E: a transient mirror problem\n"))
    with pytest.raises(GinnError):
        update_realm(
            _env(world),
            world.fake,
            RealmName(DEB),
            session=_session(world),
            hooks=_hooks(world),
            db_check=lambda _p, _c, _d: False,
        )
    assert stored(world, DEB).state is RealmState.RUNNING


@pytest.mark.fr("FR-120")
def test_a_failing_database_check_is_a_note_and_the_prior_state_is_restored(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    _upgrade_fails(world, "debian", ExecResult(100, "", "E: a transient mirror problem\n"))

    def failing(_podman: Podman, _cid: str, _definition: DistroDef) -> bool:
        raise GinnError("GINN-E-INTERNAL", "m", "r", 70)

    with pytest.raises(GinnError) as caught:
        update_realm(
            _env(world),
            world.fake,
            RealmName(DEB),
            session=_session(world),
            hooks=_hooks(world),
            db_check=failing,
        )
    assert any("package database check failed" in note for note in caught.value.__notes__)
    assert stored(world, DEB).state is RealmState.RUNNING


@pytest.mark.fr("DR-020")
def test_a_realm_that_is_not_stopped_running_or_failed_is_refused_before_any_podman_call(
    world: World,
) -> None:
    make_realm(world, "stale", RealmState.BROKEN, running=False, distro="debian")
    with pytest.raises(GinnError) as caught:
        update_realm(
            _env(world),
            world.fake,
            RealmName("stale"),
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-REALM-STATE"
    assert world.fake.ops() == []


@pytest.mark.fr("FR-023")
@pytest.mark.fr("FR-124")
def test_kali_update_runs_refresh_then_dist_upgrade_like_debian(world: World) -> None:
    make_realm(world, KALI, RealmState.RUNNING, running=True, distro="kali")
    env = _env(world)
    record = update_realm(
        env, world.fake, RealmName(KALI), session=_session(world), hooks=_hooks(world)
    )
    # Kali lists `upgrade` in refresh_before, as Debian does (kali.toml, ADR-011).
    assert _execs(world) == [
        _argv("kali", Verb.REFRESH),
        _argv("kali", Verb.UPGRADE),
        DESKTOP_LIST,
    ]
    for call in world.fake.exec_calls:
        assert call.user == "0:0"
    assert record.state is RealmState.RUNNING
    (entry,) = _audit(env, "update")
    assert entry.outcome == "ok"
    assert entry.params["distro"] == "kali"


@pytest.mark.fr("FR-023")
def test_update_of_an_unknown_realm_is_no_such_realm(world: World) -> None:
    with pytest.raises(GinnError) as caught:
        update_realm(
            _env(world),
            world.fake,
            RealmName("ghost"),
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-NO-SUCH-REALM"


@pytest.mark.fr("FR-023")
@pytest.mark.fr("FR-120")
def test_update_all_goes_on_past_a_failed_realm_and_reports_each_in_name_order(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    make_realm(world, "stale", RealmState.BROKEN, running=False, distro="debian")
    _upgrade_fails(world, "arch", ExecResult(1, "", "error: failed to commit\n"))
    rows = update_realms(
        _env(world), world.fake, None, session=_session(world), hooks=_hooks(world)
    )
    assert [row.realm for row in rows] == [ARCH, DEB, "stale"]
    assert [row.error.code if row.error else "ok" for row in rows] == [
        "GINN-E-PKG-MANAGER",
        "ok",
        "GINN-E-REALM-STATE",
    ]
    assert rows[1].record is not None and rows[1].record.state is RealmState.RUNNING


# ---- FR-024: the check -------------------------------------------------------------------------


def _check(world: World, name: str, tmpdir: str | None = None) -> RealmCheck:
    return check_realm(_env(world), world.fake, RealmName(name), hooks=_hooks(world), tmpdir=tmpdir)


@pytest.mark.fr("FR-024")
def test_arch_check_runs_the_four_scratch_steps_and_counts_the_updates(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    query = ExecResult(0, "bash 5.2-1 -> 5.2-2\nzlib 1.3-1 -> 1.3-2\n", "")
    world.fake.exec_results[ARCH_QUERY] = query
    row = _check(world, ARCH, tmpdir=TMP)
    assert row.result == CheckResult(UpdateStatus.UPDATES, 2)
    assert row.result is not None and row.result.updates
    assert _execs(world) == [ARCH_MKDIR, ARCH_LINK, ARCH_SYNC, ARCH_QUERY, ARCH_RM]
    assert all("--sysupgrade" not in argv for argv in _execs(world))
    assert _audit(_env(world), "update") == []
    assert stored(world, ARCH).state is RealmState.RUNNING


@pytest.mark.fr("FR-024")
def test_arch_check_with_no_upgradable_package_is_none(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    world.fake.exec_results[ARCH_QUERY] = ExecResult(1, "", "warning: nothing to do\n")
    row = _check(world, ARCH, tmpdir=TMP)
    assert row.result == CheckResult(UpdateStatus.NONE, 0)
    assert _execs(world)[-1] == ARCH_RM


@pytest.mark.fr("FR-024")
def test_arch_check_fails_closed_on_a_missing_sync_database_and_still_cleans_up(
    world: World,
) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    world.fake.exec_results[ARCH_QUERY] = ExecResult(
        1, "", "warning: database file for 'extra' does not exist\n"
    )
    with pytest.raises(GinnError) as caught:
        _check(world, ARCH, tmpdir=TMP)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert _execs(world)[-1] == ARCH_RM


@pytest.mark.fr("FR-024")
def test_arch_check_does_not_query_when_the_scratch_sync_fails(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    world.fake.exec_results[ARCH_SYNC] = ExecResult(1, "", "error: failed to synchronize\n")
    with pytest.raises(GinnError) as caught:
        _check(world, ARCH, tmpdir=TMP)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert ARCH_QUERY not in _execs(world)
    assert _execs(world)[-1] == ARCH_RM


@pytest.mark.fr("FR-024")
def test_debian_check_refreshes_the_lists_then_counts_the_marked_lines(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    listing = (
        "Listing...\n"
        "bash/stable 5.2 amd64 [upgradable from: 5.1]\n"
        "zlib/stable 1.3 amd64 [upgradable from: 1.2]\n"
    )
    world.fake.exec_results[("apt", "list", "--upgradable")] = ExecResult(0, listing, "")
    row = _check(world, DEB)
    assert row.result == CheckResult(UpdateStatus.UPDATES, 2)
    assert _execs(world) == [_argv("debian", Verb.REFRESH), ("apt", "list", "--upgradable")]


@pytest.mark.fr("FR-024")
def test_kali_check_refreshes_the_lists_then_counts_the_marked_lines_like_debian(
    world: World,
) -> None:
    make_realm(world, KALI, RealmState.RUNNING, running=True, distro="kali")
    listing = "Listing...\nnmap/kali-rolling 7.95 amd64 [upgradable from: 7.94]\n"
    world.fake.exec_results[("apt", "list", "--upgradable")] = ExecResult(0, listing, "")
    row = _check(world, KALI)
    assert row.result == CheckResult(UpdateStatus.UPDATES, 1)
    assert _execs(world) == [_argv("kali", Verb.REFRESH), ("apt", "list", "--upgradable")]


@pytest.mark.fr("FR-024")
def test_apt_exit_100_is_an_error_and_never_a_list_of_updates(world: World) -> None:
    """apt exits 100 on an error, so the dnf exit-100 rule does not apply to Debian (ADR-011)."""
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[("apt", "list", "--upgradable")] = ExecResult(
        100, "", "E: could not open lists\n"
    )
    with pytest.raises(GinnError) as caught:
        _check(world, DEB)
    assert caught.value.code == "GINN-E-PKG-MANAGER"


@pytest.mark.fr("FR-122")
def test_an_interrupt_during_the_upgrade_leaves_updating_and_a_journal_for_recovery(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ginn exception leaves the body unsettled, so recovery finds its entry (FR-122)."""
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    env = _env(world)

    def killed(*_args: object, **_kwargs: object) -> ExecResult:
        raise RuntimeError("killed mid-upgrade")

    monkeypatch.setattr(world.fake, "exec", killed)
    with pytest.raises(RuntimeError):
        update_realm(env, world.fake, RealmName(DEB), session=_session(world), hooks=_hooks(world))
    assert stored(world, DEB).state is RealmState.UPDATING
    assert len(find_pending(env)) == 1


@pytest.mark.fr("FR-024")
def test_fedora_exit_100_is_updates_and_its_count_is_not_read(world: World) -> None:
    make_realm(world, FEDORA, RealmState.RUNNING, running=True, distro="fedora")
    world.fake.exec_results[("dnf", "--refresh", "check-upgrade")] = ExecResult(
        100, "bash.x86_64    5.2    updates\n", ""
    )
    row = _check(world, FEDORA)
    assert row.result == CheckResult(UpdateStatus.UPDATES, None)


@pytest.mark.fr("FR-024")
def test_fedora_exit_zero_is_none(world: World) -> None:
    make_realm(world, FEDORA, RealmState.RUNNING, running=True, distro="fedora")
    row = _check(world, FEDORA)
    assert row.result == CheckResult(UpdateStatus.NONE, None)


@pytest.mark.fr("FR-024")
def test_a_stopped_realm_is_skipped_and_is_not_started_for_the_check(world: World) -> None:
    make_realm(world, ARCH, RealmState.STOPPED, running=False, distro="arch")
    row = _check(world, ARCH, tmpdir=TMP)
    assert row.result is None
    assert row.skipped is not None and "stopped" in row.skipped
    assert world.fake.ops() == []
    assert stored(world, ARCH).state is RealmState.STOPPED


@pytest.mark.fr("FR-024")
@pytest.mark.fr("FR-120")
def test_a_network_failure_of_the_check_is_retried_then_raised(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[("apt", "list", "--upgradable")] = ExecResult(100, "", DEBIAN_NETWORK)
    sleeps: list[float] = []
    with pytest.raises(GinnError) as caught:
        check_realm(_env(world), world.fake, RealmName(DEB), hooks=_hooks(world, sleeps))
    assert caught.value.code == "GINN-E-NETWORK"
    assert sleeps == [5.0, 15.0, 45.0]


@pytest.mark.fr("FR-024")
def test_check_all_reports_each_realm_and_keeps_the_others_when_one_fails(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    make_realm(world, "gone", RealmState.STOPPED, running=False, distro="debian")
    world.fake.exec_results[("apt", "list", "--upgradable")] = ExecResult(
        1, "", "E: could not open lists\n"
    )
    rows = check_realms(_env(world), world.fake, None, hooks=_hooks(world))
    assert [row.realm for row in rows] == [DEB, "gone"]
    assert rows[0].error is not None and rows[0].error.code == "GINN-E-PKG-MANAGER"
    assert rows[1].skipped is not None


@pytest.mark.fr("FR-024")
def test_check_of_an_unknown_realm_is_an_error_row(world: World) -> None:
    rows = check_realms(_env(world), world.fake, [RealmName("ghost")], hooks=_hooks(world))
    assert rows[0].error is not None and rows[0].error.code == "GINN-E-NO-SUCH-REALM"


@pytest.mark.fr("FR-121")
def test_the_disk_precheck_note_says_no_estimate_sizes_an_upgrade() -> None:
    assert DISK_PRECHECK_SKIPPED.startswith("skipped:")


# ---- FR-120 AC-2: the db-check verb of the data marks `broken`; an unknown result never does ----


@pytest.mark.fr("FR-120")
def test_a_failed_arch_upgrade_is_broken_when_the_db_check_verb_reports_inconsistent(
    world: World,
) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    _upgrade_fails(world, "arch", ExecResult(1, "", "error: failed to commit transaction\n"))
    world.fake.exec_results[_argv("arch", Verb.DB_CHECK)] = ExecResult(1, "", "error: corrupt\n")
    env = _env(world)
    with pytest.raises(GinnError) as caught:
        update_realm(env, world.fake, RealmName(ARCH), session=_session(world), hooks=_hooks(world))
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert stored(world, ARCH).state is RealmState.BROKEN
    (entry,) = _audit(env, "update")
    assert entry.outcome == "error"


@pytest.mark.fr("FR-120")
def test_a_failed_debian_upgrade_is_broken_when_the_db_check_lists_a_broken_package(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    _upgrade_fails(world, "debian", ExecResult(100, "", "E: dpkg was interrupted\n"))
    world.fake.exec_results[_argv("debian", Verb.DB_CHECK)] = ExecResult(0, "gimp: broken\n", "")
    with pytest.raises(GinnError):
        update_realm(
            _env(world), world.fake, RealmName(DEB), session=_session(world), hooks=_hooks(world)
        )
    assert stored(world, DEB).state is RealmState.BROKEN


@pytest.mark.fr("FR-120")
def test_a_consistent_db_check_after_a_failed_upgrade_restores_and_runs_as_root_once(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    _upgrade_fails(world, "debian", ExecResult(100, "", "E: a transient mirror problem\n"))
    with pytest.raises(GinnError):
        update_realm(
            _env(world), world.fake, RealmName(DEB), session=_session(world), hooks=_hooks(world)
        )
    assert stored(world, DEB).state is RealmState.RUNNING
    checks = [c for c in world.fake.exec_calls if tuple(c.argv) == _argv("debian", Verb.DB_CHECK)]
    assert len(checks) == 1
    assert checks[0].user == "0:0"
    assert checks[0].env == {"LC_ALL": "C.UTF-8"}


@pytest.mark.fr("FR-120")
def test_a_successful_update_runs_no_db_check(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    update_realm(
        _env(world), world.fake, RealmName(ARCH), session=_session(world), hooks=_hooks(world)
    )
    assert _argv("arch", Verb.DB_CHECK) not in _execs(world)


@pytest.mark.fr("FR-120")
def test_a_db_check_that_cannot_run_keeps_the_prior_state_and_notes_why(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    _upgrade_fails(world, "arch", ExecResult(1, "", "error: failed to commit transaction\n"))
    world.fake.exec_results[_argv("arch", Verb.DB_CHECK)] = ExecResult(
        127, "", "exec: pacman: not found\n"
    )
    with pytest.raises(GinnError) as caught:
        update_realm(
            _env(world), world.fake, RealmName(ARCH), session=_session(world), hooks=_hooks(world)
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert any(
        "package database check failed" in note and "GINN-E-PKG-MANAGER" in note
        for note in caught.value.__notes__
    )
    assert stored(world, ARCH).state is RealmState.RUNNING


@pytest.mark.fr("FR-120")
def test_a_caller_check_replaces_the_db_check_verb(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    _upgrade_fails(world, "arch", ExecResult(1, "", "error: failed to commit transaction\n"))
    # The verb would say inconsistent. The caller's check says otherwise, so the verb never runs.
    world.fake.exec_results[_argv("arch", Verb.DB_CHECK)] = ExecResult(1, "", "error\n")
    with pytest.raises(GinnError):
        update_realm(
            _env(world),
            world.fake,
            RealmName(ARCH),
            session=_session(world),
            hooks=_hooks(world),
            db_check=lambda _p, _c, _d: False,
        )
    assert stored(world, ARCH).state is RealmState.RUNNING
    assert _argv("arch", Verb.DB_CHECK) not in _execs(world)


@pytest.mark.fr("FR-120")
def test_a_distribution_with_no_db_check_verb_is_never_marked_broken(world: World) -> None:
    make_realm(world, "fixture", RealmState.RUNNING, running=True, distro="_test")
    _upgrade_fails(world, "_test", ExecResult(1, "", "boom\n"))
    with pytest.raises(GinnError):
        update_realm(
            _env(world),
            world.fake,
            RealmName("fixture"),
            session=_session(world),
            hooks=_hooks(world),
        )
    assert stored(world, "fixture").state is RealmState.RUNNING
    assert _execs(world) == [_argv("_test", Verb.UPGRADE)]
