# SPDX-License-Identifier: Apache-2.0
"""System runit service for the user's runsvdir: golden files and shell checks.

Covers FR-001 (the service scripts that Host Setup writes), FR-081 (the service that runit
supervises from var/service), and NFR-001 (the resident supervisor is runsvdir). NFR-001's 1 MiB
RSS and FR-084 (autostart at boot) need a booted runit host, so they are not proven here. These
tests check the rendered text only. setup.py writes the files and the link, and its tests cover
that.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
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


def test_finish_stops_the_runsv_children_it_finds_in_the_supervisor_leaf() -> None:
    """runsvdir exits at once on TERM and leaves its runsv children running (runsvdir(8)). The
    finish script sends them TERM, as runsvdir does on HUP, and only to processes named runsv
    (ADR-013, Decision 26)."""
    script = _alice()[FINISH]
    assert "LEAF=/sys/fs/cgroup/ginn/1000/supervisor\n" in script
    assert 'kill -TERM "$pid"' in script
    assert 'if [ "$name" = runsv ]; then' in script
    assert script.splitlines()[-1] == "exit 0"


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


def test_the_run_wait_sleeps_in_the_background_under_a_trap() -> None:
    """A signal to the shell ends the pending sleep too, and the trap is set before the loop and
    reset after it (ADR-013, Decision 26). The kill is followed by a wait that reaps the sleep."""
    script = _alice()[RUN]
    trap_at = script.index("trap stop_wait TERM INT HUP")
    loop_at = script.index('while [ ! -d "$SV_DIR" ]; do')
    reset_at = script.index("trap - TERM INT HUP")
    assert trap_at < loop_at < reset_at
    assert "sleep 1 &" in script
    assert 'wait "$sleep_pid"' in script
    assert (
        'kill "$sleep_pid" 2>/dev/null || :\n        wait "$sleep_pid" 2>/dev/null || :' in script
    )


def test_the_log_wait_holds_a_reader_on_its_input_and_stops_it_before_svlogd() -> None:
    script = _alice()[LOG_RUN]
    assert "exec 3<&0\ncat <&3 >/dev/null &\nreader=$!" in script
    assert 'if ! kill -0 "$reader" 2>/dev/null; then' in script
    assert "trap stop_wait TERM INT HUP" in script
    assert script.index("\nstop_reader\n") < script.index("exec setpriv")
    assert script.index("exec 3<&-") < script.index("exec setpriv")


def _group_members(pgid: int) -> list[tuple[str, str]]:
    """(command, state) of each process in process group `pgid`, read from /proc (Linux only)."""
    members: list[tuple[str, str]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            text = Path("/proc", entry, "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        head, _, rest = text.rpartition(")")
        fields = rest.split()
        if len(fields) > 2 and int(fields[2]) == pgid:
            members.append((head.partition("(")[2], fields[0]))
    return members


def _live(pgid: int) -> list[tuple[str, str]]:
    """The members of `pgid` that are not zombies."""
    return [member for member in _group_members(pgid) if member[1] != "Z"]


def _wait_for(condition: Callable[[], bool], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def _extract_block(script: str, start: str, end: str) -> str:
    """The lines strictly between the line that starts with `start` and the one with `end`."""
    lines = script.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    last = next(i for i, line in enumerate(lines) if line.startswith(end))
    return "\n".join(lines[first + 1 : last])


def _wait_harness(block: str, sv_dir: Path) -> str:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "log() {\n"
        "    printf '%s\\n' \"$*\" >&2\n"
        "}\n"
        f"SV_DIR={sv_dir}\n" + block + "\nexit 2\n"
    )


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="needs /proc")
def test_a_term_during_the_run_wait_ends_the_shell_and_its_sleep_within_0_3_seconds(
    tmp_path: Path,
) -> None:
    """The rendered wait runs on the real sh, with a 30 s sleep so that the shell is blocked in
    `wait`. TERM must end the shell and the sleep within 0.3 s. A trap without the kill leaves the
    sleep running, and a trap that does not interrupt `wait` leaves the shell blocked on it."""
    sh = shutil.which("sh")
    assert sh is not None
    block = _extract_block(_alice()[RUN], "# 4.", "# 5.")
    assert "sleep 1 &" in block
    script = tmp_path / "wait.sh"
    script.write_text(_wait_harness(block.replace("sleep 1 &", "sleep 30 &"), tmp_path / "never"))
    proc = subprocess.Popen([sh, str(script)], start_new_session=True)  # noqa: S603
    try:
        _wait_for(lambda: ("sleep", "S") in _group_members(proc.pid), 5.0)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=0.3) == 0
        _wait_for(lambda: not _live(proc.pid), 0.3 - (time.monotonic() - started))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def _log_script(tmp_path: Path) -> Path:
    """The rendered log script, with its state directory pointed at a path that never appears."""
    text = _alice()[LOG_RUN]
    rendered = "SV_DIR=/home/alice/.local/state/ginn/sv"
    assert text.count(rendered) == 1
    path = tmp_path / "log-run.sh"
    path.write_text(text.replace(rendered, f"SV_DIR={tmp_path / 'never'}"), encoding="utf-8")
    return path


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="needs /proc")
def test_closing_the_log_input_ends_the_log_wait_promptly(tmp_path: Path) -> None:
    """runsv ends the log wait by closing the input (runsv(8), command x). The wait goes on while
    the input is open, and ends within about two ticks of the close (ADR-013, Decision 26)."""
    sh = shutil.which("sh")
    assert sh is not None
    proc = subprocess.Popen(  # noqa: S603
        [sh, str(_log_script(tmp_path))], stdin=subprocess.PIPE, start_new_session=True
    )
    try:
        time.sleep(1.5)
        assert proc.poll() is None, "the wait ended while its input was still open"
        started = time.monotonic()
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=4.0) == 0
        assert time.monotonic() - started < 3.0
        assert not _live(proc.pid), "the reader or the sleep is still in the group"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="needs /proc")
def test_a_term_during_the_log_wait_ends_its_reader_and_sleep_within_0_3_seconds(
    tmp_path: Path,
) -> None:
    sh = shutil.which("sh")
    assert sh is not None
    proc = subprocess.Popen(  # noqa: S603
        [sh, str(_log_script(tmp_path))], stdin=subprocess.PIPE, start_new_session=True
    )
    try:
        _wait_for(
            lambda: (
                ("cat", "S") in _group_members(proc.pid)
                and ("sleep", "S") in _group_members(proc.pid)
            ),
            5.0,
        )
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=0.3) == 0
        _wait_for(lambda: not _live(proc.pid), 0.3 - (time.monotonic() - started))
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def _fake_runsv(bin_dir: Path, record: Path, leaf: Path, keep: int | None) -> Path:
    """A stand-in runsv. On TERM it notes the signal and leaves the leaf as a runsv that has stopped
    its service does: `keep`, when given, stays in cgroup.procs, and cgroup.events then reports the
    leaf populated. Otherwise the leaf is empty and reports populated 0."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "runsv"
    procs, events = leaf / "cgroup.procs", leaf / "cgroup.events"
    if keep is None:
        left = f': > "{procs}"; printf "populated 0\\n" > "{events}"'
    else:
        left = f'printf "%s\\n" {keep} > "{procs}"; printf "populated 1\\n" > "{events}"'
    on_term = f'printf TERM >> "{record}"; {left}; exit 0'
    path.write_text(
        f"#!/bin/sh\ntrap '{on_term}' TERM\nwhile :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _finish_for_test(tmp_path: Path, leaf: Path) -> Path:
    """The rendered finish script, with its leaf moved under tmp_path and a 2 s drain."""
    text = _alice()[FINISH]
    text = text.replace("LEAF=/sys/fs/cgroup/ginn/1000/supervisor", f"LEAF={leaf}")
    text = text.replace("DRAIN_SECONDS=20", "DRAIN_SECONDS=2")
    path = tmp_path / "finish.sh"
    path.write_text(text, encoding="utf-8")
    return path


def _make_leaf(tmp_path: Path, procs: str, populated: int) -> Path:
    """A fake leaf. cgroup.procs is a plain file here, so its size is real. On cgroupfs it is 0
    whatever it holds, which is why finish must not test it for size (ADR-013, Decision 26)."""
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "cgroup.procs").write_text(procs, encoding="utf-8")
    (leaf / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n", encoding="utf-8")
    return leaf


def _finish_run(sh: str, script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sh, str(script)], capture_output=True, text=True, timeout=10, check=False
    )


def test_finish_never_tests_the_size_of_a_cgroup_file() -> None:
    """cgroupfs reports size 0 for cgroup.procs while it holds pids, so `-s` on the leaf would end
    the drain at once. The drain reads cgroup.events instead (ADR-013, Decision 26)."""
    script = _alice()[FINISH]
    assert "[ -s" not in script
    assert '-s "$LEAF' not in script
    assert "grep -q '^populated 1' \"$LEAF/cgroup.events\"" in script


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="needs /proc")
def test_finish_waits_while_a_child_cgroup_is_populated_though_procs_is_empty(
    tmp_path: Path,
) -> None:
    """The leaf's own cgroup.procs is empty, and a child cgroup holds processes. cgroupfs reports
    the leaf populated, so finish waits, and it ends once the child empties (cgroup.events)."""
    sh = shutil.which("sh")
    assert sh is not None
    leaf = _make_leaf(tmp_path, procs="", populated=1)
    script = _finish_for_test(tmp_path, leaf)
    timer = threading.Timer(1.0, (leaf / "cgroup.events").write_text, ("populated 0\n",))
    timer.start()
    try:
        started = time.monotonic()
        result = _finish_run(sh, script)
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()
    assert result.returncode == 0
    assert elapsed >= 0.9, "finish returned while the leaf was still populated"
    assert elapsed < 2.0, "finish did not end once the leaf emptied"
    assert "still holds" not in result.stderr


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="needs /proc")
def test_finish_sends_term_to_the_runsv_only_and_waits_for_the_leaf(tmp_path: Path) -> None:
    """A fake runsv and a fake service share the leaf. Finish sends TERM to the runsv alone, then
    waits while the leaf stays populated. The service is still there after the bound, so finish
    reports it and exits 0 (ADR-013, Decision 26). Real sh, and a fake leaf."""
    sh = shutil.which("sh")
    assert sh is not None
    record = tmp_path / "signals"
    sleeper = subprocess.Popen(["sleep", "30"])  # noqa: S603, S607
    leaf = _make_leaf(tmp_path, procs="", populated=1)
    fake = _fake_runsv(tmp_path / "bin", record, leaf, keep=sleeper.pid)
    runsv = subprocess.Popen([str(fake)])  # noqa: S603
    try:
        (leaf / "cgroup.procs").write_text(f"{runsv.pid}\n{sleeper.pid}\n", encoding="utf-8")
        result = _finish_run(sh, _finish_for_test(tmp_path, leaf))
        assert result.returncode == 0
        assert record.read_text(encoding="utf-8") == "TERM"
        assert sleeper.poll() is None, "finish signalled a process that is not runsv"
        assert "still holds processes" in result.stderr
    finally:
        for child in (sleeper, runsv):
            if child.poll() is None:
                child.kill()
            child.wait()


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="needs /proc")
def test_finish_returns_at_once_when_the_runsv_leaves_an_empty_leaf(tmp_path: Path) -> None:
    sh = shutil.which("sh")
    assert sh is not None
    record = tmp_path / "signals"
    leaf = _make_leaf(tmp_path, procs="", populated=1)
    fake = _fake_runsv(tmp_path / "bin", record, leaf, keep=None)
    runsv = subprocess.Popen([str(fake)])  # noqa: S603
    try:
        (leaf / "cgroup.procs").write_text(f"{runsv.pid}\n", encoding="utf-8")
        started = time.monotonic()
        result = _finish_run(sh, _finish_for_test(tmp_path, leaf))
        assert result.returncode == 0
        assert time.monotonic() - started < 2.5
        assert "still holds" not in result.stderr
        assert record.read_text(encoding="utf-8") == "TERM"
    finally:
        if runsv.poll() is None:
            runsv.kill()
        runsv.wait()
