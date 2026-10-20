# SPDX-License-Identifier: Apache-2.0
"""`ginn install` and `ginn remove` in the ops layer (T5.3, ops/packages.py, FR-040, FR-041).

Every test runs on FakePodman, through the `world` fixture of tests/unit/realm_cli_support.py. No
package manager runs. Covered: the argv and the root user under the locale, the refresh rule of
ADR-011, the refusals before any side effect (names, --aur, the realm state, a held lock), the
failure codes, the FR-120 retry, the remove dry run that refuses a dependent removal, the manifest
and audit entries of each operation, and the Kali install and remove, which run the Debian steps
(kali.toml, ADR-011).
"""

from __future__ import annotations

import errno
import os
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

import ginn.ops.packages as packages_module
from ginn.distros import Verb, load
from ginn.domain import InstallMethod, InstallSource, ManifestEntry, RealmName, RealmState
from ginn.errors import CATALOGUE, GinnError, warn
from ginn.export.sync import DESKTOP_LIST, SyncHooks, SyncResult, sync_realm
from ginn.ops._mutating import OpEnv
from ginn.ops.lifecycle import HostUser, LifecycleHooks
from ginn.ops.packages import PackageHooks, install_packages, remove_packages
from ginn.podman.models import ExecResult
from ginn.runtime.realm_security import SecurityHooks
from ginn.store.journal import Journal, JournalEntry
from ginn.store.locks import realm_lock
from ginn.store.records import RealmRecord, RealmStore
from tests.fakes.clock import FakeClock
from tests.unit.realm_cli_support import World, make_realm, stored

GID = 1000

DEB = "deb"
ARCH = "arch"
KALI = "kali"
EXIT_SIGNATURE = 65


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


def _audit(env: OpEnv, op: str) -> list[Any]:
    return [entry for entry in env.audit.read() if entry.op == op]


def _execs(world: World) -> list[tuple[str, ...]]:
    return [tuple(call.argv) for call in world.fake.exec_calls if call.op == "exec"]


def _manifest(world: World, realm: str) -> list[ManifestEntry]:
    return stored(world, realm).manifest


def _ginn(name: str) -> ManifestEntry:
    return ManifestEntry(name=name, source=InstallSource.REPO, method=InstallMethod.GINN)


def _native(name: str) -> ManifestEntry:
    return ManifestEntry(name=name, source=InstallSource.REPO, method=InstallMethod.NATIVE)


def _init(name: str) -> ManifestEntry:
    return ManifestEntry(name=name, source=InstallSource.REPO, method=InstallMethod.GINN, init=True)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-042")
@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-124")
def test_install_refreshes_then_installs_as_root_under_the_locale_and_records_ginn(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    env = _env(world)
    record = install_packages(
        env,
        world.fake,
        RealmName(DEB),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    # The menu sync (FR-044) lists the realm's desktop files once, after the install.
    assert _execs(world) == [
        _argv("debian", Verb.REFRESH),
        _argv("debian", Verb.INSTALL, "htop"),
        DESKTOP_LIST,
    ]
    for call in world.fake.exec_calls:
        assert call.user == "0:0"
        # The menu listing (FR-044) is not a package-manager step, so it takes no locale.
        if tuple(call.argv) != DESKTOP_LIST:
            assert call.env == {"LC_ALL": "C.UTF-8"}
    assert record.manifest == [_ginn("htop")]
    (entry,) = _audit(env, "install")
    assert entry.outcome == "ok"
    assert entry.params["packages"] == ["htop"]
    assert entry.params["aur"] is False


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-023")
def test_arch_install_never_refreshes_or_upgrades_the_realm(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    install_packages(
        _env(world),
        world.fake,
        RealmName(ARCH),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    argv, listing = _execs(world)
    assert argv == _argv("arch", Verb.INSTALL, "htop")
    assert listing == DESKTOP_LIST
    assert not {"--refresh", "--sysupgrade", "-Syu"} & set(argv)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("packages", [["gimp*"], ["ok-name", "gimp*"], ["GIMP"]])
def test_a_bad_name_is_refused_before_any_podman_call_and_changes_nothing(
    world: World, packages: list[str]
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    env = _env(world)
    with pytest.raises(GinnError) as caught:
        install_packages(
            env,
            world.fake,
            RealmName(DEB),
            packages,
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-NAME-INVALID"
    assert world.fake.calls == []
    assert _audit(env, "install") == []
    assert _manifest(world, DEB) == []


@pytest.mark.fr("FR-040")
def test_an_empty_package_list_is_a_usage_error_before_any_podman_call(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName(DEB),
            [],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-USAGE"
    assert world.fake.calls == []


@pytest.mark.fr("FR-043")
def test_aur_is_an_option_error_off_arch_and_makes_no_call(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    with pytest.raises(GinnError) as off_arch:
        install_packages(
            _env(world), world.fake, RealmName(DEB), ["htop"], aur=True,
            session=_session(world), hooks=_hooks(world),
        )  # fmt: skip
    assert off_arch.value.code == "GINN-E-OPTION-INVALID"
    assert off_arch.value.exit_code == 2
    assert world.fake.calls == []


def _aur_answers(world: World) -> None:
    """The package query, srcinfo and packagelist answers of one successful AUR build."""
    from ginn.packages.aur import packagelist_argv, query_argv, srcinfo_argv
    from tests.unit.test_packages_aur import (
        AFTER_BUILT,
        AFTER_GIT,
        AFTER_RUNTIME,
        AFTER_TOOLCHAIN,
        PACKAGE_LISTING,
        S0,
        SRCINFO,
    )

    world.fake.exec_sequences[tuple(query_argv())] = [
        ExecResult(0, text, "")
        for text in (S0, AFTER_GIT, AFTER_RUNTIME, AFTER_TOOLCHAIN, AFTER_BUILT)
    ]
    world.fake.exec_results[tuple(srcinfo_argv())] = ExecResult(0, SRCINFO, "")
    world.fake.exec_results[tuple(packagelist_argv())] = ExecResult(0, PACKAGE_LISTING, "")


@pytest.mark.fr("FR-043")
@pytest.mark.fr("IR-023")
@pytest.mark.fr("DR-007")
def test_aur_on_arch_builds_inside_the_realm_and_records_an_aur_ginn_entry(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    _aur_answers(world)
    env = _env(world)
    record = install_packages(
        env, world.fake, RealmName(ARCH), ["yay-bin"], aur=True, session=_session(world),
        gid=GID, hooks=_hooks(world),
    )  # fmt: skip
    assert record.manifest == [
        ManifestEntry(name="yay-bin", source=InstallSource.AUR, method=InstallMethod.GINN)
    ]
    assert _manifest(world, ARCH) == record.manifest
    (entry,) = _audit(env, "install")
    assert (entry.outcome, entry.params["aur"]) == ("ok", True)
    builds = [call for call in world.fake.exec_calls if call.argv[0] in ("git", "makepkg")]
    assert builds and all(call.user != "0:0" for call in builds)


@pytest.mark.fr("FR-043")
@pytest.mark.fr("DR-007")
def test_a_build_only_removal_pacman_refuses_is_a_warning_and_the_install_is_kept(
    world: World,
) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    _aur_answers(world)
    removal = _argv("arch", Verb.REMOVE, "fakeroot", "gcc", "git", "go", "isl", "make", "pcre2")
    world.fake.exec_results[removal] = ExecResult(1, "", "error: failed to prepare transaction")
    warnings: list[Any] = []
    record = install_packages(
        _env(world), world.fake, RealmName(ARCH), ["yay-bin"], aur=True, session=_session(world),
        gid=GID, hooks=_hooks(world), warnings=warnings,
    )  # fmt: skip
    assert [notice.code for notice in warnings] == ["GINN-W-BUILD-RESIDUAL"]
    assert "pcre2" in warnings[0].message
    assert record.manifest == [
        ManifestEntry(name="yay-bin", source=InstallSource.AUR, method=InstallMethod.GINN)
    ]


@pytest.mark.fr("FR-043")
@pytest.mark.fr("FR-124")
def test_a_failed_aur_build_restores_the_realm_and_leaves_the_manifest_alone(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    _aur_answers(world)
    from ginn.packages.aur import query_argv
    from tests.unit.test_packages_aur import AFTER_GIT, AFTER_RUNTIME, AFTER_TOOLCHAIN, S0

    world.fake.exec_sequences[tuple(query_argv())] = [
        ExecResult(0, text, "")
        for text in (S0, AFTER_GIT, AFTER_RUNTIME, AFTER_TOOLCHAIN, AFTER_TOOLCHAIN)
    ]
    world.fake.exec_results[("makepkg", "--noconfirm", "--noprogressbar")] = ExecResult(
        2, "", "==> ERROR: A failure occurred in build()"
    )
    env = _env(world)
    with pytest.raises(GinnError) as caught:
        install_packages(
            env, world.fake, RealmName(ARCH), ["yay-bin"], aur=True, session=_session(world),
            gid=GID, hooks=_hooks(world),
        )  # fmt: skip
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert _manifest(world, ARCH) == []
    removals = [
        call.argv for call in world.fake.exec_calls if call.argv[:2] == ("pacman", "--remove")
    ]
    assert removals and "yay-bin" not in removals[0]
    (entry,) = _audit(env, "install")
    assert entry.error_code == "GINN-E-PKG-MANAGER"


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-042")
@pytest.mark.fr("DR-007")
@pytest.mark.fr("FR-124")
def test_kali_install_refreshes_then_installs_as_root_like_debian(world: World) -> None:
    make_realm(world, KALI, RealmState.RUNNING, running=True, distro="kali")
    env = _env(world)
    record = install_packages(
        env,
        world.fake,
        RealmName(KALI),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    # Kali lists `install` in refresh_before, as Debian does (kali.toml, ADR-011).
    assert _execs(world) == [
        _argv("kali", Verb.REFRESH),
        _argv("kali", Verb.INSTALL, "htop"),
        DESKTOP_LIST,
    ]
    for call in world.fake.exec_calls:
        assert call.user == "0:0"
    assert record.manifest == [_ginn("htop")]
    (entry,) = _audit(env, "install")
    assert entry.outcome == "ok"
    assert entry.params["packages"] == ["htop"]


@pytest.mark.fr("FR-040")
def test_a_stopped_realm_starts_first_and_the_install_runs_after_the_start(world: World) -> None:
    make_realm(world, ARCH, RealmState.STOPPED, running=False, distro="arch")
    env = _env(world)
    install_packages(
        env,
        world.fake,
        RealmName(ARCH),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    ops = world.fake.ops()
    assert "start" in ops
    assert ops.index("start") < ops.index("exec")
    assert _audit(env, "start") and _audit(env, "install")
    assert stored(world, ARCH).state is RealmState.RUNNING


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-124")
def test_a_failed_install_step_is_pkg_manager_and_leaves_the_manifest_alone(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    world.fake.exec_results[_argv("arch", Verb.INSTALL, "htop")] = ExecResult(
        1, "", "error: target not found: htop"
    )
    env = _env(world)
    with pytest.raises(GinnError) as caught:
        install_packages(
            env,
            world.fake,
            RealmName(ARCH),
            ["htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert caught.value.details["step"] == "install"
    assert _manifest(world, ARCH) == []
    (entry,) = _audit(env, "install")
    assert entry.outcome == "error"
    assert entry.error_code == "GINN-E-PKG-MANAGER"


@pytest.mark.fr("FR-012")
@pytest.mark.fr("FR-040")
def test_a_signature_failure_is_pkg_signature_with_exit_65(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    world.fake.exec_results[_argv("arch", Verb.INSTALL, "htop")] = ExecResult(
        1, "", "error: htop: signature from 'x' is unknown trust"
    )
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName(ARCH),
            ["htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-SIGNATURE"
    assert caught.value.exit_code == EXIT_SIGNATURE


@pytest.mark.fr("FR-120")
def test_a_network_failure_is_retried_with_the_fr120_delays_then_raised(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.fail_exec_with = "GINN-E-NETWORK"
    sleeps: list[float] = []
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName(DEB),
            ["htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world, sleeps),
        )
    assert caught.value.code == "GINN-E-NETWORK"
    assert sleeps == [5.0, 15.0, 45.0]


@pytest.mark.fr("FR-123")
def test_a_held_realm_lock_refuses_the_install_as_busy(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    env = _env(world)
    with realm_lock(env.paths, RealmName(ARCH), "start"), pytest.raises(GinnError) as caught:
        install_packages(
            env,
            world.fake,
            RealmName(ARCH),
            ["htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-REALM-BUSY"
    assert caught.value.exit_code == 75
    assert world.fake.exec_calls == []


@pytest.mark.fr("DR-020")
@pytest.mark.fr("FR-040")
def test_a_realm_in_a_state_that_forbids_a_change_is_refused_before_podman(world: World) -> None:
    make_realm(world, ARCH, RealmState.CREATING, running=False, distro="arch")
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName(ARCH),
            ["htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-REALM-STATE"
    assert caught.value.exit_code == 4
    assert world.fake.calls == []


@pytest.mark.fr("FR-025")
def test_an_unknown_realm_is_refused_before_podman(world: World) -> None:
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName("nope"),
            ["htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-NO-SUCH-REALM"
    assert world.fake.calls == []


@pytest.mark.fr("FR-041")
@pytest.mark.fr("DR-007")
def test_remove_runs_the_dry_run_then_removes_and_drops_the_entry(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    store = RealmStore(world.paths)
    record = stored(world, DEB)
    store.save(_with_manifest(record, [_ginn("htop")]))
    world.fake.exec_results[_argv("debian", Verb.REMOVE_CHECK, "htop")] = ExecResult(
        0, "Remv htop [3.3]\n", ""
    )
    env = _env(world)
    remove_packages(
        env, world.fake, RealmName(DEB), ["htop"], session=_session(world), hooks=_hooks(world)
    )
    assert _execs(world) == [
        _argv("debian", Verb.REMOVE_CHECK, "htop"),
        _argv("debian", Verb.REMOVE, "htop"),
        DESKTOP_LIST,
    ]
    assert _manifest(world, DEB) == []
    (entry,) = _audit(env, "remove")
    assert entry.outcome == "ok"


@pytest.mark.fr("FR-041")
@pytest.mark.fr("DR-007")
def test_kali_remove_runs_the_dry_run_then_removes_like_debian(world: World) -> None:
    make_realm(world, KALI, RealmState.RUNNING, running=True, distro="kali")
    store = RealmStore(world.paths)
    record = stored(world, KALI)
    store.save(_with_manifest(record, [_ginn("htop")]))
    world.fake.exec_results[_argv("kali", Verb.REMOVE_CHECK, "htop")] = ExecResult(
        0, "Remv htop [3.3]\n", ""
    )
    env = _env(world)
    remove_packages(
        env, world.fake, RealmName(KALI), ["htop"], session=_session(world), hooks=_hooks(world)
    )
    # Kali defines remove-check, the dry run that ADR-011 requires before remove.
    assert _execs(world) == [
        _argv("kali", Verb.REMOVE_CHECK, "htop"),
        _argv("kali", Verb.REMOVE, "htop"),
        DESKTOP_LIST,
    ]
    assert _manifest(world, KALI) == []
    (entry,) = _audit(env, "remove")
    assert entry.outcome == "ok"


@pytest.mark.fr("FR-041")
@pytest.mark.fr("NFR-SEC-005")
def test_a_removal_that_would_take_another_package_is_refused_before_it_runs(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[_argv("debian", Verb.REMOVE_CHECK, "gimp")] = ExecResult(
        0, "Remv gimp [2.10]\nRemv libgimp2.0 [2.10]\n", ""
    )
    env = _env(world)
    with pytest.raises(GinnError) as caught:
        remove_packages(
            env,
            world.fake,
            RealmName(DEB),
            ["gimp"],
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-DEPENDENTS"
    assert caught.value.exit_code == 4
    assert caught.value.details["dependents"] == "libgimp2.0"
    assert _execs(world) == [_argv("debian", Verb.REMOVE_CHECK, "gimp")]
    (entry,) = _audit(env, "remove")
    assert entry.error_code == "GINN-E-PKG-DEPENDENTS"


@pytest.mark.fr("FR-041")
@pytest.mark.fr("FR-124")
def test_an_unread_dry_run_refuses_the_removal_and_runs_no_remove(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[_argv("debian", Verb.REMOVE_CHECK, "gimp")] = ExecResult(
        100, "", "E: could not get lock"
    )
    with pytest.raises(GinnError) as caught:
        remove_packages(
            _env(world),
            world.fake,
            RealmName(DEB),
            ["gimp"],
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert _execs(world) == [_argv("debian", Verb.REMOVE_CHECK, "gimp")]


@pytest.mark.fr("FR-041")
def test_arch_removal_has_no_dry_run_and_no_cascade_flag(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    store = RealmStore(world.paths)
    store.save(_with_manifest(stored(world, ARCH), [_native("htop")]))
    remove_packages(
        _env(world),
        world.fake,
        RealmName(ARCH),
        ["htop"],
        session=_session(world),
        hooks=_hooks(world),
    )
    argv, listing = _execs(world)
    assert argv == _argv("arch", Verb.REMOVE, "htop")
    assert listing == DESKTOP_LIST
    assert not {"--recursive", "--cascade", "-c", "-s", "--nodeps"} & set(argv)
    assert _manifest(world, ARCH) == []


@pytest.mark.fr("FR-041")
@pytest.mark.fr("FR-040")
def test_install_then_remove_round_trips_the_manifest(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    env = _env(world)
    install_packages(
        env,
        world.fake,
        RealmName(ARCH),
        ["htop", "gimp"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    assert [entry.name for entry in _manifest(world, ARCH)] == ["gimp", "htop"]
    remove_packages(
        env,
        world.fake,
        RealmName(ARCH),
        ["htop"],
        session=_session(world),
        hooks=_hooks(world),
    )
    assert _manifest(world, ARCH) == [_ginn("gimp")]


def _with_manifest(record: RealmRecord, manifest: list[ManifestEntry]) -> RealmRecord:
    return replace(record, manifest=manifest)


# ---- T5.3 review fixes: name rules, reconciliation after a failure, network, escaping -------

DEBIAN_NETWORK = (
    "E: Failed to fetch http://deb.debian.org/debian/pool/main/g/gimp/gimp.deb  "
    "Temporary failure resolving 'deb.debian.org'\n"
)
DEBIAN_SIGNATURE_AND_NETWORK = (
    "W: GPG error: http://deb.debian.org stable InRelease: NO_PUBKEY 0123ABCD\n"
    "E: Failed to fetch http://deb.debian.org/x  Temporary failure resolving 'x'\n"
)
LIST_DEB = _argv("debian", Verb.LIST_EXPLICIT)
INSTALL_DEB = _argv("debian", Verb.INSTALL, "gimp", "htop")


@pytest.mark.fr("DR-003")
@pytest.mark.fr("AS-027")
@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("op", ["install", "remove"])
def test_a_name_ending_in_a_dash_is_refused_before_any_podman_call(world: World, op: str) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    env = _env(world)
    action = install_packages if op == "install" else remove_packages
    kwargs: dict[str, Any] = {"aur": False} if op == "install" else {}
    with pytest.raises(GinnError) as caught:
        action(
            env,
            world.fake,
            RealmName(DEB),
            ["gcc-"],
            session=_session(world),
            hooks=_hooks(world),
            **kwargs,
        )
    assert caught.value.code == "GINN-E-PKG-NAME-INVALID"
    assert "trailing '-'" in caught.value.details["rule"]
    assert world.fake.calls == []
    assert _audit(env, op) == []


@pytest.mark.fr("FR-040")
@pytest.mark.fr("AS-027")
def test_a_plus_suffix_is_a_real_name_and_installs(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    install_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        ["g++"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    # The last exec is the menu sync's listing, after the install.
    assert _execs(world)[-2] == _argv("debian", Verb.INSTALL, "g++")
    assert _execs(world)[-1] == DESKTOP_LIST


@pytest.mark.fr("FR-040")
@pytest.mark.fr("DR-003")
def test_a_fedora_name_ending_in_rpm_is_refused_as_a_file_path(world: World) -> None:
    make_realm(world, "fed", RealmState.RUNNING, running=True, distro="fedora")
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName("fed"),
            ["gimp.rpm"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-NAME-INVALID"
    assert world.fake.calls == []


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-124")
def test_a_failed_install_records_the_requested_names_the_realm_now_lists(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[INSTALL_DEB] = ExecResult(100, "", "E: Unable to locate package gimp")
    world.fake.exec_sequences[LIST_DEB] = [ExecResult(0, "htop\nvim\n", "")]
    env = _env(world)
    with pytest.raises(GinnError) as caught:
        install_packages(
            env,
            world.fake,
            RealmName(DEB),
            ["gimp", "htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert _manifest(world, DEB) == [_ginn("htop")]
    (entry,) = _audit(env, "install")
    assert entry.error_code == "GINN-E-PKG-MANAGER"


@pytest.mark.fr("FR-041")
@pytest.mark.fr("DR-007")
def test_a_failed_remove_drops_the_names_the_realm_no_longer_lists(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    store = RealmStore(world.paths)
    store.save(_with_manifest(stored(world, ARCH), [_ginn("gimp"), _ginn("htop")]))
    world.fake.exec_results[_argv("arch", Verb.REMOVE, "htop", "gimp")] = ExecResult(
        1, "", "error: target not found: x"
    )
    world.fake.exec_sequences[_argv("arch", Verb.LIST_EXPLICIT)] = [ExecResult(0, "gimp\n", "")]
    with pytest.raises(GinnError):
        remove_packages(
            _env(world),
            world.fake,
            RealmName(ARCH),
            ["htop", "gimp"],
            session=_session(world),
            hooks=_hooks(world),
        )
    assert _manifest(world, ARCH) == [_ginn("gimp")]


@pytest.mark.fr("FR-040")
def test_a_failed_install_whose_list_cannot_be_read_keeps_the_manifest_and_its_error(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[INSTALL_DEB] = ExecResult(100, "", "E: Unable to locate package gimp")
    world.fake.exec_results[LIST_DEB] = ExecResult(1, "", "E: could not read the database")
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName(DEB),
            ["gimp", "htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert caught.value.details["step"] == "install"
    assert _manifest(world, DEB) == []


@pytest.mark.fr("FR-120")
@pytest.mark.fr("FR-040")
def test_a_realistic_network_failure_is_retried_with_the_delays_and_raised_as_network(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[INSTALL_DEB] = ExecResult(100, "", DEBIAN_NETWORK)
    sleeps: list[float] = []
    env = _env(world)
    with pytest.raises(GinnError) as caught:
        install_packages(
            env,
            world.fake,
            RealmName(DEB),
            ["gimp", "htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world, sleeps),
        )
    assert caught.value.code == "GINN-E-NETWORK"
    assert caught.value.exit_code == 69
    assert sleeps == [5.0, 15.0, 45.0]
    assert _execs(world).count(INSTALL_DEB) == 4
    assert _manifest(world, DEB) == []
    (entry,) = _audit(env, "install")
    assert entry.error_code == "GINN-E-NETWORK"


@pytest.mark.fr("FR-012")
@pytest.mark.fr("FR-120")
def test_a_signature_error_wins_over_network_text_in_an_install(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[INSTALL_DEB] = ExecResult(100, "", DEBIAN_SIGNATURE_AND_NETWORK)
    sleeps: list[float] = []
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName(DEB),
            ["gimp", "htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world, sleeps),
        )
    assert caught.value.code == "GINN-E-PKG-SIGNATURE"
    assert sleeps == []


@pytest.mark.fr("NFR-040")
@pytest.mark.fr("FR-041")
def test_a_dependent_name_with_a_control_character_is_escaped_in_the_refusal(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[_argv("debian", Verb.REMOVE_CHECK, "gimp")] = ExecResult(
        0, "Remv gimp [2.10]\nRemv gi\x07mp [1]\n", ""
    )
    with pytest.raises(GinnError) as caught:
        remove_packages(
            _env(world),
            world.fake,
            RealmName(DEB),
            ["gimp"],
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-DEPENDENTS"
    assert "\x07" not in caught.value.message
    assert "gi\\x07mp" in caught.value.message


def _recording_sync(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, bool]]) -> None:
    """Wrap the menu sync (FR-044): record each call's operation and whether the realm lock is held.

    A second holder of the lock is refused as busy, so a probe inside the call shows that the call
    runs under the lock (FR-123). The real sync still runs.
    """
    real = packages_module.sync_in_lock

    def recording(
        env: OpEnv, podman: Any, name: RealmName, *, mutation: Any, hooks: Any = None
    ) -> Any:
        try:
            with realm_lock(env.paths, name, "probe"):
                held = False
        except GinnError as err:
            held = err.code == "GINN-E-REALM-BUSY"
        calls.append((mutation.op, held))
        return real(env, podman, name, mutation=mutation, hooks=hooks)

    monkeypatch.setattr(packages_module, "sync_in_lock", recording)


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-123")
def test_an_install_syncs_the_menu_once_under_the_realm_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    calls: list[tuple[str, bool]] = []
    _recording_sync(monkeypatch, calls)
    install_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
    )
    assert calls == [("install", True)]
    # Refresh, install, and one listing of the desktop files: no call per application.
    assert _execs(world) == [
        _argv("debian", Verb.REFRESH),
        _argv("debian", Verb.INSTALL, "htop"),
        DESKTOP_LIST,
    ]
    assert len(world.fake.exec_calls) == 3


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-123")
def test_a_removal_syncs_the_menu_once_under_the_realm_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    RealmStore(world.paths).save(_with_manifest(stored(world, DEB), [_ginn("htop")]))
    world.fake.exec_results[_argv("debian", Verb.REMOVE_CHECK, "htop")] = ExecResult(
        0, "Remv htop [3.3]\n", ""
    )
    calls: list[tuple[str, bool]] = []
    _recording_sync(monkeypatch, calls)
    remove_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        ["htop"],
        session=_session(world),
        hooks=_hooks(world),
    )
    assert calls == [("remove", True)]
    assert _execs(world) == [
        _argv("debian", Verb.REMOVE_CHECK, "htop"),
        _argv("debian", Verb.REMOVE, "htop"),
        DESKTOP_LIST,
    ]


@pytest.mark.fr("FR-044")
def test_a_failed_menu_listing_after_an_install_is_a_warning_and_the_install_stands(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    world.fake.exec_results[DESKTOP_LIST] = ExecResult(1, "", "find: Permission denied")
    warnings: list[Any] = []
    env = _env(world)
    record = install_packages(
        env,
        world.fake,
        RealmName(DEB),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
        warnings=warnings,
    )
    assert record.manifest == [_ginn("htop")]
    (entry,) = _audit(env, "install")
    assert entry.outcome == "ok"
    assert [w.code for w in warnings] == ["GINN-W-EXPORT-SYNC"]
    assert "find exited with status 1" in warnings[0].message
    assert CATALOGUE["GINN-W-EXPORT-SYNC"].exit_code == 0


@pytest.mark.fr("FR-044")
def test_a_failed_menu_listing_after_a_removal_is_a_warning_and_the_removal_stands(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    RealmStore(world.paths).save(_with_manifest(stored(world, DEB), [_ginn("htop")]))
    world.fake.exec_results[_argv("debian", Verb.REMOVE_CHECK, "htop")] = ExecResult(
        0, "Remv htop [3.3]\n", ""
    )
    world.fake.exec_results[DESKTOP_LIST] = ExecResult(1, "", "find: Permission denied")
    warnings: list[Any] = []
    env = _env(world)
    record = remove_packages(
        env,
        world.fake,
        RealmName(DEB),
        ["htop"],
        session=_session(world),
        hooks=_hooks(world),
        warnings=warnings,
    )
    assert record.manifest == []
    (entry,) = _audit(env, "remove")
    assert entry.outcome == "ok"
    assert [w.code for w in warnings] == ["GINN-W-EXPORT-SYNC"]


def _menu_app(world: World, app: str = "gimp") -> None:
    """One desktop file in the realm, which the menu sync lists (FR-044)."""
    path = f"/usr/share/applications/{app}.desktop"
    content = b"[Desktop Entry]\nType=Application\nName=GIMP\nExec=gimp %U\n"
    world.fake.files[path] = content
    rows = f"{len(content)} 1700000000.0 {path}\n"
    world.fake.exec_results[DESKTOP_LIST] = ExecResult(0, rows, "")


@pytest.mark.fr("FR-044")
def test_an_install_writes_the_absolute_program_and_a_following_sync_is_a_noop(
    world: World,
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    _menu_app(world)
    program = "/opt/ginn/bin/ginn"
    install_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=replace(_hooks(world), program=program),
    )
    text = (world.paths.applications_dir / "ginn-deb-gimp.desktop").read_text(encoding="utf-8")
    assert f"Exec={program} exec deb -- gimp" in text
    assert "Exec=ginn " not in text
    # The next sync runs the same program, so the stamp matches and nothing is written again.
    result = sync_realm(_env(world), world.fake, RealmName(DEB), hooks=SyncHooks(program=program))
    assert result.unchanged == ["gimp"]
    assert (result.added, result.updated, result.removed) == ([], [], [])


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-044")
def test_a_journal_write_error_during_the_menu_sync_propagates_and_is_not_a_warning(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    real_step = Journal.step

    def failing(
        self: Journal,
        entry: JournalEntry,
        name: str,
        params: Any = None,
        *,
        drop: Sequence[str] = (),
    ) -> JournalEntry:
        if name == "listed":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_step(self, entry, name, params, drop=drop)

    monkeypatch.setattr(Journal, "step", failing)
    with pytest.raises(OSError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName(DEB),
            ["htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.strerror == "No space left on device"


@pytest.mark.fr("FR-044")
def test_a_skipped_entry_warning_of_the_sync_reaches_the_caller(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    skipped = warn("GINN-W-EXPORT-SKIPPED", app="gimp", realm=DEB, reason="not an application")

    def skipping(
        env: OpEnv, podman: Any, name: RealmName, *, mutation: Any, hooks: Any = None
    ) -> Any:
        return SyncResult(name.value, warnings=[skipped])

    monkeypatch.setattr(packages_module, "sync_in_lock", skipping)
    warnings: list[Any] = []
    install_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
        warnings=warnings,
    )
    assert warnings == [skipped]


@pytest.mark.fr("FR-044")
def test_a_sync_that_finds_the_realm_not_running_adds_no_warning(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")

    def stopped(
        env: OpEnv, podman: Any, name: RealmName, *, mutation: Any, hooks: Any = None
    ) -> Any:
        return SyncResult(name.value, running=False)

    monkeypatch.setattr(packages_module, "sync_in_lock", stopped)
    warnings: list[Any] = []
    install_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        ["htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
        warnings=warnings,
    )
    assert warnings == []


@pytest.mark.fr("FR-044")
def test_a_failed_install_does_not_sync_the_menu(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    world.fake.exec_results[_argv("arch", Verb.INSTALL, "htop")] = ExecResult(
        1, "", "error: target not found: htop"
    )
    calls: list[tuple[str, bool]] = []
    _recording_sync(monkeypatch, calls)
    with pytest.raises(GinnError):
        install_packages(
            _env(world),
            world.fake,
            RealmName(ARCH),
            ["htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert calls == []
    assert DESKTOP_LIST not in _execs(world)


# ---- T7.26 init entries (ADR-015 Decision 14): an install is a no-op, a remove is refused --------


@pytest.mark.fr("FR-040")
def test_installing_an_init_entry_is_a_manifest_no_op_with_a_note(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    RealmStore(world.paths).save(_with_manifest(stored(world, DEB), [_init("systemd")]))
    env = _env(world)
    notes: list[str] = []
    record = install_packages(
        env,
        world.fake,
        RealmName(DEB),
        ["systemd"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
        notes=notes,
    )
    # No podman call at all: no refresh, no install, no menu sync, and no mutating operation.
    assert world.fake.calls == []
    assert _audit(env, "install") == []
    assert record.manifest == [_init("systemd")]
    assert _manifest(world, DEB) == [_init("systemd")]
    (note,) = notes
    assert "'systemd' is an init package of realm 'deb'" in note
    assert "changed nothing" in note


@pytest.mark.fr("FR-040")
def test_an_init_entry_in_an_install_is_skipped_and_the_other_names_install(world: World) -> None:
    make_realm(world, DEB, RealmState.RUNNING, running=True, distro="debian")
    RealmStore(world.paths).save(_with_manifest(stored(world, DEB), [_init("systemd")]))
    env = _env(world)
    notes: list[str] = []
    install_packages(
        env,
        world.fake,
        RealmName(DEB),
        ["systemd", "htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
        notes=notes,
    )
    # The package manager is asked for htop only, and never for the init entry.
    assert _execs(world) == [
        _argv("debian", Verb.REFRESH),
        _argv("debian", Verb.INSTALL, "htop"),
        DESKTOP_LIST,
    ]
    assert _manifest(world, DEB) == [_ginn("htop"), _init("systemd")]
    (entry,) = _audit(env, "install")
    assert entry.params["packages"] == ["htop"]
    assert len(notes) == 1
    assert "'systemd'" in notes[0]


@pytest.mark.fr("FR-040")
def test_an_install_of_init_entries_only_starts_nothing_in_a_stopped_realm(world: World) -> None:
    make_realm(world, DEB, RealmState.STOPPED, running=False, distro="debian")
    RealmStore(world.paths).save(_with_manifest(stored(world, DEB), [_init("systemd")]))
    notes: list[str] = []
    record = install_packages(
        _env(world),
        world.fake,
        RealmName(DEB),
        ["systemd"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
        notes=notes,
    )
    assert world.fake.calls == []
    assert record.state is RealmState.STOPPED
    assert len(notes) == 1


@pytest.mark.fr("DR-020")
@pytest.mark.fr("FR-040")
def test_an_init_only_install_in_a_state_that_forbids_a_change_is_still_refused(
    world: World,
) -> None:
    make_realm(world, ARCH, RealmState.CREATING, running=False, distro="arch")
    RealmStore(world.paths).save(_with_manifest(stored(world, ARCH), [_init("systemd")]))
    notes: list[str] = []
    with pytest.raises(GinnError) as caught:
        install_packages(
            _env(world),
            world.fake,
            RealmName(ARCH),
            ["systemd"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
            notes=notes,
        )
    assert caught.value.code == "GINN-E-REALM-STATE"
    assert world.fake.calls == []
    assert notes == []


@pytest.mark.fr("FR-041")
@pytest.mark.parametrize("packages", [["systemd"], ["htop", "systemd"]])
@pytest.mark.parametrize(
    ("state", "running"), [(RealmState.RUNNING, True), (RealmState.STOPPED, False)]
)
def test_removing_an_init_entry_is_refused_as_a_whole_before_any_package_manager_call(
    world: World, packages: list[str], state: RealmState, running: bool
) -> None:
    make_realm(world, DEB, state, running=running, distro="debian")
    manifest = [_ginn("htop"), _init("systemd")]
    RealmStore(world.paths).save(_with_manifest(stored(world, DEB), manifest))
    env = _env(world)
    with pytest.raises(GinnError) as caught:
        remove_packages(
            env,
            world.fake,
            RealmName(DEB),
            packages,
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-PROTECTED"
    assert caught.value.exit_code == 4
    assert caught.value.details["name"] == "systemd"
    assert caught.value.details["realm"] == DEB
    # Nothing ran: no start of a stopped realm, no dry run, no remove, no audit entry.
    assert world.fake.calls == []
    assert _audit(env, "remove") == []
    assert _manifest(world, DEB) == manifest


@pytest.mark.fr("FR-041")
def test_an_init_entry_that_a_start_makes_is_refused_again_under_the_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, ARCH, RealmState.STOPPED, running=False, distro="arch")
    RealmStore(world.paths).save(
        _with_manifest(stored(world, ARCH), [_ginn("htop"), _ginn("systemd")])
    )

    def rebuilding_start(env: OpEnv, podman: Any, name: RealmName, **_: Any) -> RealmRecord:
        # A rebuild in the start makes systemd an init entry, so the init set changes after the
        # early check of remove_packages (ADR-015 Decision 6). The body must refuse under the lock.
        store = RealmStore(env.paths)
        current = store.load(name)
        assert current.container_id is not None
        world.fake.start(current.container_id)
        updated = replace(
            current,
            state=RealmState.RUNNING,
            manifest=[_ginn("htop"), _init("systemd")],
        )
        store.save(updated)
        return updated

    monkeypatch.setattr(packages_module, "start_realm", rebuilding_start)
    env = _env(world)
    with pytest.raises(GinnError) as caught:
        remove_packages(
            env,
            world.fake,
            RealmName(ARCH),
            ["systemd"],
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-PROTECTED"
    # No package manager step ran, and the init entry is still there.
    assert _execs(world) == []
    assert _manifest(world, ARCH) == [_ginn("htop"), _init("systemd")]
    (entry,) = _audit(env, "remove")
    assert entry.error_code == "GINN-E-PKG-PROTECTED"


@pytest.mark.fr("FR-041")
def test_a_refused_removal_lists_every_init_name_it_refuses(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    RealmStore(world.paths).save(
        _with_manifest(stored(world, ARCH), [_init("systemd"), _init("dbus")])
    )
    with pytest.raises(GinnError) as caught:
        remove_packages(
            _env(world),
            world.fake,
            RealmName(ARCH),
            ["systemd", "dbus"],
            session=_session(world),
            hooks=_hooks(world),
        )
    assert caught.value.code == "GINN-E-PKG-PROTECTED"
    assert caught.value.details["name"] == "systemd, dbus"
    assert world.fake.calls == []


@pytest.mark.fr("FR-040")
def test_the_init_notes_survive_a_failed_install_of_the_other_names(world: World) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    RealmStore(world.paths).save(_with_manifest(stored(world, ARCH), [_init("systemd")]))
    world.fake.exec_results[_argv("arch", Verb.INSTALL, "htop")] = ExecResult(
        1, "", "error: target not found: htop"
    )
    notes: list[str] = []
    with pytest.raises(GinnError):
        install_packages(
            _env(world),
            world.fake,
            RealmName(ARCH),
            ["systemd", "htop"],
            aur=False,
            session=_session(world),
            hooks=_hooks(world),
            notes=notes,
        )
    (note,) = notes
    assert "'systemd' is an init package of realm 'arch'" in note


@pytest.mark.fr("FR-040")
def test_sent_lists_the_names_the_manager_was_asked_for_and_nothing_for_an_init_only_call(
    world: World,
) -> None:
    make_realm(world, ARCH, RealmState.RUNNING, running=True, distro="arch")
    RealmStore(world.paths).save(_with_manifest(stored(world, ARCH), [_init("systemd")]))
    env = _env(world)
    sent: list[str] = []
    install_packages(
        env,
        world.fake,
        RealmName(ARCH),
        ["systemd", "htop", "htop"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
        sent=sent,
    )
    assert sent == ["htop"]
    init_only: list[str] = []
    install_packages(
        env,
        world.fake,
        RealmName(ARCH),
        ["systemd"],
        aur=False,
        session=_session(world),
        hooks=_hooks(world),
        sent=init_only,
    )
    assert init_only == []
