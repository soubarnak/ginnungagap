# SPDX-License-Identifier: Apache-2.0
"""`ginn _sync`: the diff and apply of a realm's desktop entries to the host menu (FR-044, FR-045,
NFR-009). Fakes only: FakePodman answers the listing, the copies out and the realm's `ps`.

The 5 s timing of NFR-009 is measured on a host with installed applications (gate 3). These tests
count the podman calls of the algorithm instead, with a fixed cost per call, so the bound is the
same on every run.
"""

from __future__ import annotations

import errno
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

import ginn.export.sync as export_sync
from ginn.domain import RealmName, RealmState
from ginn.errors import GinnError, make
from ginn.export._realm import LISTING_MAX_BYTES, LISTING_TIMEOUT_S
from ginn.export.icons import ICON_LIST
from ginn.export.sync import (
    DESKTOP_LIST,
    DESKTOP_MAX_BYTES,
    SyncHooks,
    SyncResult,
    sync_in_lock,
    sync_realm,
    unexport_all,
)
from ginn.ops._mutating import OpEnv, mutating
from ginn.podman.models import ExecResult
from ginn.store.audit import AuditLog
from tests.unit.realm_cli_support import World, make_realm

PROGRAM = "/usr/bin/ginn"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
PODMAN_CALL_S = 0.05
"""The cost model of the timing test: each podman call is taken to cost this many seconds."""
_CALLS = ("ps", "exec")


def _env(world: World) -> OpEnv:
    return OpEnv.for_paths(world.paths, "alice", root_dir=world.host)


def _entry(
    name: str = "Firefox",
    *,
    icon: str | None = "firefox",
    exec_line: str = "firefox %u",
    extra: str = "",
) -> bytes:
    lines = ["[Desktop Entry]", "Type=Application", f"Name={name}", f"Exec={exec_line}"]
    if icon is not None:
        lines.append(f"Icon={icon}")
    if extra:
        lines.append(extra)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _publish(
    world: World,
    realm_apps: dict[str, bytes],
    *,
    icons: dict[str, bytes] | None = None,
    mtime: str = "1700000000.0",
    listing_status: int = 0,
) -> None:
    """Put the realm's desktop files, its icons and the two listings into the fake."""
    rows: list[str] = []
    for app, content in realm_apps.items():
        path = f"/usr/share/applications/{app}.desktop"
        world.fake.files[path] = content
        rows.append(f"{len(content)} {mtime} {path}")
    world.fake.exec_results[DESKTOP_LIST] = ExecResult(listing_status, "\n".join(rows) + "\n", "")
    icon_rows: list[str] = []
    for name, content in (icons or {}).items():
        path = f"/usr/share/icons/hicolor/48x48/apps/{name}.png"
        world.fake.files[path] = content
        icon_rows.append(f"f {len(content)} {path} ")
    world.fake.exec_results[ICON_LIST] = ExecResult(0, "\n".join(icon_rows) + "\n", "")


def _sync(world: World, realm: str = "arch") -> SyncResult:
    return sync_realm(_env(world), world.fake, RealmName(realm), hooks=SyncHooks(PROGRAM))


def _host(world: World, app: str, realm: str = "arch") -> Path:
    return world.paths.applications_dir / f"ginn-{realm}-{app}.desktop"


def _podman_calls(world: World) -> list[str]:
    return [op for op in world.fake.ops() if op in _CALLS]


def _copied(world: World) -> list[str]:
    """The realm paths read by `cat`, in order: each one is a copy of a file out of the realm."""
    return [
        c.args[1][2] for c in world.fake.calls if c.op == "exec" and c.args[1][:2] == ("cat", "--")
    ]


def _listings(world: World) -> list[str]:
    return [c.args[1][0] for c in world.fake.calls if c.op == "exec" and c.args[1][0] == "find"]


@pytest.mark.fr("FR-044")
def test_a_new_entry_is_exported_with_the_realm_name_and_a_copied_icon(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    result = _sync(world)
    assert result.added == ["firefox"]
    text = _host(world, "firefox").read_text(encoding="utf-8")
    assert "Name=Firefox (arch)" in text
    assert f"Exec={PROGRAM} exec arch -- firefox %u" in text
    icon = world.paths.icons / "arch" / "firefox.png"
    assert f"Icon={icon}" in text
    assert icon.read_bytes() == PNG
    assert "sync" in [entry.op for entry in AuditLog(world.paths, time.time).read()]


@pytest.mark.fr("FR-044")
def test_a_second_run_with_no_change_writes_and_copies_nothing(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world)
    inode = _host(world, "firefox").stat().st_ino
    state = world.paths.state / "exports" / "arch.json"
    state_inode = state.stat().st_ino
    world.fake.calls.clear()
    result = _sync(world)
    assert result.unchanged == ["firefox"]
    assert result.added == [] and result.updated == [] and result.removed == []
    assert _podman_calls(world) == ["ps", "exec"]  # one listing, no copy
    assert _host(world, "firefox").stat().st_ino == inode
    assert state.stat().st_ino == state_inode  # the state file is rewritten only on a change


@pytest.mark.fr("FR-044")
def test_the_same_app_from_two_realms_gives_two_entries(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    make_realm(world, "fedora", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world, "arch")
    _sync(world, "fedora")
    arch = _host(world, "firefox", "arch").read_text(encoding="utf-8")
    fedora = _host(world, "firefox", "fedora").read_text(encoding="utf-8")
    assert "Name=Firefox (arch)" in arch
    assert "Name=Firefox (fedora)" in fedora


@pytest.mark.fr("FR-045")
def test_a_removed_app_loses_its_entry_and_its_icon_copy(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world)
    icon = world.paths.icons / "arch" / "firefox.png"
    assert icon.exists()
    _publish(world, {})
    result = _sync(world)
    assert result.removed == ["firefox"]
    assert not _host(world, "firefox").exists()
    assert not icon.exists()


@pytest.mark.fr("FR-044")
def test_a_changed_app_is_updated_in_place(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry("Firefox")}, icons={"firefox": PNG})
    _sync(world)
    _publish(world, {"firefox": _entry("Firefox Nightly")}, icons={"firefox": PNG}, mtime="2")
    result = _sync(world)
    assert result.updated == ["firefox"]
    assert "Name=Firefox Nightly (arch)" in _host(world, "firefox").read_text(encoding="utf-8")


@pytest.mark.fr("FR-045")
def test_an_entry_that_turns_hidden_is_removed(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world)
    _publish(world, {"firefox": _entry(extra="NoDisplay=true")}, mtime="2")
    result = _sync(world)
    assert result.removed == ["firefox"]
    assert not _host(world, "firefox").exists()


@pytest.mark.fr("FR-044")
def test_links_hidden_entries_and_unreadable_files_are_skipped_with_a_warning(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(
        world,
        {
            "link": b"[Desktop Entry]\nType=Link\nName=Docs\nURL=https://example.org\n",
            "hidden": _entry(extra="Hidden=true"),
            "broken": b"[Desktop Entry]\nType=Application\nthis line has no equals\n",
        },
    )
    result = _sync(world)
    assert result.added == []
    assert sorted(result.skipped) == ["broken", "hidden", "link"]
    codes = [notice.code for notice in result.warnings]
    assert codes == ["GINN-W-EXPORT-SKIPPED"] * 3
    assert not any(world.paths.applications_dir.glob("ginn-arch-*.desktop"))


@pytest.mark.fr("FR-044")
def test_a_file_with_the_entry_name_that_ginn_did_not_write_is_not_overwritten(
    world: World,
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    world.paths.applications_dir.mkdir(parents=True, exist_ok=True)
    mine = _host(world, "firefox")
    mine.write_text("[Desktop Entry]\nName=Mine\n", encoding="utf-8")
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    result = _sync(world)
    assert result.added == []
    assert mine.read_text(encoding="utf-8") == "[Desktop Entry]\nName=Mine\n"
    assert [notice.code for notice in result.warnings] == ["GINN-W-EXPORT-SKIPPED"]
    assert _copied(world) == []  # no copy, no orphan icon
    assert not (world.paths.icons / "arch" / "firefox.png").exists()


@pytest.mark.fr("FR-045")
def test_a_failed_listing_raises_and_removes_nothing(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world)
    _publish(world, {}, listing_status=1)
    with pytest.raises(GinnError) as err:
        _sync(world)
    assert err.value.code == "GINN-E-EXPORT-LIST"
    assert _host(world, "firefox").exists()


@pytest.mark.fr("FR-044")
def test_a_failed_copy_keeps_the_host_entry_and_is_retried_next_time(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry("Firefox")}, icons={"firefox": PNG})
    _sync(world)
    before = _host(world, "firefox").read_text(encoding="utf-8")
    _publish(world, {"firefox": _entry("Firefox Nightly")}, icons={"firefox": PNG}, mtime="2")
    del world.fake.files["/usr/share/applications/firefox.desktop"]
    result = _sync(world)
    assert result.updated == [] and result.removed == []
    assert [notice.code for notice in result.warnings] == ["GINN-W-EXPORT-SKIPPED"]
    assert _host(world, "firefox").read_text(encoding="utf-8") == before
    _publish(world, {"firefox": _entry("Firefox Nightly")}, icons={"firefox": PNG}, mtime="2")
    assert _sync(world).updated == ["firefox"]


@pytest.mark.fr("FR-044")
def test_a_stopped_realm_is_not_read_and_nothing_changes(world: World) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    result = _sync(world)
    assert result.running is False
    assert _podman_calls(world) == []  # a stopped record is not even asked
    assert not _host(world, "firefox").exists()


@pytest.mark.fr("FR-044")
def test_another_realms_entries_are_never_touched(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    make_realm(world, "fedora", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world, "fedora")
    _publish(world, {})
    _sync(world, "arch")
    assert _host(world, "firefox", "fedora").exists()


@pytest.mark.fr("FR-046")
def test_the_exec_of_an_entry_reaches_the_launcher_as_the_original_argv(world: World) -> None:
    from ginn.export.desktop import DesktopEntry, split_exec

    make_realm(world, "arch", RealmState.RUNNING, running=True)
    exec_line = 'sh -c "echo $(id) `id`" %u'
    _publish(world, {"firefox": _entry(exec_line=exec_line)})
    _sync(world)
    exec_value = DesktopEntry.parse(_host(world, "firefox").read_text(encoding="utf-8")).get("Exec")
    assert exec_value is not None
    assert split_exec(exec_value) == [
        PROGRAM,
        "exec",
        "arch",
        "--",
        "sh",
        "-c",
        "echo $(id) `id`",
        "%u",
    ]


@pytest.mark.fr("FR-044")
def test_an_icon_that_cannot_be_used_is_warned_and_the_entry_goes_out_without_it(
    world: World,
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry(icon="/etc/passwd")})
    result = _sync(world)
    assert result.added == ["firefox"]
    assert [notice.code for notice in result.warnings] == ["GINN-W-ICON-SKIPPED"]
    assert "Icon=" not in _host(world, "firefox").read_text(encoding="utf-8")
    copied = _copied(world)
    assert "/etc/passwd" not in copied


@pytest.mark.fr("FR-044")
def test_a_listed_file_name_that_is_not_a_desktop_id_is_ignored(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    world.fake.exec_results[DESKTOP_LIST] = ExecResult(
        0, "30 1 /usr/share/applications/../evil.desktop\n", ""
    )
    result = _sync(world)
    assert result.added == []
    assert not any("evil" in path.name for path in world.paths.applications_dir.glob("*"))


@pytest.mark.fr("FR-044")
def test_an_oversized_desktop_file_is_skipped_before_any_copy(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    path = "/usr/share/applications/huge.desktop"
    world.fake.files[path] = _entry()
    world.fake.exec_results[DESKTOP_LIST] = ExecResult(0, f"{10**10} 1 {path}\n", "")
    world.fake.exec_results[ICON_LIST] = ExecResult(0, "", "")
    result = _sync(world)
    assert result.skipped == ["huge"]
    assert _copied(world) == []
    assert [notice.code for notice in result.warnings] == ["GINN-W-EXPORT-SKIPPED"]
    assert not _host(world, "huge").exists()


@pytest.mark.fr("FR-044")
def test_an_oversized_icon_is_not_copied_and_the_entry_goes_out_without_it(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()})
    big = "/usr/share/icons/hicolor/48x48/apps/firefox.png"
    world.fake.exec_results[ICON_LIST] = ExecResult(0, f"f {10**10} {big} \n", "")
    result = _sync(world)
    assert result.added == ["firefox"]
    assert [notice.code for notice in result.warnings] == ["GINN-W-ICON-SKIPPED"]
    assert big not in _copied(world)


@pytest.mark.fr("FR-044")
def test_an_action_icon_with_another_name_is_dropped_so_no_copy_is_overwritten(
    world: World,
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    content = (
        b"[Desktop Entry]\nType=Application\nName=Firefox\nExec=firefox %u\nIcon=firefox\n"
        b"Actions=new;\n\n[Desktop Action new]\nName=New\nExec=firefox --new\n"
        b"Icon=firefox-private\n"
    )
    _publish(world, {"firefox": content}, icons={"firefox": PNG, "firefox-private": PNG})
    result = _sync(world)
    assert result.added == ["firefox"]
    assert sorted(p.name for p in (world.paths.icons / "arch").iterdir()) == ["firefox.png"]
    assert "firefox-private" not in _host(world, "firefox").read_text(encoding="utf-8")


@pytest.mark.fr("NFR-009")
def test_two_hundred_entries_cost_a_fixed_number_of_podman_calls(world: World) -> None:
    """The algorithm, counted: one listing and one copy per changed file, not per entry per run."""
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    apps = {f"app{i:03d}": _entry(f"App {i}", icon=f"icon{i:03d}") for i in range(200)}
    icons = {f"icon{i:03d}": PNG for i in range(200)}
    _publish(world, apps, icons=icons)

    world.fake.calls.clear()
    first = _sync(world)
    assert len(first.added) == 200
    assert len(_listings(world)) == 2  # desktop listing, icon listing
    assert len(_copied(world)) == 400  # 200 entries and 200 icons, each read once

    inodes = {app: _host(world, app).stat().st_ino for app in apps}
    world.fake.calls.clear()
    second = _sync(world)
    assert len(second.unchanged) == 200
    assert _podman_calls(world) == ["ps", "exec"]
    assert {app: _host(world, app).stat().st_ino for app in apps} == inodes

    # One changed entry: the listing, its copy, and its icon, which needs the icon listing again.
    _publish(world, {**apps, "app000": _entry("App 0 changed", icon="icon000")}, icons=icons)
    world.fake.calls.clear()
    third = _sync(world)
    assert third.updated == ["app000"]
    simulated = PODMAN_CALL_S * len(_podman_calls(world))
    assert _podman_calls(world) == ["ps", "exec", "exec", "exec", "exec"]  # 2 listings, 2 reads
    assert simulated <= 5.0

    _publish(world, {})
    world.fake.calls.clear()
    fourth = _sync(world)
    assert len(fourth.removed) == 200
    assert _podman_calls(world) == ["ps", "exec"]
    assert list(world.paths.icons.glob("arch/*.png")) == []


@pytest.mark.fr("FR-045")
def test_unexport_all_removes_every_entry_icon_and_the_state_of_a_realm(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    apps = {"firefox": _entry(), "gimp": _entry("Gimp", icon="gimp")}
    _publish(world, apps, icons={"firefox": PNG})
    _sync(world)
    removed = unexport_all(world.paths, RealmName("arch"))
    assert sorted(removed) == ["firefox", "gimp"]
    assert not list(world.paths.applications_dir.glob("ginn-arch-*.desktop"))
    assert not (world.paths.icons / "arch").exists()
    assert not (world.paths.state / "exports" / "arch.json").exists()


@pytest.mark.fr("FR-044")
def test_a_lost_state_file_costs_copies_but_rewrites_no_unchanged_entry(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world)
    inode = _host(world, "firefox").stat().st_ino
    (world.paths.state / "exports" / "arch.json").unlink()
    result = _sync(world)
    assert result.unchanged == ["firefox"]
    assert _host(world, "firefox").stat().st_ino == inode


@pytest.mark.fr("FR-044")
def test_a_directory_at_the_desktop_path_is_warned_and_nothing_is_written(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    world.fake.exec_results[("cat", "--", "/usr/share/applications/firefox.desktop")] = ExecResult(
        1, "", "cat: Is a directory\n"
    )
    result = _sync(world)
    assert result.added == []
    assert [notice.code for notice in result.warnings] == ["GINN-W-EXPORT-SKIPPED"]
    assert not _host(world, "firefox").exists()


@pytest.mark.fr("FR-044")
def test_a_file_that_is_not_utf8_is_skipped_and_its_old_entry_is_removed(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world)
    _publish(world, {"firefox": b"[Desktop Entry]\nName=\xff\xfe\n"}, mtime="2")
    result = _sync(world)
    assert result.skipped == ["firefox"]
    assert result.removed == ["firefox"]
    assert [notice.code for notice in result.warnings] == ["GINN-W-EXPORT-SKIPPED"]
    assert not _host(world, "firefox").exists()


@pytest.mark.fr("FR-044")
def test_a_host_entry_over_the_cap_is_skipped_and_leaves_no_icon_copy(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    # Under the cap as read; the escapes of its Exec double it when rendered, past the cap.
    exec_line = "$" * 262_000
    content = f"[Desktop Entry]\nType=Application\nName=Big\nExec={exec_line}\nIcon=big\n"
    _publish(world, {"big": content.encode()}, icons={"big": PNG})
    result = _sync(world)
    assert result.added == []
    assert result.skipped == ["big"]
    assert [notice.code for notice in result.warnings] == ["GINN-W-EXPORT-SKIPPED"]
    assert "larger" in result.warnings[0].message
    assert not _host(world, "big").exists()
    assert not (world.paths.icons / "arch" / "big.png").exists()


@pytest.mark.fr("FR-045")
def test_a_hung_realm_listing_is_export_list_and_changes_nothing_and_releases_the_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world)
    real_exec = world.fake.exec

    def hang(container_id: str, argv: Sequence[str], **kwargs: object) -> ExecResult:
        if tuple(argv) == DESKTOP_LIST:
            hung = make("GINN-E-INTERNAL")
            hung.details["reason"] = "timed out after 30.0 s"
            raise hung
        return real_exec(container_id, argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(world.fake, "exec", hang)
    with pytest.raises(GinnError) as err:
        _sync(world)
    assert err.value.code == "GINN-E-EXPORT-LIST"
    assert "timed out" in str(err.value.details["reason"])
    assert _host(world, "firefox").exists()
    monkeypatch.setattr(world.fake, "exec", real_exec)
    assert _sync(world).unchanged == ["firefox"]  # the lock was released by the raise


@pytest.mark.fr("FR-044")
def test_the_listing_asks_for_a_timeout_and_an_output_cap(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {})
    seen: list[dict[str, object]] = []
    real_exec = world.fake.exec

    def spy(container_id: str, argv: Sequence[str], **kwargs: object) -> ExecResult:
        if tuple(argv) == DESKTOP_LIST:
            seen.append(dict(kwargs))
        return real_exec(container_id, argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(world.fake, "exec", spy)
    _sync(world)
    assert seen == [
        {
            "capture": True,
            "user": "0:0",
            "timeout_s": LISTING_TIMEOUT_S,
            "max_output": LISTING_MAX_BYTES,
        }
    ]


@pytest.mark.fr("FR-044")
def test_every_realm_read_of_a_sync_runs_as_container_root(world: World) -> None:
    """A first-level /usr/share directory the realm user cannot read (Fedora's 0711 empty.sshd)
    makes `find` exit 1 for that user, so the listing and the copies run as container root."""
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world)
    users = {call.user for call in world.fake.exec_calls}
    assert users == {"0:0"}
    assert len(world.fake.exec_calls) == 4  # listing, desktop read, icon listing, icon read


@pytest.mark.fr("FR-044")
def test_a_desktop_file_swapped_to_a_huge_one_after_the_listing_is_cut_and_skipped(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    path = "/usr/share/applications/firefox.desktop"
    world.fake.files[path] = _entry() + b"#" * (DESKTOP_MAX_BYTES + 10)
    seen: list[dict[str, object]] = []
    real_exec = world.fake.exec

    def spy(container_id: str, argv: Sequence[str], **kwargs: object) -> ExecResult:
        if tuple(argv) == ("cat", "--", path):
            seen.append(dict(kwargs))
        return real_exec(container_id, argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(world.fake, "exec", spy)
    result = _sync(world)
    assert result.added == []
    assert [notice.code for notice in result.warnings] == ["GINN-W-EXPORT-SKIPPED"]
    assert "larger" in result.warnings[0].message
    assert seen == [
        {
            "capture": True,
            "user": "0:0",
            "timeout_s": LISTING_TIMEOUT_S,
            "max_output": DESKTOP_MAX_BYTES + 1,
        }
    ]
    assert not _host(world, "firefox").exists()


@pytest.mark.fr("FR-044")
def test_the_listing_is_under_usr_share_so_a_missing_applications_directory_is_empty(
    tmp_path: Path,
) -> None:
    """The listing argv, run locally on a tree with the same shape. No realm, no podman."""
    root = tmp_path / "usr-share"
    (root / "doc").mkdir(parents=True)
    argv = [arg.replace("/usr/share", str(root)) for arg in DESKTOP_LIST]
    missing = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    assert (missing.returncode, missing.stdout) == (0, "")
    (root / "applications").mkdir()
    (root / "applications" / "firefox.desktop").write_text("x", encoding="utf-8")
    (root / "applications" / "notes.txt").write_text("x", encoding="utf-8")
    listed = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    assert listed.returncode == 0
    assert [line.split(" ", 2)[2] for line in listed.stdout.splitlines()] == [
        str(root / "applications" / "firefox.desktop")
    ]


@pytest.mark.fr("FR-044")
def test_a_missing_applications_directory_removes_the_last_exports(world: World) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    _sync(world)
    _publish(world, {})  # the listing of a realm whose last GUI package is gone
    result = _sync(world)
    assert result.removed == ["firefox"]
    assert not _host(world, "firefox").exists()


@pytest.mark.fr("FR-124")
@pytest.mark.fr("FR-044")
def test_a_sync_inside_an_install_nests_its_counts_under_sync_in_the_install_audit(
    world: World,
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry()}, icons={"firefox": PNG})
    env = _env(world)
    with mutating(env, "install", RealmName("arch")) as mutation:
        result = sync_in_lock(
            env, world.fake, RealmName("arch"), mutation=mutation, hooks=SyncHooks(PROGRAM)
        )
    assert result.added == ["firefox"]
    (entry,) = [e for e in env.audit.read() if e.op == "install"]
    assert entry.params["sync"] == {"added": 1, "updated": 0, "removed": 0, "skipped": 0}
    assert "added" not in entry.params and "removed" not in entry.params


@pytest.mark.fr("FR-124")
@pytest.mark.fr("FR-044")
def test_a_stopped_realm_inside_a_removal_nests_its_noop_and_a_plain_sync_keeps_it_top_level(
    world: World,
) -> None:
    make_realm(world, "arch", RealmState.STOPPED, running=False)
    env = _env(world)
    with mutating(env, "remove", RealmName("arch")) as mutation:
        result = sync_in_lock(
            env, world.fake, RealmName("arch"), mutation=mutation, hooks=SyncHooks(PROGRAM)
        )
    assert result.running is False
    (removal,) = [e for e in env.audit.read() if e.op == "remove"]
    assert removal.params["sync"] == {"noop": "realm not running"}
    assert "noop" not in removal.params
    _sync(world)
    plain = [e for e in env.audit.read() if e.op == "sync"]
    assert plain[-1].params.get("noop") == "realm not running"


@pytest.mark.fr("FR-044")
def test_an_os_error_writing_one_entry_skips_that_app_and_the_next_sync_exports_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(
        world,
        {"firefox": _entry(icon=None), "gimp": _entry("GIMP", icon=None, exec_line="gimp %U")},
    )
    real_write = export_sync.write_file_atomic

    def full_disk(path: Path, data: bytes, mode: int) -> None:
        if path.name == "ginn-arch-gimp.desktop":
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write(path, data, mode)

    monkeypatch.setattr(export_sync, "write_file_atomic", full_disk)
    result = _sync(world)
    assert result.added == ["firefox"]
    assert not _host(world, "gimp").exists()
    assert [w.code for w in result.warnings] == ["GINN-W-EXPORT-SKIPPED"]
    assert "No space left on device" in result.warnings[0].message
    # The entry that failed is not in the state, so the next sync writes it.
    monkeypatch.setattr(export_sync, "write_file_atomic", real_write)
    retry = _sync(world)
    assert retry.added == ["gimp"]
    assert retry.warnings == []


@pytest.mark.fr("FR-044")
def test_an_os_error_saving_the_sync_state_is_a_warning_and_the_entries_stay(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry(icon=None)})

    def read_only(path: Path, data: bytes) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(export_sync, "write_bytes_atomic", read_only)
    result = _sync(world)
    assert result.added == ["firefox"]
    assert _host(world, "firefox").exists()
    assert [w.code for w in result.warnings] == ["GINN-W-EXPORT-SYNC"]
    assert "not fully synced: Read-only file system" in result.warnings[0].message


@pytest.mark.fr("FR-044")
def test_an_os_error_before_the_listing_is_a_warning_and_writes_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_realm(world, "arch", RealmState.RUNNING, running=True)
    _publish(world, {"firefox": _entry(icon=None)})

    def denied(self: object) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(type(world.paths), "ensure", denied)
    result = _sync(world)
    assert result.added == []
    assert [w.code for w in result.warnings] == ["GINN-W-EXPORT-SYNC"]
    assert "Permission denied" in result.warnings[0].message
    assert not _host(world, "firefox").exists()
