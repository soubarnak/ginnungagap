# SPDX-License-Identifier: Apache-2.0
"""`ginn create --resume` finishes an interrupted create from the request its record keeps (FR-122).

A killed create is settled by recovery to `broken`, and its record keeps `create_pending`, the
`--with` packages and the Kali consent (store/records.py). The resume redoes the create from its
`pulled` step, keeps the Realm Home, and never starts a services-on container (ADR-003). A SIGKILL
is simulated by a BaseException that no ops code catches, so the journal entry stays for recovery.
The SIGKILL sweep at every durable effect is in test_crash_safety.py.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import pytest

from ginn.distros import Verb
from ginn.distros import load as load_distro
from ginn.domain import RealmName, RealmState
from ginn.errors import GinnError, make
from ginn.ops._mutating import OpEnv
from ginn.ops.create import CreateHooks, CreateResult, create_realm
from ginn.ops.recovery import OUTCOME_BROKEN, recover
from ginn.ops.resume import resume_create
from ginn.podman.adapter import CreateSpec
from ginn.runtime import attach
from ginn.runtime.spec import LABEL_GENERATION, LABEL_REALM
from ginn.services.runit_user import service_dir
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore
from ginn.trust import manifest as trust
from ginn.trust.manifest import TrustManifest
from tests.fakes.clock import FakeClock
from tests.unit.test_ops_create import (
    BASE_MOUNTINFO,
    GINN_PROGRAM,
    SCOPE,
    UID,
    USER,
    ScriptedPodman,
    _kali_everything_answers,
    _statvfs,
    create_entries,
    execs,
    hooks_for,
    ops_of,
    request,
)

# The fixtures of test_ops_create.py, which pytest does not share across test modules.


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    """Owned Paths under tmp_path, with every XDG variable pinned (as in test_ops_create)."""
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


@pytest.fixture
def mountinfo(tmp_path: Path) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(BASE_MOUNTINFO, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def host_identity_is_read_only_under_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """attach_at_start reads source identities with os.stat, not through a hook. Only tmp exists."""
    real = attach.read_identity

    def identity(host: str, *, mountinfo: Path = attach.MOUNTINFO) -> attach.SourceIdentity | None:
        if not str(host).startswith(str(tmp_path)):
            return None
        return real(host, mountinfo=mountinfo)

    monkeypatch.setattr(attach, "read_identity", identity)


NAME: Final = "debian"
MARKER: Final = "marker.txt"
MARKER_BYTES: Final = b"kept\n"
_NO_PROCESS: Final = 2**31 - 2
"""A pid that no process has, so the killed entry reads as dead (store/journal.py is_pid_alive)."""


class _Killed(BaseException):
    """A SIGKILL stand-in. A BaseException that no ops code catches, so the journal entry stays."""


def _kill_after(step: str) -> Callable[[str], None]:
    """A journal step hook that kills the operation right after `step` is recorded."""

    def on_step(name: str) -> None:
        if name == step:
            raise _Killed(name)

    return on_step


def _yes(_prompt: str) -> bool:
    return True


def _no_prompt(prompt: str) -> bool:
    raise AssertionError(f"the everything prompt was asked again: {prompt}")


def _die(env: OpEnv, realm: str = NAME) -> None:
    """Make the journal entry look as a SIGKILLed process leaves it: its pid is not running."""
    path = env.paths.journal / f"{realm}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pid"] = _NO_PROCESS
    data["pid_start"] = 0
    path.write_text(json.dumps(data), encoding="utf-8")


def _store(env: OpEnv) -> RealmStore:
    return RealmStore(env.paths, env.clock)


def _load(env: OpEnv, realm: str = NAME) -> RealmRecord:
    return _store(env).load(RealmName(realm))


def _marker(env: OpEnv, realm: str = NAME) -> Path:
    """Write the Realm Home marker that the resume must keep, and return its path."""
    path = env.paths.homes / realm / MARKER
    path.write_bytes(MARKER_BYTES)
    return path


def _killed_create(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    step: str,
    *,
    confirm: Callable[[str], bool] | None = None,
    **kwargs: Any,
) -> None:
    """A create killed right after journal step `step`, then settled by recovery to broken."""
    hooks = hooks_for(
        tmp_path,
        sleeps,
        mountinfo,
        on_step=_kill_after(step),
        program=GINN_PROGRAM,
        confirm=confirm,
    )
    with pytest.raises(_Killed):
        create_realm(env, fake, request(home, **kwargs), hooks=hooks)
    _die(env, kwargs.get("name", NAME))
    [outcome] = recover(env, fake)
    assert outcome.outcome == OUTCOME_BROKEN, outcome.reason


def _resume_hooks(
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    **overrides: Any,
) -> CreateHooks:
    return hooks_for(tmp_path, sleeps, mountinfo, program=GINN_PROGRAM, **overrides)


def _resume(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    hooks: CreateHooks,
    name: str = NAME,
) -> CreateResult:
    """Resume `name` as the host user of the test, whose request names the same host paths."""
    host_request = request(home)
    return resume_create(
        env,
        fake,
        RealmName(name),
        uid=host_request.uid,
        gid=host_request.gid,
        host_home=host_request.host_home,
        host_env=host_request.host_env,
        hooks=hooks,
    )


def _resumed_entries(env: OpEnv, realm: str = NAME) -> list[Any]:
    return [e for e in create_entries(env, realm) if e.params.get("resume") is True]


NEW_DIGEST: Final = "sha256:" + "cd" * 32
"""A digest that the debian pin moves to, as a later Trust Manifest refresh would pin it."""


def _repinned(digest: str = NEW_DIGEST) -> TrustManifest:
    """The Trust Manifest with the debian entry pinned to `digest` (FR-011)."""
    manifest = trust.load()
    entries = tuple(replace(e, digest=digest) if e.key == "debian" else e for e in manifest.entries)
    return replace(manifest, entries=entries)


@pytest.mark.fr("FR-122")
@pytest.mark.parametrize(
    "step", ["homed", "pulled", "created", "started", "attached", "integrated"]
)
def test_a_create_killed_after_any_step_resumes_to_one_stopped_realm(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
    step: str,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, step)
    stale = set(fake.containers)
    marker = _marker(env)

    result = _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo))

    record = result.record
    assert result.resumed is True
    assert record.state is RealmState.STOPPED
    assert record.image_digest == trust.load().entry("debian").digest
    assert record.create_pending is False
    assert record.with_at_create == []
    [cid] = list(fake.containers)
    assert cid == record.container_id
    assert not stale & set(fake.containers), "the stale container of the killed create is left"
    assert fake.containers[cid].labels[LABEL_GENERATION] == "1"
    assert marker.read_bytes() == MARKER_BYTES
    assert env.journal.pending() == []
    [entry] = _resumed_entries(env)
    assert (entry.outcome, entry.error_code) == ("ok", None)


@pytest.mark.fr("FR-122")
def test_a_stopped_realm_is_refused_before_any_write(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    before = _load(env)
    calls, audit = len(fake.calls), len(list(env.audit.read()))

    with pytest.raises(GinnError) as excinfo:
        _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo))

    assert excinfo.value.code == "GINN-E-REALM-STATE"
    assert _load(env) == before
    assert len(fake.calls) == calls
    assert len(list(env.audit.read())) == audit


@pytest.mark.fr("FR-122")
def test_a_broken_realm_that_no_create_left_broken_is_refused_and_named(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    create_realm(env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo))
    store = _store(env)
    store.save(replace(_load(env), state=RealmState.BROKEN))

    with pytest.raises(GinnError) as excinfo:
        _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo))

    assert excinfo.value.code == "GINN-E-REALM-STATE"
    assert excinfo.value.message.startswith("realm 'debian' is broken;")
    assert excinfo.value.__notes__ == [
        "no resumable create is recorded for this realm; 'ginn delete' removes it"
    ]
    assert _load(env).state is RealmState.BROKEN


@pytest.mark.fr("FR-122")
def test_a_realm_that_does_not_exist_is_no_such_realm(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    with pytest.raises(GinnError) as excinfo:
        _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo), name="nope")
    assert excinfo.value.code == "GINN-E-NO-SUCH-REALM"


@pytest.mark.fr("FR-122")
def test_a_realm_home_that_is_a_link_is_refused_before_the_stale_container_is_touched(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "created")
    realm_home = env.paths.homes / NAME
    shutil.rmtree(realm_home)
    realm_home.symlink_to(tmp_path)
    containers = dict(fake.containers)

    with pytest.raises(GinnError) as excinfo:
        _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo))

    assert excinfo.value.code == "GINN-E-INTERNAL"
    assert fake.containers == containers
    assert _load(env).state is RealmState.BROKEN


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-121")
def test_a_resume_that_must_pull_and_runs_out_of_disk_is_refused_before_any_removal(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "created")
    containers = dict(fake.containers)
    fake.images.clear()  # the pinned image is not in storage, so the resume pulls it (FR-121)

    with pytest.raises(GinnError) as excinfo:
        _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo, statvfs=_statvfs(0)))

    assert excinfo.value.code == "GINN-E-DISK-SPACE"
    assert fake.containers == containers
    assert _load(env).state is RealmState.BROKEN


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-121")
def test_a_resume_whose_image_is_in_storage_needs_no_base_space(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    """The image's layers are local, so no base space is needed, and a full disk does not refuse."""
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "created")
    assert _load(env).image_ref in fake.images

    result = _resume(
        env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo, statvfs=_statvfs(0))
    )

    assert result.record.state is RealmState.STOPPED
    [entry] = _resumed_entries(env)
    assert (entry.outcome, entry.error_code) == ("ok", None)


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-011")
def test_a_resume_under_a_new_pin_removes_the_old_image_when_no_container_uses_it(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "pulled")
    old = _load(env).image_ref
    assert old in fake.images

    result = _resume(
        env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo, manifest=_repinned())
    )

    assert result.record.image_ref != old
    assert result.record.state is RealmState.STOPPED
    assert old not in fake.images
    assert result.record.image_ref in fake.images


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-011")
def test_a_resume_under_a_new_pin_keeps_the_old_image_while_another_container_uses_it(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "pulled")
    old = _load(env).image_ref
    fake.create(
        CreateSpec(
            name="ginn-other-g1",
            image_digest_ref=old,
            network="host",
            labels={LABEL_REALM: "other"},
        )
    )

    result = _resume(
        env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo, manifest=_repinned())
    )

    assert result.record.state is RealmState.STOPPED
    assert old in fake.images
    assert "rmi" not in ops_of(fake)


@pytest.mark.fr("FR-122")
def test_a_resume_installs_the_recorded_packages_and_records_them(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "created", with_packages=("htop",))
    assert _load(env).with_at_create == ["htop"]

    result = _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo))

    install = tuple(load_distro("debian").argv(Verb.INSTALL, ["htop"]))
    assert install in [tuple(argv) for argv in execs(fake)]
    assert [entry.name for entry in result.record.manifest] == ["htop"]
    assert result.record.with_at_create == []


@pytest.mark.fr("FR-122")
def test_a_failed_resume_stays_broken_with_no_container_and_keeps_its_home_and_image(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "created")
    marker = _marker(env)
    images = dict(fake.images)
    fake.exec_errors[("getent", "passwd", str(UID))] = [make("GINN-E-INTERNAL")]

    with pytest.raises(GinnError) as excinfo:
        _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo))

    assert excinfo.value.code == "GINN-E-INTERNAL"
    record = _load(env)
    assert record.state is RealmState.BROKEN
    assert record.create_pending is True
    assert fake.containers == {}
    assert marker.read_bytes() == MARKER_BYTES
    assert fake.images == images, "an image that the resume did not pull is kept"
    assert env.journal.pending() == []
    [entry] = _resumed_entries(env)
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-INTERNAL")


@pytest.mark.fr("FR-122")
def test_a_resume_interrupted_by_ctrl_c_rolls_back_its_container_and_stays_broken(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "created")
    fake.interrupt_after_create = True

    with pytest.raises(GinnError) as excinfo:
        _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo))

    assert excinfo.value.code == "GINN-E-INTERRUPTED"
    assert fake.containers == {}
    record = _load(env)
    assert record.state is RealmState.BROKEN
    assert record.create_pending is True


@pytest.mark.fr("FR-122")
def test_a_resume_killed_after_it_moved_the_record_is_settled_broken_and_a_second_resume_finishes(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "created")
    with pytest.raises(_Killed):
        _resume(
            env,
            fake,
            home,
            _resume_hooks(tmp_path, sleeps, mountinfo, on_step=_kill_after("pulled")),
        )
    _die(env)
    [outcome] = recover(env, fake)
    assert outcome.outcome == OUTCOME_BROKEN, outcome.reason
    assert _load(env).state is RealmState.BROKEN

    result = _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo))

    assert result.record.state is RealmState.STOPPED
    assert len(fake.containers) == 1


@pytest.mark.fr("FR-122")
def test_a_services_on_resume_writes_its_runit_directory_and_never_starts_the_container(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "created", services=True)
    fake.calls.clear()

    result = _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo))

    assert "start" not in ops_of(fake)
    assert result.record.state is RealmState.STOPPED
    assert result.record.settings.services is True
    assert sorted(p.name for p in service_dir(env.paths, RealmName(NAME)).iterdir()) == [
        "down",
        "finish",
        "run",
    ]
    assert any(notice.code == "GINN-W-SERVICES-PENDING" for notice in result.warnings)


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-081")
def test_a_services_on_resume_tells_runsv_down_before_it_replaces_the_runit_directory(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "service", services=True)
    control = service_dir(env.paths, RealmName(NAME)) / "supervise" / "control"
    control.parent.mkdir()
    os.mkfifo(control)
    reader = os.open(control, os.O_RDONLY | os.O_NONBLOCK)
    try:
        result = _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo))
        assert os.read(reader, 8) == b"d"
    finally:
        os.close(reader)

    assert result.record.state is RealmState.STOPPED


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-014")
def test_a_kali_resume_without_a_yes_is_refused_before_any_write(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        "integrated",
        name="kali",
        distro="kali",
        everything=True,
    )
    before = _load(env, "kali")
    calls = len(fake.calls)

    with pytest.raises(GinnError) as excinfo:
        _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo), name="kali")

    assert excinfo.value.code == "GINN-E-USAGE"
    assert len(fake.calls) == calls
    assert _load(env, "kali") == before


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-014")
def test_a_kali_create_records_the_yes_before_it_installs_the_everything_set(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _kali_everything_answers(fake)
    _killed_create(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        "estimated",
        confirm=_yes,
        name="kali",
        distro="kali",
        everything=True,
    )
    record = _load(env, "kali")
    assert record.everything_confirmed is True
    assert record.create_pending is True


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-014")
def test_a_kali_resume_does_not_ask_again_once_the_create_confirmed_the_everything_set(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _kali_everything_answers(fake)
    _killed_create(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        "estimated",
        confirm=_yes,
        name="kali",
        distro="kali",
        everything=True,
    )

    result = _resume(
        env,
        fake,
        home,
        _resume_hooks(tmp_path, sleeps, mountinfo, confirm=_no_prompt),
        name="kali",
    )

    assert result.record.state is RealmState.STOPPED
    install = tuple(load_distro("kali").argv(Verb.INSTALL, ["kali-linux-everything"]))
    assert install in [tuple(argv) for argv in execs(fake)]


@pytest.mark.fr("FR-122")
def test_a_resume_with_a_yes_at_the_prompt_asks_once_and_installs(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _kali_everything_answers(fake)
    _killed_create(
        env,
        fake,
        home,
        tmp_path,
        sleeps,
        mountinfo,
        "integrated",
        name="kali",
        distro="kali",
        everything=True,
    )
    asked: list[str] = []

    def answer(prompt: str) -> bool:
        asked.append(prompt)
        return True

    result = _resume(
        env,
        fake,
        home,
        _resume_hooks(tmp_path, sleeps, mountinfo, confirm=answer),
        name="kali",
    )

    assert len(asked) == 1
    assert result.estimate is not None
    assert _load(env, "kali").everything_confirmed is False
    assert result.record.state is RealmState.STOPPED


@pytest.mark.fr("FR-122")
def test_the_request_is_kept_while_the_create_is_pending_and_cleared_when_it_stops(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    _killed_create(env, fake, home, tmp_path, sleeps, mountinfo, "created", with_packages=("htop",))
    pending = _load(env)
    assert (pending.state, pending.create_pending, pending.with_at_create) == (
        RealmState.BROKEN,
        True,
        ["htop"],
    )

    finished = _resume(env, fake, home, _resume_hooks(tmp_path, sleeps, mountinfo)).record

    assert (finished.state, finished.create_pending, finished.with_at_create) == (
        RealmState.STOPPED,
        False,
        [],
    )


@pytest.mark.fr("FR-122")
def test_a_stopped_record_cannot_carry_a_pending_create(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    stopped = create_realm(
        env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo)
    ).record

    with pytest.raises(ValueError, match="must not record a pending create"):
        replace(stopped, create_pending=True)
    with pytest.raises(ValueError, match="kept only while the create is pending"):
        replace(stopped, with_at_create=["htop"])


@pytest.mark.fr("FR-122")
def test_the_pending_fields_round_trip_and_a_record_without_them_reads_as_not_pending(
    env: OpEnv,
    fake: ScriptedPodman,
    home: Path,
    tmp_path: Path,
    sleeps: list[float],
    mountinfo: Path,
) -> None:
    stopped = create_realm(
        env, fake, request(home), hooks=hooks_for(tmp_path, sleeps, mountinfo)
    ).record
    pending = replace(
        stopped,
        state=RealmState.BROKEN,
        create_pending=True,
        with_at_create=["htop"],
        everything_confirmed=True,
    )

    assert RealmRecord.from_json(pending.to_json()) == pending

    data = stopped.to_json()
    for key in ("create_pending", "with_at_create", "everything_confirmed"):
        del data[key]
    old = RealmRecord.from_json(data)
    assert (old.create_pending, old.with_at_create, old.everything_confirmed) == (False, [], False)
