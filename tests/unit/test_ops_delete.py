# SPDX-License-Identifier: Apache-2.0
"""Delete a realm (T4.7, T4.8): ops/delete.py against FakePodman (FR-019, FR-025, AS-014).

Covers a stopped and a running realm, the purge of the Realm Home with and without confirmation,
the symbolic-link refusal, an interrupted delete at every journal step (a crash, and a GinnError),
the recovery of a `deleting` record, the shared image, an absent container, the audit entries, the
unknown name, another user's realm, and the services-on refusal.

The fake runs no podman. Records, the journal and the audit log are real files under tmp_path. A
killed operation is modelled by an exception that is not a GinnError, which keeps the journal file,
and by rewriting that file's pid to a process that has exited, which is what recovery needs. No
test runs podman, sudo or a host command.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import DistroId, RealmName, RealmSettings, RealmState, Tier
from ginn.errors import GinnError, make
from ginn.ops import delete as delete_module
from ginn.ops._mutating import OpEnv, mutating
from ginn.ops.delete import STEPS, DeleteHooks, check_purge, delete_realm
from ginn.ops.recovery import OUTCOME_RECOVERED, recover
from ginn.ops.security_ops import agent_state_path, proxy_state_path, read_agent_record
from ginn.podman.adapter import CreateSpec
from ginn.podman.models import ContainerInfo, ContainerState
from ginn.runtime.spec import LABEL_DISTRO, LABEL_GENERATION, LABEL_REALM
from ginn.security import agent
from ginn.services import supervisor
from ginn.services.runit_user import service_dir, write_service_dir
from ginn.services.supervisor import Result, SupervisorHooks
from ginn.store.journal import process_start_ticks
from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmRecord, RealmStore, write_json_atomic
from tests.fakes.clock import FakeClock
from tests.fakes.fake_podman import FakePodman

DIGEST = "registry.example/debian@sha256:" + "cd" * 32
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
SCOPE = ("etc", "var/db/xbps", "var/service", "usr/local", "opt")
USER = "alice"


class SimulatedCrash(Exception):
    """Stands for a SIGKILL inside the body. It is not a GinnError, so the journal file stays."""


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedPaths]:
    """Owned Paths under tmp_path, with every XDG variable pinned (as in test_ops_lifecycle)."""
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
def fake() -> FakePodman:
    return FakePodman()


def _record(
    name: str,
    state: RealmState,
    container_id: str | None,
    *,
    previous: RealmState | None = None,
    services: bool = False,
) -> RealmRecord:
    settings = RealmSettings(
        name=RealmName(name),
        distro=DistroId("debian"),
        tier=Tier.BASIC,
        services=services,
        autostart=False,
        everything=False,
    )
    return RealmRecord(
        settings=settings,
        state=state,
        previous_state=previous,
        image_ref=DIGEST,
        image_digest=DIGEST.partition("@")[2],
        container_id=container_id,
        generation=1,
        created_at=T0,
        updated_at=T0,
        restart_required=False,
        share_skipped=[],
    )


def make_realm(
    env: OpEnv,
    fake: FakePodman,
    name: str,
    state: RealmState,
    *,
    running: bool = False,
    container: bool = True,
    home: bool = True,
    services: bool = False,
    previous: RealmState | None = None,
) -> RealmRecord:
    """A stored realm with its Realm Home, a fake container and the pinned image in the fake."""
    if DIGEST not in fake.images:
        fake.pull(DIGEST)
    cid: str | None = None
    if container:
        labels = {LABEL_REALM: name, LABEL_DISTRO: "debian", LABEL_GENERATION: "1"}
        spec = CreateSpec(
            name=f"ginn-{name}-g1", image_digest_ref=DIGEST, network="host", labels=labels
        )
        cid = fake.create(spec)
        if running:
            fake.start(cid)
    if home:
        realm_home = env.paths.homes / name
        realm_home.mkdir(parents=True, exist_ok=True)
        (realm_home / "notes.txt").write_text("keep me\n", encoding="utf-8")
    record = _record(name, state, cid, previous=previous, services=services)
    RealmStore(env.paths, env.clock).save(record)
    fake.calls.clear()
    return record


def stored(env: OpEnv, name: str) -> RealmRecord:
    return RealmStore(env.paths, env.clock).load(RealmName(name))


def realm_exists(env: OpEnv, name: str) -> bool:
    return RealmStore(env.paths, env.clock).exists(RealmName(name))


def containers_of(fake: FakePodman, name: str) -> list[str]:
    return [c.id for c in fake.ps({LABEL_REALM: name})]


def audit_of(env: OpEnv, op: str) -> list[Any]:
    return [entry for entry in env.audit.read() if entry.op == op]


def yes(_prompt: str) -> bool:
    return True


def no(_prompt: str) -> bool:
    return False


def crash_at(step: str) -> Callable[[str], None]:
    """A hook that crashes the body right after journal step `step` is recorded."""

    def hook(name: str) -> None:
        if name == step:
            raise SimulatedCrash(step)

    return hook


def error_at(step: str) -> Callable[[str], None]:
    """A hook that fails the body with a GinnError right after journal step `step`."""

    def hook(name: str) -> None:
        if name == step:
            raise make("GINN-E-INTERNAL")

    return hook


def kill_journal_owner(env: OpEnv, name: str) -> None:
    """Make the journal entry of `name` look dead: its pid names a process that has exited.

    The pid and start time are those of a child process that was run and reaped, the same way the
    recovery tests model a killed operation.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    start = process_start_ticks(proc.pid)
    proc.wait()
    assert start is not None
    path = env.paths.journal / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pid"] = proc.pid
    data["pid_start"] = start
    path.write_text(json.dumps(data), encoding="utf-8")


def assert_gone(env: OpEnv, fake: FakePodman, name: str, image: str = DIGEST) -> None:
    assert not realm_exists(env, name)
    assert containers_of(fake, name) == []
    assert image not in fake.images


@pytest.mark.fr("FR-019")
def test_delete_a_stopped_realm_removes_its_container_image_and_record_but_keeps_the_home(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)

    result = delete_realm(env, fake, RealmName("lab"))

    assert (result.home, result.purge_declined, result.image) == ("kept", False, "removed")
    assert result.notes == ()
    assert_gone(env, fake, "lab")
    assert (env.paths.homes / "lab" / "notes.txt").read_text(encoding="utf-8") == "keep me\n"
    assert env.journal.pending() == []


@pytest.mark.fr("FR-019")
def test_delete_a_running_realm_stops_its_container_before_removing_it(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    (cid,) = containers_of(fake, "lab")

    delete_realm(env, fake, RealmName("lab"), hooks=DeleteHooks(stop_timeout_s=7))

    assert fake.ops().index("stop") < fake.ops().index("rm")
    assert ("stop", (cid, 7)) in fake.calls
    assert ("rm", (cid, True)) in fake.calls
    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-067")
@pytest.mark.fr("FR-019")
def test_deleting_a_running_development_realm_leaves_the_filter_and_forgets_its_proxy(
    env: OpEnv, fake: FakePodman, paths: OwnedPaths
) -> None:
    """A delete is the last word for a realm: its filter member goes, and its proxy files too."""
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    write_json_atomic(agent_state_path(paths), agent.AgentRecord(members=("lab",)).to_dict())
    proxy = proxy_state_path(paths, RealmName("lab"))
    proxy.parent.mkdir(parents=True, exist_ok=True)
    proxy.write_text(json.dumps({"realm": "lab", "apps": [], "proxy": None}), encoding="utf-8")

    delete_realm(env, fake, RealmName("lab"))

    assert read_agent_record(paths).members == ()
    assert not proxy.exists()
    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-019")
def test_a_failed_proxy_release_keeps_the_realm_deleting_until_the_next_delete(
    env: OpEnv, fake: FakePodman, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    real = delete_module.forget_proxy
    calls: list[str] = []

    def flaky(op_env: OpEnv, name: RealmName) -> None:
        calls.append(name.value)
        if len(calls) == 1:
            raise make("GINN-E-DBUS-PROXY", realm=name.value, detail="held by another command")
        real(op_env, name)

    monkeypatch.setattr(delete_module, "forget_proxy", flaky)
    with pytest.raises(GinnError):
        delete_realm(env, fake, RealmName("lab"))
    assert RealmStore(env.paths, env.clock).load(RealmName("lab")).state is RealmState.DELETING

    delete_realm(env, fake, RealmName("lab"))
    assert calls == ["lab", "lab"]
    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-020")
@pytest.mark.parametrize(
    ("state", "running"),
    [
        (RealmState.STOPPED, False),
        (RealmState.RUNNING, True),
        (RealmState.FAILED, True),
        (RealmState.BROKEN, False),
        (RealmState.DELETING, False),
    ],
)
def test_every_state_dr020_allows_to_delete_is_accepted(
    env: OpEnv, fake: FakePodman, state: RealmState, running: bool
) -> None:
    make_realm(env, fake, "lab", state, running=running)

    delete_realm(env, fake, RealmName("lab"))

    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-020")
def test_a_creating_realm_is_refused_inside_the_journal_and_audited_and_left_as_it_is(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.CREATING)
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"))

    assert caught.value.code == "GINN-E-REALM-STATE"
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert fake.ops() == []
    (entry,) = audit_of(env, "delete")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-REALM-STATE")
    assert env.journal.pending() == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("AS-014")
def test_purge_home_with_confirmation_removes_the_realm_home(env: OpEnv, fake: FakePodman) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    prompts: list[str] = []

    def ask(prompt: str) -> bool:
        prompts.append(prompt)
        return True

    result = delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=ask)

    assert (result.home, result.purge_declined) == ("removed", False)
    assert not (env.paths.homes / "lab").exists()
    assert len(prompts) == 1 and str(env.paths.homes / "lab") in prompts[0]
    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-019")
@pytest.mark.fr("AS-014")
@pytest.mark.parametrize("confirm", [no, None], ids=["declined", "no-hook"])
def test_purge_home_without_confirmation_keeps_the_home_and_reports_it(
    env: OpEnv, fake: FakePodman, confirm: Callable[[str], bool] | None
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)

    result = delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=confirm)

    assert (result.home, result.purge_declined) == ("kept", True)
    assert (env.paths.homes / "lab" / "notes.txt").exists()
    assert_gone(env, fake, "lab")
    (entry,) = audit_of(env, "delete")
    assert entry.params["purge_home"] is False
    assert entry.params["purge_declined"] is True
    assert entry.params["home"] == "kept"


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
@pytest.mark.parametrize("kind", ["link", "file"])
def test_purge_refuses_a_realm_home_that_is_a_link_or_not_a_directory_before_any_write(
    env: OpEnv, fake: FakePodman, tmp_path: Path, kind: str
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, home=False)
    home = env.paths.homes / "lab"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("not ours\n", encoding="utf-8")
    if kind == "link":
        home.symlink_to(outside, target_is_directory=True)
    else:
        home.write_text("a file\n", encoding="utf-8")
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes)

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert caught.value.exit_code == 77
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "not ours\n"
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert fake.ops() == []
    (entry,) = audit_of(env, "delete")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-HOME-UNSAFE")


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_a_realm_home_link_is_left_alone_when_the_home_is_kept(
    env: OpEnv, fake: FakePodman, tmp_path: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED, home=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    (env.paths.homes / "lab").symlink_to(outside, target_is_directory=True)

    result = delete_realm(env, fake, RealmName("lab"))

    assert result.home == "kept"
    assert (env.paths.homes / "lab").is_symlink()
    assert_gone(env, fake, "lab")


def _with_dev(st: os.stat_result, dev: int) -> os.stat_result:
    """A stat with another device number, to stand in for a mount point."""
    return os.stat_result(
        (
            st.st_mode,
            st.st_ino,
            dev,
            st.st_nlink,
            st.st_uid,
            st.st_gid,
            st.st_size,
            int(st.st_atime),
            int(st.st_mtime),
            int(st.st_ctime),
        )
    )


def _entry_on_another_device(monkeypatch: pytest.MonkeyPatch, entry_name: str) -> None:
    """Make the entry `entry_name` report another st_dev. A bind mount from the same file system
    does not report another st_dev: it keeps the device and gets its own mount id. The mount-table
    and mount-id tests below cover that case."""
    real = delete_module._listing

    def listing(
        name: RealmName, home: Path, dir_fd: int, path: Path
    ) -> list[tuple[str, os.stat_result]]:
        return [
            (child, _with_dev(st, st.st_dev + 1) if child == entry_name else st)
            for child, st in real(name, home, dir_fd, path)
        ]

    monkeypatch.setattr(delete_module, "_listing", listing)


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_an_entry_on_another_device_refuses_the_purge_before_the_record_moves(
    env: OpEnv, fake: FakePodman, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    mounted = env.paths.homes / "lab" / "mnt"
    mounted.mkdir()
    (mounted / "data.txt").write_text("another device\n", encoding="utf-8")
    _entry_on_another_device(monkeypatch, "mnt")
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes)

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert "mount point" in caught.value.message
    assert caught.value.details["filename"] == "mnt"
    assert (mounted / "data.txt").read_text(encoding="utf-8") == "another device\n"
    assert (env.paths.homes / "lab" / "notes.txt").exists()
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert fake.ops() == []
    (entry,) = audit_of(env, "delete")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-HOME-UNSAFE")


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_the_removal_walk_stops_at_the_first_entry_on_another_device(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    home = env.paths.homes / "lab"
    home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        other_device = os.fstat(home_fd).st_dev + 1
        ctx = delete_module._Walk(RealmName("lab"), home, other_device, 0, DeleteHooks())
        with pytest.raises(GinnError) as caught:
            delete_module._remove_contents(ctx, home_fd, home, 0)
    finally:
        os.close(home_fd)

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert (home / "notes.txt").read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_an_owned_read_only_subdirectory_is_emptied_and_removed(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    locked = env.paths.homes / "lab" / "locked"
    locked.mkdir()
    (locked / "file.txt").write_text("read only\n", encoding="utf-8")
    locked.chmod(0o555)
    try:
        result = delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes)
    finally:
        if locked.exists():
            locked.chmod(0o755)

    assert result.home == "removed"
    assert not (env.paths.homes / "lab").exists()
    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_an_inner_symlink_is_removed_and_its_target_survives_the_purge(
    env: OpEnv, fake: FakePodman, tmp_path: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("not ours\n", encoding="utf-8")
    (env.paths.homes / "lab" / "link").symlink_to(outside, target_is_directory=True)
    (env.paths.homes / "lab" / "file-link").symlink_to(outside / "precious.txt")

    result = delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes)

    assert result.home == "removed"
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "not ours\n"
    assert not (env.paths.homes / "lab").exists()


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_an_entry_the_user_cannot_remove_stops_the_purge_and_names_the_path(
    env: OpEnv, fake: FakePodman, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A subordinate uid owns this directory in a real realm. Here the test user is not its owner,
    # so ginn may not make it writable, and its entries stay. The test runs as the test user.
    make_realm(env, fake, "lab", RealmState.STOPPED)
    subuid = env.paths.homes / "lab" / "subuid"
    subuid.mkdir()
    (subuid / "root-owned.txt").write_text("not ours\n", encoding="utf-8")
    subuid.chmod(0o555)
    monkeypatch.setattr(delete_module, "_ours", lambda st: False)
    try:
        with pytest.raises(GinnError) as caught:
            delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes)
    finally:
        subuid.chmod(0o755)

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert caught.value.details["filename"] == "root-owned.txt"
    assert "findmnt -R" in caught.value.remedy
    assert "podman unshare rm -rf --one-file-system" in caught.value.remedy
    assert (subuid / "root-owned.txt").read_text(encoding="utf-8") == "not ours\n"
    assert stored(env, "lab").state is RealmState.DELETING
    (entry,) = audit_of(env, "delete")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-HOME-UNSAFE")
    assert env.journal.pending() == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_a_homes_directory_that_is_a_link_refuses_the_purge_before_any_write(
    env: OpEnv, fake: FakePodman, tmp_path: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    moved = tmp_path / "moved-homes"
    env.paths.homes.rename(moved)
    env.paths.homes.symlink_to(moved, target_is_directory=True)
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes)

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert "symbolic link" in caught.value.message
    assert (moved / "lab" / "notes.txt").exists()
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert fake.ops() == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-124")
def test_a_failed_rmi_keeps_the_image_and_the_delete_goes_on(env: OpEnv, fake: FakePodman) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    fake.fail_ops = {"rmi": "GINN-E-INTERNAL"}

    result = delete_realm(env, fake, RealmName("lab"))

    assert (result.image, result.image_code) == ("kept", "GINN-E-INTERNAL")
    assert not realm_exists(env, "lab")
    assert DIGEST in fake.images
    (entry,) = audit_of(env, "delete")
    assert (entry.outcome, entry.error_code) == ("ok", None)
    assert entry.params["image"] == "kept"
    assert entry.params["image_error"] == "GINN-E-INTERNAL"


@pytest.mark.fr("FR-019")
@pytest.mark.parametrize("form", ["reference", "digest", "image-id", "name"])
def test_a_container_that_shows_the_image_in_another_form_keeps_it(
    env: OpEnv, fake: FakePodman, form: str
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    make_realm(env, fake, "other", RealmState.STOPPED)
    shown = {
        "reference": DIGEST,
        "digest": DIGEST.partition("@")[2],
        "image-id": fake.images[DIGEST].id,
        "name": DIGEST.partition("@")[0],
    }[form]
    other = stored(env, "other").container_id
    assert other is not None
    fake.containers[other].image = shown

    result = delete_realm(env, fake, RealmName("lab"))

    assert DIGEST in fake.images
    assert result.image == "kept"
    assert any(f"container {other[:12]} uses it" in note for note in result.notes)


def _mount_id_with_bind_at(name: str) -> Callable[[int], int]:
    """A mount id reader where the directory `name` sits on another mount of the same device."""
    real = delete_module.mount_id_of_fd

    def mount_id(fd: int) -> int:
        target = os.readlink(f"/proc/self/fd/{fd}")
        return real(fd) + (1 if os.path.basename(target) == name else 0)

    return mount_id


def _mount_table(path: Path, *points: str) -> Path:
    """A mount table (proc(5) mountinfo layout) with one entry per mount point in `points`."""
    lines = [f"{30 + i} 1 98:0 / {point} rw - ext4 /dev/root rw" for i, point in enumerate(points)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_a_bind_mount_from_the_same_file_system_is_refused_by_its_mount_id(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    mounted = env.paths.homes / "lab" / "mnt"
    mounted.mkdir()
    (mounted / "data.txt").write_text("bind mounted\n", encoding="utf-8")
    hooks = DeleteHooks(mount_id=_mount_id_with_bind_at("mnt"))
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes, hooks=hooks)

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert "mount point" in caught.value.message
    assert caught.value.details["filename"] == "mnt"
    assert "unmount" in caught.value.remedy
    assert "podman unshare" not in caught.value.remedy
    assert (mounted / "data.txt").read_text(encoding="utf-8") == "bind mounted\n"
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert fake.ops() == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_a_mount_that_appears_after_the_check_stops_the_removal_before_it_enters(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    mounted = env.paths.homes / "lab" / "mnt"
    mounted.mkdir()
    (mounted / "data.txt").write_text("bind mounted\n", encoding="utf-8")
    hooks = DeleteHooks(mount_id=_mount_id_with_bind_at("mnt"))

    # The check did not see the mount, so the removal alone must stop it.
    with pytest.raises(GinnError) as caught:
        delete_module._remove_home(env, RealmName("lab"), hooks)

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert caught.value.details["filename"] == "mnt"
    assert (mounted / "data.txt").read_text(encoding="utf-8") == "bind mounted\n"


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
@pytest.mark.parametrize("entry", ["home", "below"])
def test_a_mount_table_entry_at_or_below_the_home_refuses_the_purge(
    env: OpEnv, fake: FakePodman, tmp_path: Path, entry: str
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    home = os.path.realpath(env.paths.homes / "lab")
    point = home if entry == "home" else home + "/mnt"
    table = _mount_table(tmp_path / "mountinfo", point)
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(
            env,
            fake,
            RealmName("lab"),
            purge_home=True,
            confirm=yes,
            hooks=DeleteHooks(mountinfo=table),
        )

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert "mount point" in caught.value.message
    assert caught.value.details["filename"] == point
    assert "unmount" in caught.value.remedy
    assert (env.paths.homes / "lab" / "notes.txt").exists()
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert fake.ops() == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_an_unreadable_mount_table_fails_closed_and_nothing_is_removed(
    env: OpEnv, fake: FakePodman, tmp_path: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    hooks = DeleteHooks(mountinfo=tmp_path / "no-such-mountinfo")

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes, hooks=hooks)

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert "mount table cannot be read" in caught.value.message
    assert (env.paths.homes / "lab" / "notes.txt").exists()
    assert fake.ops() == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_an_unreadable_mount_id_fails_closed_and_nothing_is_removed(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)

    def unreadable(fd: int) -> int:
        raise OSError(2, "no mnt_id line")

    with pytest.raises(GinnError) as caught:
        delete_realm(
            env,
            fake,
            RealmName("lab"),
            purge_home=True,
            confirm=yes,
            hooks=DeleteHooks(mount_id=unreadable),
        )

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert "mount id cannot be read" in caught.value.message
    assert (env.paths.homes / "lab" / "notes.txt").exists()
    assert fake.ops() == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_a_tree_deeper_than_the_limit_is_refused_before_the_record_moves(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    deep = env.paths.homes / "lab" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "file.txt").write_text("deep\n", encoding="utf-8")
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(
            env,
            fake,
            RealmName("lab"),
            purge_home=True,
            confirm=yes,
            hooks=DeleteHooks(max_depth=2),
        )

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert "too deep" in caught.value.message
    assert (deep / "file.txt").read_text(encoding="utf-8") == "deep\n"
    assert (env.paths.realms / "lab.json").read_bytes() == before


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_an_owned_unreadable_directory_is_repaired_and_removed(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    closed = env.paths.homes / "lab" / "closed"
    closed.mkdir()
    (closed / "inside.txt").write_text("behind a closed door\n", encoding="utf-8")
    closed.chmod(0o000)
    try:
        result = delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes)
    finally:
        if closed.exists():
            closed.chmod(0o755)

    assert result.home == "removed"
    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-124")
def test_a_container_that_shows_no_image_name_is_skipped_and_the_note_says_so(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    make_realm(env, fake, "other", RealmState.STOPPED)
    other = stored(env, "other").container_id
    assert other is not None
    fake.containers[other].image = ""

    result = delete_realm(env, fake, RealmName("lab"))

    assert (result.image, result.image_code) == ("removed", None)
    assert any(f"container {other[:12]} shows no image name" in note for note in result.notes)


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_a_home_on_another_mount_than_the_homes_directory_is_refused(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    hooks = DeleteHooks(mount_id=_mount_id_with_bind_at("lab"))
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes, hooks=hooks)

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert "mount point" in caught.value.message
    assert caught.value.details["filename"] == "lab"
    assert "unmount" in caught.value.remedy
    assert (env.paths.homes / "lab" / "notes.txt").exists()
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert fake.ops() == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_a_repair_on_another_mount_is_refused_before_any_chmod(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    closed = env.paths.homes / "lab" / "closed"
    closed.mkdir()
    (closed / "inside.txt").write_text("behind a closed door\n", encoding="utf-8")
    closed.chmod(0o000)
    hooks = DeleteHooks(mount_id=_mount_id_with_bind_at("closed"))
    try:
        with pytest.raises(GinnError) as caught:
            delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes, hooks=hooks)
        mode_after = closed.stat().st_mode & 0o777
    finally:
        closed.chmod(0o755)

    assert caught.value.code == "GINN-E-HOME-UNSAFE"
    assert "mount point" in caught.value.message
    assert mode_after == 0o000
    assert stored(env, "lab").state is RealmState.DELETING


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_the_mount_table_reader_undoes_the_octal_escapes(tmp_path: Path) -> None:
    table = tmp_path / "mountinfo"
    table.write_bytes(
        b"36 35 98:0 / /plain rw - ext4 /dev/root rw\n"
        b"37 35 98:0 / /mnt/with\\040space rw - ext4 /dev/root rw\n"
        b"38 35 98:0 / /mnt/tab\\011here rw - ext4 /dev/root rw\n"
        b"39 35 98:0 / /mnt/back\\134slash rw - ext4 /dev/root rw\n"
    )

    assert delete_module._mountinfo_points(table) == [
        "/plain",
        "/mnt/with space",
        "/mnt/tab\there",
        "/mnt/back\\slash",
    ]


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-021")
def test_the_mount_table_reader_skips_a_short_line_and_raises_when_unreadable(
    tmp_path: Path,
) -> None:
    table = tmp_path / "mountinfo"
    table.write_bytes(b"1 2 3\n36 35 98:0 / /kept rw - ext4 /dev/root rw\n")

    assert delete_module._mountinfo_points(table) == ["/kept"]
    with pytest.raises(OSError):
        delete_module._mountinfo_points(tmp_path / "missing")


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-124")
def test_a_failed_graceful_stop_is_noted_and_the_container_is_removed_anyway(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    (cid,) = containers_of(fake, "lab")
    fake.fail_ops = {"stop": "GINN-E-INTERNAL"}

    result = delete_realm(env, fake, RealmName("lab"))

    assert len(result.notes) == 1
    assert "GINN-E-INTERNAL" in result.notes[0]
    assert ("rm", (cid, True)) in fake.calls
    assert_gone(env, fake, "lab")
    (entry,) = audit_of(env, "delete")
    assert (entry.outcome, entry.error_code) == ("ok", None)
    assert entry.params["notes"] == list(result.notes)


@pytest.mark.fr("FR-019")
@pytest.mark.fr("DR-010")
def test_a_container_listed_under_another_realm_label_is_never_touched(
    env: OpEnv, fake: FakePodman, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    foreign = ContainerInfo(
        id="ab" * 32,
        name="ginn-other-g1",
        image="elsewhere/other@sha256:" + "ff" * 32,
        state=ContainerState.RUNNING,
        pid=1,
        exit_code=0,
        started_at="",
        finished_at="",
        labels={LABEL_REALM: "other"},
    )
    real_ps = fake.ps

    def listing(label_filter: Mapping[str, str] | None = None) -> list[ContainerInfo]:
        return [*real_ps(label_filter), foreign]

    monkeypatch.setattr(fake, "ps", listing)

    delete_realm(env, fake, RealmName("lab"))

    assert not [c for c in fake.calls if c.op in ("stop", "rm") and c.args[0] == foreign.id]


@pytest.mark.fr("FR-019")
def test_the_journal_steps_are_the_listed_ones_in_order(env: OpEnv, fake: FakePodman) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    seen: list[str] = []

    delete_realm(env, fake, RealmName("lab"), hooks=DeleteHooks(on_step=seen.append))

    assert tuple(seen) == STEPS
    assert STEPS == (
        "deleting",
        "stopped",
        "container-removed",
        "service-removed",
        "exports-removed",
        "image-removed",
        "home",
        "record-removed",
    )


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-122")
@pytest.mark.parametrize("step", STEPS)
def test_a_killed_delete_is_settled_by_recovery_and_a_new_delete_finishes_it(
    env: OpEnv, fake: FakePodman, step: str
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    with pytest.raises(SimulatedCrash):
        delete_realm(
            env,
            fake,
            RealmName("lab"),
            purge_home=True,
            confirm=yes,
            hooks=DeleteHooks(on_step=crash_at(step)),
        )
    (pending,) = env.journal.pending()
    assert pending.step == step
    kill_journal_owner(env, "lab")

    (outcome,) = recover(env, fake)

    assert outcome.outcome == OUTCOME_RECOVERED
    assert env.journal.pending() == []
    if realm_exists(env, "lab"):
        assert stored(env, "lab").state is RealmState.DELETING
        delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes)
        assert audit_of(env, "delete")[-1].outcome == "ok"
    assert_gone(env, fake, "lab")
    assert not (env.paths.homes / "lab").exists()


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-122")
@pytest.mark.parametrize("step", STEPS)
def test_a_delete_that_fails_with_a_ginn_error_at_each_step_is_finished_by_the_next_delete(
    env: OpEnv, fake: FakePodman, step: str
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    with pytest.raises(GinnError) as caught:
        delete_realm(
            env,
            fake,
            RealmName("lab"),
            purge_home=True,
            confirm=yes,
            hooks=DeleteHooks(on_step=error_at(step)),
        )
    assert caught.value.code == "GINN-E-INTERNAL"
    assert env.journal.pending() == []
    assert audit_of(env, "delete")[-1].error_code == "GINN-E-INTERNAL"

    if realm_exists(env, "lab"):
        assert stored(env, "lab").state is RealmState.DELETING
        delete_realm(env, fake, RealmName("lab"), purge_home=True, confirm=yes)

    assert_gone(env, fake, "lab")
    assert not (env.paths.homes / "lab").exists()


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-019")
def test_a_deleting_record_with_a_leftover_journal_file_is_accepted_only_after_recovery(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    with pytest.raises(SimulatedCrash):
        delete_realm(env, fake, RealmName("lab"), hooks=DeleteHooks(on_step=crash_at("deleting")))
    assert stored(env, "lab").state is RealmState.DELETING

    with pytest.raises(GinnError) as refused:
        delete_realm(env, fake, RealmName("lab"))
    assert refused.value.code == "GINN-E-RECOVERY-PENDING"
    assert stored(env, "lab").state is RealmState.DELETING

    kill_journal_owner(env, "lab")
    recover(env, fake)
    delete_realm(env, fake, RealmName("lab"))

    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-019")
def test_a_shared_image_is_kept_while_another_realm_uses_it(env: OpEnv, fake: FakePodman) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    make_realm(env, fake, "other", RealmState.STOPPED)

    delete_realm(env, fake, RealmName("lab"))

    assert DIGEST in fake.images
    assert containers_of(fake, "other") != []

    delete_realm(env, fake, RealmName("other"))

    assert DIGEST not in fake.images


@pytest.mark.fr("FR-019")
@pytest.mark.parametrize("container", ["absent", "no-id"])
def test_an_absent_container_is_tolerated(env: OpEnv, fake: FakePodman, container: str) -> None:
    if container == "absent":
        record = make_realm(env, fake, "lab", RealmState.STOPPED)
        assert record.container_id is not None
        del fake.containers[record.container_id]
    else:
        make_realm(env, fake, "lab", RealmState.RUNNING, container=False)

    delete_realm(env, fake, RealmName("lab"))

    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-124")
def test_a_successful_delete_writes_one_ok_audit_entry_and_no_pending_journal(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)

    delete_realm(env, fake, RealmName("lab"))

    (entry,) = audit_of(env, "delete")
    assert (entry.realm, entry.outcome, entry.error_code) == ("lab", "ok", None)
    assert entry.params["home"] == "kept"
    assert env.journal.pending() == []


@pytest.mark.fr("FR-124")
@pytest.mark.fr("FR-019")
def test_a_podman_error_is_audited_and_the_next_delete_finishes_the_realm(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    fake.fail_ops = {"rm": "GINN-E-INTERNAL"}

    with pytest.raises(GinnError):
        delete_realm(env, fake, RealmName("lab"))

    (entry,) = audit_of(env, "delete")
    assert (entry.outcome, entry.error_code) == ("error", "GINN-E-INTERNAL")
    assert env.journal.pending() == []
    assert stored(env, "lab").state is RealmState.DELETING

    delete_realm(env, fake, RealmName("lab"))

    assert_gone(env, fake, "lab")
    assert [e.outcome for e in audit_of(env, "delete")] == ["error", "ok"]


@pytest.mark.fr("FR-025")
@pytest.mark.fr("FR-019")
def test_delete_of_an_unknown_realm_is_refused_before_any_write(
    env: OpEnv, fake: FakePodman
) -> None:
    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("ghost"))

    assert caught.value.code == "GINN-E-NO-SUCH-REALM"
    assert caught.value.exit_code == 3
    assert fake.calls == []
    assert list(env.audit.read()) == []
    assert env.journal.pending() == []


@pytest.mark.fr("FR-025")
def test_another_users_realm_is_not_visible_to_delete(
    env: OpEnv, fake: FakePodman, tmp_path: Path, host: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    bob_runtime = tmp_path / "bob-runtime"
    bob_runtime.mkdir(mode=0o700)
    bob_env_vars = {
        "HOME": str(tmp_path / "bob-home"),
        "XDG_DATA_HOME": str(tmp_path / "bob-data"),
        "XDG_CONFIG_HOME": str(tmp_path / "bob-config"),
        "XDG_STATE_HOME": str(tmp_path / "bob-state"),
        "GINN_RUNTIME_DIR": str(bob_runtime),
    }
    bob_paths = OwnedPaths.from_env(bob_env_vars, uid=os.getuid())
    bob_paths.ensure()
    bob = OpEnv.for_paths(bob_paths, "bob", root_dir=host, clock=FakeClock())

    with pytest.raises(GinnError) as caught:
        delete_realm(bob, fake, RealmName("lab"))

    assert caught.value.code == "GINN-E-NO-SUCH-REALM"
    assert realm_exists(env, "lab")
    assert containers_of(fake, "lab") != []


class SvStub:
    """Stands in for sv in the delete tests: it records each argv and answers `sv -w down`.

    `on_down` is the action of the supervised process (finish records `stopped`), and `reply`
    replaces the answer, so a test can make sv fail or time out.
    """

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.on_down: Callable[[], None] | None = None
        self.reply: Result | None = None
        self.lock_during_down: list[str] = []
        self.ops_at_down: list[str] = []
        self.fake: FakePodman | None = None
        self.env: OpEnv | None = None

    def run(self, argv: Sequence[str], timeout: float) -> Result:
        self.argvs.append(list(argv))
        if self.reply is not None:
            return self.reply
        if self.fake is not None:
            self.ops_at_down = list(self.fake.ops())
        if self.env is not None:
            self.lock_during_down.append(_try_lock(self.env))
        if self.on_down is not None:
            self.on_down()
        return Result(0, "ok: down: x: 0s, normally up\n", "")


def _try_lock(env: OpEnv) -> str:
    """Take the realm lock as a probe operation: "free" when no other operation holds it."""
    try:
        with mutating(env, "probe", RealmName("lab"), {}):
            return "free"
    except GinnError:
        return "busy"


def _supervised_services_realm(
    env: OpEnv, fake: FakePodman, stub: SvStub, state: RealmState = RealmState.RUNNING
) -> DeleteHooks:
    """A services-on realm in `state` with its container running, its runit service, and runsv's
    supervise/ok FIFO."""
    previous = RealmState.RUNNING if state in (RealmState.UPDATING, RealmState.REBUILDING) else None
    make_realm(env, fake, "lab", state, running=True, services=True, previous=previous)
    write_service_dir(env.paths, RealmName("lab"), "/usr/bin/ginn")
    supervise = service_dir(env.paths, RealmName("lab")) / "supervise"
    supervise.mkdir(exist_ok=True)
    os.mkfifo(supervise / "ok")
    stub.fake = fake
    stub.env = env
    return DeleteHooks(
        supervisor=SupervisorHooks(
            sv="/usr/bin/sv", run=stub.run, clock=FakeClock(), sleep=lambda _s: None
        )
    )


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-081")
def test_a_running_services_on_realm_is_stopped_through_sv_before_its_removal(
    env: OpEnv, fake: FakePodman
) -> None:
    stub = SvStub()
    hooks = _supervised_services_realm(env, fake, stub)
    cid = containers_of(fake, "lab")[0]

    def supervised_stop() -> None:
        fake.stop(cid, 30)
        RealmStore(env.paths, env.clock).set_state(RealmName("lab"), RealmState.STOPPED)

    stub.on_down = supervised_stop

    result = delete_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert result.image == "removed"
    assert_gone(env, fake, "lab")
    assert stub.argvs == [
        [
            "/usr/bin/sv",
            "-w",
            str(supervisor.DOWN_WAIT_S),
            "down",
            str(service_dir(env.paths, RealmName("lab"))),
        ]
    ]
    assert stub.lock_during_down == ["free"], "sv down runs before the delete takes the lock"
    assert "rm" not in stub.ops_at_down, "runit is down before the container is removed"
    assert not service_dir(env.paths, RealmName("lab")).exists()


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-081")
def test_a_services_on_delete_goes_on_with_podman_when_no_supervisor_answers(
    env: OpEnv, fake: FakePodman
) -> None:
    stub = SvStub()
    hooks = _supervised_services_realm(env, fake, stub)
    stub.reply = Result(1, "", "fail: x: runsv not running\n")

    result = delete_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert result.image == "removed"
    assert_gone(env, fake, "lab")
    assert "stop" in fake.ops(), "podman stops the container when no runsv answers"


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-081")
def test_a_services_on_delete_whose_stop_times_out_writes_nothing(
    env: OpEnv, fake: FakePodman
) -> None:
    stub = SvStub()
    hooks = _supervised_services_realm(env, fake, stub)
    stub.reply = Result(1, "", "timeout: run: x: (pid 4) 60s, want down, got TERM\n")
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert caught.value.code == "GINN-E-SERVICE-TIMEOUT"
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert fake.ops() == []
    assert [entry.op for entry in env.audit.read() if entry.op != "integrity"] == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-081")
def test_a_running_services_on_realm_is_not_refused_by_the_purge_check(
    env: OpEnv, fake: FakePodman
) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True, services=True)
    before = (env.paths.realms / "lab.json").read_bytes()

    check_purge(env, RealmName("lab"))

    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert fake.ops() == []


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-080")
def test_a_stopped_services_on_realm_is_deleted_like_any_other(
    env: OpEnv, fake: FakePodman
) -> None:
    """A services-on create leaves its container stopped, so nothing is supervised (ADR-003)."""
    make_realm(env, fake, "lab", RealmState.STOPPED, running=False, services=True)

    result = delete_realm(env, fake, RealmName("lab"))

    assert result.image == "removed"
    assert_gone(env, fake, "lab")
    assert "stop" not in fake.ops()
    assert "rm" in fake.ops()


@pytest.mark.fr("FR-019")
def test_a_paused_container_is_stopped_before_removal(env: OpEnv, fake: FakePodman) -> None:
    make_realm(env, fake, "lab", RealmState.RUNNING, running=True)
    (cid,) = containers_of(fake, "lab")
    fake.containers[cid].state = ContainerState.PAUSED

    delete_realm(env, fake, RealmName("lab"))

    assert ("stop", (cid, 30)) in fake.calls
    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-006")
def test_a_host_change_during_a_delete_is_on_the_result_as_a_warning(
    env: OpEnv, fake: FakePodman, host: Path
) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)

    def change_hosts_after_stop(step: str) -> None:
        if step == "stopped":
            (host / "etc" / "hosts").write_text("10.0.0.1 changed\n", encoding="utf-8")

    result = delete_realm(
        env, fake, RealmName("lab"), hooks=DeleteHooks(on_step=change_hosts_after_stop)
    )

    assert [warning.code for warning in result.warnings] == ["GINN-W-HOST-CHANGED"]
    assert "/etc/hosts" in result.warnings[0].message
    assert result.home == "kept"
    assert_gone(env, fake, "lab")


@pytest.mark.fr("FR-006")
def test_a_clean_delete_has_no_warnings(env: OpEnv, fake: FakePodman) -> None:
    make_realm(env, fake, "lab", RealmState.STOPPED)
    assert delete_realm(env, fake, RealmName("lab")).warnings == ()


@pytest.mark.fr("FR-081")
@pytest.mark.fr("DR-020")
def test_a_services_on_delete_of_an_updating_realm_sends_no_sv_command_and_writes_nothing(
    env: OpEnv, fake: FakePodman
) -> None:
    """An sv down would stop the container under an upgrade, so the state is refused first."""
    stub = SvStub()
    hooks = _supervised_services_realm(env, fake, stub, state=RealmState.UPDATING)
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert caught.value.code == "GINN-E-REALM-STATE"
    assert stub.argvs == []
    assert fake.ops() == []
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert containers_of(fake, "lab") != []


@pytest.mark.fr("FR-081")
def test_a_services_on_delete_whose_sv_control_fails_removes_nothing(
    env: OpEnv, fake: FakePodman
) -> None:
    """runsv may still run the realm when its control FIFO cannot be opened, so the delete does not
    go on with podman: runsv would restart the container it removes."""
    stub = SvStub()
    hooks = _supervised_services_realm(env, fake, stub)
    stub.reply = Result(
        1, "", "warning: x: unable to open supervise/control: file does not exist\n"
    )
    before = (env.paths.realms / "lab.json").read_bytes()

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert caught.value.code == "GINN-E-SERVICE-CONTROL"
    assert "rm" not in fake.ops()
    assert "stop" not in fake.ops()
    assert (env.paths.realms / "lab.json").read_bytes() == before
    assert containers_of(fake, "lab") != []


@pytest.mark.fr("FR-122")
def test_a_services_on_delete_refused_by_an_unrecovered_journal_entry_stops_nothing(
    env: OpEnv, fake: FakePodman
) -> None:
    stub = SvStub()
    hooks = _supervised_services_realm(env, fake, stub)
    env.journal.begin("start", RealmName("lab"), {})

    with pytest.raises(GinnError) as caught:
        delete_realm(env, fake, RealmName("lab"), hooks=hooks)

    assert caught.value.code == "GINN-E-RECOVERY-PENDING"
    assert stub.argvs == []
    assert fake.ops() == []
    assert containers_of(fake, "lab") != []
