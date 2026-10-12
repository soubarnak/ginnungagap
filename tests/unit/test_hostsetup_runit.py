# SPDX-License-Identifier: Apache-2.0
"""System runit service for the user's runsvdir: golden files, shell checks, install.

Covers FR-001 (Host Setup writes the service and the delegated cgroup leaves), FR-081 (runit
supervises the user's realms), FR-084 (the service starts at boot, so autostart can work),
and NFR-001 (the resident supervisor is runsvdir, which is allowed 1 MiB RSS).
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from ginn.hostsetup.cgroups import CONTROLLERS, DEFAULT_CGROUP_ROOT, chown_paths
from ginn.hostsetup.runit_system import install, render

pytestmark = [pytest.mark.fr("FR-001"), pytest.mark.fr("FR-081")]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hostsetup" / "ginn-alice"
XDG = {
    "XDG_STATE_HOME": "/home/alice/.local/state",
    "XDG_DATA_HOME": "/home/alice/.local/share",
    "XDG_CONFIG_HOME": "/home/alice/.config",
}
RUN = "etc/sv/ginn-alice/run"
FINISH = "etc/sv/ginn-alice/finish"
LOG_RUN = "etc/sv/ginn-alice/log/run"


def _alice() -> dict[str, str]:
    return render("alice", 1000, 1000, "/home/alice", XDG)


def _exec_command(script: str) -> list[str]:
    """Return the words of the final exec line, with line continuations joined."""
    joined = script.replace("\\\n", " ")
    exec_lines = [line for line in joined.splitlines() if line.startswith("exec chpst")]
    assert len(exec_lines) == 1
    return shlex.split(exec_lines[0])


def test_rendered_scripts_match_the_golden_files() -> None:
    rendered = _alice()
    assert rendered[RUN] == (FIXTURES / "run").read_text(encoding="utf-8")
    assert rendered[FINISH] == (FIXTURES / "finish").read_text(encoding="utf-8")
    assert rendered[LOG_RUN] == (FIXTURES / "log-run").read_text(encoding="utf-8")


def test_scripts_pass_sh_syntax_check(tmp_path: Path) -> None:
    sh = shutil.which("sh")
    assert sh is not None
    for name, text in _alice().items():
        script = tmp_path / name.replace("/", "_")
        script.write_text(text, encoding="utf-8")
        result = subprocess.run(  # noqa: S603
            [sh, "-n", str(script)], capture_output=True, check=False
        )
        assert result.returncode == 0, result.stderr.decode()


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_scripts_are_shellcheck_clean(tmp_path: Path) -> None:
    shellcheck = shutil.which("shellcheck")
    assert shellcheck is not None
    for name, text in _alice().items():
        script = tmp_path / name.replace("/", "_")
        script.write_text(text, encoding="utf-8")
        result = subprocess.run(  # noqa: S603
            [shellcheck, "--shell=sh", str(script)], capture_output=True, check=False
        )
        assert result.returncode == 0, result.stdout.decode()


def test_run_chowns_exactly_the_paths_the_cgroup_module_lists() -> None:
    chowned: list[Path] = []
    for line in _alice()[RUN].splitlines():
        if line.startswith("chown 1000:1000 /sys/fs/cgroup/"):
            chowned.extend(Path(word) for word in line.split()[2:])
    assert sorted(chowned) == sorted(chown_paths(DEFAULT_CGROUP_ROOT, 1000))


def test_run_enables_the_same_controllers_as_the_cgroup_module() -> None:
    assert f"CONTROLLERS='{' '.join(sorted(CONTROLLERS))}'" in _alice()[RUN]


def test_run_moves_itself_into_the_supervisor_leaf_then_execs_runsvdir() -> None:
    script = _alice()[RUN]
    assert "printf '%s\\n' \"$$\" > /sys/fs/cgroup/ginn/1000/supervisor/cgroup.procs" in script
    words = _exec_command(script)
    assert words[:3] == ["exec", "chpst", "-u"]
    assert words[3] == "$USER_NAME:$GROUP_NAME"
    assert "runsvdir" in words
    assert words[words.index("runsvdir") :] == ["runsvdir", "-P", "$SV_DIR"]


@pytest.mark.fr("NFR-001")
def test_resident_process_after_exec_is_runsvdir() -> None:
    words = _exec_command(_alice()[RUN])
    assert words[-3:] == ["runsvdir", "-P", "$SV_DIR"]


def test_run_passes_the_xdg_and_runtime_environment_to_the_user_supervisor() -> None:
    words = _exec_command(_alice()[RUN])
    env: dict[str, str] = {}
    for word in words:
        name, sep, value = word.partition("=")
        if sep and (name == "HOME" or name.startswith(("XDG_", "GINN_"))):
            env[name] = value
    assert env == {
        "HOME": "$HOME_DIR",
        "XDG_RUNTIME_DIR": "$RUNTIME_DIR",
        "XDG_STATE_HOME": "$STATE_HOME",
        "XDG_DATA_HOME": "$DATA_HOME",
        "XDG_CONFIG_HOME": "$CONFIG_HOME",
        "GINN_RUNTIME_DIR": "$RUNTIME_DIR",
    }


def test_run_creates_the_runtime_dir_as_the_user_with_mode_0700() -> None:
    script = _alice()[RUN]
    assert "RUNTIME_DIR=/run/ginn/1000" in script
    assert 'mkdir -p "$RUNTIME_DIR"' in script
    assert 'chown "$UID_NUM:$GID_NUM" "$RUNTIME_DIR"' in script
    assert 'chmod 0700 "$RUNTIME_DIR"' in script


def test_run_waits_for_the_state_dir_and_gives_up_after_60_seconds() -> None:
    script = _alice()[RUN]
    assert 'while [ ! -d "$SV_DIR" ]; do' in script
    assert "-ge 60" in script
    assert "sleep 1" in script


def test_run_never_looks_up_accounts_at_runtime() -> None:
    for text in _alice().values():
        assert "getent" not in text
        assert "id -u" not in text


def test_log_run_creates_the_directory_as_the_user_before_svlogd() -> None:
    script = _alice()[LOG_RUN]
    assert "LOG_DIR=/home/alice/.local/state/ginn/log/supervisor" in script
    assert 'chpst -u "$USER_NAME:$GROUP_NAME" mkdir -p "$LOG_DIR"' in script
    assert script.rstrip().endswith('exec chpst -u "$USER_NAME:$GROUP_NAME" svlogd -tt "$LOG_DIR"')


def test_finish_only_exits_zero() -> None:
    assert _alice()[FINISH].splitlines()[-1] == "exit 0"


def test_values_are_quoted_so_odd_paths_cannot_inject_commands() -> None:
    odd = dict(XDG, XDG_STATE_HOME="/home/al ice/$(id)/.local/state")
    script = render("alice", 1000, 1000, "/home/al ice", odd)[RUN]
    assert "STATE_HOME='/home/al ice/$(id)/.local/state'" in script
    assert "HOME_DIR='/home/al ice'" in script


@pytest.mark.parametrize(
    ("user", "uid", "gid", "home", "extra", "message"),
    [
        ("alice", 0, 1000, "/home/alice", {}, "refusing"),
        ("alice", 1000, 0, "/home/alice", {}, "refusing"),
        ("al ice", 1000, 1000, "/home/alice", {}, "invalid user"),
        ("a;rm", 1000, 1000, "/home/alice", {}, "invalid user"),
        ("alice", 1000, 1000, "/home/alice", {"group": "a b"}, "invalid group"),
        ("alice", 1000, 1000, "home/alice", {}, "absolute"),
        ("alice", 1000, 1000, "/home/alice/", {}, "absolute"),
        ("alice", 1000, 1000, "/home/a\nb", {}, "control character"),
        ("alice", 1000, 1000, "/home/alice", {"runtime_dir": "run/x"}, "absolute"),
    ],
)
def test_bad_inputs_are_refused(
    user: str, uid: int, gid: int, home: str, extra: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        render(user, uid, gid, home, XDG, **extra)  # type: ignore[arg-type]


def test_missing_xdg_value_is_refused() -> None:
    partial = {key: value for key, value in XDG.items() if key != "XDG_DATA_HOME"}
    with pytest.raises(ValueError, match="XDG_DATA_HOME"):
        render("alice", 1000, 1000, "/home/alice", partial)


def test_group_and_runtime_dir_are_render_parameters() -> None:
    script = render(
        "alice",
        1000,
        1000,
        "/home/alice",
        XDG,
        group="users",
        runtime_dir="/run/ginn-test/1000",
    )[RUN]
    assert "GROUP_NAME=users" in script
    assert "RUNTIME_DIR=/run/ginn-test/1000" in script


@pytest.mark.fr("FR-084")
def test_install_writes_mode_0755_scripts_and_links_the_service_last(tmp_path: Path) -> None:
    written = install(tmp_path, _alice())
    sv = tmp_path / "etc" / "sv" / "ginn-alice"
    link = tmp_path / "var" / "service" / "ginn-alice"
    assert set(written) == {
        tmp_path / "etc",
        tmp_path / "etc" / "sv",
        sv,
        sv / "log",
        tmp_path / "var",
        tmp_path / "var" / "service",
        sv / "run",
        sv / "finish",
        sv / "log" / "run",
        link,
    }
    assert written[-1] == link
    for name in ("run", "finish", "log/run"):
        path = sv / name
        assert path.read_text(encoding="utf-8") == _alice()[f"etc/sv/ginn-alice/{name}"]
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o755
    assert os.readlink(link) == "/etc/sv/ginn-alice"


def test_second_install_changes_nothing(tmp_path: Path) -> None:
    install(tmp_path, _alice())
    assert install(tmp_path, _alice()) == []


def test_install_rewrites_only_a_file_whose_content_changed(tmp_path: Path) -> None:
    install(tmp_path, _alice())
    changed = dict(_alice())
    changed[FINISH] = changed[FINISH] + "# edited\n"
    written = install(tmp_path, changed)
    assert written == [tmp_path / "etc" / "sv" / "ginn-alice" / "finish"]


def test_install_refuses_a_regular_file_where_the_link_goes(tmp_path: Path) -> None:
    (tmp_path / "var" / "service").mkdir(parents=True)
    (tmp_path / "var" / "service" / "ginn-alice").write_text("not a link\n")
    with pytest.raises(ValueError, match="not a symlink"):
        install(tmp_path, _alice())


def test_install_refuses_a_rendered_set_that_is_not_one_service(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="expected exactly"):
        install(tmp_path, {RUN: "#!/bin/sh\n"})
