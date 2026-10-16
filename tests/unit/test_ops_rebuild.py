# SPDX-License-Identifier: Apache-2.0
"""Realm rebuild: the new generation, the replay, the swap, the refusals (T8.3a).

Covers ops/rebuild.py against FakePodman, with the create fixtures of test_ops_create (the same
ScriptedPodman, the same host and home fixtures). A realm is made by `create_realm`, so the old root
is a real create's root. The fault matrix of NFR-031 is in test_ops_rebuild_faults.py.

Each rebuild runs under `image_lock`, `mutating` and the journal, so the outcomes are read the way
the choke point records them (ops/_mutating.py): an ok rebuild has one `ok` audit entry, and a
failed one has its error code. Nothing here runs podman, sudo or the helper binary (AGENTS.md).
"""

import errno
import os
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ginn.distros import Verb
from ginn.distros import load as load_distro
from ginn.domain import InstallMethod, InstallSource, ManifestEntry, RealmName, RealmState, Tier
from ginn.domain.permissions import Toggle, effective_toggles
from ginn.errors import GinnError, make
from ginn.ops import create as create_ops
from ginn.ops import lifecycle as lifecycle_ops
from ginn.ops import rebuild as rebuild_ops
from ginn.ops._create_plan import estimate_bytes
from ginn.ops._mutating import OpEnv
from ginn.ops.create import create_realm
from ginn.ops.disk import estimate_required
from ginn.ops.lifecycle import HostUser, LifecycleHooks, realm_status, start_realm, stop_realm
from ginn.ops.permissions import set_permissions, set_tier
from ginn.ops.provision import storage_required_bytes, tmpdir_required_bytes
from ginn.ops.rebuild import RebuildHooks, RebuildResult, rebuild_for_start, rebuild_realm
from ginn.ops.recovery import OUTCOME_RECOVERED, recover
from ginn.ops.service import ServiceHooks, set_autostart, set_services_mode, start_service
from ginn.packages.aur import AurOutcome
from ginn.podman.adapter import CreateSpec, ExecTarget
from ginn.podman.models import ContainerState, ExecResult
from ginn.runtime.idle import IdleHooks
from ginn.runtime.provision import derived_image_prefix, install_argv, keep_id_entry_drop_argv
from ginn.runtime.realm_security import SecurityHooks
from ginn.runtime.spec import LABEL_GENERATION, LABEL_REALM, MACHINE_ID_PATH, RUN_USER_TMPFILES_PATH
from ginn.security.caps import option_keys
from ginn.security.nested import CONFIG_DIR, FILES
from ginn.services.runit_user import DOWN, journald_dropin, service_dir
from ginn.services.supervisor import Result, SupervisorHooks
from ginn.store.journal import Journal
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from ginn.trust import manifest as trust
from tests.fakes.clock import FakeClock
from tests.unit.test_ops_create import (
    AMPLE,
    GID,
    UID,
    USER,
    ScriptedPodman,
    _AbsentProbe,
    _kali_everything_answers,
    _kvm_hooks,
    _seccomp_source,
    _statvfs,
    hooks_for,
    request,
)
from tests.unit.test_ops_create import (
    host_identity_is_read_only_under_tmp as host_identity_is_read_only_under_tmp,  # noqa: PLC0414
)
from tests.unit.test_ops_create import mountinfo as mountinfo  # noqa: PLW0406 - fixture
from tests.unit.test_ops_create import paths as paths  # noqa: PLW0406 - fixture
from tests.unit.test_ops_delete import kill_journal_owner
from tests.unit.test_ops_lifecycle import SV, RunitFake

NAME = RealmName("debian")
DISTRO = "debian"
RUNTIME_SUBDIR = "xdg-runtime"


@pytest.fixture
def host(tmp_path: Path) -> Path:
    root = tmp_path / "host"
    for scope in ("etc", "var/db/xbps", "var/service", "usr/local", "opt"):
        (root / scope).mkdir(parents=True)
    (root / "etc" / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    return root


@pytest.fixture
def env(paths: OwnedPaths, host: Path) -> OpEnv:
    return OpEnv.for_paths(paths, USER, root_dir=host, clock=FakeClock())


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """The host home of the user, under tmp_path, so it is outside Owned Paths."""
    path = tmp_path / "home" / USER
    path.mkdir(parents=True)
    return path


PROVISION_PREFIX = "ginn_provision_"
"""The name prefix of a provisioning container (ADR-015 Decision 10)."""


def user_entries(record: RealmRecord) -> list[ManifestEntry]:
    """The manifest without the init entries, which provisioning installs (ADR-015 Decision 6)."""
    return [entry for entry in record.manifest if not entry.init]


BOOT_STATE_ARGV = ("systemctl", "is-system-running")
"""The boot check's exec in a services-on build (ADR-015 Decision 5)."""
BOOT_FAILED_ARGV = ("systemctl", "--failed", "--no-legend", "--plain")
"""The failed-units read of a boot that did not reach running (Decision 5)."""


@pytest.fixture
def fake(tmp_path: Path) -> ScriptedPodman:
    scripted = ScriptedPodman(root=str(tmp_path))
    # A boot that settles with no failed unit is the default; a test scripts another boot itself.
    scripted.exec_results[BOOT_STATE_ARGV] = ExecResult(0, "running\n", "")
    return scripted


@pytest.fixture
def sleeps() -> list[float]:
    return []


def session(home: Path) -> HostUser:
    """The host user of a test: the uid and home of create's request, and its empty runtime dir."""
    runtime = home.parent / RUNTIME_SUBDIR
    runtime.mkdir(exist_ok=True)
    return HostUser(uid=UID, host_home=home, host_env={"XDG_RUNTIME_DIR": str(runtime)})


def _under(tmp_path: Path) -> Callable[[str], bool]:
    return lambda path: str(path).startswith(str(tmp_path))


def lifecycle_hooks(tmp_path: Path, mountinfo: Path) -> LifecycleHooks:
    """The start and stop seams. Only paths under tmp_path exist, and no idle helper runs."""
    exists = _under(tmp_path)
    return LifecycleHooks(
        exists=lambda p: exists(p) and os.path.exists(p),
        mountinfo=mountinfo,
        security=SecurityHooks(gpg_extra=lambda: None, nvidia_probe=_AbsentProbe()),
        idle=IdleHooks(helper=str(tmp_path / "absent" / "ginn-idle"), spawn=_no_spawn),
    )


def _no_spawn(argv: Any, log: Any) -> int:
    return 0


def rebuild_hooks(
    tmp_path: Path, sleeps: list[float], mountinfo: Path, **overrides: Any
) -> RebuildHooks:
    """Rebuild hooks for a test. The create seams answer as in test_ops_create."""
    values: dict[str, Any] = {
        "statvfs": _statvfs(AMPLE),
        "sleep": sleeps.append,
        "exists": _under(tmp_path),
        "mountinfo": mountinfo,
        "security": SecurityHooks(nvidia_probe=_AbsentProbe()),
        "lifecycle": lifecycle_hooks(tmp_path, mountinfo),
        "program": "/usr/bin/ginn",
    }
    values.update(overrides)
    return RebuildHooks(**values)


def make_realm(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    *,
    packages: tuple[str, ...] = ("curl",),
    running: bool = False,
) -> RealmRecord:
    """Create debian with `packages` as its manifest, and start it when `running` is set."""
    hooks = hooks_for(tmp_path, sleeps, mountinfo)
    record = create_realm(env, fake, request(home, with_packages=packages), hooks=hooks).record
    if running:
        record = start_realm(
            env,
            fake,
            NAME,
            session=session(home),
            hooks=lifecycle_hooks(tmp_path, mountinfo),
        )
    return record


def rebuild(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    **overrides: Any,
) -> RebuildResult:
    return rebuild_realm(
        env,
        fake,
        NAME,
        session=session(home),
        gid=GID,
        hooks=rebuild_hooks(tmp_path, sleeps, mountinfo, **overrides),
    )


RUN_USER_STAT = ("stat", "-c", "%u", "--", f"/run/user/{UID}")
"""The argv that `run_user_state` runs for the realm's runtime dir (T7.28)."""


def owner_checks(fake: ScriptedPodman) -> int:
    """How many owner reads of the runtime dir the fake has seen."""
    return sum(1 for call in fake.exec_calls if tuple(call.argv) == RUN_USER_STAT)


def polling(
    lifecycle: LifecycleHooks,
    runit: "_Runsv",
    sleeps_seen: list[float],
    *,
    wait_s: float,
    poll_s: float,
) -> LifecycleHooks:
    """The services-on seams with the owner wait bounded, and every sleep recorded on the way."""

    def sleep(seconds: float) -> None:
        sleeps_seen.append(seconds)
        runit.sleep(seconds)

    supervisor = SupervisorHooks(sv=SV, run=runit.run, clock=runit.clock, sleep=sleep)
    return replace(lifecycle, supervisor=supervisor, run_user_wait_s=wait_s, run_user_poll_s=poll_s)


def generation_of(fake: ScriptedPodman, generation: int) -> list[str]:
    """The ids of the containers that carry generation `generation`'s label, in creation order."""
    return [
        cid
        for cid, c in fake.containers.items()
        if c.labels.get(LABEL_GENERATION) == str(generation)
    ]


def stored(env: OpEnv) -> RealmRecord:
    return RealmStore(env.paths).load(NAME)


def home_bytes(home: Path) -> dict[str, bytes]:
    """Every file under the host home, by its path. The Realm Home is checked separately."""
    return {
        str(p.relative_to(home)): p.read_bytes() for p in sorted(home.rglob("*")) if p.is_file()
    }


def root_fields(record: RealmRecord) -> tuple[Any, ...]:
    """The fields that identify the root and its install: a rebuild must keep them, or change them
    only to what the new root has. The create-time state (the services mode of the container, its
    grants) and the building container are among them, so a failed rebuild must leave them as they
    were (FR-021, DR-034)."""
    return (
        record.generation,
        record.container_id,
        record.image_digest,
        record.manifest,
        record.settings,
        record.permissions,
        record.created_options,
        record.services_at_create,
        record.building_container_id,
        record.building_generation,
        record.grants,
    )


def realm_home_tree(path: Path) -> dict[str, bytes | None]:
    """Every entry under a Realm Home: a file's bytes, or None for a directory (NFR-031)."""
    tree: dict[str, bytes | None] = {}
    if not path.is_dir():
        return tree
    for entry in sorted(path.rglob("*")):
        tree[str(entry.relative_to(path))] = None if entry.is_dir() else entry.read_bytes()
    return tree


def replay_argv(names: list[str]) -> tuple[str, ...]:
    return tuple(load_distro(DISTRO).argv(Verb.INSTALL, names))


# The happy path (FR-021).


@pytest.mark.fr("FR-021")
def test_a_rebuild_makes_generation_two_from_the_pinned_image_and_removes_the_old_root(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    old = before.container_id
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    record = result.record
    entry = trust.load().entry(DISTRO)
    assert record.generation == 2
    assert record.state is RealmState.STOPPED
    assert record.previous_state is None
    assert record.image_digest == entry.digest
    assert record.image_ref == trust.pull_reference(DISTRO)
    assert record.container_id != old
    assert record.container_id in fake.containers
    assert fake.containers[record.container_id].labels[LABEL_GENERATION] == "2"
    assert fake.containers[record.container_id].labels[LABEL_REALM] == "debian"
    assert old not in fake.containers
    assert generation_of(fake, 1) == []
    assert result.old_removed is True
    assert result.replay_missing == ()
    assert stored(env) == record


@pytest.mark.fr("FR-021")
def test_the_rebuild_reinstalls_the_manifest_in_the_new_root(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl", "git"))
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    installs = [
        c
        for c in fake.exec_calls
        if c.argv == replay_argv(["curl", "git"]) and c.container_id == result.record.container_id
    ]
    assert len(installs) == 1
    assert [(e.name, e.source) for e in result.record.manifest] == [
        ("curl", InstallSource.REPO),
        ("git", InstallSource.REPO),
    ]


@pytest.mark.fr("FR-021")
def test_the_home_files_are_unchanged_and_the_settings_and_permissions_are_kept(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    (home / "notes.txt").write_text("keep me\n", encoding="utf-8")
    realm_home = env.paths.homes / "debian"
    (realm_home / "config.txt").write_text("realm keeps this\n", encoding="utf-8")
    host_before = home_bytes(home)
    realm_before = home_bytes(realm_home)
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert home_bytes(home) == host_before
    assert home_bytes(realm_home) == realm_before
    assert result.record.settings == before.settings
    assert result.record.permissions == before.permissions
    assert result.record.settings.tier is Tier.BASIC
    assert result.record.settings.services is False
    assert result.record.created_options == before.created_options


@pytest.mark.fr("FR-021")
def test_a_stopped_realm_is_started_once_to_sync_its_menu_and_is_stopped_again(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync reads only a running realm (FR-044), so a stopped realm is started on gN+1, synced,
    and stopped again. Its exported entries are then those of gN+1 (FR-021 AC-1)."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    synced: list[str] = []
    real_sync = rebuild_ops.sync_realm

    def spy(env_: OpEnv, podman: Any, name: RealmName, **kwargs: Any) -> Any:
        synced.append(stored(env_).state.value)
        return real_sync(env_, podman, name, **kwargs)

    monkeypatch.setattr(rebuild_ops, "sync_realm", spy)
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert result.record.state is RealmState.STOPPED
    assert result.record.generation == 2
    assert synced == ["running"], "the sync runs while the realm is running"
    assert fake.containers[result.record.container_id].state is ContainerState.EXITED


@pytest.mark.fr("FR-021")
def test_a_running_realm_starts_on_the_new_generation_and_is_synced_after(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, running=True)
    old = stored(env).container_id
    seen: list[RealmState] = []

    def sync(env_: OpEnv, podman: Any, name: RealmName, *, hooks: Any) -> Any:
        seen.append(stored(env_).state)
        return type("Sync", (), {"warnings": []})()

    monkeypatch.setattr(rebuild_ops, "sync_realm", sync)
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    record = result.record
    assert record.generation == 2
    assert record.state is RealmState.RUNNING
    assert fake.containers[record.container_id].state is ContainerState.RUNNING
    assert old not in fake.containers
    assert seen == [RealmState.RUNNING]


# Replay: packages the upstream no longer offers (FR-022).


@pytest.mark.fr("FR-022")
def test_a_package_no_longer_available_is_reported_and_the_others_install_and_the_swap_completes(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl", "gone"))
    fake.exec_results[replay_argv(["curl", "gone"])] = ExecResult(
        100, "", "E: Unable to locate package gone\n"
    )
    fake.exec_results[replay_argv(["gone"])] = ExecResult(
        100, "", "E: Unable to locate package gone\n"
    )
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert result.replay_missing == ("gone",)
    assert [(w.code, w.message) for w in result.warnings] == [
        ("GINN-W-REPLAY-MISSING", "package gone is no longer available upstream")
    ]
    assert [e.name for e in result.record.manifest] == ["curl"]
    assert result.record.generation == 2
    assert fake.exec_calls and any(c.argv == replay_argv(["curl"]) for c in fake.exec_calls)
    assert result.old_removed is True


@pytest.mark.fr("FR-022")
def test_when_the_only_package_is_gone_upstream_the_swap_completes_with_a_warning(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """Every package refused with the distribution's evidence is dropped, the last one included, so
    a manifest whose packages are all gone completes with an empty manifest (CL-17)."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    refusal = ExecResult(100, "", "E: Unable to locate package curl\n")
    fake.exec_results[replay_argv(["curl"])] = refusal
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert result.replay_missing == ("curl",)
    assert [(w.code, w.message) for w in result.warnings] == [
        ("GINN-W-REPLAY-MISSING", "package curl is no longer available upstream")
    ]
    assert result.record.manifest == []
    assert result.record.generation == 2
    assert result.old_removed is True
    assert generation_of(fake, 1) == []


@pytest.mark.fr("FR-022")
def test_a_refusal_without_evidence_of_every_package_fails_the_rebuild_and_the_old_root_stays(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    refusal = ExecResult(100, "", "E: Sub-process /usr/bin/apt returned an error code (100)\n")
    fake.exec_results[replay_argv(["curl"])] = refusal
    with pytest.raises(GinnError) as caught:
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert generation_of(fake, 2) == []
    assert fake.containers[before.container_id].labels[LABEL_GENERATION] == "1"
    assert stored(env) == before


@pytest.mark.fr("FR-022")
def test_a_refusal_with_evidence_for_one_package_and_none_for_another_fails_the_rebuild(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The group refusal names a missing package, but the single attempt of `curl` is refused with
    no evidence. That refusal fails the rebuild, and the old root is kept (CL-17)."""
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl", "gone"))
    fake.exec_results[replay_argv(["curl", "gone"])] = ExecResult(
        100, "", "E: Unable to locate package gone\n"
    )
    fake.exec_results[replay_argv(["gone"])] = ExecResult(
        100, "", "E: Unable to locate package gone\n"
    )
    fake.exec_results[replay_argv(["curl"])] = ExecResult(
        100, "", "E: Sub-process /usr/bin/apt returned an error code (100)\n"
    )
    with pytest.raises(GinnError) as caught:
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert generation_of(fake, 2) == []
    assert stored(env) == before


@pytest.mark.fr("FR-022")
@pytest.mark.fr("FR-043")
def test_a_repo_package_installs_and_an_aur_package_gone_upstream_is_dropped_and_the_swap_completes(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AUR group is one package, and its refusal with evidence drops it, while the repository
    package installs and the swap completes (FR-022 AC-1, CL-17)."""
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    aur = [ManifestEntry(name="gone-aur", source=InstallSource.AUR, method=InstallMethod.GINN)]
    RealmStore(env.paths).save(replace(before, manifest=[*before.manifest, *aur]))

    def build(
        podman: Any, cid: str, definition: Any, names: list[str], **kwargs: Any
    ) -> AurOutcome:
        refusal = make("GINN-E-PKG-MANAGER", realm=kwargs["realm"])
        refusal.details["output"] = "E: Unable to locate package gone-aur\n"
        raise refusal

    monkeypatch.setattr(rebuild_ops, "install_aur", build)
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert result.replay_missing == ("gone-aur",)
    assert [e.name for e in result.record.manifest] == ["curl"]
    assert result.record.generation == 2
    assert [w.code for w in result.warnings] == ["GINN-W-REPLAY-MISSING"]
    assert any(c.argv == replay_argv(["curl"]) for c in fake.exec_calls)


# Refusals, before any write.


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-081")
@pytest.mark.fr("FR-083")
def test_a_services_on_realm_is_rebuilt_through_its_runit_service_and_keeps_its_packages(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped services-on realm is rebuilt in place. gN+1 is started by runit's `up` only, and it
    is stopped by `sv down`. Its replay is installed through podman exec, and the runit service
    directory is the one it had (ADR-003). The record names gN+1 at the swap, with no building
    field."""
    hooks = hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    before = _create_services(env, fake, home, hooks)
    RealmStore(env.paths).save(replace(before, manifest=[CURL]))
    down = service_dir(env.paths, NAME) / DOWN
    assert down.is_file()
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    starts = tracked_starts(monkeypatch, fake, runit)
    result = rebuild(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
    )
    record = result.record
    assert record.generation == 2
    assert record.state is RealmState.STOPPED
    assert record.settings.services is True
    assert record.services_at_create is True
    assert record.building_container_id is None
    assert record.building_generation is None
    assert user_entries(record) == [CURL]
    installs = [
        c
        for c in fake.exec_calls
        if c.container_id == record.container_id and c.argv == replay_argv(["curl"])
    ]
    assert len(installs) == 1
    # The rebuild starts gN+1 and stops it. A stopped realm is then started once more to sync its
    # menu, and stopped again (FR-021 AC-1), so runit sees the pair twice.
    assert [argv[3] for argv in runit.argvs if argv[1] != "status"] == ["up", "down", "up", "down"]
    assert starts == [(record.container_id, True), (record.container_id, True)], (
        "every start of gN+1 goes through runit"
    )
    assert service_dir(env.paths, NAME).is_dir()
    assert down.is_file(), "the directory is kept as it was, with down present"
    assert generation_of(fake, 1) == []


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-048")
def test_a_services_on_rebuild_drops_the_keep_id_line_of_its_uid_in_its_provisioning(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T7.30: a services-on rebuild provisions a new derived image, and the drop of the realm uid's
    keep-id line runs in that provisioning, before its commit. Without it, the rebuilt realm keeps
    podman's keep-id line and fails GINN-E-REALM-USER. Only the calls of the rebuild are counted."""
    hooks = hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    before = _create_services(env, fake, home, hooks)
    RealmStore(env.paths).save(replace(before, manifest=[CURL]))
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    tracked_starts(monkeypatch, fake, runit)
    mark = len(fake.calls)
    exec_mark = len(fake.exec_calls)
    rebuild(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
    )
    drop = tuple(keep_id_entry_drop_argv(UID))
    rebuilt = list(enumerate(fake.calls))[mark:]
    drops = [i for i, call in rebuilt if call.op == "exec" and call.args[1] == drop]
    commits = [i for i, call in rebuilt if call.op == "commit"]
    assert len(drops) == 1
    assert drops[0] < commits[0]
    (drop_call,) = [c for c in fake.exec_calls[exec_mark:] if c.argv == drop]
    assert drop_call.user == "0:0"


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-083")
def test_a_services_on_rebuild_waits_for_the_run_dir_owner_before_it_attaches(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T7.28, T716-1: gN+1 is started by runit, and its drop-in may not have chowned the runtime dir
    yet. The rebuild reads the owner three times, polling 0.2 s apart, and attaches after that."""
    _create_services(
        env, fake, home, hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    )
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    seen: list[float] = []
    lifecycle = polling(
        _supervised_lifecycle(tmp_path, mountinfo, runit), runit, seen, wait_s=10.0, poll_s=0.2
    )
    fake.exec_sequences[RUN_USER_STAT] = [
        ExecResult(0, "0\n", ""),
        ExecResult(0, "0\n", ""),
        ExecResult(0, f"{UID}\n", ""),
    ]
    attached_after: list[int] = []
    real_attach = create_ops.attach_at_start

    def attach(*args: Any, **kwargs: Any) -> Any:
        attached_after.append(owner_checks(fake))
        return real_attach(*args, **kwargs)

    monkeypatch.setattr(create_ops, "attach_at_start", attach)
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo, lifecycle=lifecycle)

    assert result.record.generation == 2
    assert attached_after[0] == 3, "the rebuild's attach follows the third owner read"
    assert [s for s in seen if s == 0.2] == [0.2, 0.2]
    assert "GINN-W-RUN-USER-PENDING" not in [w.code for w in result.warnings]


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-083")
def test_a_services_on_rebuild_whose_dir_stays_root_owned_attaches_at_its_bound(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a timeout the rebuild attaches anyway, as it did before the wait (T7.28). Nothing retries
    the attach. The owner is read at once and then after each 0.25 s sleep, up to the 1 s bound."""
    _create_services(
        env, fake, home, hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    )
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    seen: list[float] = []
    lifecycle = polling(
        _supervised_lifecycle(tmp_path, mountinfo, runit), runit, seen, wait_s=1.0, poll_s=0.25
    )
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, "0\n", "")
    attached_after: list[int] = []
    real_attach = create_ops.attach_at_start

    def attach(*args: Any, **kwargs: Any) -> Any:
        attached_after.append(owner_checks(fake))
        return real_attach(*args, **kwargs)

    monkeypatch.setattr(create_ops, "attach_at_start", attach)
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo, lifecycle=lifecycle)

    assert result.record.generation == 2
    assert attached_after[0] == 5, "four 0.25 s sleeps, then one read at the bound"
    assert [s for s in seen if s == 0.25][:4] == [0.25] * 4
    pending = [w for w in result.warnings if w.code == "GINN-W-RUN-USER-PENDING"]
    assert [w.message for w in pending] == [
        "the runtime directory of realm 'debian' was not yet owned by the realm user when its wait "
        "ended; the attach went ahead and succeeded"
    ]


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-083")
def test_a_services_on_rebuild_whose_attach_fails_after_the_wait_carries_the_pending_cause(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner wait timed out and the attach then failed. The rebuild raises the attach error,
    and its warnings are not printed, so the timeout is a note on that error (T7.28, FR-083)."""
    _create_services(
        env, fake, home, hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    )
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    seen: list[float] = []
    lifecycle = polling(
        _supervised_lifecycle(tmp_path, mountinfo, runit), runit, seen, wait_s=1.0, poll_s=0.25
    )
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, "0\n", "")

    def attach(*args: Any, **kwargs: Any) -> Any:
        raise make("GINN-E-ATTACH-FAILED", detail="ginn-rebind: mount refused")

    monkeypatch.setattr(create_ops, "attach_at_start", attach)
    with pytest.raises(GinnError) as caught:
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo, lifecycle=lifecycle)

    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    assert [n for n in caught.value.__notes__ if "GINN-W-RUN-USER-PENDING" in n] == [
        "GINN-W-RUN-USER-PENDING: the runtime directory of realm 'debian' was not yet owned by the "
        "realm user when its wait ended"
    ]


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-081")
def test_a_services_on_rebuild_whose_container_stops_in_the_owner_wait_fails_at_once(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-081: the new generation's init is killed once the rebuild has read the owner. A stopped
    container never becomes owned, so the wait ends at the next read, and the rebuild fails before
    its attach, with the same error as a container that is not running after start. No pending note
    is due (T7.28 follow-up)."""
    _create_services(
        env, fake, home, hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    )
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    seen: list[float] = []
    lifecycle = polling(
        _supervised_lifecycle(tmp_path, mountinfo, runit), runit, seen, wait_s=10.0, poll_s=0.2
    )
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, "0\n", "")
    polled_sleep = lifecycle.supervisor.sleep
    killed: list[str] = []

    def kill_in_the_wait(seconds: float) -> None:
        if owner_checks(fake) >= 1 and not killed:
            for cid in generation_of(fake, 2):
                fake.stop(cid, 0)
                killed.append(cid)
        polled_sleep(seconds)

    lifecycle = replace(lifecycle, supervisor=replace(lifecycle.supervisor, sleep=kill_in_the_wait))
    attached: list[bool] = []
    real_attach = create_ops.attach_at_start

    def attach(*args: Any, **kwargs: Any) -> Any:
        attached.append(True)
        return real_attach(*args, **kwargs)

    monkeypatch.setattr(create_ops, "attach_at_start", attach)
    with pytest.raises(GinnError) as caught:
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo, lifecycle=lifecycle)

    assert len(killed) == 1
    assert caught.value.code == "GINN-E-INTERNAL"
    assert caught.value.details["detail"] == "the rebuilt realm is not running after start"
    assert caught.value.details["state"] == "exited"
    assert owner_checks(fake) == 2, "one read before the kill, one after it, then no more"
    assert attached == []
    notes = getattr(caught.value, "__notes__", [])
    assert not [n for n in notes if "GINN-W-RUN-USER-PENDING" in n]


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-061")
def test_a_services_off_rebuild_does_not_wait_for_a_run_dir_owner(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A services-off gN+1 runs as the realm user, and its tmpfs is `U`. No owner is read, and the
    bound is 0 s here, so a read would show as a check."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    fake.exec_results[RUN_USER_STAT] = ExecResult(0, "0\n", "")
    lifecycle = replace(lifecycle_hooks(tmp_path, mountinfo), run_user_wait_s=0.0)

    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo, lifecycle=lifecycle)

    assert result.record.generation == 2
    assert owner_checks(fake) == 0


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-081")
def test_the_building_container_is_recorded_before_runit_starts_it_and_the_record_still_names_gn(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """While runit runs `_service-run`, the record reads rebuilding, names gN and has gN+1 as the
    container being built. The rebuild wrote no other record field (DR-034, ADR-003)."""
    hooks = hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    _create_services(env, fake, home, hooks)
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    seen: list[tuple[RealmState, str | None, str | None, list[str]]] = []
    supervised = runit.on_up

    def probe() -> None:
        record = stored(env)
        seen.append(
            (
                record.state,
                record.container_id,
                record.building_container_id,
                generation_of(fake, 2),
            )
        )
        assert supervised is not None
        supervised()

    runit.on_up = probe
    result = rebuild(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
    )
    (state, old_cid, building, new_ids) = seen[0]
    assert state is RealmState.REBUILDING
    assert old_cid != building
    assert new_ids == [building]
    assert result.record.container_id == building


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-081")
def test_a_running_services_on_realm_is_stopped_and_started_through_sv_around_the_swap(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop is `sv down` and the start is `sv up`, so runit never starts gN on its own and the
    container of gN+1 is started by its supervised start only (ADR-003)."""
    hooks = hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    before = _create_services(env, fake, home, hooks)
    RealmStore(env.paths).save(replace(before, manifest=[CURL]))
    old = before.container_id
    fake.start(old)
    RealmStore(env.paths).save(replace(before, state=RealmState.RUNNING))
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    runit.started.append(old)
    ok = service_dir(env.paths, NAME) / "supervise" / "ok"
    ok.parent.mkdir(exist_ok=True)
    ok.touch()
    monkeypatch.setattr(rebuild_ops, "sync_realm", _no_sync)
    starts = tracked_starts(monkeypatch, fake, runit)
    result = rebuild(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
    )
    record = result.record
    assert record.generation == 2
    assert record.state is RealmState.RUNNING
    assert fake.containers[record.container_id].state is ContainerState.RUNNING
    assert old not in fake.containers
    actions = [argv[3] for argv in runit.argvs if argv[1] != "status"]
    assert actions == ["down", "up", "down", "up"], "stop of gN, then gN+1 up, down, and the start"
    assert all(by_runit for _, by_runit in starts), (
        "every start of a services-on container is runit's"
    )


@pytest.mark.fr("FR-083")
def test_turning_services_on_then_starting_rebuilds_into_a_services_on_generation(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A services-off realm is turned on, and its next start rebuilds it with the runit service
    written by the rebuild, after its swap. The packages and the Realm Home are kept.

    The journald drop-in is removed first, so the check below proves that the rebuild writes it
    before podman binds it in the new container (ADR-007 a).
    """
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    host_before = home_bytes(home)
    set_services_mode(env, NAME, on=True)
    assert not service_dir(env.paths, NAME).exists(), "no runit file before the rebuild"
    journald_dropin(env.paths).unlink(missing_ok=True)
    seen: list[bool] = []
    original_create = fake.create

    def create_checking_source(spec: CreateSpec) -> str:
        # A provisioning container is created first, and it is not the realm's (ADR-015 Decision 2).
        if not spec.name.startswith(PROVISION_PREFIX):
            seen.append(journald_dropin(env.paths).is_file())
        return original_create(spec)

    monkeypatch.setattr(fake, "create", create_checking_source)
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    record = start_realm(
        env,
        fake,
        NAME,
        session=session(home),
        hooks=_lazy(tmp_path, mountinfo, rebuild_hooks(tmp_path, sleeps, mountinfo), runit=runit),
    )
    assert record.generation == 2
    assert record.settings.services is True
    assert record.services_at_create is True
    assert record.state is RealmState.RUNNING
    assert user_entries(record) == before.manifest == [CURL]
    assert record.permissions == before.permissions
    assert service_dir(env.paths, NAME).is_dir()
    assert (service_dir(env.paths, NAME) / DOWN).is_file(), "autostart is off"
    assert [argv[3] for argv in runit.argvs if argv[1] != "status"] == ["up", "down", "up"]
    assert fake.containers[record.container_id].state is ContainerState.RUNNING
    assert home_bytes(home) == host_before
    assert generation_of(fake, 1) == []
    assert seen == [True], "the drop-in exists when podman binds it at the rebuild's create"


@pytest.mark.fr("FR-084")
def test_an_autostart_chosen_while_a_services_change_is_pending_is_applied_by_the_rebuild(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """Before the rebuild there is no runit directory, so autostart is recorded and the `down`
    file waits. The rebuild writes the directory with `down`, then follows the flag (DR-012)."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    set_autostart(env, NAME, on=True)
    assert stored(env).settings.autostart is True
    assert not service_dir(env.paths, NAME).exists()
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    record = start_realm(
        env,
        fake,
        NAME,
        session=session(home),
        hooks=_lazy(tmp_path, mountinfo, rebuild_hooks(tmp_path, sleeps, mountinfo), runit=runit),
    )
    assert record.settings.autostart is True
    assert record.services_at_create is True
    assert not (service_dir(env.paths, NAME) / DOWN).exists()


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-081")
def test_a_services_on_rebuild_that_runit_never_starts_fails_and_starts_no_container_itself(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runit does not start the building container, so the rebuild's wait times out. The rebuild
    never starts that container with podman itself (ADR-003), and gN and its record are
    unchanged."""
    hooks = hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    before = _create_services(env, fake, home, hooks)
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    runit.on_up = None
    starts = tracked_starts(monkeypatch, fake, runit)
    with pytest.raises(GinnError) as caught:
        rebuild(
            env,
            fake,
            home,
            tmp_path,
            sleeps,
            mountinfo,
            lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
        )
    assert caught.value.code == "GINN-E-SERVICE-TIMEOUT"
    assert starts == [], "no container is started by podman, and runit started none"
    assert root_fields(stored(env)) == root_fields(before)
    assert stored(env).state is RealmState.STOPPED
    assert stored(env).building_container_id is None
    assert generation_of(fake, 2) == []


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-081")
def test_a_fault_after_the_runit_start_stops_the_building_container_through_sv_first(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A failure after gN+1 runs under runit is rolled back with `sv down` before its removal, so
    runit does not restart a container that is gone (ADR-003). gN is left as it was."""
    hooks = hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    before = _create_services(env, fake, home, hooks)
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)

    def fail_at_replay(step: str) -> None:
        if step == "replayed":
            raise make("GINN-E-INTERNAL")

    with pytest.raises(GinnError):
        rebuild(
            env,
            fake,
            home,
            tmp_path,
            sleeps,
            mountinfo,
            on_step=fail_at_replay,
            lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
        )
    actions = [argv[3] for argv in runit.argvs if argv[1] != "status"]
    assert actions == ["up", "down"]
    assert all(
        cid not in fake.containers or fake.containers[cid].state is not ContainerState.RUNNING
        for cid in runit.started
    )
    assert generation_of(fake, 2) == []
    assert root_fields(stored(env)) == root_fields(before)


@pytest.mark.fr("FR-083")
def test_turning_services_off_then_starting_removes_the_service_directory_after_the_swap(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    hooks = hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    before = _create_services(env, fake, home, hooks)
    RealmStore(env.paths).save(replace(before, manifest=[CURL]))
    set_services_mode(env, NAME, on=False)
    assert (service_dir(env.paths, NAME) / DOWN).is_file(), (
        "down is set at once, as autostart needs"
    )
    record = start_realm(
        env,
        fake,
        NAME,
        session=session(home),
        hooks=_lazy(tmp_path, mountinfo, rebuild_hooks(tmp_path, sleeps, mountinfo)),
    )
    assert record.generation == 2
    assert record.settings.services is False
    assert record.services_at_create is False
    assert record.state is RealmState.RUNNING
    assert user_entries(record) == [CURL]
    assert not service_dir(env.paths, NAME).exists()
    assert fake.containers[record.container_id].state is ContainerState.RUNNING


@pytest.mark.fr("FR-066")
def test_a_tier_change_on_a_running_realm_waits_for_the_restart_and_the_next_start_rebuilds(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """`ginn tier` records the Development tier and flags the restart. A start of the running realm
    keeps its container. The stop and the next start rebuild it, with the nested options, the
    manifest kept, the Realm Home kept, and the inner podman config written (FR-066 AC-1)."""
    before = make_realm(
        env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",), running=True
    )
    realm_home = env.paths.homes / NAME.value
    (realm_home / "config.txt").write_text("realm keeps this\n", encoding="utf-8")
    host_before, realm_before = home_bytes(home), home_bytes(realm_home)
    hooks = _nested_lazy(tmp_path, sleeps, mountinfo)

    set_tier(env, NAME, Tier.DEVELOPMENT)
    waiting = stored(env)
    assert waiting.restart_required is True
    assert waiting.state is RealmState.RUNNING
    assert (waiting.generation, waiting.container_id) == (1, before.container_id)

    still = start_realm(env, fake, NAME, session=session(home), hooks=hooks)
    assert (still.generation, still.restart_required) == (1, True)

    stop_realm(env, fake, NAME, hooks=lifecycle_hooks(tmp_path, mountinfo))
    record = start_realm(env, fake, NAME, session=session(home), hooks=hooks)
    assert record.generation == 2
    assert record.settings.tier is Tier.DEVELOPMENT
    assert record.state is RealmState.RUNNING
    assert record.restart_required is False
    assert record.created_options == NESTED_NO_KVM
    assert user_entries(record) == [CURL]
    assert record.container_id != before.container_id
    assert old_gone(fake, before.container_id)
    assert home_bytes(home) == host_before
    assert home_bytes(realm_home) == {**realm_before, **config_files()}
    assert fake.containers[record.container_id].state is ContainerState.RUNNING


@pytest.mark.fr("FR-063")
def test_a_nested_toggle_enabled_on_a_custom_realm_rebuilds_at_its_next_start(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    create_realm(
        env,
        fake,
        request(home, tier=Tier.CUSTOM, with_packages=("curl",)),
        hooks=hooks_for(tmp_path, sleeps, mountinfo),
    )
    set_permissions(env, NAME, session(home), enable=["nested"])
    assert stored(env).created_options == []
    record = start_realm(
        env, fake, NAME, session=session(home), hooks=_nested_lazy(tmp_path, sleeps, mountinfo)
    )
    stored_toggles = stored(env).permissions.toggles
    assert Toggle.NESTED in stored_toggles
    assert record.generation == 2
    assert record.created_options == option_keys(
        effective_toggles(Tier.CUSTOM, stored_toggles), kvm_present=False
    )
    assert "device=/dev/fuse" in record.created_options
    assert "device=/dev/kvm" not in record.created_options
    assert user_entries(record) == [CURL]


@pytest.mark.fr("FR-063")
@pytest.mark.fr("NFR-SEC-002")
def test_a_kvm_toggle_enabled_on_a_kvm_host_rebuilds_with_the_device_at_the_next_start(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    create_realm(
        env,
        fake,
        request(home, tier=Tier.CUSTOM, with_packages=("curl",)),
        hooks=_kvm_hooks(tmp_path, sleeps, mountinfo, kvm=True),
    )
    assert stored(env).kvm_at_create is True
    set_permissions(env, NAME, session(home), enable=["kvm"])
    hooks = lifecycle_hooks(tmp_path, mountinfo)
    rebuild_at_start = replace(
        hooks, rebuild=rebuild_hooks(tmp_path, sleeps, mountinfo, exists=_kvm_exists(tmp_path))
    )
    record = start_realm(env, fake, NAME, session=session(home), hooks=rebuild_at_start)
    assert record.generation == 2
    assert "device=/dev/kvm" in record.created_options
    assert record.created_options == option_keys(
        effective_toggles(Tier.CUSTOM, stored(env).permissions.toggles), kvm_present=True
    )
    assert record.kvm_at_create is True


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-066")
def test_a_stale_failed_realm_is_stopped_then_rebuilt_and_started_on_its_new_generation(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A failed realm with a pending change is stopped first, which records it stopped, and then
    rebuilt and started (ops/lifecycle.py, `_rebuild_stale`). It is not refused (ADR-003)."""
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    odd = replace(before, state=RealmState.FAILED, created_options=["device=/dev/fuse"])
    RealmStore(env.paths).save(odd)
    started = start_realm(
        env,
        fake,
        NAME,
        session=session(home),
        hooks=_lazy(tmp_path, mountinfo, rebuild_hooks(tmp_path, sleeps, mountinfo)),
    )
    assert started.state is RealmState.RUNNING
    assert started.generation == 2
    assert started.restart_required is False
    assert generation_of(fake, 1) == []
    assert env.journal.pending() == []


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-022")
def test_a_lazy_rebuild_that_fails_starts_the_old_root_with_a_deferred_warning(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A rebuild that a start runs and that fails rolls back gN+1 and leaves the record stopped,
    with the new settings still pending. The start goes on with gN, and says so with
    GINN-W-REBUILD-DEFERRED. `restart_required` stays true, so the next start tries again (FR-021
    AC-2, NFR-031)."""
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_tier(env, NAME, Tier.DEVELOPMENT)
    # No evidence of a missing package, so the rebuild fails and the old root starts.
    fake.exec_results[replay_argv(["curl"])] = ExecResult(
        100, "", "E: Sub-process /usr/bin/apt returned an error code (100)\n"
    )
    notices: list[Any] = []
    started = start_realm(
        env,
        fake,
        NAME,
        session=session(home),
        hooks=_nested_lazy(tmp_path, sleeps, mountinfo),
        warnings=notices,
    )
    assert [w.code for w in notices] == ["GINN-W-REBUILD-DEFERRED"]
    assert started.state is RealmState.RUNNING
    assert started.generation == 1
    assert started.restart_required is True
    assert generation_of(fake, 2) == []
    assert fake.containers[before.container_id].labels[LABEL_GENERATION] == "1"
    assert fake.containers[before.container_id].state is ContainerState.RUNNING
    record = stored(env)
    assert record.generation == 1
    assert record.settings.tier is Tier.DEVELOPMENT
    assert record.created_options == before.created_options
    assert record.manifest == before.manifest
    assert env.journal.pending() == []


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-021")
@pytest.mark.parametrize(
    ("refusal", "synced"),
    [
        ("E: Unable to locate package gone\n", True),
        ("E: Sub-process /usr/bin/apt returned an error code (100)\n", False),
    ],
    ids=["a-package-dropped", "rebuild-deferred"],
)
def test_a_lazy_rebuild_syncs_the_menu_once_the_realm_runs_on_its_new_generation(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
    synced: bool,
) -> None:
    """A start that a lazy rebuild changed syncs the menu after it has started, on the
    running realm, so the entries of a package that the rebuild dropped leave the menu
    (FR-021 AC-1, FR-044). A rebuild that failed changed nothing, so the start syncs nothing."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl", "gone"))
    set_tier(env, NAME, Tier.DEVELOPMENT)
    fake.exec_results[replay_argv(["curl", "gone"])] = ExecResult(100, "", refusal)
    fake.exec_results[replay_argv(["gone"])] = ExecResult(100, "", refusal)
    store = RealmStore(env.paths)
    seen: list[tuple[RealmName, RealmState, str | None]] = []

    def sync(env_: Any, podman: Any, name: RealmName, *, hooks: Any = None) -> Any:
        seen.append((name, store.load(name).state, hooks.program))
        return SimpleNamespace(warnings=[])

    monkeypatch.setattr(lifecycle_ops, "sync_realm", sync)
    record = start_realm(
        env,
        fake,
        NAME,
        session=session(home),
        hooks=_nested_lazy(tmp_path, sleeps, mountinfo),
    )
    assert record.state is RealmState.RUNNING
    assert seen == ([(NAME, RealmState.RUNNING, "/usr/bin/ginn")] if synced else [])


def _services_realm(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    runit: "_Runsv",
    *,
    running: bool,
) -> RealmRecord:
    """A services-on realm with its runit service, stopped or running, made through the fake
    runit."""
    hooks = hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    _create_services(env, fake, home, hooks)
    if running:
        start_realm(
            env,
            fake,
            NAME,
            session=session(home),
            hooks=_supervised_lifecycle(tmp_path, mountinfo, runit),
        )
    return stored(env)


def _create_services(env: OpEnv, fake: ScriptedPodman, home: Path, hooks: Any) -> RealmRecord:
    return create_realm(env, fake, request(home, services=True), hooks=hooks).record


@pytest.mark.fr("FR-021")
@pytest.mark.parametrize("state", [RealmState.BROKEN, RealmState.FAILED, RealmState.DELETING])
def test_a_realm_in_a_state_that_cannot_rebuild_is_refused_and_untouched(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    state: RealmState,
) -> None:
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    odd = replace(before, state=state, previous_state=None)
    RealmStore(env.paths).save(odd)
    containers = dict(fake.containers)
    with pytest.raises(GinnError) as caught:
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert caught.value.code == "GINN-E-REALM-STATE"
    assert fake.containers == containers
    assert stored(env) == odd
    assert env.journal.pending() == []


@pytest.mark.fr("FR-021")
def test_a_realm_that_does_not_exist_is_refused(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    with pytest.raises(GinnError) as caught:
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert caught.value.code == "GINN-E-NO-SUCH-REALM"
    assert fake.containers == {}


@pytest.mark.fr("DR-034")
def test_the_record_moves_to_rebuilding_only_inside_the_journaled_body(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    seen: list[tuple[str, RealmState]] = []

    def watch(step: str) -> None:
        seen.append((step, stored(env).state))

    rebuild(env, fake, home, tmp_path, sleeps, mountinfo, on_step=watch)
    assert seen[0] == ("rebuilding", RealmState.REBUILDING)
    assert seen[-1] == ("swapping", RealmState.REBUILDING)
    assert stored(env).state is RealmState.STOPPED


@pytest.mark.fr("FR-021")
def test_the_swap_recomputes_the_create_time_options_of_the_new_root(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    stale = replace(before, created_options=["stale-option"])
    RealmStore(env.paths).save(stale)
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert result.record.created_options == before.created_options
    assert result.record.kvm_at_create == before.kvm_at_create
    assert result.record.gpu_cdi == before.gpu_cdi


@pytest.mark.fr("DR-034")
def test_the_audit_entry_names_the_generations_and_the_missing_packages(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    entries = [e for e in env.audit.read() if e.op == "rebuild"]
    assert [(e.outcome, e.error_code) for e in entries] == [("ok", None)]
    assert entries[0].params["generation"] == 1
    assert entries[0].params["new_generation"] == 2
    assert entries[0].params["replay_missing"] == []
    assert entries[0].params["old_removed"] is True


@pytest.mark.fr("FR-043")
def test_an_aur_entry_is_built_through_install_aur_and_one_it_refuses_is_reported(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    aur = [
        ManifestEntry(name="gone-aur", source=InstallSource.AUR, method=InstallMethod.GINN),
        ManifestEntry(name="yay-bin", source=InstallSource.AUR, method=InstallMethod.GINN),
    ]
    RealmStore(env.paths).save(replace(before, manifest=[*before.manifest, *aur]))
    built: list[tuple[list[str], int, int]] = []

    def build(
        podman: Any,
        cid: str,
        definition: Any,
        names: list[str],
        *,
        realm: str,
        uid: int,
        gid: int,
        **_: Any,
    ) -> AurOutcome:
        built.append((list(names), uid, gid))
        if "gone-aur" in names:
            # This fixture is a debian realm, so the distribution's own pattern is the evidence that
            # counts (FR-022 AC-1). Arch's AUR wording is pinned in test_distros_loader.py.
            refusal = make("GINN-E-PKG-MANAGER", realm=realm)
            refusal.details["output"] = "E: Unable to locate package gone-aur\n"
            raise refusal
        return AurOutcome()

    monkeypatch.setattr(rebuild_ops, "install_aur", build)
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert built == [
        (["gone-aur", "yay-bin"], UID, GID),
        (["gone-aur"], UID, GID),
        (["yay-bin"], UID, GID),
    ]
    assert result.replay_missing == ("gone-aur",)
    assert [e.name for e in result.record.manifest] == ["curl", "yay-bin"]
    assert [w.code for w in result.warnings] == ["GINN-W-REPLAY-MISSING"]
    assert not any("yay-bin" in c.argv for c in fake.exec_calls)


def _kali_realm(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> RealmRecord:
    """A Kali realm with the everything set, made with a yes at its prompt (FR-014)."""
    _kali_everything_answers(fake)
    hooks = hooks_for(tmp_path, sleeps, mountinfo, confirm=lambda prompt: True)
    create_realm(env, fake, request(home, name="kali", distro="kali", everything=True), hooks=hooks)
    return RealmStore(env.paths).load(KALI)


def _kali_services_realm(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> RealmRecord:
    """A stopped services-on Kali realm with the everything set (ADR-003).

    A services-on create with the everything set is refused (ops/_create_plan.py), so the realm is
    made services-off with a yes at its prompt, turned on, and started. That start rebuilds it into
    a services-on container through runit, and the stop that follows leaves it stopped
    (generation 2).
    """
    _kali_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    set_services_mode(env, KALI, on=True)
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps, name=KALI)
    start_realm(
        env,
        fake,
        KALI,
        session=session(home),
        hooks=_lazy(
            tmp_path,
            mountinfo,
            rebuild_hooks(tmp_path, sleeps, mountinfo),
            runit=runit,
        ),
    )
    stop_realm(env, fake, KALI, hooks=_supervised_lifecycle(tmp_path, mountinfo, runit))
    return RealmStore(env.paths).load(KALI)


KALI = RealmName("kali")
EVERYTHING = "kali-linux-everything"


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-121")
def test_a_kali_everything_set_is_installed_once_by_the_rebuild_with_no_second_prompt(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The rebuild has no confirm seam. The set is installed because the manifest records it, so
    the rebuild completes with no answer given (FR-014 AC-2, FR-021)."""
    _kali_everything_answers(fake)
    before = _kali_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    result = rebuild_realm(
        env,
        fake,
        KALI,
        session=session(home),
        gid=GID,
        hooks=rebuild_hooks(tmp_path, sleeps, mountinfo),
    )
    record = result.record
    assert record.generation == 2
    assert (
        record.manifest
        == before.manifest
        == [ManifestEntry(name=EVERYTHING, source=InstallSource.REPO, method=InstallMethod.GINN)]
    )
    assert result.replay_missing == ()
    kali = load_distro("kali")
    everything = tuple(kali.argv(Verb.INSTALL, [EVERYTHING]))
    installs = [
        c for c in fake.exec_calls if c.argv == everything and c.container_id == record.container_id
    ]
    assert len(installs) == 1


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-014")
def test_a_kali_everything_set_the_manifest_no_longer_records_is_not_installed_again(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """`ginn remove` took the set out of the manifest, so the rebuild replays the manifest and does
    not put the set back. The realm still has its everything flag, and nothing prompts."""
    before = _kali_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    RealmStore(env.paths).save(replace(before, manifest=[]))
    result = rebuild_realm(
        env,
        fake,
        KALI,
        session=session(home),
        gid=GID,
        hooks=rebuild_hooks(tmp_path, sleeps, mountinfo),
    )
    record = result.record
    everything = tuple(load_distro("kali").argv(Verb.INSTALL, [EVERYTHING]))
    assert record.generation == 2
    assert record.manifest == []
    assert record.settings.everything is True
    assert not any(
        c.argv == everything for c in fake.exec_calls if c.container_id == record.container_id
    )


# The rebuild at a start, and the runit service of a services-on realm (FR-066, FR-081, FR-083).

CURL = ManifestEntry(name="curl", source=InstallSource.REPO, method=InstallMethod.GINN)
NESTED_NO_KVM = option_keys(effective_toggles(Tier.DEVELOPMENT, frozenset()), kvm_present=False)


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-014")
def test_a_services_on_kali_realm_replays_its_package_and_reinstalls_the_everything_set_once(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The everything set is installed once by the rebuild with no prompt, and the other manifest
    package is replayed through podman. The rebuild keeps the runit directory with `down`
    (FR-021, ADR-003)."""
    before = _kali_services_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    htop = ManifestEntry(name="htop", source=InstallSource.REPO, method=InstallMethod.GINN)
    RealmStore(env.paths).save(replace(before, manifest=[*before.manifest, htop]))
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps, name=KALI)
    result = rebuild_realm(
        env,
        fake,
        KALI,
        session=session(home),
        gid=GID,
        hooks=rebuild_hooks(
            tmp_path,
            sleeps,
            mountinfo,
            lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
        ),
    )
    record = result.record
    kali = load_distro("kali")
    assert record.generation == 3, "the helper's start made generation 2"
    assert record.state is RealmState.STOPPED
    assert record.services_at_create is True
    assert result.replay_missing == ()
    assert record.manifest == [*before.manifest, htop]
    everything = tuple(kali.argv(Verb.INSTALL, [EVERYTHING]))
    replayed = tuple(kali.argv(Verb.INSTALL, ["htop"]))
    on_new = [c.argv for c in fake.exec_calls if c.container_id == record.container_id]
    assert on_new.count(everything) == 1
    assert on_new.count(replayed) == 1
    assert (service_dir(env.paths, KALI) / DOWN).is_file()


@pytest.mark.fr("FR-083")
@pytest.mark.fr("FR-014")
def test_turning_services_on_a_kali_realm_rebuilds_it_at_its_next_start_with_the_everything_set(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A services-off Kali realm is turned on, and its next start rebuilds it into a services-on
    container. The lazy rebuild installs the everything set with no prompt, and then the realm runs
    under runit (FR-083, ADR-003, FR-014). `ginn start` has no yes to give, so none is asked."""
    before = _kali_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    set_services_mode(env, KALI, on=True)
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps, name=KALI)
    record = start_realm(
        env,
        fake,
        KALI,
        session=session(home),
        hooks=_lazy(
            tmp_path,
            mountinfo,
            rebuild_hooks(tmp_path, sleeps, mountinfo),
            runit=runit,
        ),
    )
    kali = load_distro("kali")
    assert record.generation == 2
    assert record.state is RealmState.RUNNING
    assert record.services_at_create is True
    assert user_entries(record) == before.manifest
    everything = tuple(kali.argv(Verb.INSTALL, [EVERYTHING]))
    on_new = [c.argv for c in fake.exec_calls if c.container_id == record.container_id]
    assert on_new.count(everything) == 1
    assert [argv[3] for argv in runit.argvs if argv[1] != "status"] == ["up", "down", "up"]
    assert (service_dir(env.paths, KALI) / DOWN).is_file(), "autostart is off"


class _Runsv(RunitFake):
    """The fake sv of a test, and runsvdir's pick-up of a directory: `supervise/ok` appears on the
    first wait of the start, as runsvdir starts a runsv for a directory it finds (FR-081).

    `in_up` is True while an `up` runs, so a test can tell a start made by runit from one made by
    the rebuild (ADR-003). `started` holds the containers that the supervised starts returned, and a
    `down` stops them, as ginn-wait does on TERM. `fake` is the podman fake they live in.
    """

    fake: ScriptedPodman

    def __init__(self, service: Path, clock: FakeClock) -> None:
        super().__init__(service, clock)
        self.in_up = False
        self.started: list[str] = []
        self.on_down = self._stop_started

    def run(self, argv: Sequence[str], timeout: float) -> Result:
        self.in_up = argv[1] != "status" and argv[3] == "up"
        try:
            return super().run(argv, timeout)
        finally:
            self.in_up = False

    def _stop_started(self) -> None:
        for cid in self.started:
            if cid in self.fake.containers and (
                self.fake.containers[cid].state is ContainerState.RUNNING
            ):
                self.fake.stop(cid, 0)

    def sleep(self, seconds: float) -> None:
        ok = self.service / "supervise" / "ok"
        if self.service.is_dir() and not ok.exists():
            ok.parent.mkdir(exist_ok=True)
            ok.touch()
        super().sleep(seconds)


def tracked_starts(
    monkeypatch: pytest.MonkeyPatch, fake: ScriptedPodman, runit: _Runsv
) -> list[tuple[str, bool]]:
    """Record every podman start of a realm container of `fake`, and whether runit's `up` made it.

    The provisioning container of a build is started by podman itself, and it is not a realm
    container (ADR-015 Decision 2), so its start is not recorded.
    """
    starts: list[tuple[str, bool]] = []
    real_start = fake.start

    def start(container_id: str) -> None:
        if not fake.containers[container_id].name.startswith(PROVISION_PREFIX):
            starts.append((container_id, runit.in_up))
        real_start(container_id)

    monkeypatch.setattr(fake, "start", start)
    return starts


def _runit(
    env: OpEnv,
    tmp_path: Path,
    mountinfo: Path,
    home: Path,
    fake: ScriptedPodman,
    sleeps: list[float],
    name: RealmName = NAME,
) -> _Runsv:
    """A fake runit of realm `name` (NAME by default) whose `up` runs the supervised start, as
    runsv runs `run`."""
    helper = tmp_path / "ginn-wait"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    runit = _Runsv(service_dir(env.paths, name), FakeClock(1_800_000_000.0))
    runit.fake = fake

    def supervised() -> None:
        service_hooks = ServiceHooks(
            lifecycle=lifecycle_hooks(tmp_path, mountinfo),
            wait_helper=str(helper),
            sleep=sleeps.append,
        )
        argv = start_service(
            env,
            fake,
            name,
            global_argv=["/usr/bin/podman"],
            session=session(home),
            hooks=service_hooks,
        )
        runit.started.append(argv[-1])

    runit.on_up = supervised
    return runit


def _supervised_lifecycle(tmp_path: Path, mountinfo: Path, runit: _Runsv) -> LifecycleHooks:
    """The lifecycle seams of a services-on realm: the fake sv, and a short poll."""
    return replace(
        lifecycle_hooks(tmp_path, mountinfo),
        supervisor=SupervisorHooks(sv=SV, run=runit.run, clock=runit.clock, sleep=runit.sleep),
        start_poll_s=1.0,
    )


def _lazy(
    tmp_path: Path,
    mountinfo: Path,
    rebuild: RebuildHooks,
    *,
    runit: _Runsv | None = None,
) -> LifecycleHooks:
    """The lifecycle seams of a start that may rebuild the realm first (FR-066).

    With `runit`, the rebuild's own start and stop of a services-on container go through that fake
    sv too, as the start's do.
    """
    base = (
        lifecycle_hooks(tmp_path, mountinfo)
        if runit is None
        else _supervised_lifecycle(tmp_path, mountinfo, runit)
    )
    if runit is not None:
        rebuild = replace(rebuild, lifecycle=base)
    return replace(base, rebuild=rebuild)


def _nested_hooks(
    tmp_path: Path, sleeps: list[float], mountinfo: Path, *, kvm: bool = False
) -> RebuildHooks:
    """Rebuild hooks for a host with the nested devices and a profile source (ADR-006)."""
    create = _kvm_hooks(
        tmp_path, sleeps, mountinfo, kvm=kvm, seccomp_source=_seccomp_source(tmp_path)
    )
    return rebuild_hooks(
        tmp_path,
        sleeps,
        mountinfo,
        exists=create.exists,
        seccomp_source=create.seccomp_source,
    )


def _nested_lazy(tmp_path: Path, sleeps: list[float], mountinfo: Path) -> LifecycleHooks:
    return _lazy(tmp_path, mountinfo, _nested_hooks(tmp_path, sleeps, mountinfo))


def _no_sync(*args: Any, **kwargs: Any) -> Any:
    return type("Sync", (), {"warnings": []})()


def _kvm_exists(tmp_path: Path) -> Callable[[str], bool]:
    return lambda path: path == "/dev/kvm" or str(path).startswith(str(tmp_path))


def old_gone(fake: ScriptedPodman, container_id: str) -> bool:
    return container_id not in fake.containers


def config_files() -> dict[str, bytes]:
    return {str(Path(CONFIG_DIR) / name): text.encode("utf-8") for name, text in FILES.items()}


# The T8.3 review fixes: the stale flag, the missing packages, the rollback, the sweep and the
# notices (FR-021, FR-022, NFR-031, DR-034, FR-066, FR-083).


def _raise_at(step_name: str, error: BaseException) -> Callable[[str], None]:
    """An `on_step` hook that raises `error` at journal step `step_name`, and nowhere else."""

    def hook(step: str) -> None:
        if step == step_name:
            raise error

    return hook


@pytest.mark.fr("FR-066")
@pytest.mark.fr("FR-081")
def test_a_runit_start_of_a_stale_realm_keeps_restart_required_and_status_reports_it(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """runit starts a services-on realm without rebuilding it (ADR-003), so the container it
    starts is the old one. The pending change stays pending: the record keeps the flag, and the
    status reports it, as the CLI and the Control Plane both read it (FR-066)."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    _services_realm(env, fake, home, tmp_path, sleeps, mountinfo, runit, running=False)
    set_tier(env, NAME, Tier.DEVELOPMENT)
    assert stored(env).restart_required is False, "a stopped realm does not flag its change"
    start_service(
        env,
        fake,
        NAME,
        global_argv=["/usr/bin/podman"],
        session=session(home),
        hooks=ServiceHooks(
            lifecycle=lifecycle_hooks(tmp_path, mountinfo),
            wait_helper=str(tmp_path / "ginn-wait"),
            sleep=sleeps.append,
        ),
    )
    record = stored(env)
    assert record.state is RealmState.RUNNING
    assert record.generation == 1
    assert record.restart_required is True
    status = realm_status(env, fake, NAME, hooks=_supervised_lifecycle(tmp_path, mountinfo, runit))
    assert status.restart_required is True
    assert status.to_json()["restart_required"] is True


@pytest.mark.fr("FR-021")
@pytest.mark.fr("NFR-031")
def test_a_failed_rebuild_of_a_running_realm_with_a_pending_change_leaves_it_running_on_gn(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The restart after a failed rebuild does not rebuild again under the image lock it released
    (GINN-E-REALM-BUSY), so the realm runs on gN again, with its change still pending."""
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, running=True)
    set_tier(env, NAME, Tier.DEVELOPMENT)
    hooks = replace(
        _nested_hooks(tmp_path, sleeps, mountinfo),
        on_step=_raise_at("replayed", make("GINN-E-INTERNAL")),
    )
    with pytest.raises(GinnError) as caught:
        rebuild_realm(env, fake, NAME, session=session(home), gid=GID, hooks=hooks)
    assert caught.value.code == "GINN-E-INTERNAL"
    notes = getattr(caught.value, "__notes__", [])
    assert not any("left stopped" in note for note in notes)
    record = stored(env)
    assert record.state is RealmState.RUNNING
    assert record.generation == 1
    assert record.restart_required is True
    assert fake.containers[before.container_id].state is ContainerState.RUNNING
    assert generation_of(fake, 2) == []


@pytest.mark.fr("AS-018")
def test_a_cancelled_lazy_rebuild_raises_and_does_not_start_the_old_root(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A Ctrl-C in a lazy rebuild is a cancel. It is not read as a failed rebuild, so the start
    does not go on to start the old root (ops/lifecycle.py, `_rebuild_stale`)."""
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    set_tier(env, NAME, Tier.DEVELOPMENT)
    hooks = replace(
        _nested_hooks(tmp_path, sleeps, mountinfo),
        on_step=_raise_at("replayed", KeyboardInterrupt()),
    )
    with pytest.raises(GinnError) as caught:
        start_realm(env, fake, NAME, session=session(home), hooks=_lazy(tmp_path, mountinfo, hooks))
    assert caught.value.code == "GINN-E-INTERRUPTED"
    assert stored(env).state is RealmState.STOPPED
    assert fake.containers[before.container_id].state is not ContainerState.RUNNING


@pytest.mark.fr("FR-083")
@pytest.mark.fr("FR-021")
def test_a_deferred_services_off_to_on_change_starts_the_old_root_through_podman(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A failed lazy rebuild of a services change leaves the old container, which is services-off.
    The start runs it with podman, because the container's own mode picks the path (ADR-003)."""
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    set_services_mode(env, NAME, on=True)
    hooks = rebuild_hooks(
        tmp_path, sleeps, mountinfo, on_step=_raise_at("created", make("GINN-E-INTERNAL"))
    )
    notices: list[Any] = []
    started = start_realm(
        env,
        fake,
        NAME,
        session=session(home),
        hooks=_lazy(tmp_path, mountinfo, hooks),
        warnings=notices,
    )
    assert [w.code for w in notices] == ["GINN-W-REBUILD-DEFERRED"]
    assert started.state is RealmState.RUNNING
    assert started.services_at_create is False
    assert started.settings.services is True
    assert started.restart_required is True
    assert fake.containers[before.container_id].state is ContainerState.RUNNING


@pytest.mark.fr("FR-083")
@pytest.mark.fr("FR-021")
def test_a_deferred_services_on_to_off_change_starts_the_old_root_through_sv(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The same failure from the other side: the old container is services-on, so the start goes
    through its runit service, and the pending mode is not applied (ADR-003)."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    before = _services_realm(env, fake, home, tmp_path, sleeps, mountinfo, runit, running=False)
    set_services_mode(env, NAME, on=False)
    hooks = rebuild_hooks(
        tmp_path, sleeps, mountinfo, on_step=_raise_at("created", make("GINN-E-INTERNAL"))
    )
    notices: list[Any] = []
    started = start_realm(
        env,
        fake,
        NAME,
        session=session(home),
        hooks=_lazy(tmp_path, mountinfo, hooks, runit=runit),
        warnings=notices,
    )
    assert [w.code for w in notices] == ["GINN-W-REBUILD-DEFERRED"]
    assert started.state is RealmState.RUNNING
    assert started.services_at_create is True
    assert started.settings.services is False
    assert runit.started == [before.container_id]


@pytest.mark.fr("FR-022")
@pytest.mark.fr("FR-014")
def test_an_everything_metapackage_refused_with_evidence_is_dropped_and_the_swap_completes(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The everything set is one name, and the rule of `_replay_group` applies to it: a refusal with
    the distribution's evidence drops it, with a GINN-W-REPLAY-MISSING warning, and the swap
    completes (FR-022 AC-1, CL-17). Its Install Manifest entry leaves with it."""
    _kali_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    everything = tuple(load_distro("kali").argv(Verb.INSTALL, [EVERYTHING]))
    fake.exec_results[everything] = ExecResult(
        100, "", "E: Unable to locate package kali-linux-everything\n"
    )
    result = rebuild_realm(
        env,
        fake,
        KALI,
        session=session(home),
        gid=GID,
        hooks=rebuild_hooks(tmp_path, sleeps, mountinfo),
    )
    assert result.replay_missing == (EVERYTHING,)
    assert [(w.code, w.message) for w in result.warnings][:1] == [
        ("GINN-W-REPLAY-MISSING", f"package {EVERYTHING} is no longer available upstream")
    ]
    assert EVERYTHING not in [e.name for e in result.record.manifest]
    assert result.record.generation == 2
    assert generation_of(fake, 1) == []


@pytest.mark.fr("FR-022")
def test_an_everything_metapackage_refused_without_evidence_fails_and_keeps_the_old_root(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A refusal of the everything set with no evidence of a missing package is a failure, so the
    old root is kept (FR-022, CL-17)."""
    before = _kali_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    everything = tuple(load_distro("kali").argv(Verb.INSTALL, [EVERYTHING]))
    fake.exec_results[everything] = ExecResult(
        100, "", "E: Sub-process /usr/bin/apt returned an error code (100)\n"
    )
    with pytest.raises(GinnError) as caught:
        rebuild_realm(
            env,
            fake,
            KALI,
            session=session(home),
            gid=GID,
            hooks=rebuild_hooks(tmp_path, sleeps, mountinfo),
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert generation_of(fake, 2) == []
    assert RealmStore(env.paths).load(KALI) == before


@pytest.mark.fr("FR-022")
@pytest.mark.fr("NFR-031")
def test_a_full_disk_during_the_replay_is_not_a_missing_package_and_the_old_root_stays(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """Output that says the disk is full is never a missing package, even when it also names one.
    The rebuild fails at once, with no per-package probe, and the old root is kept."""
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl", "gone"))
    fake.exec_results[replay_argv(["curl", "gone"])] = ExecResult(
        100,
        "",
        "E: Unable to locate package gone\nE: Write error - No space left on device\n",
    )
    with pytest.raises(GinnError) as caught:
        rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert not any(c.argv == replay_argv(["gone"]) for c in fake.exec_calls)
    assert generation_of(fake, 2) == []
    assert stored(env) == before


def _refusal(output: str | None) -> GinnError:
    """A package manager's refusal of a name, with its output as `failure()` keeps it."""
    err = make("GINN-E-PKG-MANAGER", realm="debian")
    if output is not None:
        err.details["output"] = output
    return err


@pytest.mark.fr("FR-022")
def test_a_refusal_with_no_output_is_never_read_as_a_missing_package() -> None:
    """A time or size limit is a refusal with no output (packages/manager.py). It is no evidence,
    so the group is not split into single installs, and the rebuild fails."""
    calls: list[list[str]] = []

    def install(names: list[str]) -> None:
        calls.append(list(names))
        raise _refusal(None)

    with pytest.raises(GinnError):
        rebuild_ops._replay_group(["curl", "gone"], install, load_distro("debian"))
    assert calls == [["curl", "gone"]]


@pytest.mark.fr("FR-022")
def test_a_name_is_missing_only_when_its_own_refusal_names_the_distributions_message() -> None:
    """The batch is refused with evidence, and each name is tried alone: only the refused one is
    returned, and the others stay installed (AC-1)."""
    installed: list[str] = []

    def install(names: list[str]) -> None:
        if "gone" in names and names != ["curl"]:
            raise _refusal("E: Unable to locate package gone\n")
        installed.extend(names)

    missing = rebuild_ops._replay_group(["curl", "gone"], install, load_distro("debian"))
    assert missing == ["gone"]
    assert installed == ["curl"]


@pytest.mark.fr("FR-022")
def test_a_second_refusal_without_evidence_fails_the_group_even_after_a_missing_name() -> None:
    def install(names: list[str]) -> None:
        if names == ["curl", "gone"] or names == ["gone"]:
            raise _refusal("E: Unable to locate package gone\n")
        if names == ["curl"]:
            raise _refusal("E: Sub-process /usr/bin/dpkg returned an error code (1)\n")

    with pytest.raises(GinnError) as caught:
        rebuild_ops._replay_group(["curl", "gone"], install, load_distro("debian"))
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert caught.value.details["output"].startswith("E: Sub-process")


@pytest.mark.fr("FR-022")
def test_every_name_refused_with_evidence_is_returned_and_the_group_does_not_fail() -> None:
    """Each name that the upstream refuses with the evidence is returned, and the group does not
    fail for that, even when every name is returned. The rebuild then completes without them
    (AC-1, CL-17)."""
    tried: list[list[str]] = []

    def install(names: list[str]) -> None:
        tried.append(list(names))
        raise _refusal("E: Unable to locate package gone\n")

    missing = rebuild_ops._replay_group(["curl", "gone"], install, load_distro("debian"))
    assert missing == ["curl", "gone"]
    assert tried == [["curl", "gone"], ["curl"], ["gone"]]


@pytest.mark.fr("FR-022")
@pytest.mark.fr("FR-120")
def test_a_network_failure_is_raised_and_never_read_as_a_missing_package() -> None:
    def install(names: list[str]) -> None:
        raise make("GINN-E-NETWORK")

    with pytest.raises(GinnError) as caught:
        rebuild_ops._replay_group(["gone"], install, load_distro("debian"))
    assert caught.value.code == "GINN-E-NETWORK"


@pytest.mark.fr("DR-034")
@pytest.mark.fr("FR-021")
def test_a_rebuild_removes_every_older_generation_a_failed_removal_left_behind(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The sweep after the swap is by label, below the swapped generation, so a generation-1 root
    that an earlier removal left is removed by the rebuild to generation 3 (DR-034)."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert generation_of(fake, 1) == []
    leftover = fake.create(
        CreateSpec(
            name="ginn-debian-leftover",
            image_digest_ref=stored(env).image_ref,
            network="host",
            labels={LABEL_REALM: NAME.value, LABEL_GENERATION: "1"},
        )
    )
    assert generation_of(fake, 1) == [leftover]
    result = rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert result.record.generation == 3
    assert result.old_removed is True
    assert generation_of(fake, 1) == []
    assert generation_of(fake, 2) == []


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-066")
def test_a_nested_rebuild_writes_only_the_config_file_that_is_missing_and_keeps_an_edited_one(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A realm that becomes nested may have one of the two inner podman files already. The one
    that is there is never overwritten, so the user's edit stays (ops/rebuild.py)."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    set_tier(env, NAME, Tier.DEVELOPMENT)
    config = env.paths.homes / NAME.value / CONFIG_DIR
    config.mkdir(parents=True)
    (config / "storage.conf").write_text("# edited by the user\n", encoding="utf-8")
    start_realm(
        env, fake, NAME, session=session(home), hooks=_nested_lazy(tmp_path, sleeps, mountinfo)
    )
    assert stored(env).generation == 2
    assert (config / "storage.conf").read_text(encoding="utf-8") == "# edited by the user\n"
    assert (config / "containers.conf").read_bytes() == FILES["containers.conf"].encode("utf-8")


@pytest.mark.fr("NFR-031")
@pytest.mark.fr("FR-066")
def test_a_failed_nested_rebuild_leaves_the_realm_home_byte_identical(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The inner podman files are written before the swap, and the rollback removes what the
    rebuild created: every file and directory of the Realm Home is as it was (NFR-031)."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    set_tier(env, NAME, Tier.DEVELOPMENT)
    home_before = realm_home_tree(env.paths.homes / NAME.value)
    hooks = replace(
        _nested_hooks(tmp_path, sleeps, mountinfo),
        on_step=_raise_at("replayed", make("GINN-E-INTERNAL")),
    )
    with pytest.raises(GinnError):
        rebuild_realm(env, fake, NAME, session=session(home), gid=GID, hooks=hooks)
    assert realm_home_tree(env.paths.homes / NAME.value) == home_before
    assert not (env.paths.homes / NAME.value / CONFIG_DIR).exists()


@pytest.mark.fr("FR-066")
def test_a_change_that_needs_a_new_container_says_the_next_start_rebuilds(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A tier change that adds a create-time option is a warning at change time, the pending
    rebuild notice, so the start that rebuilds it is never a surprise (CL-19). A repeated change
    writes nothing and says nothing."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    notices: list[Any] = []
    set_tier(env, NAME, Tier.DEVELOPMENT, warnings=notices)
    assert [w.code for w in notices] == ["GINN-W-REBUILD-PENDING"]
    assert notices[0].message == (
        "realm 'debian': the next start rebuilds it into a new container, because the tier changed"
    )
    again: list[Any] = []
    set_tier(env, NAME, Tier.DEVELOPMENT, warnings=again)
    assert again == []


@pytest.mark.fr("FR-083")
def test_a_services_mode_change_says_the_next_start_rebuilds(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    notices: list[Any] = []
    set_services_mode(env, NAME, on=True, warnings=notices)
    assert [w.code for w in notices] == ["GINN-W-REBUILD-PENDING"]
    assert "the services mode changed" in notices[0].message


@pytest.mark.fr("FR-063")
def test_a_custom_toggle_that_needs_a_new_container_says_the_next_start_rebuilds(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The nested toggle is a create-time option (ADR-006), so enabling it is a change that the
    next start rebuilds. An attach-time toggle, such as downloads, is not, and says nothing
    (CL-19)."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    set_tier(env, NAME, Tier.CUSTOM)
    quiet: list[Any] = []
    set_permissions(env, NAME, session(home), enable=["downloads"], warnings=quiet)
    assert quiet == []
    notices: list[Any] = []
    set_permissions(env, NAME, session(home), enable=["nested"], warnings=notices)
    assert [w.code for w in notices] == ["GINN-W-REBUILD-PENDING"]
    assert "the permissions changed" in notices[0].message


@pytest.mark.fr("FR-066")
@pytest.mark.fr("FR-081")
def test_a_failed_services_on_realm_with_a_pending_change_is_stopped_then_rebuilt_through_runit(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A services-on realm that failed, with a create-time change pending, is stopped first through
    its runit service, which records it stopped. It is then rebuilt, and started through runit on
    its new generation (ops/lifecycle.py, `_rebuild_stale`). The nested tier needs the host's
    /dev/fuse, which the nested hooks provide."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    before = _services_realm(env, fake, home, tmp_path, sleeps, mountinfo, runit, running=False)
    set_tier(env, NAME, Tier.DEVELOPMENT)
    RealmStore(env.paths).save(replace(stored(env), state=RealmState.FAILED))
    notices: list[Any] = []
    started = start_realm(
        env,
        fake,
        NAME,
        session=session(home),
        hooks=_lazy(tmp_path, mountinfo, _nested_hooks(tmp_path, sleeps, mountinfo), runit=runit),
        warnings=notices,
    )
    assert notices == []
    assert started.state is RealmState.RUNNING
    assert started.generation == 2
    assert started.services_at_create is True
    assert started.restart_required is False
    assert generation_of(fake, 1) == []
    assert before.generation == 1


class _Killed(Exception):
    """A SIGKILL inside a rebuild step: not a GinnError, so the journal entry stays (NFR-031)."""


@pytest.mark.fr("NFR-031")
@pytest.mark.fr("FR-014")
@pytest.mark.parametrize(
    ("step", "kind"),
    [
        ("estimated", "error"),
        ("estimated", "kill"),
        ("everything", "error"),
        ("everything", "kill"),
    ],
)
def test_a_fault_in_the_everything_set_of_a_kali_rebuild_leaves_the_old_root_usable(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    step: str,
    kind: str,
) -> None:
    """The Kali everything steps of a rebuild: the estimate, and the install of the set. A fault in
    either rolls the new root back (or, for a kill, recovery does), and the old root is kept."""
    before = _kali_realm(env, fake, home, tmp_path, sleeps, mountinfo)
    home_before = realm_home_tree(env.paths.homes / KALI.value)
    error: BaseException = make("GINN-E-INTERNAL") if kind == "error" else _Killed(step)
    hooks = rebuild_hooks(tmp_path, sleeps, mountinfo, on_step=_raise_at(step, error))
    with pytest.raises(GinnError if kind == "error" else _Killed):
        rebuild_realm(env, fake, KALI, session=session(home), gid=GID, hooks=hooks)
    if kind == "kill":
        kill_journal_owner(env, KALI.value)
        assert [o.outcome for o in recover(env, fake)] == [OUTCOME_RECOVERED]
    assert env.journal.pending() == []
    record = RealmStore(env.paths).load(KALI)
    assert record.generation == 1
    assert record.manifest == before.manifest
    assert record.state is RealmState.STOPPED
    assert generation_of(fake, 2) == []
    assert realm_home_tree(env.paths.homes / KALI.value) == home_before


@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-021")
def test_a_services_on_rebuild_writes_the_realms_machine_id_into_the_new_generation(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """ADR-007, F6: gN+1 copies the id that the record holds, so the realm keeps one identity
    across generations. The rebuild inherits the create's copy, and the swap keeps the id."""
    hooks = hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    before = _create_services(env, fake, home, hooks)
    assert before.machine_id is not None
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    result = rebuild(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
    )
    record = result.record
    assert record.generation == 2
    assert record.machine_id == before.machine_id
    content, _mode = fake.containers[record.container_id].copied[MACHINE_ID_PATH]
    assert content == f"{before.machine_id}\n".encode("ascii")


@pytest.mark.fr("FR-083")
def test_a_services_on_rebuild_copies_the_run_user_drop_in_into_the_new_generation(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """T7.28, T716-1: gN+1 is created by the same create path, so it gets the realm user's
    tmpfiles.d drop-in too. Without it the realm user cannot attach grants under /run/user/<uid>."""
    hooks = hooks_for(tmp_path, sleeps, mountinfo, program="/usr/bin/ginn")
    _create_services(env, fake, home, hooks)
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    record = rebuild(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
    ).record
    assert record.generation == 2
    content, mode = fake.containers[record.container_id].copied[RUN_USER_TMPFILES_PATH]
    assert content == f"d /run/user/{UID} 0700 {UID} {GID} -\n".encode("ascii")
    assert mode == 0o644


@pytest.mark.fr("FR-066")
@pytest.mark.fr("FR-080")
def test_a_lazy_services_on_rebuild_whose_image_has_no_init_is_deferred_with_the_reason(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """F3 on the lazy path: a services-off realm turned on, whose image has no /sbin/init. Its
    start's rebuild is refused with GINN-E-NO-INIT, which the start reports as
    GINN-W-REBUILD-DEFERRED with that message as its reason. The start goes on with the services-off
    root, and no new generation is made or kept."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    fake.init_absent = True
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    notices: list[Any] = []
    started = start_realm(
        env,
        fake,
        NAME,
        session=session(home),
        hooks=_lazy(tmp_path, mountinfo, rebuild_hooks(tmp_path, sleeps, mountinfo), runit=runit),
        warnings=notices,
    )
    assert [w.code for w in notices] == ["GINN-W-REBUILD-DEFERRED"]
    assert "services-on needs systemd in the realm" in notices[0].message
    assert started.generation == 1
    assert generation_of(fake, 2) == []
    assert stored(env).generation == 1


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


@pytest.mark.fr("FR-122")
def test_a_rebuild_replay_records_exec_acked_for_each_install(
    env: OpEnv,
    fake: Any,
    home: Path,
    host: Path,
    sleeps: list[float],
    mountinfo: Path,
    journal_steps: list[str],
) -> None:
    make_realm(
        env,
        fake,
        home,
        host,
        sleeps,
        mountinfo,
        packages=("curl", "git"),
    )
    rebuild(
        env,
        fake,
        home,
        host,
        sleeps,
        mountinfo,
    )
    assert "exec-acked" in journal_steps
    assert journal_steps.index("exec-acked") < journal_steps.index("swapping")


@pytest.mark.fr("FR-084")
def test_a_rebuild_syncs_the_relay_while_rebuilding_and_again_at_the_swap(
    env: OpEnv,
    fake: Any,
    home: Path,
    host: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states: list[str] = []

    def record(paths: Any, *_args: Any, **_kwargs: Any) -> None:
        states.append(RealmStore(paths).load(NAME).state.value)

    monkeypatch.setattr("ginn.ops.rebuild.sync_exec_service", record)
    make_realm(
        env,
        fake,
        home,
        host,
        sleeps,
        mountinfo,
    )
    rebuild(
        env,
        fake,
        home,
        host,
        sleeps,
        mountinfo,
    )
    assert states == [RealmState.REBUILDING.value, RealmState.STOPPED.value]


@pytest.mark.fr("FR-084")
def test_a_failed_relay_sync_during_a_rebuild_is_a_warning_and_the_swap_stands(
    env: OpEnv,
    fake: Any,
    home: Path,
    host: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.EIO, "the sv directory is unreadable")

    monkeypatch.setattr("ginn.ops.rebuild.sync_exec_service", broken)
    make_realm(
        env,
        fake,
        home,
        host,
        sleeps,
        mountinfo,
    )
    result = rebuild(
        env,
        fake,
        home,
        host,
        sleeps,
        mountinfo,
    )
    assert result.record.generation == 2
    assert result.record.state is RealmState.STOPPED
    assert "GINN-W-RELAY-SYNC" in [w.code for w in result.warnings]


# ADR-015: the image of a build (Decision 7), the derived image (Decisions 2, 9 and 13), the boot
# check (Decision 5) and the init entries (Decision 6). A services-on realm here is a stopped curl
# realm turned services-on, so its first build is a `provision` build.

WRONG_IMAGE_ID = "f" * 64
"""A well-formed image ID that no derived image of the fake holds (Decision 9)."""


def stale_rebuild(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    *,
    runit: _Runsv | None = None,
    services_only: bool = True,
    warnings: list[Any] | None = None,
    **overrides: Any,
) -> RealmRecord:
    """The rebuild that a start runs for NAME, through `rebuild_for_start` (FR-066, FR-083)."""
    lifecycle = (
        lifecycle_hooks(tmp_path, mountinfo)
        if runit is None
        else _supervised_lifecycle(tmp_path, mountinfo, runit)
    )
    hooks = rebuild_hooks(tmp_path, sleeps, mountinfo, lifecycle=lifecycle, **overrides)
    return rebuild_for_start(
        env,
        fake,
        NAME,
        session=session(home),
        gid=GID,
        hooks=hooks,
        services_only=services_only,
        warnings=warnings,
    )


def derived_ids(fake: ScriptedPodman) -> set[str]:
    """The IDs of the realm's derived images that the fake holds (ADR-015 Decision 10)."""
    prefix = derived_image_prefix(NAME)
    return {fake.image_id(name) for name in fake.images if name.startswith(prefix)}


def pulls(fake: ScriptedPodman) -> int:
    return sum(1 for call in fake.calls if call.op == "pull")


def commits(fake: ScriptedPodman) -> int:
    return sum(1 for call in fake.calls if call.op == "commit")


def last_rebuild_params(env: OpEnv) -> dict[str, Any]:
    """The audit params of the newest rebuild, which name its image source (FR-124)."""
    return [entry for entry in env.audit.read() if entry.op == "rebuild"][-1].params


def failed_boot(fake: ScriptedPodman, *units: str) -> None:
    """Script a boot that reads `degraded`, with `units` as its failed units (Decision 5)."""
    fake.exec_results[BOOT_STATE_ARGV] = ExecResult(1, "degraded\n", "")
    listing = "".join(f"{unit} loaded failed failed {unit}\n" for unit in units)
    fake.exec_results[BOOT_FAILED_ARGV] = ExecResult(0, listing, "")


@pytest.mark.fr("FR-021")
def test_a_provision_build_runs_the_new_root_from_the_derived_image_it_names(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """AC-1 for `provision`: the root keeps the settings, the permissions and the user packages.
    It is created from the derived image its provisioning made, and the record keeps the pin it
    verified (ADR-015 Decisions 2, 6, 7 and 9)."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    record = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    assert record.generation == 2
    assert record.state is RealmState.STOPPED
    assert record.services_at_create is True
    assert record.settings.tier is before.settings.tier
    assert record.permissions == before.permissions
    assert record.image_digest == before.image_digest
    assert record.init_image_id is not None
    assert record.building_init_image_id is None
    assert record.init_image_id in derived_ids(fake)
    init_names = list(load_distro("debian").services_packages)
    assert [entry.name for entry in record.manifest if entry.init] == init_names
    assert user_entries(record) == [CURL]
    spec = fake.created_specs[-1]
    assert spec.image_id == record.init_image_id
    assert spec.image_digest_ref == before.image_ref
    assert last_rebuild_params(env)["image_source"] == "provision"
    replayed = [c for c in fake.exec_calls if c.container_id == record.container_id]
    assert not any("systemd" in arg for call in replayed for arg in call.argv), (
        "the init entries are never replayed (Decision 6)"
    )


@pytest.mark.fr("FR-021")
def test_a_pinned_build_creates_its_root_from_the_pin_and_provisions_nothing(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """AC-1 for `pinned`: a services-off realm with no derived image is rebuilt from its pin, as it
    was before ADR-015. Nothing is provisioned, so nothing is committed and no image ID is named."""
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    record = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, services_only=False)
    assert record.generation == 2
    assert record.state is RealmState.STOPPED
    assert record.init_image_id is None
    assert record.manifest == [CURL]
    spec = fake.created_specs[-1]
    assert spec.image_id is None
    assert spec.image_digest_ref == before.image_ref
    assert commits(fake) == 0
    assert not any(tuple(c.argv) == BOOT_STATE_ARGV for c in fake.exec_calls), (
        "a services-off build has no boot check (ADR-015 Decision 5)"
    )
    assert last_rebuild_params(env)["image_source"] == "pinned"


@pytest.mark.fr("FR-021")
def test_a_reuse_build_creates_its_root_from_the_recorded_derived_image_with_no_pull_or_commit(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """AC-1 for `reuse`: turning services off keeps the derived image and its ID. The root is
    created from that ID, with no pull and no commit, and the record keeps its pin (Decision 9)."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    on = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    set_services_mode(env, NAME, on=False)
    pulled, committed = pulls(fake), commits(fake)
    notices: list[Any] = []
    record = stale_rebuild(
        env, fake, home, tmp_path, sleeps, mountinfo, runit=runit, warnings=notices
    )
    assert record.generation == 3
    assert record.services_at_create is False
    assert not any(
        c.container_id == record.container_id and tuple(c.argv) == BOOT_STATE_ARGV
        for c in fake.exec_calls
    ), "the reused services-off root has no boot check"
    assert not any(w.code == "GINN-W-PROVISION-LEFTOVER" for w in notices), (
        "a successful reuse build leaves nothing behind but its recorded image"
    )
    assert record.init_image_id == on.init_image_id
    assert record.image_digest == on.image_digest == before.image_digest
    assert record.image_ref == before.image_ref
    assert fake.created_specs[-1].image_id == on.init_image_id
    assert (pulls(fake), commits(fake)) == (pulled, committed)
    assert on.init_image_id in derived_ids(fake)
    assert last_rebuild_params(env)["image_source"] == "reuse"


@pytest.mark.fr("FR-021")
def test_a_reuse_build_refuses_a_recorded_image_id_that_no_derived_image_holds(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """Decision 9: a reuse build creates nothing from an ID that the image store does not hold, and
    the derived image it did not use is not removed."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    on = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    set_services_mode(env, NAME, on=False)
    RealmStore(env.paths).save(replace(stored(env), init_image_id=WRONG_IMAGE_ID))
    created = len(fake.created_specs)
    with pytest.raises(GinnError) as caught:
        stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    assert caught.value.code == "GINN-E-IMAGE-DERIVED"
    assert len(fake.created_specs) == created, "nothing is created from the refused ID"
    assert stored(env).generation == 2
    assert stored(env).state is RealmState.STOPPED
    assert on.init_image_id in derived_ids(fake)


@pytest.mark.fr("FR-021")
def test_a_degraded_boot_fails_the_build_names_its_failed_units_and_keeps_the_old_root(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """AC-2 with GINN-E-REALM-BOOT: a boot that reads `degraded` fails the build at once, the failed
    units are in the error, and the derived image the build made is removed with the rollback."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    failed_boot(fake, "foo.service", "bar.service")
    set_services_mode(env, NAME, on=True)
    with pytest.raises(GinnError) as caught:
        stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    err = caught.value
    assert err.code == "GINN-E-REALM-BOOT"
    assert err.details["failed_units"] == ["foo.service", "bar.service"]
    assert "foo.service, bar.service" in err.message
    assert stored(env).generation == 1
    assert stored(env).state is RealmState.STOPPED
    assert stored(env).init_image_id is None
    assert derived_ids(fake) == set()
    assert generation_of(fake, 2) == []
    assert before.container_id in fake.containers


@pytest.mark.fr("FR-021")
def test_a_services_on_build_is_checked_for_running_before_its_attach_even_with_no_packages(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The boot check runs through the relay, once for a boot that is running at once, before the
    attach. It runs with an empty user manifest too (ADR-015 Decision 5)."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=())
    set_services_mode(env, NAME, on=True)
    record = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    checks = [i for i, c in enumerate(fake.exec_calls) if tuple(c.argv) == BOOT_STATE_ARGV]
    assert len(checks) == 1
    assert fake.exec_calls[checks[0]].target == ExecTarget(
        realm=NAME.value, services=True, mutating=True
    )
    attaches = [i for i, c in enumerate(fake.exec_calls) if tuple(c.argv) == RUN_USER_STAT]
    assert attaches and checks[0] < attaches[0], "the boot check comes before the attach"
    assert user_entries(record) == []
    assert record.state is RealmState.STOPPED


@pytest.mark.fr("FR-021")
def test_a_boot_still_starting_at_the_deadline_fails_the_build(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A boot that does not reach running within the start's wait fails with GINN-E-REALM-BOOT,
    and the detail says the state it was in (ADR-015 Decision 5)."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    fake.exec_results[BOOT_STATE_ARGV] = ExecResult(1, "starting\n", "")
    set_services_mode(env, NAME, on=True)
    with pytest.raises(GinnError) as caught:
        stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    assert caught.value.code == "GINN-E-REALM-BOOT"
    assert "state starting at the deadline" in caught.value.message
    assert caught.value.details["failed_units"] == []
    assert stored(env).generation == 1


@pytest.mark.fr("FR-022")
def test_an_init_package_that_provisioning_refuses_fails_the_build_and_is_not_a_missing_replay(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The init set is never replayed, so a refusal of it fails the build (FR-022). The refusal
    here names a missing package, which a replay would turn into GINN-W-REPLAY-MISSING. The build
    raises instead, and the old root and the record are unchanged."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    distro = load_distro("debian")
    argv = install_argv(distro, distro.services_packages)
    assert argv is not None
    fake.exec_results[tuple(argv)] = ExecResult(100, "", "E: Unable to locate package systemd\n")
    set_services_mode(env, NAME, on=True)
    with pytest.raises(GinnError) as caught:
        stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert stored(env).generation == 1
    assert stored(env).state is RealmState.STOPPED
    assert stored(env).init_image_id is None
    assert derived_ids(fake) == set()


@pytest.mark.fr("NFR-031")
@pytest.mark.fr("FR-021")
def test_a_reuse_build_that_fails_keeps_the_old_root_and_the_derived_image_it_runs_from(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """AC-2 for `reuse`: a failed reuse build removes the root it made, and never the derived image,
    which the record still names and the old root still runs from."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    on = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    set_services_mode(env, NAME, on=False)
    with pytest.raises(GinnError):
        stale_rebuild(
            env,
            fake,
            home,
            tmp_path,
            sleeps,
            mountinfo,
            runit=runit,
            on_step=_raise_at("started", make("GINN-E-INTERNAL")),
        )
    assert stored(env).generation == 2
    assert stored(env).state is RealmState.STOPPED
    assert stored(env).init_image_id == on.init_image_id
    assert on.init_image_id in derived_ids(fake)
    assert generation_of(fake, 3) == []


@pytest.mark.fr("FR-021")
def test_a_rebuild_of_a_realm_with_a_derived_image_provisions_it_again_and_removes_the_old_one(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """`ginn rebuild` is a `provision` build for a realm with a derived image (Decision 7). The new
    image replaces the old one, which the swap removes after its containers (Decision 13)."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    first = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    result = rebuild(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
    )
    record = result.record
    assert record.generation == 3
    assert record.init_image_id not in (None, first.init_image_id)
    assert record.init_image_id in derived_ids(fake)
    assert first.init_image_id not in derived_ids(fake)
    assert last_rebuild_params(env)["image_source"] == "provision"
    assert user_entries(record) == [CURL]


@pytest.mark.fr("FR-066")
def test_a_tier_change_of_a_realm_with_a_derived_image_reprovisions_it_from_the_current_pin(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """A tier change is not a services-only change, so its start re-provisions the realm (FR-066),
    even though the realm has a derived image. The image it had goes after the swap."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    first = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    set_tier(env, NAME, Tier.DEVELOPMENT)
    committed = commits(fake)
    hooks = replace(
        _nested_hooks(tmp_path, sleeps, mountinfo),
        lifecycle=_supervised_lifecycle(tmp_path, mountinfo, runit),
    )
    record = rebuild_for_start(
        env, fake, NAME, session=session(home), gid=GID, hooks=hooks, services_only=False
    )
    assert record.generation == 3
    assert record.settings.tier is Tier.DEVELOPMENT
    assert commits(fake) > committed, "the tier change provisions a new derived image"
    assert record.init_image_id not in (None, first.init_image_id)
    assert first.init_image_id not in derived_ids(fake)
    assert last_rebuild_params(env)["image_source"] == "provision"


def storage_formula(distro: str) -> tuple[int, int]:
    """The storage formula of a services-on build of `distro`, and the staging formula of its commit
    (ADR-015 Decision 11), from the pinned entry and the distribution data."""
    entry = trust.load().entry(distro)
    definition = load_distro(distro)
    storage = storage_required_bytes(
        estimate_bytes(entry),
        definition.services_estimated_installed_bytes,
        definition.integration_estimated_installed_bytes,
        definition.package_cache_peak_bytes,
    )
    staging = tmpdir_required_bytes(
        definition.services_estimated_installed_bytes,
        definition.integration_estimated_installed_bytes,
    )
    return storage, staging


@pytest.mark.fr("FR-121")
def test_a_provision_build_is_refused_before_its_pull_when_the_storage_formula_does_not_fit(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """ADR-015 Decision 11: a provision build needs the create formula of its stage, with the base
    counted, the init set and the Integration Package Set twice, and the package cache peak. The
    rebuild refuses before its pull, and the old root is kept."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    pulled, committed = pulls(fake), commits(fake)
    storage, _ = storage_formula("debian")
    with pytest.raises(GinnError) as caught:
        stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, statvfs=_statvfs(0))
    assert caught.value.code == "GINN-E-DISK-SPACE"
    assert caught.value.details["needed"] == f"{storage} bytes"
    assert (pulls(fake), commits(fake)) == (pulled, committed)
    assert stored(env).generation == 1
    assert stored(env).state is RealmState.STOPPED
    assert stored(env).init_image_id is None


@pytest.mark.fr("FR-121")
def test_a_provision_build_is_refused_before_its_pull_when_the_image_staging_directory_is_short(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """ADR-015 Decision 11: the image copy of a commit is staged in the image staging directory,
    which has its own check with the init set and the Integration Package Set. A short directory is
    refused before the pull, even when the storage has room."""
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    staging_dir = env.paths.image_tmp
    staging_dir.mkdir(parents=True, exist_ok=True)

    def statvfs(path: Any) -> SimpleNamespace:
        free = 0 if str(path) == str(staging_dir) else AMPLE
        return SimpleNamespace(f_bavail=free, f_frsize=1)

    pulled, committed = pulls(fake), commits(fake)
    _, staging = storage_formula("debian")
    with pytest.raises(GinnError) as caught:
        stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, statvfs=statvfs)
    assert caught.value.code == "GINN-E-DISK-SPACE"
    assert caught.value.details["needed"] == f"{staging} bytes"
    assert caught.value.details["path"] == str(staging_dir)
    assert (pulls(fake), commits(fake)) == (pulled, committed)
    assert stored(env).generation == 1


@pytest.mark.fr("FR-121")
def test_a_reuse_build_is_refused_when_the_writable_layer_of_its_container_does_not_fit(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """ADR-015 Decision 11: a reuse build needs the writable layer of the one container it creates.
    With less free than that, it is refused before its container is created, and the old root and
    the derived image it runs from are kept. The current disk use term is not added (deferred)."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    on = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    set_services_mode(env, NAME, on=False)
    layer = estimate_required(load_distro("debian").writable_layer_bytes)
    created = len(fake.created_specs)
    with pytest.raises(GinnError) as caught:
        stale_rebuild(
            env, fake, home, tmp_path, sleeps, mountinfo, runit=runit, statvfs=_statvfs(layer - 1)
        )
    assert caught.value.code == "GINN-E-DISK-SPACE"
    assert caught.value.details["needed"] == f"{layer} bytes"
    assert len(fake.created_specs) == created, "no container is created from the derived image"
    assert stored(env).generation == 2
    assert stored(env).state is RealmState.STOPPED
    assert stored(env).init_image_id == on.init_image_id
    assert on.init_image_id in derived_ids(fake)


@pytest.mark.fr("FR-121")
def test_a_reuse_build_needs_its_writable_layer_and_no_base_or_init_bytes(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """ADR-015 Decision 11: the free space that a reuse build needs is its writable layer, and no
    base or init bytes. With exactly that free, far less than the base estimate, it goes on."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    on = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    set_services_mode(env, NAME, on=False)
    layer = estimate_required(load_distro("debian").writable_layer_bytes)
    assert layer < estimate_bytes(trust.load().entry("debian")), "the base is far larger"
    record = stale_rebuild(
        env, fake, home, tmp_path, sleeps, mountinfo, runit=runit, statvfs=_statvfs(layer)
    )
    assert record.generation == 3
    assert record.init_image_id == on.init_image_id
    assert fake.created_specs[-1].image_id == on.init_image_id
    assert last_rebuild_params(env)["image_source"] == "reuse"


@pytest.mark.fr("FR-021")
def test_a_reuse_build_names_the_digest_its_derived_image_was_built_from_not_the_current_pin(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """ADR-015 Decision 9: the reuse container names the digest that its derived image was built
    from, which the record holds. The pin has moved since, so the record's old digest is the one the
    container names, and the record keeps it. The current pin is not used."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    before = make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    on = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    old_digest = "sha256:" + "c" * 64
    old_ref = before.image_ref.rsplit("@", 1)[0] + "@" + old_digest
    assert old_ref != before.image_ref, "the test moves the pin the record names"
    set_services_mode(env, NAME, on=False)
    RealmStore(env.paths).save(replace(stored(env), image_ref=old_ref, image_digest=old_digest))
    record = stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    assert record.generation == 3
    assert fake.created_specs[-1].image_digest_ref == old_ref
    assert fake.created_specs[-1].image_id == on.init_image_id
    assert record.image_ref == old_ref
    assert record.image_digest == old_digest


@pytest.mark.fr("FR-021")
def test_the_boot_check_gets_only_the_budget_that_the_start_left(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-015 Decision 5: the start and the boot check share one bound of `start_wait_s`. Here the
    start takes 50 s of the 60 s, so the boot check polls for the 10 s that are left, and not for a
    fresh 60 s. It checks once at each second, and the last check is at the deadline."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    real_start = fake.start

    def slow_start(container_id: str) -> None:
        if not fake.containers[container_id].name.startswith(PROVISION_PREFIX):
            runit.clock.advance(50)
        real_start(container_id)

    monkeypatch.setattr(fake, "start", slow_start)
    fake.exec_results[BOOT_STATE_ARGV] = ExecResult(1, "starting\n", "")
    with pytest.raises(GinnError) as caught:
        stale_rebuild(env, fake, home, tmp_path, sleeps, mountinfo, runit=runit)
    assert caught.value.code == "GINN-E-REALM-BOOT"
    assert "at the deadline" in caught.value.message
    boots = [c for c in fake.exec_calls if tuple(c.argv) == BOOT_STATE_ARGV]
    assert len(boots) == 11, "one check at each second of the 10 s left, the last at the deadline"


@pytest.mark.fr("FR-122")
def test_a_provisioning_leftover_comes_back_as_a_warning_of_the_build(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """ADR-015 Decision 2: a stage removal that fails leaves its image, and the build still
    completes. The note of the leftover comes back as a GINN-W-PROVISION-LEFTOVER warning in the
    list that `rebuild_for_start` appends to, so the start shows it."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    fake.fail_ops["rmi"] = "GINN-E-INTERNAL"
    notices: list[Any] = []
    record = stale_rebuild(
        env, fake, home, tmp_path, sleeps, mountinfo, runit=runit, warnings=notices
    )
    leftovers = [w for w in notices if w.code == "GINN-W-PROVISION-LEFTOVER"]
    assert leftovers, "the failed removal is a warning"
    assert leftovers[0].message.startswith(
        f"realm '{NAME.value}': a provisioning or derived-image leftover was not removed: "
    )
    assert record.generation == 2
    assert record.state is RealmState.STOPPED
    assert record.init_image_id is not None


@pytest.mark.fr("FR-021")
def test_the_swap_clears_the_building_init_image_id_and_names_the_same_derived_image(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """ADR-015 Decision 6: `building_init_image_id` is written before the start of a provision
    build, and the swap clears it while it sets `init_image_id` to the same image. The record read
    at `swapping` still holds the building ID, so the clear is the swap's own write."""
    runit = _runit(env, tmp_path, mountinfo, home, fake, sleeps)
    make_realm(env, fake, home, tmp_path, sleeps, mountinfo, packages=("curl",))
    set_services_mode(env, NAME, on=True)
    at_swap: list[str | None] = []

    def on_step(step: str) -> None:
        if step == "swapping":
            at_swap.append(stored(env).building_init_image_id)

    record = stale_rebuild(
        env, fake, home, tmp_path, sleeps, mountinfo, runit=runit, on_step=on_step
    )
    assert at_swap and at_swap[0] is not None, "the building ID is recorded before the swap"
    assert at_swap[0] == record.init_image_id
    assert record.building_init_image_id is None
    assert stored(env).building_init_image_id is None
