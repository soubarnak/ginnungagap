# SPDX-License-Identifier: Apache-2.0
"""The provisioning stage of a services-on realm (ADR-015 Decisions 2, 3, 9, 10, 11 and 13).

The stage runs against FakePodman. Each test states the requirement it proves. The kill sweeps
(NFR-030) stop the stage at every podman call and every journal step, in turn, from a fresh world.
A kill is a BaseException, as in tests/unit/crash_support.py, so no rollback runs for it. The sweep
then runs the removal that recovery runs, and the state must be clean. An interrupt rolls back at
the same points, with no sweep at all.
"""

import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ginn.distros import DistroDef, Verb, load
from ginn.domain import InstallMethod, InstallSource, ManifestEntry, RealmName
from ginn.errors import GinnError, make
from ginn.ops.disk import estimate_required
from ginn.ops.provision import (
    STEP_IMAGE_COMMITTED,
    STEP_INIT_INSTALLED,
    STEP_IPS_INSTALLED,
    STEP_PROVISION_CREATED,
    STEP_PROVISION_REMOVED,
    STEP_RESET_CREATED,
    STEP_RESET_REMOVED,
    STEP_STAGE_COMMITTED,
    STEP_STAGE_REMOVED,
    ProvisionEnv,
    ProvisionPlan,
    ProvisionResult,
    check_init,
    provision_init,
    remove_derived,
    storage_required_bytes,
    sweep_provisions,
    tmpdir_required_bytes,
    verify_derived_id,
)
from ginn.podman.adapter import CreateSpec, ExecTarget
from ginn.podman.models import ExecResult, ImageInfo
from ginn.runtime.provision import (
    derived_image_name,
    derived_image_prefix,
    init_set_hash,
    keep_id_entry_check_argv,
    keep_id_entry_drop_argv,
    provision_create_spec,
    provision_name,
    reset_name,
    stage_image_name,
)
from ginn.store.paths import OwnedPaths
from tests.fakes.fake_podman import FakePodman

PIN_DIGEST = "sha256:" + "a" * 64
PIN_REF = f"docker.io/library/debian@{PIN_DIGEST}"
REALM = RealmName("ginnrealm")
GENERATION = 1
DEBIAN = load("debian")
FEDORA = load("fedora")
KALI = load("kali")
ARCH = load("arch")
DERIVED = derived_image_name(REALM, GENERATION)
STAGE = stage_image_name(REALM, GENERATION)
PROVISION = provision_name(REALM, GENERATION)
RESET = reset_name(REALM, GENERATION)
EXEC_TARGET = ExecTarget(realm="ginnrealm", services=False, mutating=True)
UID = 1000
"""The host uid of the realm user, whose keep-id passwd line the stage removes (T7.30)."""
BYPASS_TOKENS = (
    "nogpgcheck",
    "gpgcheck=0",
    "allow-unauthenticated",
    "AllowUnauthenticated",
    "force-yes",
    "no-verify",
    "SigLevel",
)
"""Words that would turn a signature check off in any package manager's argv (FR-012)."""
ADR_BASE_BYTES = 81_256_893
ADR_CACHE_PEAK_BYTES = 21_576_652
ADR_IPS_BYTES = 23_975_936


class SimulatedKill(BaseException):
    """A SIGKILL, in process. A BaseException, so the stage's rollback does not catch it."""


class _Vfs:
    f_bavail = 0
    f_frsize = 4096


class _Journal:
    """Records each step name, in order. It is the StepJournal the stage writes to."""

    def __init__(self) -> None:
        self.steps: list[str] = []

    def step(self, name: str, params: Mapping[str, Any] | None = None) -> None:
        self.steps.append(name)


class _Killer:
    """Counts the points of one run, and raises at point `kill_at` (crash_support.py's model).

    A point is each state-changing podman call, before and after it runs, and each journal step,
    after it is recorded. A dry run with no kill lists the points, and each later run kills at one.
    """

    def __init__(self, kill_at: int | None, exc: type[BaseException]) -> None:
        self.points: list[str] = []
        self._kill_at = kill_at
        self._exc = exc

    def hit(self, label: str) -> None:
        index = len(self.points)
        self.points.append(label)
        if index == self._kill_at:
            raise self._exc(label)


MUTATORS: frozenset[str] = frozenset(
    {"pull", "create", "start", "stop", "rm", "rmi", "commit", "exec", "cp_into"}
)


class _Proxy:
    """A FakePodman that reports each state-changing call to the killer, before and after it."""

    def __init__(self, fake: FakePodman, killer: _Killer) -> None:
        self._fake = fake
        self._killer = killer

    def __getattr__(self, name: str) -> Callable[..., Any] | Any:
        attr = getattr(self._fake, name)
        if name not in MUTATORS:
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            self._killer.hit(f"{name} before")
            result = attr(*args, **kwargs)
            self._killer.hit(f"{name} after")
            return result

        return call


class _FailingJournal(_Journal):
    """A journal whose write fails at one step: the durable record of that step cannot be made."""

    def __init__(self, fail_at: str) -> None:
        super().__init__()
        self._fail_at = fail_at

    def step(self, name: str, params: Mapping[str, Any] | None = None) -> None:
        if name == self._fail_at:
            raise make("GINN-E-INTERNAL")
        super().step(name, params)


class _KillJournal(_Journal):
    def __init__(self, killer: _Killer) -> None:
        super().__init__()
        self._killer = killer

    def step(self, name: str, params: Mapping[str, Any] | None = None) -> None:
        super().step(name, params)
        self._killer.hit(f"step {name}")


def _world(
    paths: OwnedPaths,
    *,
    distro: DistroDef = DEBIAN,
    pinned: bool = True,
    lang: str | None = "C.UTF-8",
    statvfs: Callable[[str], Any] = os.statvfs,
    sleep: Callable[[float], None] = lambda seconds: None,
    keep_ids: frozenset[str] = frozenset(),
) -> tuple[FakePodman, ProvisionEnv, ProvisionPlan, _Journal]:
    fake = FakePodman()
    if pinned:
        fake.pull(PIN_REF)
    env = ProvisionEnv(podman=fake, paths=paths, sleep=sleep, statvfs=statvfs)
    plan = ProvisionPlan(
        realm=REALM,
        generation=GENERATION,
        distro=distro,
        image_digest_ref=PIN_REF,
        lang=lang,
        ips_installed_bytes=ADR_IPS_BYTES,
        keep_ids=keep_ids,
        uid=UID,
    )
    return fake, env, plan, _Journal()


def _killed_run(
    paths: OwnedPaths,
    *,
    kill_at: int | None = None,
    exc: type[BaseException] = SimulatedKill,
    pinned: bool = True,
) -> tuple[FakePodman, _Killer, ProvisionResult | BaseException]:
    fake, env, plan, _ = _world(paths, pinned=pinned)
    killer = _Killer(kill_at, exc)
    proxied = replace(env, podman=_Proxy(fake, killer))
    try:
        return fake, killer, provision_init(proxied, plan, _KillJournal(killer))
    except BaseException as caught:
        return fake, killer, caught


def _leftovers(fake: FakePodman) -> list[str]:
    """What a provisioning leaves behind: its containers and the realm's derived and stage images.

    The pinned base is not in this list. The callers check it, because only a base that the stage
    pulled is the stage's own to remove, and a kill leaves even that for recovery.
    """
    found = [info.name for info in fake.ps() if info.name.startswith("ginn_provision_")]
    prefix = derived_image_prefix(REALM)
    found += [name for name in fake.images if name.startswith(prefix)]
    return found


def _changes_of(fake: FakePodman, name: str) -> tuple[str, ...]:
    """The --change list of the commit to `name`, as the fake recorded it."""
    for call in fake.calls:
        if call.op == "commit" and call.args[1] == name:
            changes = call.args[2]
            assert isinstance(changes, tuple)
            return tuple(str(change) for change in changes)
    raise AssertionError(f"no commit to {name}")


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


@pytest.mark.fr("FR-080")
def test_the_stage_runs_its_steps_in_adr_order_and_leaves_one_derived_image(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths)
    result = provision_init(env, plan, journal)
    assert journal.steps == [
        STEP_PROVISION_CREATED,
        STEP_INIT_INSTALLED,
        STEP_IPS_INSTALLED,
        STEP_STAGE_COMMITTED,
        STEP_RESET_CREATED,
        STEP_IMAGE_COMMITTED,
        STEP_RESET_REMOVED,
        STEP_STAGE_REMOVED,
        STEP_PROVISION_REMOVED,
    ]
    assert result.image_id == fake.images[DERIVED].id
    assert fake.ps() == []
    assert set(fake.images) == {PIN_REF, DERIVED}
    integration = DEBIAN.integration_set("C.UTF-8")
    argvs = [call.argv for call in fake.exec_calls]
    assert argvs[0] == tuple(DEBIAN.argv(Verb.REFRESH))
    assert argvs[1] == tuple(DEBIAN.argv(Verb.INSTALL, DEBIAN.services_packages))
    assert argvs[2] == tuple(DEBIAN.argv(Verb.INSTALL, integration.packages))
    clean = [tuple(step) for step in DEBIAN.clean_steps]
    assert list(argvs[-len(clean) :]) == clean


@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-048")
def test_the_stage_drops_the_keep_id_line_of_the_realm_uid_before_the_stage_commit(
    paths: OwnedPaths,
) -> None:
    """T7.30: podman's keep-id line for the realm uid must not reach the derived image. A realm
    create keeps a line that is already there, so the user check would fail on every rebuild."""
    fake, env, plan, journal = _world(paths)
    provision_init(env, plan, journal)
    drop = tuple(keep_id_entry_drop_argv(UID))
    drops = [i for i, call in enumerate(fake.calls) if call.op == "exec" and call.args[1] == drop]
    commits = [
        i for i, call in enumerate(fake.calls) if call.op == "commit" and call.args[1] == STAGE
    ]
    assert len(drops) == 1 and len(commits) == 1
    assert drops[0] < commits[0]
    argvs = [call.argv for call in fake.exec_calls]
    install = tuple(DEBIAN.argv(Verb.INSTALL, DEBIAN.services_packages))
    check = tuple(keep_id_entry_check_argv(UID))
    clean = tuple(DEBIAN.clean_steps[0])
    assert argvs.index(install) < argvs.index(drop) < argvs.index(check) < argvs.index(clean)
    assert fake.exec_calls[argvs.index(drop)].user == "0:0"
    assert fake.exec_calls[argvs.index(check)].user == "0:0"


@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-048")
def test_a_getent_that_finds_no_account_at_the_uid_lets_the_stage_commit(paths: OwnedPaths) -> None:
    """T7.30: exit 2 is the only proof that the uid has no account, and the stage then commits."""
    fake, env, plan, journal = _world(paths)
    fake.exec_results[tuple(keep_id_entry_check_argv(UID))] = ExecResult(2, "", "")
    result = provision_init(env, plan, journal)
    assert result.image_id == fake.images[DERIVED].id
    assert any(call.op == "commit" and call.args[1] == STAGE for call in fake.calls)


@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-048")
@pytest.mark.parametrize("found", [0, 1])
def test_an_account_at_the_uid_after_the_drop_refuses_the_stage_before_any_commit(
    paths: OwnedPaths, found: int
) -> None:
    """T7.30: a base image that already has an account at the host uid, or a line the drop left,
    is refused before the stage commit. No stage or derived image is made, and nothing is left."""
    fake, env, plan, journal = _world(paths)
    fake.exec_results[tuple(keep_id_entry_check_argv(UID))] = ExecResult(
        found, "builder:x:1000:1000::/home/builder:/bin/sh\n", ""
    )
    with pytest.raises(GinnError) as info:
        provision_init(env, plan, journal)
    assert info.value.code == "GINN-E-INTERNAL"
    assert not [call for call in fake.calls if call.op == "commit"]
    assert STAGE not in fake.images
    assert DERIVED not in fake.images
    assert _leftovers(fake) == []


@pytest.mark.fr("FR-080")
@pytest.mark.fr("FR-048")
def test_a_failed_drop_is_an_internal_error_and_no_stage_image_is_committed(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths)
    fake.exec_results[tuple(keep_id_entry_drop_argv(UID))] = ExecResult(
        1, "", "sed: can't read /etc/passwd\n"
    )
    with pytest.raises(GinnError) as info:
        provision_init(env, plan, journal)
    assert info.value.code == "GINN-E-INTERNAL"
    assert STAGE not in fake.images
    assert _leftovers(fake) == []


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.fr("CR-007")
def test_every_exec_is_an_argv_list_as_root_with_the_services_off_target(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths)
    provision_init(env, plan, journal)
    assert fake.exec_calls
    for call in fake.exec_calls:
        assert call.target == EXEC_TARGET
        assert call.user == "0:0"
        assert isinstance(call.argv, tuple)
        assert all(isinstance(word, str) for word in call.argv)


@pytest.mark.fr("FR-012")
@pytest.mark.parametrize("distro", [DEBIAN, FEDORA, KALI, ARCH], ids=lambda d: d.id)
def test_no_install_argv_turns_a_signature_check_off(paths: OwnedPaths, distro: DistroDef) -> None:
    fake, env, plan, journal = _world(paths, distro=distro)
    provision_init(env, plan, journal)
    assert fake.exec_calls
    for call in fake.exec_calls:
        for word in call.argv:
            assert not any(token in word for token in BYPASS_TOKENS), call.argv


@pytest.mark.fr("FR-080")
@pytest.mark.parametrize("distro", [DEBIAN, FEDORA, KALI, ARCH], ids=lambda d: d.id)
def test_each_distribution_stages_and_records_its_init_entries(
    paths: OwnedPaths, distro: DistroDef
) -> None:
    fake, env, plan, journal = _world(paths, distro=distro)
    result = provision_init(env, plan, journal)
    assert result.init_entries == tuple(
        ManifestEntry(
            name=name,
            source=InstallSource.REPO,
            method=InstallMethod.GINN,
            init=True,
        )
        for name in distro.services_packages
    )
    assert journal.steps[-1] == STEP_PROVISION_REMOVED
    assert set(fake.images) == {PIN_REF, DERIVED}


@pytest.mark.fr("FR-010")
def test_arch_provisions_for_its_integration_set_with_an_empty_init_set(paths: OwnedPaths) -> None:
    assert ARCH.services_packages == ()
    fake, env, plan, journal = _world(paths, distro=ARCH)
    result = provision_init(env, plan, journal)
    assert result.init_entries == ()
    assert STEP_INIT_INSTALLED in journal.steps
    # No init install runs: refresh, then the Integration Package Set, its locale steps, the keep-id
    # drop and its getent check (T7.30), and clean.
    integration = ARCH.integration_set("C.UTF-8")
    expected = 1 + (1 if integration.packages else 0) + len(integration.steps)
    expected += 2 + len(ARCH.clean_steps)
    assert len(fake.exec_calls) == expected
    assert fake.exec_calls[0].argv == tuple(ARCH.argv(Verb.REFRESH))


@pytest.mark.fr("FR-011")
def test_a_pinned_image_that_is_present_is_not_pulled_again(paths: OwnedPaths) -> None:
    fake, env, plan, journal = _world(paths)
    provision_init(env, plan, journal)
    assert fake.ops().count("pull") == 1  # the fixture's own pull, before the stage


@pytest.mark.fr("FR-011")
def test_a_digest_mismatch_refuses_before_any_container_and_keeps_no_layers(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths, pinned=False)
    fake.pull_digest_override = "sha256:" + "b" * 64
    with pytest.raises(GinnError) as caught:
        provision_init(env, plan, journal)
    assert caught.value.code == "GINN-E-IMAGE-UNVERIFIED"
    assert fake.images == {}
    assert fake.containers == {}
    assert "create" not in fake.ops()
    assert journal.steps == []


@pytest.mark.fr("FR-080")
def test_an_image_without_an_init_is_refused_before_the_commit(paths: OwnedPaths) -> None:
    fake, env, plan, journal = _world(paths)
    fake.init_absent = True
    with pytest.raises(GinnError) as caught:
        provision_init(env, plan, journal)
    assert caught.value.code == "GINN-E-NO-INIT"
    assert not any(c.op == "commit" for c in fake.calls)
    assert fake.ps() == []
    assert set(fake.images) == {PIN_REF}
    assert STEP_IPS_INSTALLED in journal.steps
    assert STEP_STAGE_COMMITTED not in journal.steps


@pytest.mark.fr("FR-080")
def test_check_init_reads_the_init_of_a_container_and_names_a_missing_one(
    paths: OwnedPaths,
) -> None:
    fake = FakePodman()
    fake.create(CreateSpec(name="ginn-probe", image_digest_ref=PIN_REF, network="host"))
    container_id = next(iter(fake.containers))
    check_init(fake, paths, container_id, image=PIN_REF, realm="ginnrealm")
    fake.init_absent = True
    with pytest.raises(GinnError) as caught:
        check_init(fake, paths, container_id, image=PIN_REF, realm="ginnrealm")
    assert caught.value.code == "GINN-E-NO-INIT"
    assert caught.value.details["image"] == PIN_REF


@pytest.mark.fr("CR-007")
def test_the_provisioning_container_has_no_realm_home_device_mount_or_passwd_entry(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths)
    provision_init(env, plan, journal)
    spec = fake.create_specs[0]
    assert spec == provision_create_spec(REALM, GENERATION, PIN_REF)
    assert spec.name == PROVISION
    assert spec.mounts == []
    assert spec.devices == []
    assert spec.tmpfs == []
    assert spec.passwd_entry is None
    assert spec.user == "0:0"
    assert spec.userns == "keep-id"
    assert spec.entrypoint_argv == ["sleep"]
    assert spec.command == ["infinity"]
    assert spec.labels == {"io.ginn.provision": "ginnrealm:1"}


@pytest.mark.fr("CR-007")
def test_the_stage_commit_keeps_the_pinned_user_and_environment_and_clears_the_realm(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths)
    fake.images[PIN_REF] = replace(
        fake.images[PIN_REF], user="1000:1000", env=("PATH=/usr/bin", "HOME=/root")
    )
    provision_init(env, plan, journal)
    changes = _changes_of(fake, STAGE)
    assert "USER 1000:1000" in changes
    assert "ENV PATH=/usr/bin" in changes
    assert "ENV HOME=/root" in changes
    assert "LABEL io.ginn.provision=" in changes
    assert f"LABEL io.ginn.init-set={init_set_hash(DEBIAN.services_packages)}" in changes
    assert f"LABEL io.ginn.base-digest={PIN_DIGEST}" in changes
    assert not any(change.startswith(("ENTRYPOINT", "CMD")) for change in changes)


@pytest.mark.fr("CR-007")
def test_a_stage_user_defaults_to_root_when_the_pinned_image_names_none(paths: OwnedPaths) -> None:
    fake, env, plan, journal = _world(paths)
    provision_init(env, plan, journal)
    assert "USER 0" in _changes_of(fake, STAGE)


@pytest.mark.fr("CR-007")
def test_the_reset_container_has_a_null_entrypoint_and_command_and_is_never_started(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths)
    provision_init(env, plan, journal)
    reset_spec = next(spec for spec in fake.create_specs if spec.name == RESET)
    argv = reset_spec.to_argv()
    assert reset_spec.clear_entrypoint is True
    assert reset_spec.command == []
    assert argv[argv.index("--entrypoint") + 1] == "[]"
    assert argv[-1] == reset_spec.image_id
    assert reset_spec.labels == {"io.ginn.provision": "ginnrealm:1"}
    # Only the provisioning container is started. The reset container is created and never run.
    assert fake.ops().count("start") == 1


@pytest.mark.fr("CR-007")
def test_the_derived_image_has_no_realm_label_and_keeps_the_three_ginn_labels(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths)
    provision_init(env, plan, journal)
    labels = fake.images[DERIVED].labels
    assert labels["io.ginn.provision"] == ""
    assert "io.ginn.realm" not in labels
    assert labels["io.ginn.init-set"] == init_set_hash(DEBIAN.services_packages)
    assert labels["io.ginn.base-digest"] == PIN_DIGEST


@pytest.mark.fr("CR-007")
def test_an_environment_value_that_would_split_the_commit_is_refused_before_any_container(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths)
    fake.images[PIN_REF] = replace(fake.images[PIN_REF], env=("BAD=a,b",))
    with pytest.raises(GinnError) as caught:
        provision_init(env, plan, journal)
    assert caught.value.code == "GINN-E-INTERNAL"
    assert "create" not in fake.ops()
    assert fake.ps() == []


@pytest.mark.fr("DR-040")
def test_the_derived_id_is_checked_after_the_final_commit(paths: OwnedPaths) -> None:
    fake, env, plan, journal = _world(paths)
    result = provision_init(env, plan, journal)
    assert len(result.image_id) == 64
    assert fake.image_id(DERIVED) == result.image_id


@pytest.mark.fr("DR-040")
def test_verify_derived_id_refuses_an_image_with_another_id_or_none(paths: OwnedPaths) -> None:
    fake = FakePodman()
    fake.pull(PIN_REF)
    good = fake.image_id(PIN_REF)
    verify_derived_id(fake, "ginnrealm", PIN_REF, good)
    with pytest.raises(GinnError) as caught:
        verify_derived_id(fake, "ginnrealm", PIN_REF, "b" * 64)
    assert caught.value.code == "GINN-E-IMAGE-DERIVED"
    assert caught.value.details["realm"] == "ginnrealm"
    with pytest.raises(GinnError) as absent:
        verify_derived_id(fake, "ginnrealm", DERIVED, good)
    assert absent.value.code == "GINN-E-IMAGE-DERIVED"
    with pytest.raises(ValueError):
        verify_derived_id(fake, "ginnrealm", PIN_REF, "sha256:" + good)


@pytest.mark.fr("DR-040")
@pytest.mark.fr("FR-122")
def test_a_failed_stage_commit_is_image_derived_and_leaves_nothing_behind(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths)
    fake.fail_ops["commit"] = "GINN-E-INTERNAL"
    with pytest.raises(GinnError) as caught:
        provision_init(env, plan, journal)
    assert caught.value.code == "GINN-E-IMAGE-DERIVED"
    assert fake.ps() == []
    assert set(fake.images) == {PIN_REF}


@pytest.mark.fr("FR-121")
def test_the_storage_formula_matches_the_adr_worked_example_for_debian() -> None:
    raw = ADR_BASE_BYTES + 2 * (DEBIAN.services_estimated_installed_bytes + ADR_IPS_BYTES)
    raw += ADR_CACHE_PEAK_BYTES
    assert raw == 189_117_833
    # The 1.10 margin rounds up (ADR-015 Decision 11): 189,117,833 x 1.10 = 208,029,616.3.
    assert storage_required_bytes(
        ADR_BASE_BYTES,
        DEBIAN.services_estimated_installed_bytes,
        ADR_IPS_BYTES,
        ADR_CACHE_PEAK_BYTES,
    ) == estimate_required(raw)
    assert estimate_required(raw) == 208_029_617
    assert tmpdir_required_bytes(19_166_208, ADR_IPS_BYTES) == 47_456_359


@pytest.mark.fr("FR-121")
def test_the_formulas_refuse_a_negative_estimate() -> None:
    with pytest.raises(ValueError):
        storage_required_bytes(-1, 0, 0, 0)
    with pytest.raises(ValueError):
        tmpdir_required_bytes(0, -1)


@pytest.mark.fr("FR-121")
def test_a_short_staging_directory_refuses_before_the_stage_commit(paths: OwnedPaths) -> None:
    fake, env, plan, journal = _world(paths, statvfs=lambda path: _Vfs())
    with pytest.raises(GinnError) as caught:
        provision_init(env, plan, journal)
    assert caught.value.code == "GINN-E-DISK-SPACE"
    assert not any(c.op == "commit" for c in fake.calls)
    assert fake.ps() == []
    assert STEP_STAGE_COMMITTED not in journal.steps


@pytest.mark.fr("FR-120")
def test_a_network_failure_is_retried_on_the_fr120_waits_and_then_rolls_back(
    paths: OwnedPaths,
) -> None:
    waits: list[float] = []
    fake, env, plan, journal = _world(paths, sleep=waits.append)
    fake.fail_exec_with = "GINN-E-NETWORK"
    with pytest.raises(GinnError) as caught:
        provision_init(env, plan, journal)
    assert caught.value.code == "GINN-E-NETWORK"
    assert waits == [5.0, 15.0, 45.0]
    assert fake.ps() == []
    assert set(fake.images) == {PIN_REF}


@pytest.mark.fr("FR-122")
def test_a_leftover_of_this_generation_is_removed_before_the_build(paths: OwnedPaths) -> None:
    fake, env, plan, journal = _world(paths)
    fake.images[STAGE] = _image("5" * 64)
    fake.images[DERIVED] = _image("6" * 64)
    fake.create(provision_create_spec(REALM, GENERATION, PIN_REF))
    result = provision_init(env, plan, journal)
    assert result.image_id not in {"5" * 64, "6" * 64}
    assert fake.images[DERIVED].id == result.image_id
    assert set(fake.images) == {PIN_REF, DERIVED}
    assert fake.ps() == []


@pytest.mark.fr("FR-122")
def test_a_recorded_image_of_this_generation_is_refused_and_never_removed(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths, keep_ids=frozenset({"6" * 64}))
    fake.images[DERIVED] = _image("6" * 64)
    with pytest.raises(GinnError) as caught:
        provision_init(env, plan, journal)
    assert caught.value.code == "GINN-E-INTERNAL"
    assert "a leftover of this generation stays" in str(caught.value)
    assert fake.images[DERIVED].id == "6" * 64
    assert "create" not in fake.ops()
    assert journal.steps == []


@pytest.mark.fr("FR-122")
def test_a_leftover_that_cannot_be_removed_refuses_and_says_why(paths: OwnedPaths) -> None:
    fake, env, plan, journal = _world(paths)
    fake.images[DERIVED] = _image("6" * 64)
    fake.fail_ops["rmi"] = "GINN-E-INTERNAL"
    with pytest.raises(GinnError) as caught:
        provision_init(env, plan, journal)
    assert caught.value.code == "GINN-E-INTERNAL"
    assert "rmi failed with GINN-E-INTERNAL" in str(caught.value)
    assert DERIVED in fake.images
    assert "create" not in fake.ops()
    assert journal.steps == []


@pytest.mark.fr("FR-013")
def test_an_unusable_host_locale_is_returned_as_the_locale_note(paths: OwnedPaths) -> None:
    fake, env, plan, journal = _world(paths, lang="xx")
    result = provision_init(env, plan, journal)
    assert result.locale_note is not None
    assert "C.UTF-8" in result.locale_note
    fake, env, plan, journal = _world(paths, lang=None)
    assert provision_init(env, plan, journal).locale_note is None


@pytest.mark.fr("FR-122")
def test_sweep_provisions_takes_only_this_realms_provisioning_containers(paths: OwnedPaths) -> None:
    fake = FakePodman()
    fake.pull(PIN_REF)
    fake.create(provision_create_spec(REALM, 1, PIN_REF))
    fake.create(provision_create_spec(REALM, 2, PIN_REF))
    fake.create(provision_create_spec(RealmName("otherrealm"), 1, PIN_REF))
    fake.create(
        CreateSpec(
            name="ginn_provision_ginnrealm_g3",
            image_digest_ref=PIN_REF,
            network="host",
            labels={"io.ginn.provision": "ginnrealm"},
        )
    )
    fake.create(
        CreateSpec(
            name="ginn-ginnrealm-g1",
            image_digest_ref=PIN_REF,
            network="host",
            labels={"io.ginn.provision": "ginnrealm:1"},
        )
    )
    removed = sweep_provisions(fake, REALM)
    assert sorted(removed) == [
        "ginn_provision_ginnrealm_g1",
        "ginn_provision_ginnrealm_g2",
    ]
    assert sorted(info.name for info in fake.ps()) == [
        "ginn-ginnrealm-g1",
        "ginn_provision_ginnrealm_g3",
        "ginn_provision_otherrealm_g1",
    ]


@pytest.mark.fr("FR-122")
def test_remove_derived_removes_by_id_and_keeps_the_recorded_and_the_used(
    paths: OwnedPaths,
) -> None:
    fake = FakePodman()
    fake.pull(PIN_REF)
    fake.images[DERIVED] = _image("1" * 64)
    fake.images[STAGE] = _image("2" * 64)
    fake.images[derived_image_name(REALM, 2)] = _image("3" * 64)
    fake.images[derived_image_name(RealmName("otherrealm"), 1)] = _image("4" * 64)
    fake.create(
        CreateSpec(name="ginn-user", image_digest_ref=PIN_REF, network="host", image_id="2" * 64)
    )
    removal = remove_derived(fake, REALM, keep=["3" * 64, None])
    assert removal.removed == ("1" * 64,)
    assert set(removal.kept) == {"2" * 64, "3" * 64}
    assert any("still uses" in note for note in removal.notes)
    assert any("recorded" in note for note in removal.notes)
    assert set(fake.images) == {
        PIN_REF,
        STAGE,
        derived_image_name(REALM, 2),
        derived_image_name(RealmName("otherrealm"), 1),
    }


@pytest.mark.fr("FR-122")
def test_remove_derived_sweeps_the_provisioning_containers_first(paths: OwnedPaths) -> None:
    fake = FakePodman()
    fake.pull(PIN_REF)
    fake.create(provision_create_spec(REALM, GENERATION, PIN_REF))
    fake.images[DERIVED] = _image("1" * 64)
    removal = remove_derived(fake, REALM, keep=())
    assert removal.swept == (PROVISION,)
    assert removal.removed == ("1" * 64,)
    assert fake.ps() == []


@pytest.mark.fr("FR-122")
def test_remove_derived_with_nothing_recorded_removes_every_derived_image_of_the_realm(
    paths: OwnedPaths,
) -> None:
    fake = FakePodman()
    fake.pull(PIN_REF)
    fake.images[DERIVED] = _image("1" * 64)
    fake.images[STAGE] = _image("2" * 64)
    removal = remove_derived(fake, REALM, keep=())
    assert sorted(removal.removed) == ["1" * 64, "2" * 64]
    assert set(fake.images) == {PIN_REF}


def _container_from(fake: FakePodman, name: str, image_id: str, shown_as: str) -> None:
    """A container that runs from `image_id`, which podman shows by the name `shown_as`."""
    fake.create(CreateSpec(name=name, image_digest_ref=PIN_REF, network="host", image_id=image_id))
    container_id = next(cid for cid, c in fake.containers.items() if c.name == name)
    fake.containers[container_id].image = shown_as


@pytest.mark.fr("FR-122")
def test_remove_derived_matches_containers_by_image_id_so_g1_is_not_blocked_by_g10_or_stage(
    paths: OwnedPaths,
) -> None:
    fake = FakePodman()
    fake.pull(PIN_REF)
    g1 = derived_image_name(REALM, 1)
    g10 = derived_image_name(REALM, 10)
    fake.images[g1] = _image("1" * 64)
    fake.images[g10] = _image("c" * 64)
    fake.images[STAGE] = _image("2" * 64)
    _container_from(fake, "ginn-on-g10", "c" * 64, shown_as=g10)
    _container_from(fake, "ginn-on-stage", "2" * 64, shown_as=STAGE)
    removal = remove_derived(fake, REALM, keep=())
    assert removal.removed == ("1" * 64,)
    assert set(removal.kept) == {"c" * 64, "2" * 64}
    assert set(fake.images) == {PIN_REF, g10, STAGE}


@pytest.mark.fr("FR-122")
def test_remove_derived_keeps_an_id_that_podman_will_not_remove_while_two_names_hold_it(
    paths: OwnedPaths,
) -> None:
    fake = FakePodman()
    fake.pull(PIN_REF)
    fake.images[DERIVED] = _image("1" * 64)
    fake.images[STAGE] = fake.images[DERIVED]
    removal = remove_derived(fake, REALM, keep=())
    assert removal.removed == ()
    assert removal.kept == ("1" * 64,)
    assert any("rmi failed with GINN-E-INTERNAL" in note for note in removal.notes)
    assert DERIVED in fake.images and STAGE in fake.images


@pytest.mark.fr("FR-122")
def test_the_fake_untags_a_name_and_removes_the_image_with_its_last_name(paths: OwnedPaths) -> None:
    fake = FakePodman()
    image = _image("1" * 64)
    fake.images[DERIVED] = image
    fake.images[STAGE] = image
    fake.rmi(STAGE)
    assert STAGE not in fake.images
    assert fake.images[DERIVED] == image
    fake.rmi(DERIVED)
    assert DERIVED not in fake.images


@pytest.mark.fr("DR-018")
def test_a_failed_undo_is_noted_on_the_error_that_rolled_back(paths: OwnedPaths) -> None:
    fake, env, plan, journal = _world(paths, pinned=False)
    fake.fail_ops = {"commit": "GINN-E-INTERNAL", "rmi": "GINN-E-INTERNAL"}
    with pytest.raises(GinnError) as caught:
        provision_init(env, plan, journal)
    assert caught.value.code == "GINN-E-IMAGE-DERIVED"
    notes = caught.value.__notes__
    assert any(PIN_REF in note and "rmi failed with GINN-E-INTERNAL" in note for note in notes), (
        notes
    )
    assert PIN_REF in fake.images


@pytest.mark.fr("FR-122")
def test_a_failed_removal_after_the_commit_is_noted_and_the_image_is_kept(
    paths: OwnedPaths,
) -> None:
    fake, env, plan, journal = _world(paths)
    fake.fail_ops["rm"] = "GINN-E-INTERNAL"
    result = provision_init(env, plan, journal)
    assert result.image_id == fake.images[DERIVED].id
    assert DERIVED in fake.images
    assert any("reset-removed failed with GINN-E-INTERNAL" in note for note in result.notes)
    assert STEP_RESET_REMOVED not in journal.steps
    assert STEP_PROVISION_REMOVED in journal.steps
    assert RESET in [info.name for info in fake.ps()]


@pytest.mark.fr("NFR-030")
@pytest.mark.parametrize("pinned", [True, False], ids=["base-present", "base-pulled-here"])
def test_a_kill_at_every_provisioning_point_is_cleaned_by_the_sweep(
    paths: OwnedPaths, pinned: bool
) -> None:
    """A kill runs no rollback. The sweep removes the containers and derived images it leaves.

    With `pinned=False` the stage pulls the base itself. A kill leaves that base in place: the
    sweep owns containers and derived images, and recovery owns a base (ADR-015 Decision 13).
    """
    _, dry, outcome = _killed_run(paths, pinned=pinned)
    assert isinstance(outcome, ProvisionResult)
    points = dry.points
    assert len(points) >= 30, points
    problems: list[str] = []
    for index, label in enumerate(points):
        fake, _, outcome = _killed_run(paths, kill_at=index, pinned=pinned)
        if not isinstance(outcome, SimulatedKill):
            problems.append(f"{label}: the kill did not land ({outcome!r})")
            continue
        sweep_provisions(fake, REALM)
        remove_derived(fake, REALM, keep=())
        problems += [f"{label}: {item}" for item in _leftovers(fake)]
    assert not problems


@pytest.mark.fr("NFR-030")
@pytest.mark.parametrize("pinned", [True, False], ids=["base-present", "base-pulled-here"])
def test_an_interrupt_at_every_provisioning_point_leaves_nothing_behind_without_a_sweep(
    paths: OwnedPaths, pinned: bool
) -> None:
    """Every undo stays armed until the build returns, so an interrupt at any point is rolled back.

    That includes the commit and the removals. The derived image, the stage, the reset and the
    provisioning containers, and a base the stage pulled all go, with no sweep (ADR-015 Decision
    2h). A base that was already there stays.
    """
    _, dry, _ = _killed_run(paths, pinned=pinned)
    problems: list[str] = []
    for index, label in enumerate(dry.points):
        fake, _, outcome = _killed_run(paths, kill_at=index, exc=KeyboardInterrupt, pinned=pinned)
        if not isinstance(outcome, KeyboardInterrupt):
            problems.append(f"{label}: the interrupt did not land ({outcome!r})")
            continue
        base_kept = (PIN_REF in fake.images) if pinned else (PIN_REF not in fake.images)
        if not base_kept:
            problems.append(f"{label}: the base is not as the rollback should leave it")
        problems += [f"{label}: {item}" for item in _leftovers(fake)]
    assert not problems


@pytest.mark.fr("NFR-030")
@pytest.mark.parametrize(
    "failing_step",
    [STEP_IMAGE_COMMITTED, STEP_RESET_REMOVED, STEP_STAGE_REMOVED, STEP_PROVISION_REMOVED],
)
@pytest.mark.parametrize("pinned", [True, False], ids=["base-present", "base-pulled-here"])
def test_a_journal_failure_after_the_commit_rolls_back_the_derived_image_too(
    paths: OwnedPaths, failing_step: str, pinned: bool
) -> None:
    """The caller never gets the ID of an image that a failed journal write left, so it is removed.

    A removal that fails is only a note (the test before this one). A journal write that fails
    raises, and the rollback removes the derived image with everything else.
    """
    fake, env, plan, _ = _world(paths, pinned=pinned)
    journal = _FailingJournal(failing_step)
    with pytest.raises(GinnError) as caught:
        provision_init(env, plan, journal)
    assert caught.value.code == "GINN-E-INTERNAL"
    assert failing_step not in journal.steps
    assert _leftovers(fake) == []
    assert (PIN_REF in fake.images) == pinned


@pytest.mark.fr("NFR-030")
def test_a_kill_after_the_final_commit_leaves_the_derived_image_for_the_sweep(
    paths: OwnedPaths,
) -> None:
    """Killed once the derived image is committed, the sweep removes it and the stage by ID."""
    _, dry, _ = _killed_run(paths)
    fake, _, outcome = _killed_run(paths, kill_at=dry.points.index("step image-committed"))
    assert isinstance(outcome, SimulatedKill)
    assert DERIVED in fake.images
    sweep_provisions(fake, REALM)  # the reset container still uses the stage image
    stage_id = fake.image_id(STAGE)
    derived_id = fake.image_id(DERIVED)
    removal = remove_derived(fake, REALM, keep=())
    assert sorted(removal.removed) == sorted([derived_id, stage_id])
    assert DERIVED not in fake.images
    assert STAGE not in fake.images


def _image(image_id: str) -> ImageInfo:
    return ImageInfo(
        id=image_id,
        digest="",
        repo_digests=(),
        size=1,
        created="1970-01-01T00:00:00Z",
        labels={},
    )
