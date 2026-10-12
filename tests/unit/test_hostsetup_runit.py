# SPDX-License-Identifier: Apache-2.0
"""System runit service for the user's runsvdir: golden files and shell checks.

Covers FR-001 (the service scripts that Host Setup writes), FR-081 (the service that runit
supervises from var/service), and NFR-001 (the resident supervisor is runsvdir). NFR-001's 1 MiB
RSS and FR-084 (autostart at boot) need a booted runit host, so they are not proven here. These
tests check the rendered text only. setup.py writes the files and the link, and its tests cover
that.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from ginn.hostsetup.cgroups import CONTROLLERS, DEFAULT_CGROUP_ROOT, chown_paths
from ginn.hostsetup.runit_system import render

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
    """Return the words of the single `exec setpriv` line, with line continuations joined."""
    joined = script.replace("\\\n", " ")
    exec_lines = [line for line in joined.splitlines() if line.startswith("exec setpriv")]
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


@pytest.mark.parametrize("name", [RUN, LOG_RUN], ids=["run", "log-run"])
def test_the_script_sets_umask_022_before_its_first_mkdir(name: str) -> None:
    """The delegated cgroup directory is 0755 whatever umask runit passed in (ADR-013, Decision 27).

    The log directory is made by the same kind of script, so its script sets the umask too.
    """
    lines = _alice()[name].splitlines()
    umask_at = lines.index("umask 022")
    mkdirs = [index for index, line in enumerate(lines) if line.startswith("mkdir ")]
    assert mkdirs, name
    assert umask_at < mkdirs[0], name


def test_run_chowns_exactly_the_paths_the_cgroup_module_lists() -> None:
    chowned: list[Path] = []
    for line in _alice()[RUN].splitlines():
        if line.startswith("chown 1000:1000 /sys/fs/cgroup/"):
            chowned.extend(Path(word) for word in line.split()[2:])
    assert sorted(chowned) == sorted(chown_paths(DEFAULT_CGROUP_ROOT, 1000))


@pytest.mark.fr("FR-001")
def test_run_chowns_nothing_under_the_supervisor_leaf() -> None:
    # The leaf stays root-owned (ADR-003): the run script writes the supervisor pid into it as root.
    leaf = f"{DEFAULT_CGROUP_ROOT}/ginn/1000/supervisor"
    for line in _alice()[RUN].splitlines():
        if line.startswith("chown "):
            for word in line.split()[2:]:
                assert word != leaf and not word.startswith(f"{leaf}/"), line


def test_run_enables_the_same_controllers_as_the_cgroup_module() -> None:
    assert f"CONTROLLERS='{' '.join(sorted(CONTROLLERS))}'" in _alice()[RUN]


def test_run_moves_itself_into_the_supervisor_leaf_then_execs_setpriv_and_runsvdir() -> None:
    script = _alice()[RUN]
    assert "printf '%s\\n' \"$$\" > /sys/fs/cgroup/ginn/1000/supervisor/cgroup.procs" in script
    words = _exec_command(script)
    assert words[:4] == ["exec", "setpriv", "--reuid=$UID_NUM", "--regid=$GID_NUM"]
    assert "--init-groups" in words
    assert "--reset-env" in words
    assert words[words.index("--reset-env") + 1] == "env"
    assert words[words.index("runsvdir") :] == ["runsvdir", "-P", "$SV_DIR"]


def test_every_setpriv_drop_clears_the_inheritable_capabilities() -> None:
    # setpriv(1): when dropping from root, --inh-caps=-all clears the inheritable set as well.
    expected = {RUN: 1, LOG_RUN: 2}  # run: the exec drop; log/run: the mkdir drop and the exec drop
    for name, count in expected.items():
        joined = _alice()[name].replace("\\\n", " ")
        drops = [line for line in joined.splitlines() if "setpriv --reuid" in line]
        assert len(drops) == count, name
        for line in drops:
            assert "--inh-caps=-all" in shlex.split(line), name


def test_no_service_script_drops_privileges_with_chpst() -> None:
    # chpst -u user:group keeps only the primary group. setpriv --init-groups keeps the rest.
    for name, text in _alice().items():
        assert "chpst" not in text, name
        if name != FINISH:
            assert "--init-groups" in text, name


@pytest.mark.fr("FR-001")
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
    # XDG_RUNTIME_DIR is kept because podman reads it for its rootless runtime directory.
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


def test_run_does_no_account_lookup_of_its_own() -> None:
    # setpriv --init-groups reads the groups itself. The scripts never call getent or id.
    for text in _alice().values():
        assert "getent" not in text
        assert "id -u" not in text


def test_log_run_creates_the_directory_as_the_user_before_svlogd() -> None:
    script = _alice()[LOG_RUN]
    assert "LOG_DIR=/home/alice/.local/state/ginn/log/supervisor" in script
    # The mkdir line is continued with a backslash, so compare it with the continuations joined.
    flat = " ".join(script.replace("\\\n", " ").split())
    mkdir_line = (
        'setpriv --reuid="$UID_NUM" --regid="$GID_NUM" --init-groups --inh-caps=-all --reset-env '
        'mkdir -p "$LOG_DIR"'
    )
    assert mkdir_line in flat
    words = _exec_command(script)
    assert words[:7] == [
        "exec",
        "setpriv",
        "--reuid=$UID_NUM",
        "--regid=$GID_NUM",
        "--init-groups",
        "--inh-caps=-all",
        "--reset-env",
    ]
    assert words[7:] == ["svlogd", "-tt", "$LOG_DIR"]


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


def test_runtime_dir_is_a_render_parameter() -> None:
    script = render("alice", 1000, 1000, "/home/alice", XDG, runtime_dir="/run/ginn-test/1000")[RUN]
    assert "RUNTIME_DIR=/run/ginn-test/1000" in script


def test_primary_gid_is_numeric_and_never_derived_from_the_user_name() -> None:
    # alice's primary group is gid 100 ("users"). Nothing may fall back to the name "alice".
    rendered = render("alice", 1000, 100, "/home/alice", XDG)
    for text in rendered.values():
        assert "GROUP_NAME" not in text
    script = rendered[RUN]
    assert "GID_NUM=100" in script
    assert _exec_command(script)[3] == "--regid=$GID_NUM"
    assert "chown 1000:100 /sys/fs/cgroup/ginn/1000 " in script
