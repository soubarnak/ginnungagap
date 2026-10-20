# SPDX-License-Identifier: Apache-2.0
"""export/watch.py: the signal of a native package transaction to the host menu (FR-044, FR-045,
NFR-009, ADR-016).

Nothing here runs podman, a package manager or the real ginn-watch. The container is a stub that
records each exec, and the spawn is a recorder. What a hook does inside a real realm, and the 5 s
bound, are host-only (tests/integration/test_export_real.py).
"""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ginn.distros import available, load
from ginn.domain import RealmName
from ginn.errors import GinnError, make
from ginn.export import watch
from ginn.export.watch import (
    MARKER_NAME,
    STAMP_NAME,
    WatchHooks,
    arm,
    ensure_hooks,
    open_stamp_dir,
    rearm,
    stamp_realm_path,
)
from ginn.podman.adapter import ExecTarget
from ginn.podman.models import ExecResult
from ginn.store.paths import OwnedPaths

NAME = RealmName("lab")
CID = "c" * 64
HOME = "/home/alice"
INIT = 4100
INIT_START = 9100
WATCHER = 4242
USER = "1000"


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OwnedPaths:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("GINN_RUNTIME_DIR", str(tmp_path / "runtime"))
    resolved = OwnedPaths.from_env(uid=os.getuid())
    resolved.ensure()
    (resolved.homes / NAME.value).mkdir(parents=True)
    return resolved


def _make_stamp_dir(paths: OwnedPaths, marker: str | None = None) -> Path:
    directory = paths.homes / NAME.value / ".local" / "state" / "ginn-export"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / STAMP_NAME).write_bytes(b"")
    if marker is not None:
        (directory / MARKER_NAME).write_text(marker, encoding="utf-8")
    return directory


class StubPodman:
    """Records each exec. `on_exec` can make the effect that the real exec would have."""

    def __init__(self, paths: OwnedPaths) -> None:
        self.paths = paths
        self.calls: list[tuple[str, list[str], str | None, ExecTarget]] = []
        self.exit_codes: list[int] = []
        self.error: GinnError | None = None
        self.make_dirs = True

    def exec(
        self,
        container_id: str,
        argv: Sequence[str],
        *,
        target: ExecTarget,
        user: str | None = None,
        capture: bool = True,
        timeout_s: float | None = None,
    ) -> ExecResult:
        assert capture and timeout_s is not None
        self.calls.append((container_id, list(argv), user, target))
        if self.error is not None:
            raise self.error
        if len(self.calls) == 2 and self.make_dirs:
            _make_stamp_dir(self.paths, marker=container_id)
        code = self.exit_codes.pop(0) if self.exit_codes else 0
        return ExecResult(code, "", "")


def _ensure(stub: StubPodman, paths: OwnedPaths, distro: str = "debian", home: str = HOME) -> Any:
    return ensure_hooks(
        stub,  # type: ignore[arg-type]
        paths,
        NAME,
        load(distro),
        container_id=CID,
        host_home=home,
        user=USER,
        services=False,
    )


# ---- the hook text of each distribution -------------------------------------------------------


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-045")
@pytest.mark.fr("NFR-060")
@pytest.mark.parametrize("distro", ["arch", "debian", "fedora", "kali"])
def test_every_shipped_distribution_has_a_hook_that_touches_the_stamp(distro: str) -> None:
    definition = load(distro)
    assert definition.export_hook is not None
    stamp = stamp_realm_path(HOME)
    assert stamp == f"{HOME}/.local/state/ginn-export/{STAMP_NAME}"
    text = definition.export_hook.render(stamp)
    assert f"/usr/bin/touch {stamp}" in text
    assert "mkdir" not in text  # a hook run by container root must never make the directory
    assert "{stamp}" not in text
    assert STAMP_NAME.endswith(".desktop")


@pytest.mark.fr("FR-044")
def test_the_pacman_hook_runs_after_an_install_upgrade_or_removal_of_a_desktop_file() -> None:
    hook = load("arch").export_hook
    assert hook is not None
    assert hook.path == "/etc/pacman.d/hooks/ginn-export.hook"
    text = hook.render(stamp_realm_path(HOME))
    for line in (
        "Type = Path",
        "Target = usr/share/applications/*.desktop",
        "When = PostTransaction",
        "Operation = Install",
        "Operation = Upgrade",
        "Operation = Remove",
    ):
        assert line in text.splitlines()


@pytest.mark.fr("FR-044")
@pytest.mark.parametrize("distro", ["debian", "kali"])
def test_the_apt_hook_never_fails_the_apt_command(distro: str) -> None:
    hook = load(distro).export_hook
    assert hook is not None
    assert hook.path == "/etc/apt/apt.conf.d/99ginn-export"
    assert hook.render(stamp_realm_path(HOME)).startswith(
        f'DPkg::Post-Invoke {{ "/usr/bin/touch {stamp_realm_path(HOME)} 2>/dev/null || true"; }};'
    )


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-013")
@pytest.mark.fr("CR-007")
def test_the_dnf5_hook_is_an_actions_line_and_adds_no_package_to_the_integration_set() -> None:
    """The actions plugin is not in the Integration Package Set (CL-35): the line is inert until
    the user installs it."""
    fedora = load("fedora")
    assert fedora.export_hook is not None
    line = fedora.export_hook.render(stamp_realm_path(HOME)).strip()
    assert line == f"post_transaction::::/usr/bin/touch {stamp_realm_path(HOME)}"
    assert "'" not in line and '"' not in line  # no quote for dnf5 to parse (CL-35)
    assert "libdnf5-plugin-actions" not in fedora.integration_set(None).packages
    assert "libdnf5-plugin-actions" not in fedora.integration_set("en_US.UTF-8").packages
    assert not hasattr(fedora.export_hook, "packages")


@pytest.mark.fr("FR-044")
@pytest.mark.fr("CR-006")
@pytest.mark.parametrize("distro", ["arch", "debian", "fedora", "kali"])
def test_the_hook_command_touches_the_stamp_and_never_makes_the_directory(
    distro: str, tmp_path: Path
) -> None:
    """Run the command of each hook with the directory there, then with it removed. A directory
    that container root makes has a subordinate-id owner, so the hook must not make it."""
    import re
    import shlex
    import subprocess

    hook = load(distro).export_hook
    assert hook is not None
    home = tmp_path / "home"
    directory = home / ".local" / "state" / "ginn-export"
    directory.mkdir(parents=True)
    stamp = Path(stamp_realm_path(str(home)))
    text = hook.render(str(stamp))
    found = re.search(r"/usr/bin/touch \S+( 2>/dev/null \|\| true)?", text)
    assert found is not None
    command = found.group(0)
    argv = ["/bin/sh", "-c", command] if "||" in command else shlex.split(command)
    done = subprocess.run(  # noqa: S603 - the hook's own fixed command, in a temp dir
        argv, capture_output=True, text=True, check=False, timeout=10
    )
    assert done.returncode == 0, done.stderr
    assert stamp.is_file()
    stamp.unlink()
    directory.rmdir()
    done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=10)  # noqa: S603
    assert not directory.exists()
    if "||" in command:
        assert done.returncode == 0  # apt: a failed touch never fails the apt command


@pytest.mark.fr("NFR-060")
def test_a_distribution_data_file_without_the_table_has_no_hook() -> None:
    assert all(load(name).export_hook is not None for name in available())
    from ginn.distros import from_mapping

    raw = _minimal_mapping()
    assert from_mapping(raw).export_hook is None


def _minimal_mapping() -> dict[str, Any]:
    import tomllib
    from importlib.resources import files

    text = (files("ginn.distros") / "data" / "debian.toml").read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    del raw["export_hook"]
    return raw


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"path": "relative/hook"}, "export_hook.path"),
        ({"path": "/etc/../hook"}, "export_hook.path"),
        ({"path": "/etc/a hook"}, "export_hook.path"),
        ({"text": "no placeholder"}, "export_hook.text"),
        ({"text": "{stamp} {other}"}, "export_hook.text"),
        ({"text": "{stamp"}, "export_hook.text"),
        ({"text": "touch {stampdir}"}, "export_hook.text"),
        ({"text": "mkdir {stampdir} && touch {stamp}"}, "export_hook.text"),
        ({"packages": ["libdnf5-plugin-actions"]}, "export_hook"),
        ({"extra": 1}, "export_hook"),
    ],
)
def test_the_loader_refuses_a_malformed_hook_table(patch: dict[str, Any], field: str) -> None:
    from ginn.distros import from_mapping

    raw = _minimal_mapping()
    raw["export_hook"] = {"path": "/etc/x", "text": "touch {stamp}\n", **patch}
    with pytest.raises(GinnError) as caught:
        from_mapping(raw)
    assert caught.value.code == "GINN-E-DISTRO-DATA"
    assert caught.value.details["field"] == field


# ---- the stamp directory ----------------------------------------------------------------------


@pytest.mark.fr("CR-006")
def test_the_stamp_directory_is_used_only_when_it_is_a_plain_directory_of_the_realm_home(
    paths: OwnedPaths, tmp_path: Path
) -> None:
    with open_stamp_dir(paths, NAME) as missing:
        assert missing is None
    plain = _make_stamp_dir(paths)
    with open_stamp_dir(paths, NAME) as fd:
        assert fd is not None
        assert os.readlink(f"/proc/self/fd/{fd}") == str(plain)
    with pytest.raises(OSError):
        os.fstat(fd)  # closed on exit


@pytest.mark.fr("CR-006")
@pytest.mark.parametrize("linked", [".local", "state", "ginn-export"])
def test_a_link_in_the_path_gives_no_stamp_directory(
    paths: OwnedPaths, tmp_path: Path, linked: str
) -> None:
    """The realm can make any component a link to a host directory (an absolute target resolves
    on the host). Nothing is watched or read then, and `create` makes nothing outside."""
    outside = tmp_path / "outside"
    (outside / "state" / "ginn-export").mkdir(parents=True)
    home = paths.homes / NAME.value
    if linked == ".local":
        (home / ".local").symlink_to(outside)
    elif linked == "state":
        (home / ".local").mkdir()
        (home / ".local" / "state").symlink_to(outside / "state")
    else:
        (home / ".local" / "state").mkdir(parents=True)
        (home / ".local" / "state" / "ginn-export").symlink_to(outside / "state" / "ginn-export")
    for create in (False, True):
        with open_stamp_dir(paths, NAME, create=create) as fd:
            assert fd is None
    assert sorted(p.name for p in outside.rglob("*")) == ["ginn-export", "state"]


@pytest.mark.fr("CR-006")
def test_a_directory_swapped_for_a_link_after_the_walk_changes_nothing_that_is_held(
    paths: OwnedPaths, tmp_path: Path
) -> None:
    plain = _make_stamp_dir(paths)
    inode = plain.stat().st_ino
    with open_stamp_dir(paths, NAME) as fd:
        assert fd is not None
        moved = tmp_path / "moved"
        plain.rename(moved)
        plain.symlink_to(tmp_path)
        assert os.fstat(fd).st_ino == inode
        assert os.readlink(f"/proc/self/fd/{fd}") == str(moved)


@pytest.mark.fr("CR-006")
def test_a_marker_that_is_a_link_is_not_read(paths: OwnedPaths, tmp_path: Path) -> None:
    directory = _make_stamp_dir(paths)
    secret = tmp_path / "secret"
    secret.write_text(CID, encoding="utf-8")
    (directory / MARKER_NAME).symlink_to(secret)
    stub = StubPodman(paths)
    assert _ensure(stub, paths) is None
    assert len(stub.calls) == 2


# ---- ensure_hooks -----------------------------------------------------------------------------


@pytest.mark.fr("FR-044")
@pytest.mark.fr("NFR-SEC-005")
def test_ensure_hooks_writes_the_hook_as_root_then_the_stamp_as_the_realm_user(
    paths: OwnedPaths,
) -> None:
    stub = StubPodman(paths)
    assert _ensure(stub, paths) is None

    (cid1, hook_argv, hook_user, target), (cid2, stamp_argv, stamp_user, _) = stub.calls
    assert cid1 == cid2 == CID
    assert target == ExecTarget(realm="lab", services=False, mutating=False)
    assert hook_user == "0:0"
    assert hook_argv[:3] == ["sh", "-c", watch._WRITE_HOOK]
    assert hook_argv[3:5] == ["sh", "/etc/apt/apt.conf.d/99ginn-export"]
    # The text and the path are arguments of a fixed script, never part of it.
    debian_hook = load("debian").export_hook
    assert debian_hook is not None
    assert hook_argv[5] == debian_hook.render(stamp_realm_path(HOME))
    assert stamp_user == USER
    assert stamp_argv == [
        "sh",
        "-c",
        watch._WRITE_STAMP,
        "sh",
        f"{HOME}/.local/state/ginn-export",
        STAMP_NAME,
        CID,
        MARKER_NAME,
    ]


@pytest.mark.fr("NFR-004")
def test_a_container_with_its_marker_runs_no_exec(paths: OwnedPaths) -> None:
    _make_stamp_dir(paths, marker=CID)
    stub = StubPodman(paths)
    assert _ensure(stub, paths) is None
    assert stub.calls == []


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-044")
def test_a_new_container_gets_its_hooks_again(paths: OwnedPaths) -> None:
    """A rebuild makes a new container with the same Realm Home: the marker names the old one."""
    _make_stamp_dir(paths, marker="d" * 64)
    stub = StubPodman(paths)
    assert _ensure(stub, paths) is None
    assert len(stub.calls) == 2


@pytest.mark.fr("FR-044")
def test_a_distribution_without_a_hook_runs_nothing(
    paths: OwnedPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    stub = StubPodman(paths)
    bare = replace(load("debian"), export_hook=None)
    assert (
        ensure_hooks(
            stub,  # type: ignore[arg-type]
            paths,
            NAME,
            bare,
            container_id=CID,
            host_home=HOME,
            user=USER,
            services=False,
        )
        is None
    )
    assert stub.calls == []


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("home", ["/home/a b", "/home/a'b", '/home/a"b', "/home/a$b", "/h/{x}"])
def test_a_home_path_the_hook_cannot_carry_gets_a_warning_and_no_exec(
    paths: OwnedPaths, home: str
) -> None:
    stub = StubPodman(paths)
    notice = _ensure(stub, paths, home=home)
    assert notice is not None
    assert notice.code == "GINN-W-EXPORT-SYNC"
    assert "cannot carry" in notice.message
    assert stub.calls == []


@pytest.mark.fr("FR-044")
def test_a_failed_exec_is_a_warning_and_the_stamp_step_does_not_run(paths: OwnedPaths) -> None:
    stub = StubPodman(paths)
    stub.exit_codes = [1]
    notice = _ensure(stub, paths)
    assert notice is not None
    assert notice.code == "GINN-W-EXPORT-SYNC"
    assert "status 1" in notice.message
    assert len(stub.calls) == 1


@pytest.mark.fr("FR-044")
def test_a_podman_error_is_a_warning_not_an_exception(paths: OwnedPaths) -> None:
    stub = StubPodman(paths)
    stub.error = make("GINN-E-INTERNAL")
    notice = _ensure(stub, paths)
    assert notice is not None
    assert notice.code == "GINN-W-EXPORT-SYNC"


@pytest.mark.fr("CR-006")
def test_a_stamp_directory_that_is_not_plain_after_the_install_is_a_warning(
    paths: OwnedPaths,
) -> None:
    stub = StubPodman(paths)
    stub.make_dirs = False
    notice = _ensure(stub, paths)
    assert notice is not None
    assert "not a plain directory" in notice.message


# ---- arm --------------------------------------------------------------------------------------


class Recorder:
    def __init__(self, pid: int = WATCHER) -> None:
        self.pid = pid
        self.calls: list[tuple[list[str], Path]] = []
        self.passed: list[tuple[int, str, int]] = []
        self.error: OSError | None = None

    def __call__(self, argv: Any, log: Path, *, pass_fds: Sequence[int] = ()) -> int:
        self.calls.append((list(argv), log))
        # The descriptor is closed when arm returns, so read what it names now.
        self.passed = [
            (fd, os.readlink(f"/proc/self/fd/{fd}"), os.fstat(fd).st_ino) for fd in pass_fds
        ]
        if self.error is not None:
            raise self.error
        return self.pid


class Signals:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        self.sent.append((pid, sig))


@pytest.fixture
def helper(tmp_path: Path) -> Path:
    path = tmp_path / "ginn-watch"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _hooks(
    helper: Path,
    spawn: Recorder,
    *,
    ticks: Callable[[int], int | None] | None = None,
    kill: Signals | None = None,
    watching: bool = True,
) -> WatchHooks:
    return WatchHooks(
        helper=str(helper),
        program="/usr/bin/ginn",
        spawn=spawn,
        start_ticks=ticks or (lambda pid: INIT_START if pid == INIT else 500 + pid),
        watching=lambda pid: watching,
        kill=kill or Signals(),
    )


def _arm(paths: OwnedPaths, hooks: WatchHooks) -> Any:
    return arm(paths, NAME, init_pid=INIT, hooks=hooks)


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-045")
@pytest.mark.fr("NFR-009")
def test_arm_starts_ginn_watch_on_the_stamp_directory_for_the_containers_init(
    paths: OwnedPaths, helper: Path
) -> None:
    directory = _make_stamp_dir(paths)
    spawn = Recorder()
    assert _arm(paths, _hooks(helper, spawn)) is None

    argv, log = spawn.calls[0]
    assert argv == [
        str(helper),
        "--exit-with",
        str(INIT),
        f"/proc/self/fd/{spawn.passed[0][0]}",
        "500",
        "1500",
        "/usr/bin/ginn",
        "_sync",
        "lab",
    ]
    assert spawn.passed == [(spawn.passed[0][0], str(directory), directory.stat().st_ino)]
    assert log == paths.runtime / "watch" / "lab.log"
    record = json.loads((paths.runtime / "watch" / "lab.json").read_text(encoding="utf-8"))
    assert record == {
        "watcher_pid": WATCHER,
        "watcher_start": 500 + WATCHER,
        "init_pid": INIT,
        "init_start": INIT_START,
        "rearms": 0,
        "rearm_at": 0.0,
    }


@pytest.mark.fr("NFR-009")
def test_the_debounce_leaves_the_sync_most_of_the_five_seconds() -> None:
    assert watch.QUIET_MS + 0 <= 500
    assert watch.MAX_MS <= 1500
    assert 5000 - watch.MAX_MS >= 3500


@pytest.mark.fr("FR-044")
def test_a_start_for_the_same_init_with_a_live_watcher_starts_none(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_stamp_dir(paths)
    spawn = Recorder()
    kill = Signals()
    hooks = _hooks(helper, spawn, kill=kill)
    assert _arm(paths, hooks) is None
    monkeypatch.setattr(watch, "is_pid_alive", lambda pid, start: True)

    assert _arm(paths, hooks) is None

    assert len(spawn.calls) == 1
    assert kill.sent == []


@pytest.mark.fr("FR-044")
def test_a_live_watcher_of_an_earlier_init_is_stopped_before_the_new_one_starts(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_stamp_dir(paths)
    spawn = Recorder()
    kill = Signals()
    assert _arm(paths, _hooks(helper, spawn, kill=kill)) is None
    monkeypatch.setattr(watch, "is_pid_alive", lambda pid, start: True)

    other = Recorder(pid=5555)
    again = arm(
        paths,
        NAME,
        init_pid=INIT + 1,
        hooks=_hooks(
            helper,
            other,
            kill=kill,
            ticks=lambda pid: 12 if pid == INIT + 1 else 700 + pid,
        ),
    )

    assert again is None
    assert kill.sent == [(WATCHER, signal.SIGTERM)]
    assert len(other.calls) == 1


@pytest.mark.fr("FR-044")
def test_a_dead_watcher_record_is_replaced_without_a_signal(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_stamp_dir(paths)
    spawn = Recorder()
    kill = Signals()
    hooks = _hooks(helper, spawn, kill=kill)
    assert _arm(paths, hooks) is None
    monkeypatch.setattr(watch, "is_pid_alive", lambda pid, start: False)

    assert _arm(paths, hooks) is None

    assert len(spawn.calls) == 2
    assert kill.sent == []


@pytest.mark.fr("FR-044")
def test_a_missing_helper_is_a_warning_and_starts_nothing(
    paths: OwnedPaths, tmp_path: Path
) -> None:
    _make_stamp_dir(paths)
    spawn = Recorder()
    hooks = WatchHooks(helper=str(tmp_path / "absent"), program="/usr/bin/ginn", spawn=spawn)
    notice = _arm(paths, hooks)
    assert notice is not None
    assert notice.code == "GINN-W-EXPORT-SYNC"
    assert "is not installed" in notice.message
    assert spawn.calls == []


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize("program", [None, "ginn", "bin/ginn"])
def test_a_ginn_command_that_is_not_absolute_is_a_warning(
    paths: OwnedPaths, helper: Path, program: str | None
) -> None:
    _make_stamp_dir(paths)
    spawn = Recorder()
    hooks = WatchHooks(helper=str(helper), program=program, spawn=spawn)
    notice = _arm(paths, hooks)
    assert notice is not None
    assert "not an absolute path" in notice.message
    assert spawn.calls == []


@pytest.mark.fr("CR-006")
def test_no_stamp_directory_means_no_watcher(paths: OwnedPaths, helper: Path) -> None:
    spawn = Recorder()
    notice = _arm(paths, _hooks(helper, spawn))
    assert notice is not None
    assert "missing or a link" in notice.message
    assert spawn.calls == []


@pytest.mark.fr("FR-044")
def test_an_init_that_is_gone_is_a_warning(paths: OwnedPaths, helper: Path) -> None:
    _make_stamp_dir(paths)
    spawn = Recorder()
    notice = _arm(paths, _hooks(helper, spawn, ticks=lambda pid: None))
    assert notice is not None
    assert "not running" in notice.message
    assert spawn.calls == []


@pytest.mark.fr("FR-044")
def test_a_failed_spawn_and_a_watcher_that_exits_at_once_are_warnings(
    paths: OwnedPaths, helper: Path
) -> None:
    _make_stamp_dir(paths)
    spawn = Recorder()
    spawn.error = OSError(2, "No such file or directory")
    failed = _arm(paths, _hooks(helper, spawn))
    assert failed is not None
    assert "did not start" in failed.message

    gone = _arm(
        paths, _hooks(helper, Recorder(), ticks=lambda pid: INIT_START if pid == INIT else None)
    )
    assert gone is not None
    assert "exited at once" in gone.message
    assert not (paths.runtime / "watch" / "lab.json").exists()


@pytest.mark.fr("FR-019")
def test_remove_deletes_the_record_and_the_log_and_ignores_absent_files(
    paths: OwnedPaths, helper: Path
) -> None:
    _make_stamp_dir(paths)
    assert _arm(paths, _hooks(helper, Recorder())) is None
    (paths.runtime / "watch" / "lab.log").write_text("x", encoding="utf-8")

    watch.remove(paths, NAME)
    watch.remove(paths, NAME)

    assert not (paths.runtime / "watch" / "lab.json").exists()
    assert not (paths.runtime / "watch" / "lab.log").exists()
    assert watch.read_record(paths, NAME) is None


# ---- rearm ------------------------------------------------------------------------------------


def _armed(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Recorder, Signals, WatchHooks]:
    _make_stamp_dir(paths)
    spawn = Recorder()
    kill = Signals()
    hooks = _hooks(helper, spawn, kill=kill)
    assert _arm(paths, hooks) is None
    monkeypatch.setattr(watch, "is_pid_alive", lambda pid, start: True)
    return spawn, kill, hooks


@pytest.mark.fr("FR-044")
def test_rearm_does_nothing_for_a_realm_without_a_watcher_record(
    paths: OwnedPaths, helper: Path
) -> None:
    spawn = Recorder()
    assert rearm(paths, NAME, hooks=_hooks(helper, spawn)) is None
    assert spawn.calls == []
    assert not (paths.homes / NAME.value / ".local").exists()


@pytest.mark.fr("FR-044")
def test_rearm_leaves_a_live_watcher_of_the_present_directory_alone(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn, kill, hooks = _armed(paths, helper, monkeypatch)
    assert rearm(paths, NAME, hooks=hooks) is None
    assert len(spawn.calls) == 1
    assert kill.sent == []


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-045")
def test_rearm_makes_a_removed_directory_again_and_starts_a_fresh_watcher(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The realm cleaned `~/.local/state`: the watcher got status 3; its last sync calls rearm."""
    import shutil

    spawn, kill, hooks = _armed(paths, helper, monkeypatch)
    shutil.rmtree(paths.homes / NAME.value / ".local")
    hooks = replace(hooks, watching=lambda pid: False)  # the kernel dropped the watch

    assert rearm(paths, NAME, hooks=hooks) is None

    made = paths.homes / NAME.value / ".local" / "state" / "ginn-export"
    assert made.is_dir()
    assert (made.stat().st_mode & 0o777) == 0o700
    assert len(spawn.calls) == 2
    assert kill.sent == [(WATCHER, signal.SIGTERM)]
    assert spawn.passed == [(spawn.passed[0][0], str(made), made.stat().st_ino)]


@pytest.mark.fr("FR-044")
def test_rearm_follows_a_directory_that_the_realm_made_again_with_another_inode(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The realm made it again before the last sync ran: same path, maybe the same inode."""
    spawn, _kill, hooks = _armed(paths, helper, monkeypatch)
    directory = paths.homes / NAME.value / ".local" / "state" / "ginn-export"
    import shutil

    shutil.rmtree(directory)
    directory.mkdir()
    hooks = replace(hooks, watching=lambda pid: False)  # same path, maybe the same inode number
    assert rearm(paths, NAME, hooks=hooks) is None
    assert len(spawn.calls) == 2


@pytest.mark.fr("FR-044")
def test_rearm_starts_a_watcher_that_died(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn, kill, hooks = _armed(paths, helper, monkeypatch)
    monkeypatch.setattr(watch, "is_pid_alive", lambda pid, start: False)
    assert rearm(paths, NAME, hooks=hooks) is None
    assert len(spawn.calls) == 2
    assert kill.sent == []


@pytest.mark.fr("FR-044")
def test_rearm_does_nothing_once_the_container_init_is_gone(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn, _kill, _hooks_ = _armed(paths, helper, monkeypatch)
    stopped = _hooks(helper, spawn, ticks=lambda pid: None)
    assert rearm(paths, NAME, hooks=stopped) is None
    assert len(spawn.calls) == 1


@pytest.mark.fr("CR-006")
def test_rearm_makes_nothing_through_a_link_and_says_so(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shutil

    spawn, _kill, hooks = _armed(paths, helper, monkeypatch)
    home = paths.homes / NAME.value
    shutil.rmtree(home / ".local")
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".local").symlink_to(outside)

    notice = rearm(paths, NAME, hooks=hooks)

    assert notice is not None
    assert "cannot be made again" in notice.message
    assert list(outside.iterdir()) == []
    assert len(spawn.calls) == 1


@pytest.mark.fr("FR-044")
def test_rearm_is_off_when_the_watch_is_disabled(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn, _kill, _hooks_ = _armed(paths, helper, monkeypatch)
    off = WatchHooks(enabled=False)
    assert rearm(paths, NAME, hooks=off) is None


# ---- the hook file ----------------------------------------------------------------------------


@pytest.mark.fr("FR-044")
def test_the_hook_is_written_to_a_temporary_name_and_moved_over_the_hook(tmp_path: Path) -> None:
    """A package manager that reads the file at once never sees half of it (a live realm)."""
    import subprocess

    target = tmp_path / "etc" / "hooks" / "ginn-export.hook"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    done = subprocess.run(  # noqa: S603 - fixed script, argv, temp dir
        ["sh", "-c", watch._WRITE_HOOK, "sh", str(target), "new text\n"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert done.returncode == 0, done.stderr
    assert target.read_text(encoding="utf-8") == "new text\n"
    assert [p.name for p in target.parent.iterdir()] == ["ginn-export.hook"]
    assert ".tmp" in watch._WRITE_HOOK and "mv -f" in watch._WRITE_HOOK


@pytest.mark.fr("FR-044")
def test_a_watch_is_seen_in_fdinfo_and_is_gone_with_its_directory(tmp_path: Path) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(0)
    assert fd >= 0
    try:
        directory = tmp_path / "d"
        directory.mkdir()
        wd = libc.inotify_add_watch(fd, str(directory).encode(), 0x00000100)
        assert wd >= 0
        info = Path(f"/proc/{os.getpid()}/fdinfo/{fd}").read_text(encoding="utf-8")
        assert "inotify wd:" in info
        assert watch.has_inotify_watch(os.getpid()) is True
        directory.rmdir()
        info = Path(f"/proc/{os.getpid()}/fdinfo/{fd}").read_text(encoding="utf-8")
        assert "inotify wd:" not in info
    finally:
        os.close(fd)
    assert watch.has_inotify_watch(2**22 + 1) is False


# ---- owner of the stamp, the re-arm limit, the stamp script -----------------------------------


@pytest.mark.fr("CR-006")
def test_a_stamp_or_marker_owned_by_another_uid_is_unlinked_and_the_stamp_made_again(
    paths: OwnedPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Container root makes files owned by a subordinate id. The directory is the host user's, so
    the host removes them. Here every file counts as foreign: the host user is someone else."""
    directory = _make_stamp_dir(paths, marker=CID)
    old = (directory / STAMP_NAME).stat().st_ino
    monkeypatch.setattr(watch, "_host_uid", lambda: os.getuid() + 1)
    with open_stamp_dir(paths, NAME) as fd:
        assert fd is not None
        watch.own_stamp_files(fd)
    assert not (directory / MARKER_NAME).exists()
    assert (directory / STAMP_NAME).is_file()
    assert (directory / STAMP_NAME).read_bytes() == b""
    del old  # the inode number may be reused; the owner is what counts below
    monkeypatch.setattr(watch, "_host_uid", os.getuid)
    (directory / MARKER_NAME).write_text(CID, encoding="utf-8")
    with open_stamp_dir(paths, NAME) as fd:
        assert fd is not None
        watch.own_stamp_files(fd)
    assert (directory / MARKER_NAME).read_text(encoding="utf-8") == CID  # ours: kept


@pytest.mark.fr("CR-006")
def test_a_missing_stamp_is_made_by_the_host_so_the_hooks_touch_never_creates_it(
    paths: OwnedPaths,
) -> None:
    directory = _make_stamp_dir(paths)
    (directory / STAMP_NAME).unlink()
    with open_stamp_dir(paths, NAME) as fd:
        assert fd is not None
        watch.own_stamp_files(fd)
    assert (directory / STAMP_NAME).stat().st_uid == os.getuid()


@pytest.mark.fr("CR-006")
def test_arm_makes_the_stamp_the_host_users_before_it_starts_the_watcher(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _make_stamp_dir(paths, marker=CID)
    monkeypatch.setattr(watch, "_host_uid", lambda: os.getuid() + 1)
    spawn = Recorder()
    assert _arm(paths, _hooks(helper, spawn)) is None
    assert not (directory / MARKER_NAME).exists()
    assert len(spawn.calls) == 1


@pytest.mark.fr("CR-006")
def test_the_stamp_script_unlinks_a_stamp_and_marker_that_exist_before_it_creates_them(
    tmp_path: Path,
) -> None:
    import subprocess

    directory = tmp_path / "d"
    directory.mkdir()
    (directory / STAMP_NAME).write_text("old", encoding="utf-8")
    (directory / MARKER_NAME).write_text("old", encoding="utf-8")
    before = [(directory / n).stat().st_ino for n in (STAMP_NAME, MARKER_NAME)]
    done = subprocess.run(  # noqa: S603 - the fixed script with argv, in a temp dir
        ["sh", "-c", watch._WRITE_STAMP, "sh", str(directory), STAMP_NAME, CID, MARKER_NAME],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert done.returncode == 0, done.stderr
    assert (directory / STAMP_NAME).read_bytes() == b""
    assert (directory / MARKER_NAME).read_text(encoding="utf-8") == CID
    assert "rm -f" in watch._WRITE_STAMP
    del before  # a freed inode number can be handed out again at once


@pytest.mark.fr("FR-044")
def test_more_than_three_rearms_in_a_minute_are_refused_with_a_warning(
    paths: OwnedPaths, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    spawn, _kill, hooks = _armed(paths, helper, monkeypatch)
    now = [1000.0]
    hooks = replace(hooks, watching=lambda pid: False, clock=lambda: now[0])
    home = paths.homes / NAME.value
    for _ in range(3):
        shutil.rmtree(home / ".local")
        assert rearm(paths, NAME, hooks=hooks) is None
        now[0] += 5.0
    shutil.rmtree(home / ".local")

    notice = rearm(paths, NAME, hooks=hooks)

    assert notice is not None
    assert notice.code == "GINN-W-EXPORT-SYNC"
    assert "more than 3 times" in notice.message
    assert len(spawn.calls) == 4  # the start's and three re-arms
    assert not (home / ".local").exists(), "a refused re-arm makes nothing"
    record = watch.read_record(paths, NAME)
    assert record is not None
    assert (record.rearms, record.rearm_at) == (3, 1000.0)

    now[0] += 61.0  # the minute is over
    assert rearm(paths, NAME, hooks=hooks) is None
    record = watch.read_record(paths, NAME)
    assert record is not None
    assert record.rearms == 1
    assert len(spawn.calls) == 5
