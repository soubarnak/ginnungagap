# SPDX-License-Identifier: Apache-2.0
"""The Kali everything estimate on FakePodman: sizes read, no lasting ginn state, always undone.

Covers ops/estimate.py (FR-014 AC-1, FR-121). The throwaway container and the base image are the
only podman artifacts, and every test checks that both are gone on each exit: a success, a GinnError
from a step, an interrupt, and a failed undo. No test runs podman, sudo or the helper binary.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ginn.distros import Verb
from ginn.distros import load as load_distro
from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.errors import GinnError
from ginn.ops import _create_plan
from ginn.ops import estimate as estimate_ops
from ginn.ops._mutating import OpEnv
from ginn.ops.create import CreateHooks, CreateRequest, SizeEstimate
from ginn.ops.delete import DeleteHooks, delete_realm
from ginn.ops.disk import estimate_required
from ginn.ops.estimate import LABEL_ESTIMATE, EverythingEstimate, estimate_everything
from ginn.ops.provision import storage_peak_for
from ginn.ops.recovery import OUTCOME_BLOCKED, OUTCOME_BUSY, OUTCOME_SWEPT, sweep_estimates
from ginn.podman.adapter import CreateSpec, ExecTarget, RetryHook
from ginn.podman.models import ExecResult, ImageInfo
from ginn.runtime.realm_security import SecurityHooks
from ginn.runtime.spec import LABEL_REALM
from ginn.store.audit import AuditLog
from ginn.store.journal import Journal
from ginn.store.locks import image_lock
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from ginn.trust import manifest as trust
from tests.fakes.clock import FakeClock
from tests.fakes.fake_podman import FakePodman

DIRECT_TARGET = ExecTarget(realm="test", services=False, mutating=False)

UID = 1000
GID = 1000
USER = "alice"
AMPLE = 10**15
T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)

KALI_LISTING = (
    "kali-linux-everything\n"
    "  Depends: alpha\n"
    "  Depends: <virtual-thing>\n"
    "alpha\n"
    "  Depends: beta\n"
    "<virtual-thing>\n"
    "beta\n"
)
KALI_SHOW = (
    "Package: kali-linux-everything\nVersion: 2026.3\nSize: 1000\nInstalled-Size: 1\n\n"
    "Package: alpha\nVersion: 1.0\nSize: 2000\nInstalled-Size: 2\n\n"
    "Package: beta\nVersion: 1.0\nSize: 3000\nInstalled-Size: 3\n"
)
DOWNLOAD = 1000 + 2000 + 3000
INSTALLED = (1 + 2 + 3) * 1024
TOTAL = DOWNLOAD + INSTALLED
SHORT = estimate_required(TOTAL) - 1


class _AbsentProbe:
    """A host with no NVIDIA driver: no proc file, no device, no library and no musl loader."""

    def read_text(self, path: str) -> str | None:
        return None

    def glob(self, pattern: str) -> list[str]:
        return []

    def realpath(self, path: str) -> str:
        return path

    def char_device(self, path: str) -> tuple[int, int] | None:
        return None

    def which(self, name: str) -> str | None:
        return None


class _Vfs:
    """A statvfs answer with `available` bytes for an unprivileged user (ops/disk.py)."""

    def __init__(self, available: int) -> None:
        self.f_bavail = available
        self.f_frsize = 1


class KaliFake(FakePodman):
    """The fake, plus the seams an estimate needs: the specs it made, and raised answers per argv.

    `raise_on` raises the queued error when the argv runs. Each entry is taken once, so a retry
    after a network error sees the next answer.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.created_specs: list[CreateSpec] = []
        self.raise_on: dict[tuple[str, ...], list[BaseException]] = {}
        self.interrupt_after_pull = False
        self.alias_after_pull: str | None = None

    def create(self, spec: CreateSpec) -> str:
        self.created_specs.append(spec)
        return super().create(spec)

    def pull(self, ref_with_digest: str, *, retry_hook: RetryHook | None = None) -> ImageInfo:
        image = super().pull(ref_with_digest, retry_hook=retry_hook)
        if self.alias_after_pull is not None:
            # A realm's container that shows the pulled image under another name (ops/images.py).
            self.create(
                CreateSpec(
                    name="ginn-alias",
                    image_digest_ref=self.alias_after_pull,
                    network="pasta",
                    labels={LABEL_REALM: "other"},
                )
            )
        if self.interrupt_after_pull:
            raise KeyboardInterrupt  # the image is stored, and the call never returns
        return image

    def exec(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        queued = self.raise_on.get(tuple(argv))
        if queued:
            self._record("exec", container_id, tuple(argv))
            raise queued.pop(0)
        return super().exec(container_id, argv, **kwargs)


def _kali_answers(fake: KaliFake) -> None:
    kali = load_distro("kali")
    fake.exec_results[tuple(kali.argv(Verb.RESOLVE, ["kali-linux-everything"]))] = ExecResult(
        0, KALI_LISTING, ""
    )
    fake.exec_results[
        tuple(kali.argv(Verb.ESTIMATE, ["kali-linux-everything", "alpha", "beta"]))
    ] = ExecResult(0, KALI_SHOW, "")


def _statvfs_after(calls_before_short: int, short: int) -> Callable[[str], _Vfs]:
    """Ample space for the first checks, then `short` bytes: the set check fails after the base."""
    calls: list[str] = []

    def statvfs(path: str) -> _Vfs:
        calls.append(path)
        return _Vfs(AMPLE if len(calls) <= calls_before_short else short)

    return statvfs


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
def host(tmp_path: Path) -> Path:
    root = tmp_path / "host"
    for scope in ("etc", "var/db/xbps", "var/service", "usr/local", "opt"):
        (root / scope).mkdir(parents=True)
    return root


@pytest.fixture
def env(paths: OwnedPaths, host: Path) -> OpEnv:
    env = OpEnv.for_paths(paths, USER, root_dir=host, clock=FakeClock())
    # The host baseline that a ginn host has after its first operation (FR-006). The estimate's own
    # gate writes nothing once it exists, so the tests start from it.
    env.integrity.before_op(env.audit, env.user)
    return env


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home" / USER
    path.mkdir(parents=True)
    return path


@pytest.fixture
def fake() -> KaliFake:
    return KaliFake()


@pytest.fixture
def sleeps() -> list[float]:
    return []


def _request(home: Path, **kwargs: Any) -> CreateRequest:
    runtime = home.parent / "xdg-runtime"
    runtime.mkdir(exist_ok=True)
    return CreateRequest(
        name=RealmName("kali"),
        distro="kali",
        uid=UID,
        gid=GID,
        host_home=home,
        host_env={"XDG_RUNTIME_DIR": str(runtime)},
        everything=True,
        **kwargs,
    )


def _hooks(sleeps: list[float], **overrides: Any) -> CreateHooks:
    values: dict[str, Any] = {
        "statvfs": lambda _path: _Vfs(AMPLE),
        "sleep": sleeps.append,
        "security": SecurityHooks(nvidia_probe=_AbsentProbe()),
    }
    values.update(overrides)
    return CreateHooks(**values)


def _base_peak() -> int:
    """The raw services-off storage peak of the kali base, pulled (T7.35): the estimate's figure."""
    entry = trust.load().entry("kali")
    return storage_peak_for(
        load_distro("kali"),
        services=False,
        download_bytes=entry.compressed_size_bytes,
        base_pulled=True,
    )


def _image_ref() -> str:
    return trust.pull_reference("kali")


def _tree(root: Path) -> list[str]:
    """Every path under `root`, relative, sorted. Used to show that nothing new was written."""
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _assert_nothing_kept(env: OpEnv, fake: KaliFake, home: Path) -> None:
    """What every estimate leaves: no record, Realm Home, journal, audit entry or container.

    The only audit entry is the host baseline of the fixture (`env`), which the gate wrote first.
    """
    assert fake.containers == {}
    assert RealmStore(env.paths, env.clock).exists(RealmName("kali")) is False
    assert not (env.paths.homes / "kali").exists()
    assert Journal(env.paths).pending() == []
    assert [entry.op for entry in AuditLog(env.paths).read()] == ["integrity"]
    assert not any(c.labels.get(LABEL_ESTIMATE) for c in fake.containers.values())


def _kali_record(env: OpEnv) -> RealmRecord:
    """A stopped Kali realm with no container, for the delete that the lock tests run."""
    settings = RealmSettings(
        name=RealmName("kali"),
        distro=DistroId("kali"),
        tier=Tier.BASIC,
        services=False,
        autostart=False,
        everything=True,
    )
    return RealmRecord(
        settings=settings,
        state=RealmState.STOPPED,
        previous_state=None,
        image_ref=_image_ref(),
        image_digest=_image_ref().partition("@")[2],
        container_id=None,
        generation=1,
        created_at=T0,
        updated_at=T0,
        restart_required=False,
        share_skipped=[],
    )


def _killed_estimate(fake: KaliFake, ref: str) -> None:
    """The leftovers of an estimate that was killed: its labelled container, and the image."""
    fake.create(
        CreateSpec(
            name="ginn-estimate-killed",
            image_digest_ref=ref,
            network="pasta",
            labels={LABEL_ESTIMATE: "kali"},
        )
    )


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-121")
def test_the_estimate_reads_the_sizes_and_keeps_nothing(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    before = (_tree(env.paths.data), _tree(env.paths.state))

    result = estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert result == EverythingEstimate(
        distro="kali",
        everything=("kali-linux-everything",),
        size=SizeEstimate(packages=3, download_bytes=DOWNLOAD, installed_bytes=INSTALLED),
        base_peak_bytes=_base_peak(),
    )
    # T7.35: the figure of the whole create is the base's storage peak and the set, times 1.10.
    assert result.required_bytes == estimate_required(_base_peak() + TOTAL)
    assert result.base_required_bytes == estimate_required(_base_peak())
    assert result.everything_required_bytes == estimate_required(TOTAL)
    assert (_tree(env.paths.data), _tree(env.paths.state)) == before
    _assert_nothing_kept(env, fake, home)
    assert fake.images == {}  # the base image was pulled here, so it is removed here


@pytest.mark.fr("FR-014")
def test_the_throwaway_container_is_a_keep_alive_with_its_own_label_and_no_realm_label(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    (spec,) = fake.created_specs
    labels = spec.labels
    assert labels == {LABEL_ESTIMATE: "kali"}
    assert LABEL_REALM not in labels
    argv = spec.to_argv()
    assert argv[argv.index("--network") + 1] == "pasta"
    assert "--privileged" not in argv
    assert "--passwd-entry" not in argv
    assert "--user" not in argv


@pytest.mark.fr("FR-014")
def test_an_image_that_was_already_present_is_kept(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    ref = _image_ref()
    fake.pull(ref)
    fake.calls.clear()

    estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert ref in fake.images
    assert "rmi" not in fake.ops()
    _assert_nothing_kept(env, fake, home)


@pytest.mark.fr("FR-014")
def test_a_container_that_a_killed_estimate_left_is_removed_before_the_new_one(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    stale = fake.create(
        CreateSpec(
            name="ginn-estimate-stale",
            image_digest_ref=_image_ref(),
            network="pasta",
            labels={LABEL_ESTIMATE: "kali"},
        )
    )
    fake.calls.clear()

    estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    ops = fake.ops()
    assert ops.index("rm") < ops.index("create")
    assert stale not in fake.containers
    _assert_nothing_kept(env, fake, home)


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-121")
def test_a_failed_refresh_removes_the_container_and_the_pulled_image(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    kali = load_distro("kali")
    fake.exec_results[tuple(kali.argv(Verb.REFRESH))] = ExecResult(1, "", "E: lists broke")

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-PKG-MANAGER"
    _assert_nothing_kept(env, fake, home)
    assert fake.images == {}


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-120")
def test_a_network_failure_in_the_refresh_is_retried_with_the_fr_120_delays(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    kali = load_distro("kali")
    refresh = tuple(kali.argv(Verb.REFRESH))
    fake.exec_sequences[refresh] = [
        ExecResult(100, "", "Temporary failure resolving 'http.kali.org'"),
        ExecResult(0, "", ""),
    ]

    result = estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert result.size.packages == 3
    assert sleeps == [5]
    _assert_nothing_kept(env, fake, home)


@pytest.mark.fr("FR-014")
def test_an_interrupt_during_the_listing_is_a_cancel_and_is_undone(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    kali = load_distro("kali")
    fake.raise_on[tuple(kali.argv(Verb.RESOLVE, ["kali-linux-everything"]))] = [KeyboardInterrupt()]

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-INTERRUPTED"
    assert any("cancelled" in note for note in caught.value.__notes__)
    _assert_nothing_kept(env, fake, home)
    assert fake.images == {}


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-121")
def test_an_interrupt_during_the_pull_removes_the_partly_known_image(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    fake.interrupt_after_pull = True

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-INTERRUPTED"
    _assert_nothing_kept(env, fake, home)
    assert fake.images == {}


@pytest.mark.fr("FR-011")
@pytest.mark.fr("FR-014")
def test_a_digest_that_does_not_match_the_pin_is_refused_and_the_image_is_removed(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    fake.pull_digest_override = "sha256:" + "0" * 64

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-IMAGE-UNVERIFIED"
    assert fake.images == {}
    _assert_nothing_kept(env, fake, home)


@pytest.mark.fr("FR-121")
def test_a_short_base_disk_is_refused_before_the_image_is_pulled(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)

    with pytest.raises(GinnError) as caught:
        estimate_everything(
            env, fake, _request(home), hooks=_hooks(sleeps, statvfs=lambda _p: _Vfs(0))
        )

    assert caught.value.code == "GINN-E-DISK-SPACE"
    assert caught.value.details["needed"] == f"{estimate_required(_base_peak())} bytes"
    assert "pull" not in fake.ops()
    assert fake.calls == []


@pytest.mark.fr("FR-121")
@pytest.mark.fr("FR-107")
def test_the_space_checked_step_reports_the_base_figure_it_compared(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    """T7.35: the estimate's `space-checked` carries the base's figure and the free space it was
    held to, before any pull, so the progress line of `create --everything --estimate` shows it."""
    _kali_answers(fake)
    details: dict[str, dict[str, object]] = {}
    hooks = _hooks(
        sleeps,
        statvfs=lambda _path: _Vfs(AMPLE),
        on_step_detail=lambda step, detail: details.setdefault(step, dict(detail)),
    )

    estimate_everything(env, fake, _request(home), hooks=hooks)

    assert details["space-checked"] == {
        "required_bytes": estimate_required(_base_peak()),
        "free_bytes": AMPLE,
        "reason": None,
    }


@pytest.mark.fr("FR-121")
@pytest.mark.fr("FR-014")
def test_a_short_disk_for_the_set_is_refused_and_undone(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    # Two checks pass for the base image (podman root and Realm Home), then the set's two fail.
    hooks = _hooks(sleeps, statvfs=_statvfs_after(2, SHORT))

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, _request(home), hooks=hooks)

    assert caught.value.code == "GINN-E-DISK-SPACE"
    assert caught.value.details["needed"] == f"{estimate_required(TOTAL)} bytes"
    _assert_nothing_kept(env, fake, home)
    assert fake.images == {}


@pytest.mark.fr("FR-014")
def test_a_failed_undo_is_not_reported_as_a_clean_estimate(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    fake.fail_ops["rm"] = "GINN-E-INTERNAL"

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-INTERNAL"
    assert any(note.startswith("rollback:") for note in caught.value.__notes__)
    assert len(fake.containers) == 1  # the container that could not be removed is still there


@pytest.mark.fr("FR-014")
def test_the_estimate_is_refused_while_a_create_of_the_same_distribution_holds_the_image_lock(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)

    with image_lock(env.paths, "kali", "create of kali image"):
        with pytest.raises(GinnError) as caught:
            estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-REALM-BUSY"
    assert fake.calls == []


@pytest.mark.fr("FR-014")
def test_a_distribution_without_an_everything_set_is_refused_before_any_podman_call(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    request = CreateRequest(
        name=RealmName("debian"),
        distro="debian",
        uid=UID,
        gid=GID,
        host_home=home,
        everything=True,
    )

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, request, hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-OPTION-INVALID"
    assert fake.calls == []


@pytest.mark.fr("FR-014")
def test_a_request_without_everything_is_a_usage_error_before_any_podman_call(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    request = CreateRequest(name=RealmName("kali"), distro="kali", uid=UID, gid=GID, host_home=home)

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, request, hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-USAGE"
    assert fake.calls == []


@pytest.mark.fr("FR-014")
def test_the_closure_is_read_in_chunks_and_the_totals_add_up(
    env: OpEnv,
    fake: KaliFake,
    home: Path,
    sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(estimate_ops, "EVERYTHING_CHUNK", 2)
    kali = load_distro("kali")
    fake.exec_results[tuple(kali.argv(Verb.RESOLVE, ["kali-linux-everything"]))] = ExecResult(
        0, KALI_LISTING, ""
    )
    for chunk in (["kali-linux-everything", "alpha"], ["beta"]):
        fake.exec_results[tuple(kali.argv(Verb.ESTIMATE, chunk))] = ExecResult(0, KALI_SHOW, "")

    result = estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert result.size == _create_plan.size_totals(
        KALI_SHOW, ["kali-linux-everything", "alpha", "beta"], realm="kali", step="estimate"
    )
    assert result.size.download_bytes == DOWNLOAD
    shows = [
        call.args[1]
        for call in fake.calls
        if call.op == "exec" and call.args[1][:2] == ("apt-cache", "show")
    ]
    assert shows == [
        tuple(kali.argv(Verb.ESTIMATE, chunk))
        for chunk in (["kali-linux-everything", "alpha"], ["beta"])
    ]


@pytest.mark.fr("FR-107")
@pytest.mark.fr("FR-014")
def test_each_step_is_reported_to_the_hook_in_order(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    seen: list[str] = []

    estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps, on_step=seen.append))

    assert seen == ["space-checked", "pulled", "created", "refreshed", "estimated"]
    _assert_nothing_kept(env, fake, home)


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-121")
def test_a_podman_failure_mid_estimate_is_undone_and_only_the_steps_reached_are_reported(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    fake.fail_ops["start"] = "GINN-E-INTERNAL"
    seen: list[str] = []

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps, on_step=seen.append))

    assert caught.value.code == "GINN-E-INTERNAL"
    assert seen == ["space-checked", "pulled", "created"]
    _assert_nothing_kept(env, fake, home)
    assert fake.images == {}


@pytest.mark.fr("FR-014")
def test_a_refusal_while_the_listing_is_read_names_the_estimate_and_not_the_realm(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    kali = load_distro("kali")
    listing = tuple(kali.argv(Verb.RESOLVE, ["kali-linux-everything"]))
    fake.exec_results[listing] = ExecResult(0, "BAD!PKG\n", "")

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert caught.value.details["realm"] == "estimate:kali"
    _assert_nothing_kept(env, fake, home)


@pytest.mark.fr("FR-006")
@pytest.mark.fr("FR-014")
def test_a_changed_host_refuses_the_estimate_before_any_podman_call(
    env: OpEnv, fake: KaliFake, home: Path, host: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")

    with pytest.raises(GinnError) as caught:
        estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-HOST-UNACKED"
    assert "/etc/hosts" in caught.value.details["paths"]
    assert fake.calls == []
    assert "/etc/hosts" in (env.integrity.pending() or [])


@pytest.mark.fr("FR-123")
@pytest.mark.fr("FR-014")
@pytest.mark.parametrize("holder", ["delete", "rebuild"])
def test_a_delete_or_a_rebuild_holding_the_kali_image_lock_refuses_the_estimate(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float], holder: str
) -> None:
    _kali_answers(fake)
    op = f"{holder} of kali"

    with image_lock(env.paths, "kali", op):
        with pytest.raises(GinnError) as caught:
            estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    assert caught.value.code == "GINN-E-REALM-BUSY"
    assert caught.value.details["op"] == op
    assert fake.calls == []


@pytest.mark.fr("FR-123")
@pytest.mark.fr("FR-014")
def test_a_real_delete_of_kali_refuses_an_estimate_while_the_delete_holds_the_lock(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    RealmStore(env.paths, env.clock).save(_kali_record(env))
    refused: list[str] = []

    def estimate_during_delete(step: str) -> None:
        if step == "stopped":  # the delete holds the kali image lock here
            with pytest.raises(GinnError) as caught:
                estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))
            refused.append(caught.value.code)

    delete_realm(env, fake, RealmName("kali"), hooks=DeleteHooks(on_step=estimate_during_delete))

    assert refused == ["GINN-E-REALM-BUSY"]


@pytest.mark.fr("FR-123")
@pytest.mark.fr("FR-014")
def test_an_estimate_in_progress_refuses_a_delete_of_kali_and_keeps_the_record(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    RealmStore(env.paths, env.clock).save(_kali_record(env))
    refused: list[str] = []

    def delete_during_estimate(step: str) -> None:
        if step == "pulled":  # the estimate holds the kali image lock here
            with pytest.raises(GinnError) as caught:
                delete_realm(env, fake, RealmName("kali"))
            refused.append(caught.value.code)

    estimate_everything(
        env, fake, _request(home), hooks=_hooks(sleeps, on_step=delete_during_estimate)
    )

    assert refused == ["GINN-E-REALM-BUSY"]
    assert RealmStore(env.paths, env.clock).exists(RealmName("kali")) is True


@pytest.mark.fr("FR-014")
@pytest.mark.fr("FR-011")
def test_an_alias_of_the_pulled_image_keeps_it_in_the_undo(
    env: OpEnv, fake: KaliFake, home: Path, sleeps: list[float]
) -> None:
    _kali_answers(fake)
    ref = _image_ref()
    fake.alias_after_pull = "example.org/kali-rolling@" + ref.partition("@")[2]

    estimate_everything(env, fake, _request(home), hooks=_hooks(sleeps))

    # The realm's container shows the image by its digest under another name, so it is still used.
    assert ref in fake.images
    assert "rmi" not in fake.ops()
    assert [c.labels.get(LABEL_REALM) for c in fake.containers.values()] == ["other"]


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-014")
def test_the_sweep_removes_a_killed_estimates_container_and_the_image_it_pulled(
    env: OpEnv, fake: KaliFake
) -> None:
    ref = _image_ref()
    fake.pull(ref)
    _killed_estimate(fake, ref)

    outcomes = sweep_estimates(env, fake)

    assert [(o.realm, o.outcome) for o in outcomes] == [("estimate:kali", OUTCOME_SWEPT)]
    assert fake.containers == {}
    assert fake.images == {}


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-014")
def test_the_sweep_keeps_the_image_while_a_container_uses_it_under_another_name(
    env: OpEnv, fake: KaliFake
) -> None:
    ref = _image_ref()
    fake.pull(ref)
    _killed_estimate(fake, ref)
    alias = "example.org/kali-rolling@" + ref.partition("@")[2]
    fake.create(
        CreateSpec(
            name="ginn-realm", image_digest_ref=alias, network="pasta", labels={LABEL_REALM: "kali"}
        )
    )

    (outcome,) = sweep_estimates(env, fake)

    assert outcome.outcome == OUTCOME_SWEPT
    assert "base image stays" in outcome.reason
    assert ref in fake.images
    assert [c.labels.get(LABEL_REALM) for c in fake.containers.values()] == ["kali"]


@pytest.mark.fr("FR-123")
@pytest.mark.fr("FR-122")
def test_the_sweep_leaves_a_distribution_whose_image_lock_is_held_and_makes_no_change(
    env: OpEnv, fake: KaliFake
) -> None:
    ref = _image_ref()
    fake.pull(ref)
    _killed_estimate(fake, ref)
    fake.calls.clear()

    with image_lock(env.paths, "kali", "create of kali"):
        (outcome,) = sweep_estimates(env, fake)

    assert outcome.outcome == OUTCOME_BUSY
    assert fake.ops() == ["ps"]  # the listing only: nothing was stopped, removed or unpulled
    assert len(fake.containers) == 1
    assert ref in fake.images


@pytest.mark.fr("FR-122")
def test_a_podman_refusal_in_the_sweep_is_reported_as_blocked_and_not_raised(
    env: OpEnv, fake: KaliFake
) -> None:
    _killed_estimate(fake, _image_ref())
    fake.fail_ops["rm"] = "GINN-E-INTERNAL"

    (outcome,) = sweep_estimates(env, fake)

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "GINN-E-INTERNAL" in outcome.reason
    assert len(fake.containers) == 1  # left for the next command


@pytest.mark.fr("FR-122")
def test_a_refused_rmi_in_the_sweep_keeps_the_image_and_reports_it_blocked(
    env: OpEnv, fake: KaliFake
) -> None:
    ref = _image_ref()
    fake.pull(ref)
    _killed_estimate(fake, ref)
    fake.fail_ops["rmi"] = "GINN-E-INTERNAL"

    (outcome,) = sweep_estimates(env, fake)

    assert outcome.outcome == OUTCOME_BLOCKED
    assert "base image was not" in outcome.reason
    assert fake.containers == {}  # the containers went before the image step
    assert ref in fake.images


@pytest.mark.fr("FR-122")
def test_a_sweep_with_nothing_left_makes_one_listing_and_no_change(
    env: OpEnv, fake: KaliFake
) -> None:
    assert sweep_estimates(env, fake) == []
    assert fake.ops() == ["ps"]
