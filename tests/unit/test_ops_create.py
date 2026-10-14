# SPDX-License-Identifier: Apache-2.0
"""Realm create: order, final record, refusals, retries, digest, rollback at every step (T4.4).

Covers ops/create.py and ops/_rollback.py against FakePodman. The fake runs no podman. Its helper
rows come from the host, the way ginn-rebind reports them, so the attach path runs for real.
No test runs podman, sudo or the helper binary (AGENTS.md).

Failure outcomes are checked the way the choke point records them: a GINN-E error rolls back and
finishes the journal, a KeyboardInterrupt becomes GINN-E-INTERRUPTED after the rollback, and any
other exception keeps the journal entry and rolls nothing back (ops/_mutating.py, FR-122).

Test isolation. Two autouse or pinned settings keep the host out. `attach_at_start` reads source
identities with os.stat, not through a hook, so `host_identity_is_read_only_under_tmp` answers
only for paths under tmp_path. Every request pins XDG_RUNTIME_DIR to an empty tmp directory,
because the test host has a live session under /run/user/1000 (a PipeWire socket), and a real
socket would bind an audio grant and run a mkdir that the test does not expect.
"""

import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ginn.distros import Verb
from ginn.distros import load as load_distro
from ginn.domain import (
    InstallMethod,
    InstallSource,
    ManifestEntry,
    RealmName,
    RealmState,
    Tier,
)
from ginn.domain.permissions import BASIC_TOGGLES, Permissions, Toggle
from ginn.errors import GinnError, make
from ginn.ops import _create_plan
from ginn.ops._mutating import OpEnv
from ginn.ops.create import (
    CreateHooks,
    CreateRequest,
    CreateResult,
    create_realm,
    required_bytes,
)
from ginn.podman.adapter import CreateSpec
from ginn.podman.models import ContainerState, ExecResult, ImageInfo
from ginn.runtime import attach
from ginn.runtime.realm_security import SecurityHooks
from ginn.security.nvidia import NvidiaPlan, Status
from ginn.store.locks import image_lock
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from ginn.trust import manifest as trust
from tests.fakes.clock import FakeClock
from tests.fakes.fake_podman import FakePodman

UID = 1000
GID = 1000
USER = "alice"
AMPLE = 10**15
BASE_MOUNTINFO = (
    "36 35 98:0 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
    "37 36 98:1 / /opt rw - ext4 /dev/root rw\n"
)
SCOPE = ("etc", "var/db/xbps", "var/service", "usr/local", "opt")
STEPS = (
    "checked",
    "space-checked",
    "recorded",
    "homed",
    "pulled",
    "created",
    "started",
    "attached",
    "user",
    "integrated",
    "packages",
    "stopped",
)
SERVICES_STEPS = ("checked", "space-checked", "recorded", "homed", "pulled", "created", "stopped")
"""A services-on create makes the container and stops there: nothing runs in it (ADR-003)."""
FORBIDDEN_TOKENS = frozenset(
    {
        "--privileged",
        "--nogpgcheck",
        "--no-verify",
        "--allow-unauthenticated",
        "--allow-untrusted",
        "--force-yes",
        "--ignore-signature",
        "--no-gpg-checks",
        "--skip-signature",
    }
)
FORBIDDEN_SUBSTRINGS = ("gpgcheck=0", "SigLevel", "--cap-add")


def _helper_rows(argv: Sequence[str], root: str) -> str:
    """The helper's stdout for `argv`: one row per group, read from the host as ginn-rebind does.

    Only paths under `root` exist for this test, so the real host (its /media, its sockets) never
    answers a row.
    """
    rows: list[str] = []
    tokens = list(argv[2:])
    while tokens:
        if tokens[0] == "--unmount":
            rows.append("missing")
            tokens = tokens[2:]
            continue
        while tokens[0].startswith("--"):
            tokens = tokens[1:]
        host = tokens[0]
        tokens = tokens[2:]
        if host.startswith(root) and os.path.exists(host):
            st = os.stat(host)
            rows.append(f"attached {st.st_dev} {st.st_ino}")
        else:
            rows.append("missing")
    return "".join(f"{index} {row}\n" for index, row in enumerate(rows))


class ScriptedPodman(FakePodman):
    """The fake plus the answers that the real podman and helper give (test seams only).

    `created_specs` keeps each CreateSpec, so a test can read the argv. `exec_errors` raises a
    queued error once for one exact argv. `pull_error` is raised by the next pull, which stands in
    for a kill during the download.
    """

    def __init__(self, *, root: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.root = root
        self.created_specs: list[CreateSpec] = []
        self.exec_errors: dict[tuple[str, ...], list[GinnError]] = {}
        self.pull_error: BaseException | None = None
        self.interrupt_after_create = False
        self.interrupt_after_pull = False

    def create(self, spec: CreateSpec) -> str:
        self.created_specs.append(spec)
        cid = super().create(spec)
        if self.interrupt_after_create:
            raise KeyboardInterrupt  # the container exists, and the call never returns its id
        return cid

    def pull(self, ref_with_digest: str, *, retry_hook: Any = None) -> ImageInfo:
        if self.pull_error is not None:
            self._record("pull", ref_with_digest)
            raise self.pull_error
        image = super().pull(ref_with_digest, retry_hook=retry_hook)
        if self.interrupt_after_pull:
            raise KeyboardInterrupt  # the image is stored, and the call never returns
        return image

    def exec(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        queued = self.exec_errors.get(tuple(argv))
        if queued:
            self.exec_users.append(kwargs.get("user"))
            self._record("exec", container_id, tuple(argv))
            raise queued.pop(0)
        return super().exec(container_id, argv, **kwargs)

    def unshare(self, argv: Sequence[str]) -> ExecResult:
        if self.unshare_results:
            return super().unshare(argv)
        self._record("unshare", tuple(argv))
        return ExecResult(0, _helper_rows(argv, self.root), "")


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    """Owned Paths under tmp_path, with every XDG variable pinned (as in test_ops_mutating)."""
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
    for scope in SCOPE:
        (root / scope).mkdir(parents=True)
    (root / "etc" / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    return root


@pytest.fixture
def env(paths: OwnedPaths, host: Path) -> OpEnv:
    return OpEnv.for_paths(paths, USER, root_dir=host, clock=FakeClock())


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """The host home of the user. Under tmp_path, so it is outside Owned Paths."""
    path = tmp_path / "home" / USER
    path.mkdir(parents=True)
    return path


@pytest.fixture
def fake(tmp_path: Path) -> ScriptedPodman:
    return ScriptedPodman(root=str(tmp_path))


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture(autouse=True)
def host_identity_is_read_only_under_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """attach_at_start reads source identities with os.stat, not through a hook. Only tmp exists."""
    real = attach.read_identity

    def identity(host: str, *, mountinfo: Path = attach.MOUNTINFO) -> attach.SourceIdentity | None:
        if not str(host).startswith(str(tmp_path)):
            return None
        return real(host, mountinfo=mountinfo)

    monkeypatch.setattr(attach, "read_identity", identity)


@pytest.fixture
def mountinfo(tmp_path: Path) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(BASE_MOUNTINFO, encoding="utf-8")
    return path


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


def hooks_for(
    tmp_path: Path, sleeps: list[float], mountinfo: Path, **overrides: Any
) -> CreateHooks:
    """Hooks for a test. Only paths under tmp_path exist, so no host state changes a plan.

    The NVIDIA probe answers as a host with no driver, so a test never reads the real driver.
    """

    def under_tmp(path: str) -> bool:
        return str(path).startswith(str(tmp_path))

    values: dict[str, Any] = {
        "disk_free": lambda path: AMPLE,
        "sleep": sleeps.append,
        "exists": under_tmp,
        "mountinfo": mountinfo,
        "security": SecurityHooks(nvidia_probe=_AbsentProbe()),
    }
    values.update(overrides)
    return CreateHooks(**values)


def request(
    home: Path, name: str = "debian", distro: str = "debian", **kwargs: Any
) -> CreateRequest:
    """A request whose host session is empty: its runtime directory is an empty tmp directory."""
    runtime = home.parent / "xdg-runtime"
    runtime.mkdir(exist_ok=True)
    host_env = {"XDG_RUNTIME_DIR": str(runtime), **kwargs.pop("host_env", {})}
    return CreateRequest(
        name=RealmName(name),
        distro=distro,
        uid=UID,
        gid=GID,
        host_home=home,
        host_env=host_env,
        **kwargs,
    )


def ops_of(fake: FakePodman) -> list[str]:
    return [call.op for call in fake.calls]


def execs(fake: FakePodman) -> list[list[str]]:
    return [list(call.args[1]) for call in fake.calls if call.op == "exec"]  # type: ignore[misc]


def create_entries(env: OpEnv, name: str = "debian") -> list[Any]:
    return [entry for entry in env.audit.read() if entry.op == "create" and entry.realm == name]


def assert_nothing_left(env: OpEnv, fake: FakePodman, name: str = "debian") -> None:
    """No container, Realm Home, record, journal entry or pulled image is left behind."""
    assert fake.containers == {}
    assert not (env.paths.homes / name).exists()
    assert not env.paths.realms.joinpath(f"{name}.json").exists()
    assert env.journal.pending() == []
    assert fake.images == {}


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-011")
@pytest.mark.fr("AS-013")
def test_a_clean_create_leaves_a_stopped_basic_realm_with_services_off(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    steps: list[str] = []
    hooks = hooks_for(tmp_path, sleeps, mountinfo, on_step=steps.append)
    result = create_realm(env, fake, request(home), hooks=hooks)
    record = result.record
    entry = trust.load().entry("debian")
    assert record.state is RealmState.STOPPED
    assert record.settings.tier is Tier.BASIC
    assert record.settings.services is False
    assert record.settings.autostart is False
    assert record.generation == 1
    assert record.image_ref == trust.pull_reference("debian")
    assert record.image_digest == entry.digest
    assert record.container_id in fake.containers
    assert [c.state for c in fake.containers.values()] == [ContainerState.EXITED]
    assert RealmStore(env.paths).load(RealmName("debian")) == record
    assert steps == list(STEPS)
    assert env.journal.pending() == []
    assert [(e.outcome, e.error_code) for e in create_entries(env)] == [("ok", None)]


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-080")
def test_a_services_on_create_makes_the_container_and_stops_with_no_start(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    steps: list[str] = []
    hooks = hooks_for(tmp_path, sleeps, mountinfo, on_step=steps.append)
    result = create_realm(env, fake, request(home, services=True), hooks=hooks)
    record = result.record
    assert record.state is RealmState.STOPPED
    assert record.settings.services is True
    assert record.container_id in fake.containers
    assert [c.state for c in fake.containers.values()] == [ContainerState.CREATED]
    assert record.grants == []
    assert record.manifest == []
    assert steps == list(SERVICES_STEPS)
    assert {"start", "exec", "stop", "unshare"}.isdisjoint(ops_of(fake))
    assert [w.code for w in result.warnings] == ["GINN-W-SERVICES-PENDING"]
    (spec,) = fake.created_specs
    argv = spec.to_argv()
    assert "--systemd=always" in argv
    assert "--init" not in argv
    assert argv[-1] == "/sbin/init"
    assert RealmStore(env.paths).load(RealmName("debian")) == record
    assert env.journal.pending() == []


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-080")
def test_a_services_on_create_with_packages_is_refused_before_any_write(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    with pytest.raises(GinnError) as caught:
        create_realm(
            env,
            fake,
            request(home, services=True, with_packages=("htop",)),
            hooks=hooks_for(tmp_path, sleeps, mountinfo),
        )
    assert caught.value.code == "GINN-E-NOT-SUPPORTED-YET"
    assert "--with packages on a services-on realm arrives in T7.4" in caught.value.message
    assert fake.calls == []
    assert not (env.paths.homes / "debian").exists()
    assert create_entries(env) == []


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-011")
@pytest.mark.fr("FR-013")
def test_pull_then_create_then_start_then_root_steps_then_stop_in_that_order(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    ops = ops_of(fake)
    assert ops.index("image_exists") < ops.index("pull") < ops.index("create")
    assert ops.index("create") < ops.index("start") < ops.index("exec") < ops.index("stop")
    assert fake.ops().count("pull") == 1
    assert fake.exec_users == ["0:0", "0:0", "0:0"]
    assert execs(fake) == [
        ["getent", "passwd", str(UID)],
        ["apt-get", "update"],
        ["apt-get", "--yes", "--no-install-recommends", "install", "ca-certificates", "locales"],
    ]


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-048")
def test_the_realm_user_is_the_passwd_entry_podman_writes_and_no_useradd_runs(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    argv = fake.created_specs[0].to_argv()
    assert argv[argv.index("--passwd-entry") + 1] == f"alice:*:1000:1000:alice:{home}:/bin/bash"
    assert "--passwd=false" not in argv
    assert not any("useradd" in token for call in execs(fake) for token in call)
    assert execs(fake)[0] == ["getent", "passwd", str(UID)]
    assert fake.exec_users[0] == "0:0"


@pytest.mark.fr("FR-013")
def test_the_locale_steps_follow_the_host_lang(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    host_env = {"LANG": "en_US.UTF-8"}
    result = create_realm(
        env, fake, request(home, host_env=host_env), hooks=hooks_for(tmp_path, sleeps, mountinfo)
    )
    assert execs(fake)[-1] == ["localedef", "-i", "en_US", "-f", "UTF-8", "en_US.UTF-8"]
    assert result.locale_note is None


@pytest.mark.fr("FR-013")
def test_integrate_false_runs_no_refresh_and_no_package_step(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The host gate's seam (T4.9, Gate 3): no refresh, no integration package, no locale step."""
    result = create_realm(
        env,
        fake,
        request(home),
        hooks=hooks_for(tmp_path, sleeps, mountinfo, integrate=False),
    )
    assert result.record.state == RealmState.STOPPED
    assert not any("apt-get" in argv or "localedef" in argv for argv in execs(fake))
    assert not any("install" in argv or "update" in argv for argv in execs(fake))


@pytest.mark.fr("FR-013")
def test_integrate_false_reports_no_locale_note_even_for_an_unusable_lang(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """With integrate=False the locale note is not set: no locale step was decided or run."""
    result = create_realm(
        env,
        fake,
        request(home, host_env={"LANG": "en.UTF-8"}),
        hooks=hooks_for(tmp_path, sleeps, mountinfo, integrate=False),
    )
    assert result.locale_note is None
    assert not any("localedef" in argv for argv in execs(fake))


@pytest.mark.fr("FR-013")
def test_an_unusable_host_lang_is_reported_and_the_create_goes_on(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    result = create_realm(
        env,
        fake,
        request(home, host_env={"LANG": "en.UTF-8"}),
        hooks=hooks_for(tmp_path, sleeps, mountinfo),
    )
    assert result.locale_note is not None
    assert "en.UTF-8" in result.locale_note
    assert not any("localedef" in argv for argv in execs(fake))
    assert result.record.state is RealmState.STOPPED


@pytest.mark.fr("FR-013")
@pytest.mark.parametrize(
    ("distro", "lang", "install"),
    [
        ("arch", None, ["pacman", "--sync", "--needed", "--noconfirm", "ca-certificates"]),
        (
            "debian",
            None,
            [
                "apt-get",
                "--yes",
                "--no-install-recommends",
                "install",
                "ca-certificates",
                "locales",
            ],
        ),
        # The Fedora langpack needs a LANG, so with none set the Integration Package Set is empty.
        ("fedora", None, None),
        (
            "fedora",
            "en_US.UTF-8",
            [
                "dnf",
                "--assumeyes",
                "--setopt=install_weak_deps=False",
                "install",
                "glibc-langpack-en",
            ],
        ),
    ],
)
def test_the_integration_set_is_the_only_addition_on_each_distribution(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    distro: str,
    lang: str | None,
    install: list[str] | None,
) -> None:
    host_env = {} if lang is None else {"LANG": lang}
    create_realm(
        env,
        fake,
        request(home, name=distro, distro=distro, host_env=host_env),
        hooks=hooks_for(tmp_path, sleeps, mountinfo),
    )
    refresh = load_distro(distro).argv(Verb.REFRESH)
    assert execs(fake)[1] == refresh
    if install is None:
        assert len(execs(fake)) == 2
    else:
        assert execs(fake)[2] == install


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-011")
@pytest.mark.fr("AS-027")
def test_with_packages_install_after_the_integration_set(
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
        request(home, with_packages=("htop",)),
        hooks=hooks_for(tmp_path, sleeps, mountinfo),
    )
    assert execs(fake)[-1] == ["apt-get", "--yes", "--no-install-recommends", "install", "htop"]
    assert create_entries(env)[0].params["with"] == ["htop"]


@pytest.mark.fr("FR-010")
@pytest.mark.fr("NFR-SEC-005")
def test_an_invalid_package_name_is_refused_before_any_podman_call(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    with pytest.raises(GinnError) as caught:
        create_realm(
            env,
            fake,
            request(home, with_packages=("gimp*",)),
            hooks=hooks_for(tmp_path, sleeps, mountinfo),
        )
    assert caught.value.code == "GINN-E-PKG-NAME-INVALID"
    assert fake.calls == []
    assert not (env.paths.homes / "debian").exists()
    assert create_entries(env) == []


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-014")
@pytest.mark.parametrize(
    ("name", "distro", "kwargs", "expected"),
    [
        ("kali", "kali", {}, NotImplementedError),
        ("debian", "debian", {"everything": True}, GinnError),
        ("nosuch", "nosuch", {}, GinnError),
    ],
)
def test_requests_the_code_does_not_support_are_refused_with_no_side_effect(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    name: str,
    distro: str,
    kwargs: dict[str, Any],
    expected: type[BaseException],
) -> None:
    with pytest.raises(expected):
        create_realm(
            env,
            fake,
            request(home, name=name, distro=distro, **kwargs),
            hooks=hooks_for(tmp_path, sleeps, mountinfo),
        )
    assert fake.calls == []
    assert create_entries(env, name) == []


@pytest.mark.fr("FR-010")
@pytest.mark.fr("NFR-SEC-005")
def test_a_user_name_that_a_realm_user_cannot_take_is_refused_before_any_podman_call(
    paths: OwnedPaths,
    host: Path,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    bad_env = OpEnv.for_paths(paths, "-rf /", root_dir=host, clock=FakeClock())
    with pytest.raises(GinnError) as caught:
        create_realm(bad_env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-REALM-USER"
    assert caught.value.details["realm"] == "debian"
    assert "'-rf /'" in caught.value.details["detail"]
    assert fake.calls == []


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-015")
def test_a_taken_name_is_refused_and_the_existing_realm_stays_byte_identical(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    hooks = hooks_for(tmp_path, sleeps, mountinfo)
    create_realm(env, fake, request(home), hooks=hooks)
    record_path = env.paths.realms / "debian.json"
    before = record_path.read_bytes()
    home_before = sorted(p.name for p in (env.paths.homes / "debian").iterdir())
    calls_before = len(fake.calls)
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks)
    assert caught.value.code == "GINN-E-NAME-TAKEN"
    assert caught.value.exit_code == 4
    assert record_path.read_bytes() == before
    assert sorted(p.name for p in (env.paths.homes / "debian").iterdir()) == home_before
    assert len(fake.calls) == calls_before
    assert env.journal.pending() == []


@pytest.mark.fr("FR-010")
def test_a_realm_home_without_a_record_also_takes_the_name(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    (env.paths.homes / "debian").mkdir(parents=True)
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-NAME-TAKEN"
    assert fake.calls == []


@pytest.mark.fr("FR-015")
def test_two_realms_of_one_distribution_have_separate_homes_and_containers(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    hooks = hooks_for(tmp_path, sleeps, mountinfo)
    first = create_realm(env, fake, request(home), hooks=hooks).record
    second = create_realm(env, fake, request(home, name="debian-dev"), hooks=hooks).record
    assert first.name != second.name
    assert (env.paths.homes / "debian").is_dir()
    assert (env.paths.homes / "debian-dev").is_dir()
    names = sorted(spec.name for spec in fake.created_specs)
    assert names == ["ginn-debian-dev-g1", "ginn-debian-g1"]


@pytest.mark.fr("FR-121")
@pytest.mark.fr("AS-023")
def test_free_space_below_the_estimate_refuses_before_any_download(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    needed = required_bytes(trust.load().entry("debian"))
    hooks = hooks_for(tmp_path, sleeps, mountinfo, disk_free=lambda path: needed - 1)
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks)
    assert caught.value.code == "GINN-E-DISK-SPACE"
    assert caught.value.exit_code == 73
    assert caught.value.details["needed"] == f"{needed} bytes"
    assert "pull" not in ops_of(fake)
    assert "create" not in ops_of(fake)
    assert_nothing_left(env, fake)
    assert [e.error_code for e in create_entries(env)] == ["GINN-E-DISK-SPACE"]


@pytest.mark.fr("FR-121")
def test_free_space_equal_to_the_estimate_times_1_10_is_enough(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    needed = required_bytes(trust.load().entry("debian"))
    hooks = hooks_for(tmp_path, sleeps, mountinfo, disk_free=lambda path: needed)
    result = create_realm(env, fake, request(home), hooks=hooks)
    assert result.record.state is RealmState.STOPPED


@pytest.mark.fr("FR-011")
@pytest.mark.fr("NFR-SEC-004")
def test_a_pull_is_by_the_pinned_digest_and_never_by_tag(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    pulls = [call.args[0] for call in fake.calls if call.op == "pull"]
    assert pulls == [trust.pull_reference("debian")]
    assert "@sha256:" in str(pulls[0])


@pytest.mark.fr("FR-011")
def test_a_digest_mismatch_removes_the_image_and_leaves_no_record(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    fake.pull_digest_override = "sha256:" + "0" * 64
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-IMAGE-UNVERIFIED"
    assert caught.value.exit_code == 65
    assert "rmi" in ops_of(fake)
    assert "create" not in ops_of(fake)
    assert_nothing_left(env, fake)
    assert [e.error_code for e in create_entries(env)] == ["GINN-E-IMAGE-UNVERIFIED"]


@pytest.mark.fr("FR-120")
@pytest.mark.fr("AS-023")
def test_network_failure_retries_three_times_with_5_15_and_45_seconds_then_rolls_back(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    fake.fail_pull_with = "GINN-E-NETWORK"
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-NETWORK"
    assert caught.value.exit_code == 69
    assert ops_of(fake).count("pull") == 4
    assert sleeps == [5.0, 15.0, 45.0]
    assert_nothing_left(env, fake)
    assert [e.error_code for e in create_entries(env)] == ["GINN-E-NETWORK"]


@pytest.mark.fr("FR-120")
def test_a_network_failure_that_clears_on_the_third_attempt_completes_the_create(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    fake.fail_pull_with = "GINN-E-NETWORK"
    fake.fail_pull_times = 2
    result = create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert result.record.state is RealmState.STOPPED
    assert ops_of(fake).count("pull") == 3
    assert sleeps == [5.0, 15.0]


@pytest.mark.fr("FR-120")
def test_a_network_error_raised_by_exec_is_retried_before_the_step_runs_again(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    fake.exec_errors[("apt-get", "update")] = [make("GINN-E-NETWORK")]
    result = create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert result.record.state is RealmState.STOPPED
    assert sleeps == [5.0]
    assert execs(fake).count(["apt-get", "update"]) == 2


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-120")
@pytest.mark.fr("DR-018")
@pytest.mark.parametrize("failing_step", STEPS)
def test_a_ginn_error_after_any_step_rolls_back_every_artifact(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    failing_step: str,
) -> None:
    def fail_at(step: str) -> None:
        if step == failing_step:
            raise make("GINN-E-INTERNAL")

    hooks = hooks_for(tmp_path, sleeps, mountinfo, on_step=fail_at)
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks)
    assert caught.value.code == "GINN-E-INTERNAL"
    assert_nothing_left(env, fake)
    entries = create_entries(env)
    assert [(e.outcome, e.error_code) for e in entries] == [("error", "GINN-E-INTERNAL")]


@pytest.mark.fr("AS-018")
@pytest.mark.fr("DR-018")
@pytest.mark.parametrize("cancel_step", STEPS)
def test_a_cancel_rolls_back_then_reports_interrupted_and_finishes_the_journal(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    cancel_step: str,
) -> None:
    def cancel_at(step: str) -> None:
        if step == cancel_step:
            raise KeyboardInterrupt

    hooks = hooks_for(tmp_path, sleeps, mountinfo, on_step=cancel_at)
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks)
    assert caught.value.code == "GINN-E-INTERRUPTED"
    assert caught.value.exit_code == 130
    assert isinstance(caught.value.__cause__, KeyboardInterrupt)
    assert_nothing_left(env, fake)
    assert [(e.outcome, e.error_code) for e in create_entries(env)] == [
        ("error", "GINN-E-INTERRUPTED")
    ]


@pytest.mark.fr("FR-122")
@pytest.mark.fr("NFR-030")
def test_an_unexpected_exception_rolls_nothing_back_and_keeps_the_journal_entry(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    def crash_at(step: str) -> None:
        if step == "created":
            raise RuntimeError("simulated crash after create")

    hooks = hooks_for(tmp_path, sleeps, mountinfo, on_step=crash_at)
    with pytest.raises(RuntimeError, match="simulated crash"):
        create_realm(env, fake, request(home), hooks=hooks)
    assert len(fake.containers) == 1
    assert (env.paths.homes / "debian").is_dir()
    assert RealmStore(env.paths).load(RealmName("debian")).state is RealmState.CREATING
    pending = env.journal.pending()
    assert [(entry.op, entry.step) for entry in pending] == [("create", "created")]
    assert [e.error_code for e in create_entries(env)] == [None]


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-011")
def test_a_kill_during_the_pull_leaves_a_creating_record_for_recovery(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    fake.pull_error = RuntimeError("simulated kill during the download")
    with pytest.raises(RuntimeError):
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert RealmStore(env.paths).load(RealmName("debian")).state is RealmState.CREATING
    assert [entry.op for entry in env.journal.pending()] == ["create"]


@pytest.mark.fr("FR-122")
@pytest.mark.fr("DR-020")
def test_a_rollback_step_that_fails_keeps_the_error_and_marks_the_record_broken(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    fake.fail_ops["rm"] = "GINN-E-INTERNAL"

    def fail_at(step: str) -> None:
        if step == "created":
            raise make("GINN-E-PKG-MANAGER", realm="debian")

    hooks = hooks_for(tmp_path, sleeps, mountinfo, on_step=fail_at)
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks)
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert any(note.startswith("rollback: container:") for note in caught.value.__notes__)
    assert RealmStore(env.paths).load(RealmName("debian")).state is RealmState.BROKEN
    assert len(fake.containers) == 1
    assert not (env.paths.homes / "debian").exists()
    assert env.journal.pending() == []


@pytest.mark.fr("DR-018")
def test_an_image_that_was_already_present_is_kept_when_the_create_fails(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    ref = trust.pull_reference("debian")
    fake.images[ref] = ImageInfo(
        id="0" * 64,
        digest=trust.load().entry("debian").digest,
        repo_digests=(ref,),
        size=1,
        created="",
    )

    def fail_at(step: str) -> None:
        if step == "created":
            raise make("GINN-E-INTERNAL")

    with pytest.raises(GinnError):
        create_realm(
            env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo, on_step=fail_at)
        )
    assert "rmi" not in ops_of(fake)
    assert ref in fake.images


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-048")
@pytest.mark.fr("DR-018")
@pytest.mark.parametrize(
    ("answer", "fragment"),
    [
        (ExecResult(2, "", ""), "getent passwd exited 2"),
        (ExecResult(127, "", ""), "getent passwd exited 127"),
        # The entry podman writes when no --passwd-entry is sent: home / and shell /bin/sh.
        (
            ExecResult(0, "alice:*:1000:1000:Alice:/:/bin/sh\n", ""),
            "found 'alice:*:1000:1000:Alice:/:/bin/sh'",
        ),
    ],
)
def test_a_realm_user_that_is_not_the_entry_sent_is_refused_and_rolls_back(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    answer: ExecResult,
    fragment: str,
) -> None:
    fake.exec_results[("getent", "passwd", str(UID))] = answer
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-REALM-USER"
    assert caught.value.details["realm"] == "debian"
    assert fragment in caught.value.details["detail"]
    assert caught.value.details["expected"] == f"alice:*:1000:1000:alice:{home}:/bin/bash"
    assert_nothing_left(env, fake)


@pytest.mark.fr("FR-048")
@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("name", ["a$b", "a:b", "a\nb"])
def test_a_host_home_the_passwd_entry_cannot_carry_is_refused_before_any_podman_call(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    name: str,
) -> None:
    with pytest.raises(GinnError) as caught:
        create_realm(
            env, fake, request(home.parent / name), hooks=hooks_for(tmp_path, sleeps, mountinfo)
        )
    assert caught.value.code == "GINN-E-REALM-USER"
    assert "must not be empty or hold" in caught.value.details["detail"]
    assert fake.calls == []


@pytest.mark.fr("FR-012")
@pytest.mark.fr("FR-010")
def test_a_signature_failure_in_a_package_step_maps_to_pkg_signature(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    argv = ("apt-get", "--yes", "--no-install-recommends", "install", "htop")
    fake.exec_results[argv] = ExecResult(
        100, "", "W: GPG error\nNO_PUBKEY 0123456789ABCDEF signatures were invalid\n"
    )
    with pytest.raises(GinnError) as caught:
        create_realm(
            env,
            fake,
            request(home, with_packages=("htop",)),
            hooks=hooks_for(tmp_path, sleeps, mountinfo),
        )
    assert caught.value.code == "GINN-E-PKG-SIGNATURE"
    assert caught.value.exit_code == 65
    assert caught.value.details["step"] == "with"
    assert_nothing_left(env, fake)


@pytest.mark.fr("FR-040")
@pytest.mark.fr("FR-010")
@pytest.mark.fr("DR-018")
def test_a_failed_package_step_maps_to_pkg_manager_with_the_decisive_line(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    argv = ("apt-get", "--yes", "--no-install-recommends", "install", "htop")
    fake.exec_results[argv] = ExecResult(100, "", "E: Unable to locate package htop\n")
    with pytest.raises(GinnError) as caught:
        create_realm(
            env,
            fake,
            request(home, with_packages=("htop",)),
            hooks=hooks_for(tmp_path, sleeps, mountinfo),
        )
    assert caught.value.code == "GINN-E-PKG-MANAGER"
    assert caught.value.exit_code == 1
    assert caught.value.details["detail"] == "E: Unable to locate package htop"
    assert_nothing_left(env, fake)


@pytest.mark.fr("FR-012")
@pytest.mark.fr("NFR-SEC-001")
@pytest.mark.parametrize("distro", ["arch", "debian", "fedora"])
def test_no_signature_check_is_disabled_and_no_capability_is_added(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    distro: str,
) -> None:
    create_realm(
        env,
        fake,
        request(home, name=distro, distro=distro, with_packages=("htop",)),
        hooks=hooks_for(tmp_path, sleeps, mountinfo),
    )
    for argv in execs(fake):
        assert not FORBIDDEN_TOKENS.intersection(argv), argv
        assert not any(sub in token for token in argv for sub in FORBIDDEN_SUBSTRINGS), argv
    for spec in fake.created_specs:
        argv = spec.to_argv()
        assert "--privileged" not in argv
        assert "--cap-add" not in argv
        assert spec.caps_add == []


@pytest.mark.fr("FR-010")
@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-064")
@pytest.mark.fr("FR-066")
def test_a_present_download_folder_is_attached_and_its_grant_is_recorded(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    (home / "Downloads").mkdir()
    result = create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    grants = {grant.realm: grant for grant in result.record.grants}
    downloads = grants[str(home / "Downloads")]
    assert downloads.identity is not None
    assert downloads.mode == "rw"
    assert "unshare" in ops_of(fake)
    assert "share skipped: /run/media/alice (missing)" in result.record.share_skipped
    reloaded = RealmStore(env.paths).load(RealmName("debian"))
    assert reloaded.grants == result.record.grants


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-064")
def test_a_failed_attach_stops_the_realm_and_rolls_back_the_create(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    (home / "Downloads").mkdir()
    fake.unshare_results.append(ExecResult(1, "", "ginn-rebind: boom\n"))
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    ops = ops_of(fake)
    unshare_at = ops.index("unshare")
    assert ops[unshare_at + 1 :][:3] == ["ps", "stop", "rm"]
    assert_nothing_left(env, fake)


@pytest.mark.fr("FR-010")
def test_a_stop_failure_at_the_end_rolls_the_whole_create_back(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    fake.fail_ops["stop"] = "GINN-E-INTERNAL"
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-INTERNAL"
    assert_nothing_left(env, fake)


@pytest.mark.fr("FR-124")
@pytest.mark.fr("FR-010")
def test_the_audit_entry_names_the_distribution_tier_services_and_packages(
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
        request(home, with_packages=("htop",)),
        hooks=hooks_for(tmp_path, sleeps, mountinfo),
    )
    (entry,) = create_entries(env)
    assert entry.outcome == "ok"
    assert entry.params["distro"] == "debian"
    assert entry.params["tier"] == "basic"
    assert entry.params["services"] is False
    assert entry.params["with"] == ["htop"]
    assert entry.user == USER


@pytest.mark.fr("FR-010")
def test_a_create_returns_its_result_type_and_no_host_warning_when_nothing_changed(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    result = create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert isinstance(result, CreateResult)
    assert result.warnings == []


@pytest.mark.fr("AS-018")
@pytest.mark.fr("FR-122")
def test_a_cancel_whose_rollback_fails_keeps_the_notes_and_marks_the_record_broken(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    fake.fail_ops["rm"] = "GINN-E-INTERNAL"

    def cancel_at(step: str) -> None:
        if step == "created":
            raise KeyboardInterrupt

    hooks = hooks_for(tmp_path, sleeps, mountinfo, on_step=cancel_at)
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks)
    assert caught.value.code == "GINN-E-INTERRUPTED"
    assert any(note.startswith("rollback: container:") for note in caught.value.__notes__)
    assert RealmStore(env.paths).load(RealmName("debian")).state is RealmState.BROKEN
    assert len(fake.containers) == 1


@pytest.mark.fr("FR-010")
@pytest.mark.fr("DR-018")
@pytest.mark.parametrize("failing_op", ["create", "start", "inspect", "stop"])
def test_a_failure_of_any_podman_call_in_the_body_rolls_the_whole_create_back(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    failing_op: str,
) -> None:
    fake.fail_ops[failing_op] = "GINN-E-INTERNAL"
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-INTERNAL"
    assert_nothing_left(env, fake)
    assert [(e.outcome, e.error_code) for e in create_entries(env)] == [
        ("error", "GINN-E-INTERNAL")
    ]


@pytest.mark.fr("AS-018")
@pytest.mark.fr("DR-018")
def test_an_interrupt_inside_the_create_call_leaves_nothing(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The container exists, but the id was never kept. The undo still finds it by its labels."""
    fake.interrupt_after_create = True
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-INTERRUPTED"
    assert len(fake.created_specs) == 1
    assert_nothing_left(env, fake)


@pytest.mark.fr("AS-018")
@pytest.mark.fr("DR-018")
@pytest.mark.parametrize("stored", [False, True], ids=["before-store", "after-store"])
def test_an_interrupt_during_the_pull_leaves_nothing(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    stored: bool,
) -> None:
    if stored:
        fake.interrupt_after_pull = True
    else:
        fake.pull_error = KeyboardInterrupt()
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-INTERRUPTED"
    assert_nothing_left(env, fake)


@pytest.mark.fr("AS-018")
@pytest.mark.fr("DR-018")
def test_an_interrupt_inside_the_realm_home_mkdir_leaves_nothing(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The directory is made, and then the call is interrupted before it returns."""
    real_mkdir = Path.mkdir

    def mkdir_then_interrupt(self: Path, *args: Any, **kwargs: Any) -> None:
        real_mkdir(self, *args, **kwargs)
        if self.name == "debian":
            raise KeyboardInterrupt

    monkeypatch.setattr(Path, "mkdir", mkdir_then_interrupt)
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-INTERRUPTED"
    assert_nothing_left(env, fake)


@pytest.mark.fr("AS-018")
@pytest.mark.fr("DR-018")
def test_an_interrupt_just_after_the_creating_record_is_written_leaves_nothing(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record file is written, and then the call is interrupted before the store returns."""
    real_save = RealmStore.save

    def save_then_interrupt(self: RealmStore, record: RealmRecord) -> None:
        real_save(self, record)
        if record.state is RealmState.CREATING and record.container_id is None:
            raise KeyboardInterrupt

    monkeypatch.setattr(RealmStore, "save", save_then_interrupt)
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-INTERRUPTED"
    assert_nothing_left(env, fake)


@pytest.mark.fr("FR-015")
@pytest.mark.fr("FR-123")
def test_a_second_create_of_the_same_distribution_is_refused_while_one_holds_the_image(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    hooks = hooks_for(tmp_path, sleeps, mountinfo)
    with image_lock(env.paths, "debian", "create of debian image"):
        with pytest.raises(GinnError) as caught:
            create_realm(env, fake, request(home, name="debian-dev"), hooks=hooks)
        assert caught.value.code == "GINN-E-REALM-BUSY"
        assert caught.value.exit_code == 75
        assert fake.calls == []
        assert not (env.paths.homes / "debian-dev").exists()
    result = create_realm(env, fake, request(home, name="debian-dev"), hooks=hooks)
    assert result.record.state is RealmState.STOPPED


@pytest.mark.fr("FR-010")
@pytest.mark.fr("DR-007")
def test_with_packages_are_recorded_in_the_install_manifest_as_ginn_installs(
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
        request(home, with_packages=("htop", "vim")),
        hooks=hooks_for(tmp_path, sleeps, mountinfo),
    )
    (name,) = RealmStore(env.paths).list_names()
    record = RealmStore(env.paths).load(name)
    assert record.manifest == [
        ManifestEntry(name="htop", source=InstallSource.REPO, method=InstallMethod.GINN),
        ManifestEntry(name="vim", source=InstallSource.REPO, method=InstallMethod.GINN),
    ]


SECCOMP_SOURCE = {
    "defaultAction": "SCMP_ACT_ERRNO",
    "syscalls": [
        {
            "names": ["sethostname", "setdomainname"],
            "action": "SCMP_ACT_ALLOW",
            "args": [],
            "includes": {"caps": ["CAP_SYS_ADMIN"]},
        },
        {"names": ["read"], "action": "SCMP_ACT_ALLOW", "args": []},
    ],
}
NESTED_OPTIONS = [
    "device=/dev/fuse",
    "device=/dev/kvm",
    "device=/dev/net/tun",
    "group-add=keep-groups",
    "security-opt=seccomp",
    "security-opt=unmask",
]


def _seccomp_source(tmp_path: Path) -> Path:
    source = tmp_path / "containers-seccomp.json"
    source.write_text(json.dumps(SECCOMP_SOURCE), encoding="utf-8")
    return source


NESTED_HOST_DEVICES = ("/dev/fuse", "/dev/net/tun")


def _kvm_hooks(
    tmp_path: Path, sleeps: list[float], mountinfo: Path, *, kvm: bool = True, **overrides: Any
) -> CreateHooks:
    """Hooks for a host with `/dev/kvm` and the nested devices, and a fixture profile source.

    `kvm` False is a host without `/dev/kvm`. Every other path answers as `tmp_path` does.
    """
    base = hooks_for(tmp_path, sleeps, mountinfo, **overrides)
    under_tmp = base.exists

    def exists(path: str) -> bool:
        if path in NESTED_HOST_DEVICES:
            return True
        return (kvm and path == "/dev/kvm") or under_tmp(path)

    return replace(base, exists=exists)


@pytest.mark.fr("FR-060")
@pytest.mark.fr("FR-062")
@pytest.mark.fr("NFR-SEC-002")
def test_a_development_create_adds_the_nested_options_and_writes_the_tier_files(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    hooks = _kvm_hooks(tmp_path, sleeps, mountinfo, seccomp_source=_seccomp_source(tmp_path))
    result = create_realm(env, fake, request(home, tier=Tier.DEVELOPMENT), hooks=hooks)
    record = result.record
    assert record.state is RealmState.STOPPED
    assert record.settings.tier is Tier.DEVELOPMENT
    assert record.created_options == NESTED_OPTIONS
    assert record.kvm_at_create is True
    argv = fake.created_specs[0].to_argv()
    pairs = _pairs(argv)
    for option in (
        ("--device", "/dev/fuse"),
        ("--device", "/dev/net/tun"),
        ("--device", "/dev/kvm"),
        ("--group-add", "keep-groups"),
        ("--security-opt", "unmask=/proc/*"),
    ):
        assert option in pairs
    assert "--privileged" not in argv and "--cap-add" not in argv
    assert "--passwd-entry" in argv
    # T6.7: the realm user is the passwd entry podman writes, so no useradd runs for it.
    assert not any(command and command[0] == "useradd" for command in execs(fake))
    # The name is the hash of the generated profile, so the directory holds one file.
    (profile,) = list(env.paths.seccomp.glob("*.json"))
    assert profile.is_file()
    assert b'"sethostname"' in profile.read_bytes()
    assert ("--security-opt", f"seccomp={profile}") in pairs
    config = env.paths.homes / "debian" / ".config" / "containers"
    assert 'driver = "overlay"' in (config / "storage.conf").read_text(encoding="utf-8")
    assert "cgroup_manager" in (config / "containers.conf").read_text(encoding="utf-8")
    reloaded = RealmStore(env.paths).load(RealmName("debian"))
    assert reloaded.created_options == NESTED_OPTIONS
    assert reloaded.kvm_at_create is True


def _pairs(argv: list[str]) -> list[tuple[str, str]]:
    """The (flag, value) pairs of an argv, for every flag that takes a value."""
    return [(token, argv[i + 1]) for i, token in enumerate(argv[:-1]) if token.startswith("--")]


@pytest.mark.fr("FR-060")
@pytest.mark.fr("FR-063")
def test_a_custom_create_starts_with_the_basic_toggles_and_no_option(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    result = create_realm(
        env, fake, request(home, tier=Tier.CUSTOM), hooks=hooks_for(tmp_path, sleeps, mountinfo)
    )
    assert result.record.permissions.toggles == BASIC_TOGGLES
    assert result.record.permissions.shares == ()
    assert result.record.created_options == []
    assert not (home / ".config" / "containers").exists()


@pytest.mark.fr("FR-062")
@pytest.mark.fr("AS-010")
def test_a_development_create_attaches_projects_and_the_shell_files(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    present = {str(home / "Projects"), str(home / ".bashrc"), *NESTED_HOST_DEVICES}
    hooks = hooks_for(
        tmp_path,
        sleeps,
        mountinfo,
        seccomp_source=_seccomp_source(tmp_path),
        exists=present.__contains__,
    )
    result = create_realm(env, fake, request(home, tier=Tier.DEVELOPMENT), hooks=hooks)
    grants = {grant.realm: grant for grant in result.record.grants}
    assert grants[str(home / "Projects")].mode == "rw"
    assert grants[str(home / ".bashrc")].mode == "ro"
    # A missing shell file is no grant and no skip (FR-062, AS-010).
    assert str(home / ".zshrc") not in grants
    assert not any(".zshrc" in message for message in result.record.share_skipped)


@pytest.mark.fr("FR-062")
@pytest.mark.fr("DR-010")
def test_a_development_create_with_no_host_profile_is_refused_before_any_write(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """ADR-006: no profile means no nested realm, and never `seccomp=unconfined`."""
    hooks = _kvm_hooks(tmp_path, sleeps, mountinfo, seccomp_source=tmp_path / "absent.json")
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home, tier=Tier.DEVELOPMENT), hooks=hooks)
    assert caught.value.code == "GINN-E-SECCOMP-SOURCE"
    assert_nothing_left(env, fake)
    assert not env.paths.seccomp.exists()
    assert create_entries(env) == []


@pytest.mark.fr("FR-062")
@pytest.mark.fr("NFR-SEC-002")
def test_a_host_without_a_nested_device_refuses_the_create_before_any_write(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """ADR-006: the inner podman needs /dev/fuse and /dev/net/tun, and the host is checked first."""
    under_tmp = hooks_for(tmp_path, sleeps, mountinfo).exists
    hooks = replace(
        hooks_for(tmp_path, sleeps, mountinfo, seccomp_source=_seccomp_source(tmp_path)),
        exists=lambda path: path != "/dev/fuse" and under_tmp(path),
    )
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home, tier=Tier.DEVELOPMENT), hooks=hooks)
    assert caught.value.code == "GINN-E-HOST-DEVICE"
    assert "/dev/fuse" in caught.value.message
    assert_nothing_left(env, fake)
    assert not env.paths.seccomp.exists()


@pytest.mark.fr("FR-062")
def test_a_development_create_on_a_host_without_kvm_records_that(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    hooks = _kvm_hooks(
        tmp_path, sleeps, mountinfo, kvm=False, seccomp_source=_seccomp_source(tmp_path)
    )
    result = create_realm(env, fake, request(home, tier=Tier.DEVELOPMENT), hooks=hooks)
    assert result.record.kvm_at_create is False
    assert "device=/dev/kvm" not in result.record.created_options
    assert "device=/dev/fuse" in result.record.created_options


def _gpu(status: Status, root: Path, **overrides: Any) -> NvidiaPlan:
    """A grant plan of `status`, with its spec and drop-in under `root` (the Owned data dir)."""
    values: dict[str, Any] = {
        "status": status,
        "reason": "test host",
        "spec_path": str(root / "cdi" / "nvidia.json"),
        "spec_text": '{"cdiVersion": "0.6.0"}\n',
        "ld_path": str(root / "ld" / "ginn-nvidia.conf"),
        "ld_text": "/usr/lib/ginn-nvidia\n",
    }
    values.update(overrides)
    return NvidiaPlan(**values)


@pytest.mark.fr("FR-069")
def test_a_ready_nvidia_grant_is_a_cdi_device_with_its_spec_written_once(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _create_plan, "nvidia_plan", lambda paths, hooks: _gpu(Status.READY, paths.data)
    )
    result = create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert result.record.gpu_cdi is True
    assert result.gpu_note is None
    spec = env.paths.cdi / "nvidia.json"
    assert spec.read_text(encoding="utf-8") == '{"cdiVersion": "0.6.0"}\n'
    created = fake.created_specs[0]
    assert created.cdi_devices == ["nvidia.com/gpu=all"]
    assert created.cdi_spec_dir == str(env.paths.cdi)
    assert created.global_flags() == ["--cdi-spec-dir", str(env.paths.cdi)]
    assert ("--device", "nvidia.com/gpu=all") in _pairs(created.to_argv())


@pytest.mark.fr("FR-069")
def test_a_host_without_a_driver_gets_a_note_and_no_device(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """FR-069 AC-2 and R-005: an absent driver is informational; the create goes on without it."""
    result = create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert result.record.gpu_cdi is False
    assert result.gpu_note is not None and "no NVIDIA kernel driver" in result.gpu_note
    assert fake.created_specs[0].cdi_devices == []
    assert not (env.paths.cdi / "nvidia.json").exists()


@pytest.mark.fr("FR-069")
def test_a_musl_host_is_informational_and_not_a_failure(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    musl = _gpu(Status.MUSL, env.paths.data, reason="NVIDIA unsupported on musl")
    monkeypatch.setattr(_create_plan, "nvidia_plan", lambda paths, hooks: musl)
    result = create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert result.record.state is RealmState.STOPPED
    assert result.gpu_note is not None and "musl" in result.gpu_note


@pytest.mark.fr("FR-069")
def test_a_stale_driver_refuses_the_create_before_any_write(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stale(paths: OwnedPaths, hooks: object) -> NvidiaPlan:
        raise make("GINN-E-NVIDIA-STALE", detail="driver 595, library names libnvidia-x.so.1")

    monkeypatch.setattr(_create_plan, "nvidia_plan", stale)
    with pytest.raises(GinnError) as caught:
        create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    assert caught.value.code == "GINN-E-NVIDIA-STALE"
    assert_nothing_left(env, fake)
    assert not (env.paths.cdi / "nvidia.json").exists()


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-069")
def test_a_create_records_the_gpu_and_fonts_toggles_it_made(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    def host_has_dri(path: str) -> bool:
        return path == "/dev/dri" or str(path).startswith(str(tmp_path))

    hooks = hooks_for(tmp_path, sleeps, mountinfo, exists=host_has_dri)
    result = create_realm(env, fake, request(home), hooks=hooks)
    assert (result.record.gpu_at_create, result.record.fonts_at_create) == (True, True)
    assert ("--device", "/dev/dri") in _pairs(fake.created_specs[0].to_argv())


@pytest.mark.fr("FR-061")
@pytest.mark.fr("FR-063")
@pytest.mark.fr("FR-069")
def test_a_custom_create_without_the_gpu_toggle_has_no_device_no_cdi_and_no_spec_file(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with a ready NVIDIA driver, a realm whose GPU toggle is off gets no `/dev/dri` device,
    no CDI grant and no CDI spec. create starts Custom from the Basic toggles, so the stored
    toggles are set here through `initial_permissions`."""
    off = Permissions(toggles=BASIC_TOGGLES - {Toggle.GPU})
    monkeypatch.setattr(_create_plan, "initial_permissions", lambda tier: off)
    monkeypatch.setattr(
        _create_plan, "nvidia_plan", lambda paths, hooks: _gpu(Status.READY, paths.data)
    )
    result = create_realm(
        env, fake, request(home, tier=Tier.CUSTOM), hooks=hooks_for(tmp_path, sleeps, mountinfo)
    )
    created = fake.created_specs[0]
    assert ("--device", "/dev/dri") not in _pairs(created.to_argv())
    assert created.cdi_devices == []
    assert result.record.gpu_cdi is False
    assert result.record.gpu_at_create is False
    assert not (env.paths.cdi / "nvidia.json").exists()
