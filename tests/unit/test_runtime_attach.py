# SPDX-License-Identifier: Apache-2.0
"""Attach executor: argv, placeholders, skips, retries, identity checks, and the grant record.

Covers runtime/attach.py. The fake podman records every `unshare` and `exec` call, so these tests
never run podman or the helper. `test_unshare_keeps_the_global_flags` runs PodmanCli against a
small script that records its argv and environment; that script is not podman. The helper's own
syscalls are checked in tests/unit/test_native_rebind.py, and on a host by T4.9.

Failure semantics under test: a start failure raises, and the caller stops the realm. A re-attach
failure raises, and the caller keeps the realm running, so those tests check that no stop follows.
"""

import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ginn.domain.grants import GrantRecord, SourceIdentity
from ginn.errors import GinnError
from ginn.podman.adapter import CreateSpec
from ginn.podman.cli import PodmanCli
from ginn.podman.models import ContainerState, ExecResult
from ginn.runtime.attach import (
    DEFAULT_HELPER,
    HELPER_ENV,
    NOT_VISIBLE,
    Admit,
    AttachResult,
    Op,
    admission,
    attach_at_start,
    helper_path,
    make_mountpoints,
    needs_reattach,
    read_identity,
    reattach,
    rebind_argv,
    records_from_plan,
    run_helper,
    share_skipped,
)
from ginn.runtime.env import SessionEnv
from ginn.runtime.mounts import Attach, AttachPlan, Mountpoint, SessionBind, Skipped
from tests.fakes.fake_podman import FakePodman

pytestmark = [
    pytest.mark.fr("FR-061"),
    pytest.mark.fr("FR-064"),
    pytest.mark.fr("FR-066"),
    pytest.mark.fr("IR-025"),
]

HELPER = "/opt/ginn-test/libexec/ginn-rebind"
DIGEST = (
    "docker.io/library/alpine@sha256:"
    "d56c381f961d307a21b3ca004cf1e3910f106644aefb1f43e654c8a56c4fd395"
)
WAYLAND = "/run/user/1000/wayland-0"
X11_DIR = "/tmp/.X11-unix"  # noqa: S108 - the fixed X11 socket directory, not a temp file
PULSE_REALM = "/run/user/1000/pulse/native"
BASE_MOUNTINFO = (
    "36 35 98:0 / / rw,relatime shared:1 - ext4 /dev/root rw\n"
    "37 36 98:1 / /opt rw - ext4 /dev/root rw\n"
)


def allow(grant: GrantRecord) -> str | None:
    """An `admit` that lets every grant through, for tests that are not about the DR-004 rules."""
    return None


def refuse(grant: GrantRecord) -> str | None:
    return "refused by the test"


@dataclass
class _Helper:
    """The helper runner of these tests. It records each argv and answers from a queue, in order.

    Once the queue is empty it answers status 0 with no output. It never runs podman.
    """

    calls: list[tuple[str, ...]] = field(default_factory=list)
    answers: list[ExecResult] = field(default_factory=list)

    def __call__(self, podman: object, argv: Sequence[str]) -> ExecResult:
        del podman
        self.calls.append(tuple(argv))
        return self.answers.pop(0) if self.answers else ExecResult(0, "", "")


@dataclass
class World:
    fake: FakePodman
    cid: str
    pid: int
    host_home: Path
    realm_home: Path
    mountinfo: Path
    src: Path
    helper: _Helper


@pytest.fixture
def world(tmp_path: Path) -> World:
    fake = FakePodman()
    cid = fake.create(CreateSpec(name="ginn-arch-g1", image_digest_ref=DIGEST, network="host"))
    fake.start(cid)
    realm_home = tmp_path / "owned" / "realm-home"
    realm_home.mkdir(parents=True)
    src = tmp_path / "src"
    (src / "Downloads").mkdir(parents=True)
    (src / "wayland").write_text("", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(BASE_MOUNTINFO, encoding="utf-8")
    return World(
        fake=fake,
        cid=cid,
        pid=fake.inspect(cid).pid,
        host_home=tmp_path / "home" / "alice",
        realm_home=realm_home,
        mountinfo=mountinfo,
        src=src,
        helper=_Helper(),
    )


def _folder(w: World, name: str = "Downloads") -> GrantRecord:
    return GrantRecord("share", str(w.src / name), str(w.host_home / name), "rw", True)


def _socket(w: World, name: str = "wayland") -> GrantRecord:
    return GrantRecord("session", str(w.src / name), WAYLAND, "rw", False)


def _session_of(grants: Sequence[GrantRecord]) -> SessionEnv:
    """A session that binds each session grant to its own host, as a session now would."""
    binds = [SessionBind(g.host, g.realm, g.mode, g.is_dir) for g in grants if g.kind == "session"]
    return SessionEnv(env={}, binds=binds)


def _start(
    w: World,
    grants: Sequence[GrantRecord],
    admit: Admit = allow,
    session: SessionEnv | None = None,
) -> AttachResult:
    return attach_at_start(
        w.fake,
        w.cid,
        pid=w.pid,
        grants=grants,
        session=_session_of(grants) if session is None else session,
        realm_home=w.realm_home,
        host_home=w.host_home,
        admit=admit,
        helper=HELPER,
        mountinfo=w.mountinfo,
        run=w.helper,
    )


def _reattach(
    w: World,
    grants: Sequence[GrantRecord],
    admit: Admit = allow,
    session: SessionEnv | None = None,
) -> AttachResult:
    return reattach(
        w.fake,
        w.cid,
        pid=w.pid,
        grants=grants,
        session=_session_of(grants) if session is None else session,
        realm_home=w.realm_home,
        host_home=w.host_home,
        admit=admit,
        helper=HELPER,
        mountinfo=w.mountinfo,
        run=w.helper,
    )


def _rows(*rows: tuple[str, str]) -> str:
    """The helper's stdout for (status, host) rows. An attached row names the host's identity."""
    lines = []
    for index, (status, host) in enumerate(rows):
        if status == "attached":
            st = os.stat(host)
            lines.append(f"{index} attached {st.st_dev} {st.st_ino}")
        else:
            lines.append(f"{index} {status}")
    return "".join(f"{line}\n" for line in lines)


def _queue(w: World, stdout: str, *, status: int = 0, stderr: str = "") -> None:
    w.helper.answers.append(ExecResult(status, stdout, stderr))


def _helper_argvs(w: World) -> list[tuple[str, ...]]:
    return list(w.helper.calls)


def _realm_shows(w: World, points: Sequence[str]) -> None:
    """The realm's mount table for the next `cat /proc/self/mountinfo`, one line per mount point."""
    table = "".join(
        f"{40 + i} 36 8:1 / {point} rw - vfat /dev/sdb1 rw\n" for i, point in enumerate(points)
    )
    w.fake.exec_results[("cat", "/proc/self/mountinfo")] = ExecResult(0, BASE_MOUNTINFO + table, "")


def _exec_argvs(fake: FakePodman) -> list[tuple[str, ...]]:
    return [call.args[1] for call in fake.calls if call.op == "exec"]  # type: ignore[misc]


@pytest.mark.fr("NFR-SEC-005")
def test_rebind_argv_is_the_adr_form_with_the_unmount_group() -> None:
    ops: list[tuple[GrantRecord, Op]] = [
        (GrantRecord("share", "/h/a", "/r/a", "ro", True), "force"),
        (GrantRecord("session", "/h/b", "/r/b", "rw", False), "attach"),
        (GrantRecord("session", "/h/c", "/r/c", "rw", False), "unmount"),
    ]
    assert rebind_argv(HELPER, 4242, ops) == [
        HELPER,
        "4242",
        "--force",
        "--ro",
        "/h/a",
        "/r/a",
        "/h/b",
        "/r/b",
        "--unmount",
        "/r/c",
    ]


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    ("helper", "pid", "host", "realm"),
    [
        (HELPER, 0, "/h", "/r"),
        (HELPER, 1, "/h", "/r"),
        ("ginn-rebind", 4242, "/h", "/r"),
        (HELPER, 4242, "relative", "/r"),
        (HELPER, 4242, "/h", "relative"),
        (HELPER, 4242, "/h/../etc", "/r"),
        (HELPER, 4242, "/h", "/r/.."),
        (HELPER, 4242, "/h", "/r/../x"),
    ],
)
def test_rebind_argv_refuses_bad_input(helper: str, pid: int, host: str, realm: str) -> None:
    grant = GrantRecord("share", host, realm, "rw", True)
    with pytest.raises(ValueError):
        rebind_argv(helper, pid, [(grant, "attach")])


@pytest.mark.fr("NFR-SEC-005")
def test_helper_path_reads_the_override_then_the_install_path() -> None:
    assert helper_path({}) == DEFAULT_HELPER
    assert helper_path({HELPER_ENV: "/x/ginn-rebind"}) == "/x/ginn-rebind"
    with pytest.raises(ValueError, match=HELPER_ENV):
        helper_path({HELPER_ENV: "ginn-rebind"})


@pytest.mark.fr("FR-065")
def test_identity_of_a_folder_covers_the_mounts_below_it(tmp_path: Path) -> None:
    folder = tmp_path / "run-media"
    folder.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(BASE_MOUNTINFO, encoding="utf-8")
    before = read_identity(str(folder), mountinfo=mountinfo)
    assert before is not None and before.below == ()
    # A drive mounted under the folder leaves the folder's own inode unchanged. The octal escape
    # in the mount table stands for a space.
    drive = f"{folder}/my\\040drive"
    mountinfo.write_text(
        BASE_MOUNTINFO + f"40 36 8:1 / {drive} rw - vfat /dev/sdb1 rw\n", encoding="utf-8"
    )
    after = read_identity(str(folder), mountinfo=mountinfo)
    assert after is not None
    assert (after.dev, after.ino) == (before.dev, before.ino)
    assert after.below == (f"{folder}/my drive",)
    assert after != before


@pytest.mark.fr("FR-065")
def test_mounts_below_the_real_path_of_a_linked_folder_count(tmp_path: Path) -> None:
    """The mount table records resolved paths, so the prefix is taken from the resolved folder."""
    real = Path(os.path.realpath(tmp_path)) / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        BASE_MOUNTINFO + f"40 36 8:1 / {real}/drive rw - vfat /dev/sdb1 rw\n", encoding="utf-8"
    )
    identity = read_identity(str(link), mountinfo=mountinfo)
    assert identity is not None
    assert identity.below == (f"{real}/drive",)


@pytest.mark.fr("FR-065")
def test_mount_table_escapes_are_undone_before_the_name_is_decoded(tmp_path: Path) -> None:
    folder = tmp_path / "run"
    folder.mkdir()
    mountinfo = tmp_path / "mountinfo"
    # \303\251 is the UTF-8 for a letter e with an acute accent.
    mountinfo.write_text(
        BASE_MOUNTINFO + f"40 36 8:1 / {folder}/\\303\\251 rw - vfat /dev/sdb1 rw\n",
        encoding="utf-8",
    )
    identity = read_identity(str(folder), mountinfo=mountinfo)
    assert identity is not None
    assert identity.below == (f"{folder}/\u00e9",)


@pytest.mark.fr("FR-064")
def test_identity_of_a_missing_source_is_none(tmp_path: Path) -> None:
    assert read_identity(str(tmp_path / "gone"), mountinfo=tmp_path / "m") is None


@pytest.mark.fr("FR-065")
def test_an_unreadable_mount_table_is_a_failure_not_an_empty_identity(tmp_path: Path) -> None:
    folder = tmp_path / "run"
    folder.mkdir()
    with pytest.raises(GinnError) as caught:
        read_identity(str(folder), mountinfo=tmp_path / "no-such-table")
    assert caught.value.code == "GINN-E-ATTACH-FAILED"


@pytest.mark.fr("FR-066")
def test_records_from_plan_keeps_attached_grants_and_retryable_skips() -> None:
    plan = AttachPlan(
        attach=[Attach("share", "/h/Downloads", "/home/alice/Downloads", "rw")],
        mountpoints=[Mountpoint("/home/alice/Downloads", True, "realm_home")],
        skipped=[
            Skipped(
                "share",
                "~/Projects",
                "missing",
                "/h/Projects",
                "/home/alice/Projects",
                "rw",
                True,
                missing=True,
            ),
            Skipped("share", "/etc", "outside the allowed roots"),
        ],
    )
    wayland = Skipped(
        "session", "WAYLAND_DISPLAY", "socket missing", "/h/wl", WAYLAND, "rw", False, missing=True
    )
    session = SessionEnv(
        env={},
        binds=[],
        skipped=[wayland],
        absent=[SessionBind("/h/pulse", PULSE_REALM)],
    )
    assert records_from_plan(plan, session) == [
        GrantRecord("share", "/h/Downloads", "/home/alice/Downloads", "rw", True),
        GrantRecord("share", "/h/Projects", "/home/alice/Projects", "rw", True),
        GrantRecord("session", "/h/wl", WAYLAND, "rw", False),
        GrantRecord("session", "/h/pulse", PULSE_REALM, "rw", False),
    ]


@pytest.mark.fr("FR-066")
def test_records_from_plan_lists_each_realm_path_once_and_the_attach_wins() -> None:
    plan = AttachPlan(
        attach=[Attach("share", "/h/Downloads", "/home/alice/Downloads", "rw")],
        mountpoints=[Mountpoint("/home/alice/Downloads", True, "realm_home")],
        skipped=[
            Skipped(
                "share",
                "~/Downloads",
                "missing",
                "/h/Downloads",
                "/home/alice/Downloads",
                "rw",
                True,
                missing=True,
            )
        ],
    )
    records = records_from_plan(plan, SessionEnv(env={}, binds=[]))
    assert [record.realm for record in records] == ["/home/alice/Downloads"]


@pytest.mark.fr("FR-084")
def test_an_autostart_with_no_session_variables_still_gets_retryable_slots() -> None:
    plan = AttachPlan(attach=[], mountpoints=[], skipped=[])
    session = SessionEnv(
        env={},
        binds=[],
        absent=[SessionBind("", WAYLAND, "rw", False), SessionBind("", PULSE_REALM, "rw", False)],
    )
    records = records_from_plan(plan, session)
    assert [(r.kind, r.host, r.realm) for r in records] == [
        ("session", "", WAYLAND),
        ("session", "", PULSE_REALM),
    ]


@pytest.mark.fr("FR-064")
def test_share_skipped_lists_plan_and_session_skips_then_new_attach_time_skips_once() -> None:
    plan = AttachPlan(
        attach=[Attach("share", "/h/Downloads", "/home/alice/Downloads", "rw")],
        mountpoints=[Mountpoint("/home/alice/Downloads", True, "realm_home")],
        skipped=[
            Skipped(
                "share",
                "~/Downloads",
                "missing",
                "/h/Downloads",
                "/home/alice/Downloads",
                "rw",
                True,
                missing=True,
            ),
            Skipped(
                "share",
                "~/Projects",
                "missing",
                "/h/Projects",
                "/home/alice/Projects",
                "rw",
                True,
                missing=True,
            ),
            Skipped("share", "/etc", "outside the allowed roots"),
        ],
    )
    wayland = Skipped(
        "session", "WAYLAND_DISPLAY", "socket missing", "/h/wl", WAYLAND, "rw", False, missing=True
    )
    session = SessionEnv(env={}, binds=[], skipped=[wayland])
    result = AttachResult(
        records=[
            GrantRecord(
                "share", "/h/Downloads", "/home/alice/Downloads", "rw", True, SourceIdentity(1, 2)
            ),
            GrantRecord("share", "/h/Projects", "/home/alice/Projects", "rw", True),
            GrantRecord("share", "/h/Music", "/home/alice/Music", "rw", True),
        ],
        skipped=[
            Skipped(
                "share",
                "/h/Projects",
                "missing",
                "/h/Projects",
                "/home/alice/Projects",
                "rw",
                True,
                missing=True,
            ),
            Skipped(
                "share",
                "/h/Music",
                "missing",
                "/h/Music",
                "/home/alice/Music",
                "rw",
                True,
                missing=True,
            ),
        ],
    )
    assert share_skipped(plan, session, result) == [
        "share skipped: ~/Projects (missing)",
        "share skipped: /etc (outside the allowed roots)",
        "session skipped: WAYLAND_DISPLAY (socket missing)",
        "share skipped: /h/Music (missing)",
    ]


@pytest.mark.fr("FR-084")
def test_a_session_slot_that_is_silent_is_never_listed_as_skipped() -> None:
    plan = AttachPlan(attach=[], mountpoints=[], skipped=[])
    session = SessionEnv(env={}, binds=[], absent=[SessionBind("/h/pulse", PULSE_REALM)])
    result = AttachResult(
        records=[GrantRecord("session", "/h/pulse", PULSE_REALM, "rw", False)],
        skipped=[
            Skipped(
                "session", "/h/pulse", "missing", "/h/pulse", PULSE_REALM, "rw", False, missing=True
            )
        ],
    )
    assert share_skipped(plan, session, result) == []


@pytest.mark.fr("FR-064")
def test_a_session_skip_stays_listed_even_when_its_directory_grant_is_attached() -> None:
    """DISPLAY names the X11 socket. Its skip keeps the directory realm path, bound or not."""
    x11_dir = X11_DIR
    plan = AttachPlan(attach=[], mountpoints=[], skipped=[])
    display = Skipped(
        "session",
        "DISPLAY",
        "socket missing",
        f"{X11_DIR}/X0",
        x11_dir,
        "ro",
        True,
        missing=True,
    )
    session = SessionEnv(env={}, binds=[], skipped=[display])
    result = AttachResult(
        records=[GrantRecord("session", X11_DIR, x11_dir, "ro", True, SourceIdentity(1, 2))],
        skipped=[],
    )
    assert share_skipped(plan, session, result) == ["session skipped: DISPLAY (socket missing)"]


@pytest.mark.fr("FR-066")
def test_a_grant_record_round_trips_through_plain_data() -> None:
    attached = GrantRecord(
        "session", "/h/wl", WAYLAND, "ro", False, SourceIdentity(7, 9, ("/h/wl/drive",))
    )
    skipped = GrantRecord("share", "/h/Music", "/home/alice/Music", "rw", True)
    hidden = GrantRecord(
        "share", "/h/Projects", "/home/alice/Projects", "rw", True, None, SourceIdentity(3, 4)
    )
    for record in (attached, skipped, hidden):
        data = json.loads(json.dumps(record.to_dict()))
        assert GrantRecord.from_dict(data) == record


@pytest.mark.fr("FR-066")
@pytest.mark.parametrize(
    "patch",
    [
        {"mode": "xx"},
        {"is_dir": "yes"},
        {"identity": "x"},
        {"identity": {"dev": True, "ino": 1, "below": []}},
        {"identity": {"dev": 1, "ino": 2, "below": [3]}},
        {"host": None},
        {"not_visible": "x"},
    ],
)
def test_a_grant_record_from_bad_data_is_refused(patch: dict[str, object]) -> None:
    data: dict[str, object] = {
        "kind": "share",
        "host": "/h/Music",
        "realm": "/home/alice/Music",
        "mode": "rw",
        "is_dir": True,
        "identity": None,
    }
    data.update(patch)
    with pytest.raises(ValueError):
        GrantRecord.from_dict(data)


@pytest.mark.fr("FR-061")
def test_start_attaches_every_grant_without_force(world: World) -> None:
    folder_host = str(world.src / "Downloads")
    folder_realm = str(world.host_home / "Downloads")
    wayland_host = str(world.src / "wayland")
    _queue(world, _rows(("attached", folder_host), ("attached", wayland_host)))
    result = _start(world, [_folder(world), _socket(world)])
    assert _helper_argvs(world) == [
        (HELPER, str(world.pid), folder_host, folder_realm, wayland_host, WAYLAND)
    ]
    assert all(record.identity is not None for record in result.records)
    assert result.skipped == []
    assert (world.realm_home / "Downloads").is_dir()
    assert ("mkdir", "-p", "--", "/run/user/1000") in _exec_argvs(world.fake)
    assert not [argv for argv in _exec_argvs(world.fake) if argv[0] == "touch"]


@pytest.mark.fr("FR-064")
@pytest.mark.fr("FR-084")
def test_missing_sources_are_skipped_and_the_start_goes_on(world: World) -> None:
    result = _start(world, [_folder(world, "Nope"), _socket(world, "absent")])
    assert [record.identity for record in result.records] == [None, None]
    assert result.skipped == [
        Skipped(
            "share",
            str(world.src / "Nope"),
            "missing",
            str(world.src / "Nope"),
            str(world.host_home / "Nope"),
            "rw",
            True,
            missing=True,
        ),
        Skipped(
            "session",
            str(world.src / "absent"),
            "missing",
            str(world.src / "absent"),
            WAYLAND,
            "rw",
            False,
            missing=True,
        ),
    ]
    assert _helper_argvs(world) == []
    assert _exec_argvs(world.fake) == []


@pytest.mark.fr("FR-064")
def test_a_source_that_vanishes_before_the_helper_runs_is_skipped_not_raised(world: World) -> None:
    _queue(world, "0 missing\n")
    result = _start(world, [_folder(world)])
    assert result.records[0].identity is None
    assert [skip.reason for skip in result.skipped] == ["missing"]


@pytest.mark.fr("FR-061")
def test_a_start_failure_raises_so_the_caller_stops_the_realm(world: World) -> None:
    _queue(world, "", status=1, stderr="ginn-rebind: open_tree /src: Operation not permitted\n")
    with pytest.raises(GinnError) as caught:
        _start(world, [_folder(world)])
    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    assert caught.value.details["exit"] == 1
    assert "Operation not permitted" in str(caught.value.details["detail"])


@pytest.mark.fr("FR-061")
def test_the_helper_stderr_line_it_printed_is_the_detail(world: World) -> None:
    _queue(
        world,
        "",
        status=1,
        stderr="podman: warning: something\nginn-rebind: open_tree /src: No such file\n",
    )
    with pytest.raises(GinnError) as caught:
        _start(world, [_folder(world)])
    assert caught.value.details["detail"] == "ginn-rebind: open_tree /src: No such file"


@pytest.mark.parametrize(
    "stdout",
    [
        "0 attached 1 2\n",
        "1 attached 1 2\n0 attached 1 2\n",
        "0 mounted\n1 attached 1 2\n",
        "0 attached 1 2\n1\n",
        "0 attached 1\n1 missing\n",
    ],
)
@pytest.mark.fr("FR-061")
def test_helper_output_must_match_the_groups(world: World, stdout: str) -> None:
    _queue(world, stdout)
    with pytest.raises(GinnError) as caught:
        _start(world, [_folder(world), _socket(world)])
    assert caught.value.code == "GINN-E-ATTACH-FAILED"


@pytest.mark.fr("FR-064")
def test_at_start_a_device_or_inode_mismatch_is_a_skip_and_the_tree_is_unmounted(
    world: World,
) -> None:
    """The source is not the one the store namespace shows. The start goes on, the mount goes."""
    folder_realm = str(world.host_home / "Downloads")
    _queue(world, "0 attached 1 2\n")
    _queue(world, "0 missing\n")
    result = _start(world, [_folder(world)])
    assert result.records[0].identity is None
    assert result.records[0].not_visible == read_identity(
        str(world.src / "Downloads"), mountinfo=world.mountinfo
    )
    assert [skip.reason for skip in result.skipped] == [NOT_VISIBLE]
    assert _helper_argvs(world)[-1] == (HELPER, str(world.pid), "--unmount", folder_realm)


@pytest.mark.fr("FR-064")
def test_a_failed_unmount_of_a_mismatched_tree_fails_the_start(world: World) -> None:
    _queue(world, "0 attached 1 2\n")
    _queue(world, "0 attached 3 4\n")
    with pytest.raises(GinnError) as caught:
        _start(world, [_folder(world)])
    assert caught.value.code == "GINN-E-ATTACH-FAILED"


@pytest.mark.fr("FR-064")
def test_a_reattach_identity_mismatch_is_a_skip_and_is_unmounted(world: World) -> None:
    folder = world.src / "Downloads"
    folder_realm = str(world.host_home / "Downloads")
    _queue(world, _rows(("attached", str(folder))))
    started = _start(world, [_folder(world)]).records
    os.rename(folder, world.src / "Downloads-old")
    folder.mkdir()  # a new inode, and the store namespace shows a different tree on it
    _queue(world, "0 attached 1 2\n")
    _queue(world, "0 missing\n")
    result = _reattach(world, started)
    assert _helper_argvs(world)[-1] == (HELPER, str(world.pid), "--unmount", folder_realm)
    assert result.records[0].identity is None
    assert result.records[0].not_visible == read_identity(str(folder), mountinfo=world.mountinfo)
    assert [skip.reason for skip in result.skipped] == [NOT_VISIBLE]


@pytest.mark.fr("FR-066")
def test_reattach_passes_only_the_changed_grant_with_force(world: World) -> None:
    (world.src / "Projects").mkdir()
    projects = world.src / "Projects"
    _queue(world, _rows(("attached", str(world.src / "Downloads")), ("attached", str(projects))))
    started = _start(world, [_folder(world, "Downloads"), _folder(world, "Projects")]).records
    os.rename(projects, world.src / "Projects-old")
    projects.mkdir()  # same path, new inode: a changed host source
    _queue(world, _rows(("attached", str(projects))))
    before = len(_helper_argvs(world))
    records = _reattach(world, started).records
    assert _helper_argvs(world)[before:] == [
        (HELPER, str(world.pid), "--force", str(projects), str(world.host_home / "Projects"))
    ]
    assert records[0] == started[0]
    assert records[1].identity == read_identity(str(projects), mountinfo=world.mountinfo)


@pytest.mark.fr("FR-066")
def test_reattach_with_no_change_makes_no_helper_call(world: World) -> None:
    _queue(world, _rows(("attached", str(world.src / "Downloads"))))
    started = _start(world, [_folder(world)]).records
    before = len(_helper_argvs(world))
    assert _reattach(world, started).records == started
    assert len(_helper_argvs(world)) == before


@pytest.mark.fr("FR-065")
def test_a_drive_mounted_below_a_folder_is_a_change(world: World) -> None:
    folder = str(world.src / "Downloads")
    _queue(world, _rows(("attached", folder)))
    started = _start(world, [_folder(world)]).records
    drive = world.src / "Downloads" / "usb"
    world.mountinfo.write_text(
        BASE_MOUNTINFO + f"40 36 8:1 / {drive} rw - vfat /dev/sdb1 rw\n", encoding="utf-8"
    )
    _queue(world, _rows(("attached", folder)))
    _realm_shows(world, [str(world.host_home / "Downloads" / "usb")])
    before = len(_helper_argvs(world))
    result = _reattach(world, started)
    assert _helper_argvs(world)[before:] == [
        (HELPER, str(world.pid), "--force", folder, str(world.host_home / "Downloads"))
    ]
    assert result.records[0].identity == read_identity(folder, mountinfo=world.mountinfo)


@pytest.mark.fr("FR-064")
def test_a_source_that_is_gone_is_unmounted_and_recorded_as_skipped(world: World) -> None:
    _queue(world, _rows(("attached", str(world.src / "Downloads"))))
    started = _start(world, [_folder(world)]).records
    os.rename(world.src / "Downloads", world.src / "Downloads-old")
    _queue(world, "0 missing\n")
    before = len(_helper_argvs(world))
    result = _reattach(world, started)
    assert _helper_argvs(world)[before:][0][2] == "--force"
    assert result.records[0].identity is None
    assert [skip.reason for skip in result.skipped] == ["missing"]


@pytest.mark.fr("FR-064")
def test_a_skipped_grant_is_tried_again_when_its_source_appears(world: World) -> None:
    started = _start(world, [_folder(world, "Music")]).records
    assert started[0].identity is None
    (world.src / "Music").mkdir()
    _queue(world, _rows(("attached", str(world.src / "Music"))))
    records = _reattach(world, started).records
    assert records[0].identity == read_identity(str(world.src / "Music"), mountinfo=world.mountinfo)


@pytest.mark.fr("FR-066")
def test_an_attached_read_only_file_is_not_touched_when_it_is_re_attached(world: World) -> None:
    """A placeholder is made once. A re-attach writes nothing into the realm (EROFS, DR-010)."""
    xauth = world.src / "xauthority"
    xauth.write_text("old", encoding="utf-8")
    realm = "/run/user/1000/ginn-xauthority"
    grant = GrantRecord("session", str(xauth), realm, "ro", False)
    _queue(world, _rows(("attached", str(xauth))))
    started = _start(world, [grant]).records
    os.rename(xauth, world.src / "xauthority-old")
    xauth.write_text("rotated", encoding="utf-8")
    before_exec = len(_exec_argvs(world.fake))
    _queue(world, _rows(("attached", str(xauth))))
    records = _reattach(world, started).records
    assert len(_exec_argvs(world.fake)) == before_exec
    assert _helper_argvs(world)[-1] == (
        HELPER,
        str(world.pid),
        "--force",
        "--ro",
        str(xauth),
        realm,
    )
    assert records[0].identity == read_identity(str(xauth), mountinfo=world.mountinfo)


@pytest.mark.fr("FR-064")
def test_a_source_that_vanished_while_the_helper_ran_takes_the_identity_the_helper_saw(
    world: World,
) -> None:
    """A forced group whose source reappeared before the helper ran: its (dev, ino) is recorded."""
    folder = world.src / "Downloads"
    _queue(world, _rows(("attached", str(folder))))
    started = _start(world, [_folder(world)]).records
    folder.rmdir()
    _queue(world, "0 attached 1 2\n")
    result = _reattach(world, started)
    assert result.records[0].identity == SourceIdentity(1, 2)
    assert result.skipped == []


@pytest.mark.fr("FR-081")
def test_a_failed_reattach_raises_and_the_realm_keeps_running(world: World) -> None:
    _queue(world, _rows(("attached", str(world.src / "Downloads"))))
    started = _start(world, [_folder(world)]).records
    os.rename(world.src / "Downloads", world.src / "Downloads-old")
    (world.src / "Downloads").mkdir()
    _queue(world, "", status=1, stderr="ginn-rebind: umount2 /x: Device or resource busy\n")
    with pytest.raises(GinnError) as caught:
        _reattach(world, started)
    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    ops = world.fake.ops()
    assert "stop" not in ops and "rm" not in ops
    assert world.fake.containers[world.cid].state is ContainerState.RUNNING


@pytest.mark.fr("FR-066")
def test_a_failed_reattach_keeps_the_old_record_so_the_next_try_resends_it(world: World) -> None:
    """A failed re-attach returns no record, so the caller keeps the old list and resends the grant.

    The forced failure between umount2 and move_mount is proven in C by T4.9 and in the helper's
    own namespace run. This test covers the Python half only: nothing is recorded on failure.
    """
    _queue(world, _rows(("attached", str(world.src / "Downloads"))))
    started = _start(world, [_folder(world)]).records
    os.rename(world.src / "Downloads", world.src / "Downloads-old")
    (world.src / "Downloads").mkdir()
    _queue(world, "", status=1, stderr="ginn-rebind: move_mount x: Invalid argument\n")
    with pytest.raises(GinnError):
        _reattach(world, started)
    _queue(world, _rows(("attached", str(world.src / "Downloads"))))
    before = len(_helper_argvs(world))
    _reattach(world, started)
    assert _helper_argvs(world)[before:][0][2] == "--force"


@pytest.mark.fr("DR-004")
def test_a_grant_refused_at_start_is_skipped_and_never_passed_to_the_helper(world: World) -> None:
    (world.src / "Music").mkdir()
    result = _start(world, [_folder(world, "Music")], admit=refuse)
    assert _helper_argvs(world) == []
    assert result.records[0].identity is None
    assert result.skipped[0].reason == "refused by the test"
    assert result.skipped[0].missing is False


@pytest.mark.fr("DR-004")
def test_a_grant_attached_before_and_refused_now_is_unmounted_and_skipped(world: World) -> None:
    """The old mount goes, and the refusal is the recorded reason. Nothing raises (ADR-008)."""
    folder_realm = str(world.host_home / "Downloads")
    _queue(world, _rows(("attached", str(world.src / "Downloads"))))
    started = _start(world, [_folder(world)]).records
    _queue(world, "0 missing\n")
    result = _reattach(world, started, admit=refuse)
    assert _helper_argvs(world)[-1] == (HELPER, str(world.pid), "--unmount", folder_realm)
    assert result.records[0].identity is None
    assert [skip.reason for skip in result.skipped] == ["refused by the test"]


@pytest.mark.fr("FR-061")
def test_an_empty_session_leaves_an_attached_session_grant_alone_while_its_source_exists(
    world: World,
) -> None:
    """A caller with no session variables (SSH, a TTY, cron) keeps a live Wayland grant."""
    _queue(world, _rows(("attached", str(world.src / "wayland"))))
    started = _start(world, [_socket(world)]).records
    before = len(_helper_argvs(world))
    result = _reattach(world, started, session=SessionEnv(env={}, binds=[]))
    assert len(_helper_argvs(world)) == before
    assert result.records[0].identity == started[0].identity
    assert result.skipped == []


@pytest.mark.fr("FR-061")
def test_a_session_grant_whose_source_is_gone_is_removed_even_with_an_empty_session(
    world: World,
) -> None:
    _queue(world, _rows(("attached", str(world.src / "wayland"))))
    started = _start(world, [_socket(world)]).records
    os.remove(world.src / "wayland")
    _queue(world, "0 missing\n")
    result = _reattach(world, started, session=SessionEnv(env={}, binds=[]))
    assert _helper_argvs(world)[-1] == (
        HELPER,
        str(world.pid),
        "--force",
        str(world.src / "wayland"),
        WAYLAND,
    )
    assert result.records[0].identity is None
    assert [skip.reason for skip in result.skipped] == ["missing"]


@pytest.mark.fr("FR-064")
def test_a_not_visible_grant_is_held_while_the_host_identity_holds(world: World) -> None:
    """A mismatch is recorded against the identity read before it. While that identity holds, the
    next enter makes no helper run and the pre-check is false (ADR-008, Resolution A)."""
    _queue(world, "0 attached 1 2\n")
    _queue(world, "0 missing\n")
    started = _start(world, [_folder(world)]).records
    assert started[0].identity is None
    assert started[0].not_visible == read_identity(
        str(world.src / "Downloads"), mountinfo=world.mountinfo
    )
    before = len(_helper_argvs(world))
    session = SessionEnv(env={}, binds=[])
    assert needs_reattach(started, session, admit=allow, mountinfo=world.mountinfo) is False
    result = _reattach(world, started)
    assert len(_helper_argvs(world)) == before
    assert [skip.reason for skip in result.skipped] == [NOT_VISIBLE]


@pytest.mark.fr("FR-064")
def test_a_not_visible_grant_is_retried_when_the_host_identity_changes(world: World) -> None:
    folder = world.src / "Downloads"
    _queue(world, "0 attached 1 2\n")
    _queue(world, "0 missing\n")
    started = _start(world, [_folder(world)]).records
    os.rename(folder, world.src / "Downloads-old")
    folder.mkdir()
    _queue(world, _rows(("attached", str(folder))))
    result = _reattach(world, started)
    assert _helper_argvs(world)[-1] == (
        HELPER,
        str(world.pid),
        "--force",
        str(folder),
        str(world.host_home / "Downloads"),
    )
    assert result.records[0].identity == read_identity(str(folder), mountinfo=world.mountinfo)
    assert result.records[0].not_visible is None


@pytest.mark.fr("FR-064")
def test_a_realm_with_fewer_mounts_than_the_host_is_held_and_costs_one_run_per_host_change(
    world: World,
) -> None:
    """The host has a drive below the folder and the realm does not show it. Nothing is kept as
    attached. At enter the hold costs no helper run. A host change costs one attach and one unmount,
    and an enter with no further change costs none."""
    folder = world.src / "Downloads"
    realm = world.host_home / "Downloads"
    session = SessionEnv(env={}, binds=[])
    world.mountinfo.write_text(
        BASE_MOUNTINFO + f"40 36 8:1 / {folder / 'usb'} rw - vfat /dev/sdb1 rw\n", encoding="utf-8"
    )
    _queue(world, _rows(("attached", str(folder))))
    _queue(world, "0 missing\n")
    _realm_shows(world, [])
    result = _start(world, [_folder(world)])
    assert result.records[0].identity is None
    assert [skip.reason for skip in result.skipped] == [NOT_VISIBLE]
    assert _helper_argvs(world)[-1] == (HELPER, str(world.pid), "--unmount", str(realm))
    assert needs_reattach(result.records, session, admit=allow, mountinfo=world.mountinfo) is False
    held_calls = len(_helper_argvs(world))
    _reattach(world, result.records)
    assert len(_helper_argvs(world)) == held_calls
    # The host changes: a second drive appears below the folder. One attach, which is still short,
    # and one unmount. Then the next enter with no change makes no helper run.
    world.mountinfo.write_text(
        BASE_MOUNTINFO
        + f"40 36 8:1 / {folder / 'usb'} rw - vfat /dev/sdb1 rw\n"
        + f"41 36 8:2 / {folder / 'usb2'} rw - vfat /dev/sdc1 rw\n",
        encoding="utf-8",
    )
    _queue(world, _rows(("attached", str(folder))))
    _queue(world, "0 missing\n")
    _realm_shows(world, [])
    assert needs_reattach(result.records, session, admit=allow, mountinfo=world.mountinfo) is True
    again = _reattach(world, result.records)
    assert len(_helper_argvs(world)) == held_calls + 2
    assert _helper_argvs(world)[-2] == (HELPER, str(world.pid), "--force", str(folder), str(realm))
    assert [skip.reason for skip in again.skipped] == [NOT_VISIBLE]
    _reattach(world, again.records)
    assert len(_helper_argvs(world)) == held_calls + 2


@pytest.mark.fr("FR-064")
def test_a_realm_that_shows_the_drive_is_attached_and_recorded(world: World) -> None:
    folder = world.src / "Downloads"
    realm = world.host_home / "Downloads"
    world.mountinfo.write_text(
        BASE_MOUNTINFO + f"40 36 8:1 / {folder / 'usb'} rw - vfat /dev/sdb1 rw\n", encoding="utf-8"
    )
    _queue(world, _rows(("attached", str(folder))))
    _realm_shows(world, [str(realm / "usb")])
    result = _start(world, [_folder(world)])
    assert result.records[0].identity == read_identity(str(folder), mountinfo=world.mountinfo)
    assert result.skipped == []


@pytest.mark.fr("FR-064")
def test_a_mount_hidden_under_another_at_the_same_place_is_not_counted(world: World) -> None:
    """A drive below the folder hangs from the root, and a mount over the folder hides it. The
    source resolves to the mount over it, which has no drive below, so nothing is short."""
    folder = world.src / "Downloads"
    world.mountinfo.write_text(
        BASE_MOUNTINFO
        + f"40 36 8:1 / {folder / 'usb'} rw - vfat /dev/sdb1 rw\n"
        + f"41 36 8:2 / {folder} rw - tmpfs tmpfs rw\n",
        encoding="utf-8",
    )
    identity = read_identity(str(folder), mountinfo=world.mountinfo)
    assert identity is not None and identity.below == ()
    _queue(world, _rows(("attached", str(folder))))
    _realm_shows(world, [])
    result = _start(world, [_folder(world)])
    assert result.records[0].identity == identity
    assert result.skipped == []
    assert not [argv for argv in _helper_argvs(world) if argv[2:3] == ("--unmount",)]


@pytest.mark.fr("FR-064")
def test_a_nested_grant_in_the_realm_is_not_a_mismatch(world: World) -> None:
    """More mounts below the path in the realm than on the host are not a mismatch: a nested grant
    may be one of them. Only fewer is flagged."""
    folder = world.src / "Downloads"
    realm = world.host_home / "Downloads"
    world.mountinfo.write_text(
        BASE_MOUNTINFO + f"40 36 8:1 / {folder / 'usb'} rw - vfat /dev/sdb1 rw\n", encoding="utf-8"
    )
    _queue(world, _rows(("attached", str(folder))))
    _realm_shows(world, [str(realm / "usb"), str(realm / "nested")])
    result = _start(world, [_folder(world)])
    assert result.records[0].identity == read_identity(str(folder), mountinfo=world.mountinfo)
    assert result.skipped == []


@pytest.mark.fr("FR-061")
def test_a_symbolic_link_source_is_admitted_and_attached_as_its_resolved_path(
    world: World,
) -> None:
    """ADR-008, Resolution A: admission and the helper both get the resolved path, and the record
    keeps the path the user named."""
    target = world.src / "Real"
    target.mkdir()
    link = world.src / "Link"
    link.symlink_to(target, target_is_directory=True)
    resolved = os.path.realpath(link)
    realm = str(world.host_home / "Downloads")
    seen: list[str] = []

    def admit_resolved(grant: GrantRecord) -> str | None:
        seen.append(grant.host)
        return None

    _queue(world, _rows(("attached", resolved)))
    grant = GrantRecord("share", str(link), realm, "rw", True)
    result = _start(world, [grant], admit=admit_resolved)
    assert resolved in seen
    assert _helper_argvs(world)[-1] == (HELPER, str(world.pid), resolved, realm)
    assert result.records[0].host == str(link)
    assert result.records[0].identity == read_identity(str(link), mountinfo=world.mountinfo)


@pytest.mark.fr("FR-061")
def test_a_symbolic_link_to_a_refused_target_is_refused_before_the_helper_runs(
    world: World,
) -> None:
    target = world.src / "Real"
    target.mkdir()
    link = world.src / "Link"
    link.symlink_to(target, target_is_directory=True)
    resolved = os.path.realpath(link)

    def refuse_target(grant: GrantRecord) -> str | None:
        return "the resolved target is refused" if grant.host == resolved else None

    result = _start(
        world,
        [GrantRecord("share", str(link), str(world.host_home / "Downloads"), "rw", True)],
        admit=refuse_target,
    )
    assert _helper_argvs(world) == []
    assert result.skipped[0].reason == "the resolved target is refused"


@pytest.mark.fr("FR-061")
def test_the_helper_runs_on_the_host_and_never_under_podman_unshare(world: World) -> None:
    """ADR-008 amendment (2026-10-13): podman's mount namespace is the stale copy, so no podman."""
    _queue(world, _rows(("attached", str(world.src / "Downloads"))))
    _start(world, [_folder(world)])
    assert not [call for call in world.fake.calls if call.op == "unshare"]
    assert _helper_argvs(world) == [
        (HELPER, str(world.pid), str(world.src / "Downloads"), str(world.host_home / "Downloads"))
    ]


@pytest.mark.fr("NFR-SEC-005")
def test_run_helper_runs_an_argv_list_and_returns_its_status_and_output() -> None:
    result = run_helper([sys.executable, "-c", "import sys; print('0 missing'); sys.exit(3)"])
    assert result == ExecResult(3, "0 missing\n", "")


@pytest.mark.fr("FR-064")
def test_run_helper_refuses_a_helper_it_cannot_start(tmp_path: Path) -> None:
    with pytest.raises(GinnError) as caught:
        run_helper([str(tmp_path / "no-such-helper")])
    assert caught.value.code == "GINN-E-ATTACH-FAILED"


@pytest.mark.fr("FR-061")
def test_a_session_path_takes_the_host_the_current_session_binds(world: World) -> None:
    """A new session names its socket differently. The fixed realm path stays, the host moves."""
    old = str(world.src / "wayland")
    _queue(world, _rows(("attached", old)))
    started = _start(world, [_socket(world)]).records
    new = world.src / "wayland-2"
    new.write_text("", encoding="utf-8")
    session = SessionEnv(env={}, binds=[SessionBind(str(new), WAYLAND, "rw", False)])
    _queue(world, _rows(("attached", str(new))))
    before = len(_helper_argvs(world))
    result = _reattach(world, started, session=session)
    assert _helper_argvs(world)[before:] == [(HELPER, str(world.pid), "--force", str(new), WAYLAND)]
    assert result.records[0].host == str(new)


@pytest.mark.fr("FR-061")
def test_the_admission_rules_refuse_what_the_plan_would_refuse(world: World) -> None:
    (world.host_home / "Downloads").mkdir(parents=True)
    admit = admission(
        home=world.host_home,
        owned_paths=[world.realm_home],
        runtime_root=world.realm_home / "run",
        env={},
    )
    host = str(world.host_home / "Downloads")
    inside = GrantRecord("share", host, host, "rw", True)
    outside = GrantRecord("share", "/etc", host, "rw", True)
    assert admit(inside) is None
    assert admit(outside) is not None


@pytest.mark.fr("FR-061")
def test_a_link_in_the_realm_home_is_refused_and_writes_nothing_outside(
    world: World, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (world.realm_home / "Downloads").symlink_to(outside, target_is_directory=True)
    with pytest.raises(GinnError) as caught:
        make_mountpoints(
            world.fake,
            world.cid,
            [_folder(world)],
            realm_home=world.realm_home,
            host_home=world.host_home,
        )
    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    assert list(outside.iterdir()) == []


@pytest.mark.fr("IR-020")
def test_unshare_keeps_the_global_flags(tmp_path: Path) -> None:
    """Real PodmanCli argv and env, run against a recording script that is not podman.

    The attach helper no longer runs under `podman unshare` (ADR-008 amendment, 2026-10-13), so
    this checks the adapter's own `unshare` call, which keeps the global flags (ADR-004).
    """
    record = tmp_path / "record.json"
    script = tmp_path / "recording-podman"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(record)!r}, 'w', encoding='utf-8') as f:\n"
        "    json.dump({'argv': sys.argv[1:], 'env': dict(os.environ)}, f)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    store = tmp_path / "store"
    store.mkdir()
    conf = store / "containers.conf"
    storage = store / "storage.conf"
    conf.write_text("", encoding="utf-8")
    storage.write_text("", encoding="utf-8")
    image_tmp = store / "image-tmp"
    image_tmp.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cli = PodmanCli(
        store / "root",
        store / "runroot",
        tmpdir=store / "tmp",
        image_tmp=image_tmp,
        containers_conf=conf,
        storage_conf=storage,
        runtime_root=runtime,
        binary=str(script),
    )
    cli.unshare([sys.executable, "-c", "pass"])
    recorded = json.loads(record.read_text(encoding="utf-8"))
    argv = recorded["argv"]
    assert argv[:9] == [
        "--remote=false",
        "--root",
        str(store / "root"),
        "--runroot",
        str(store / "runroot"),
        "--tmpdir",
        str(store / "tmp"),
        "--runtime",
        "crun",
    ]
    assert argv[9:] == ["unshare", sys.executable, "-c", "pass"]
    assert recorded["env"]["XDG_RUNTIME_DIR"] == str(runtime)


class _RecordingExec:
    """A podman stand-in that records each exec argv and reports success (make_mountpoints).

    `users` holds the `user` of each call, in order, next to its argv.
    """

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.users: list[str] = []

    def exec(self, container: str, argv: list[str], **kwargs: object) -> ExecResult:
        self.argvs.append(list(argv))
        self.users.append(str(kwargs.get("user")))
        # `test -L` answers whether the path is a symbolic link: exit 1 means it is not one.
        not_a_link = argv[:2] == ["test", "-L"]
        return ExecResult(exit_code=1 if not_a_link else 0, stdout="", stderr="")


@pytest.mark.fr("FR-061")
def test_a_runtime_subdirectory_is_made_by_the_realm_user_at_mode_0700() -> None:
    """The gnupg parent is made as the realm user, at mode 0700, so no root command touches it.

    The realm user owns /run/user/<uid> (the tmpfs is chowned to it), so a root `mkdir`, `test -L`,
    `chown` or `chmod` there would follow a link the realm planted (FR-061).
    """
    podman = _RecordingExec()
    grant = GrantRecord(
        "session", "/run/user/1000/gpg.sock", "/run/user/1000/gnupg/S.gpg-agent", "ro", False
    )
    make_mountpoints(
        podman,  # type: ignore[arg-type]
        "ginn-lab-g1",
        [grant],
        realm_home=Path("/owned/home/lab"),
        host_home=Path("/home/alice"),
    )
    calls = list(zip(podman.users, podman.argvs, strict=True))
    assert ("1000", ["test", "-L", "/run/user/1000/gnupg"]) in calls
    assert ("1000", ["mkdir", "-p", "-m", "0700", "--", "/run/user/1000/gnupg"]) in calls
    assert not any("/run/user/1000/gnupg" in argv and user == "0:0" for user, argv in calls)
    assert not any(argv[0] in ("chown", "chmod") for argv in podman.argvs)


@pytest.mark.fr("FR-067")
def test_the_runtime_directory_itself_and_other_roots_are_not_chowned() -> None:
    podman = _RecordingExec()
    grants = [
        GrantRecord("session", "/run/user/1000/w", "/run/user/1000/ginn-ssh-agent", "rw", False),
        GrantRecord("share", "/mnt/x", "/mnt/x", "rw", True),
    ]
    make_mountpoints(
        podman,  # type: ignore[arg-type]
        "ginn-lab-g1",
        grants,
        realm_home=Path("/owned/home/lab"),
        host_home=Path("/home/alice"),
    )
    assert not any(argv[0] in ("chown", "chmod") for argv in podman.argvs)


@pytest.mark.fr("FR-061")
def test_a_symbolic_link_runtime_subdirectory_is_refused_before_anything_is_made() -> None:
    """`test -L` runs as the realm user first, and a link there stops the start before `mkdir`."""

    class _LinkExec(_RecordingExec):
        """`test -L` exits 0: the path is a symbolic link."""

        def exec(self, container: str, argv: list[str], **kwargs: object) -> ExecResult:
            self.argvs.append(list(argv))
            self.users.append(str(kwargs.get("user")))
            return ExecResult(exit_code=0, stdout="", stderr="")

    podman = _LinkExec()
    grant = GrantRecord(
        "session", "/run/user/1000/gpg.sock", "/run/user/1000/gnupg/S.gpg-agent", "ro", False
    )
    with pytest.raises(GinnError) as caught:
        make_mountpoints(
            podman,  # type: ignore[arg-type]
            "ginn-lab-g1",
            [grant],
            realm_home=Path("/owned/home/lab"),
            host_home=Path("/home/alice"),
        )
    assert caught.value.code == "GINN-E-ATTACH-FAILED"
    assert podman.argvs == [["test", "-L", "/run/user/1000/gnupg"]]
    assert podman.users == ["1000"]
