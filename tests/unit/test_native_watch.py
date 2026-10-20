# SPDX-License-Identifier: Apache-2.0
"""native/ginn-watch.c: it compiles clean, stays within 200 lines, validates its argv, and drives
a command after changes to desktop files in a directory (FR-044, FR-045, NFR-009).

Every case runs the helper on a temporary directory with a stub command that writes its argv to a
file. Nothing here runs podman, and no realm is involved: the realm's application directory is the
path a caller gives the helper. Where ginn starts the helper is M7 (services).
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "native"
SOURCE = NATIVE / "ginn-watch.c"
WARNINGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2"]
LINE_LIMIT = 200

pytestmark = [pytest.mark.fr("FR-044"), pytest.mark.fr("NFR-SEC-005")]
QUIET_MS = 300
MAX_MS = 400
WAIT_S = 10.0


def _compiler() -> str | None:
    return shutil.which(os.environ.get("CC", "cc"))


@pytest.fixture(scope="module")
def helper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cc = _compiler()
    if cc is None:
        pytest.skip("no C compiler on PATH")
    out = tmp_path_factory.mktemp("watch") / "ginn-watch"
    built = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [cc, *WARNINGS, "-o", str(out), str(SOURCE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    return out


class _Lines:
    """Lines from a process's stdout, read with a timeout so a test never waits forever."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._buf = b""

    def next(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([self._fd], [], [], left)
            if not ready:
                return None
            chunk = os.read(self._fd, 4096)
            if not chunk:
                return None
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line.decode("utf-8")

    def until(self, prefix: str, timeout: float = WAIT_S) -> str:
        """The next line that starts with `prefix`. Fails the test when none comes in time."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.next(max(deadline - time.monotonic(), 0.0))
            if line is None:
                break
            if line.startswith(prefix):
                return line
        pytest.fail(f"no line starting with {prefix!r} within {timeout} s")


def _stub(tmp_path: Path, name: str = "hook.sh", status: int = 0) -> tuple[Path, Path]:
    """A command that appends its argv to a log, one argument per line, then exits `status`."""
    log = tmp_path / f"{name}.log"
    script = tmp_path / name
    script.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{log}'\nexit {status}\n", encoding="utf-8"
    )
    script.chmod(0o755)
    return script, log


_STARTED: list[subprocess.Popen[bytes]] = []
"""Every watch that `_start` made. A test that fails before its own stop leaves one running."""


@pytest.fixture(autouse=True)
def _no_watch_left_running() -> Iterator[None]:
    """Kill the watches that the test started and did not stop, so none outlives its test."""
    yield
    while _STARTED:
        proc = _STARTED.pop()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=WAIT_S)


def _start(
    helper: Path,
    watched: Path,
    command: list[str],
    *,
    once: bool = False,
    quiet: int = QUIET_MS,
    maximum: int = MAX_MS,
) -> subprocess.Popen[bytes]:
    argv = [str(helper)]
    if once:
        argv.append("--once")
    argv += [str(watched), str(quiet), str(maximum), *command]
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    _STARTED.append(proc)
    return proc


def _finish(proc: subprocess.Popen[bytes]) -> tuple[int, str]:
    """Wait for a helper that exits by itself, and return its status and stderr.

    The helper prints its last event line and then returns. It is killed only when it is still
    running after WAIT_S, which is a hang. Killing it at once would race its own exit status.
    """
    try:
        _out, err = proc.communicate(timeout=WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        _out, err = proc.communicate(timeout=WAIT_S)
    return proc.returncode, err.decode("utf-8")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Stop a helper that loops until it is killed (a watch that never ends on its own)."""
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=WAIT_S)


def _watched(tmp_path: Path) -> Path:
    watched = tmp_path / "applications"
    watched.mkdir()
    return watched


def test_source_stays_within_the_line_limit() -> None:
    lines = SOURCE.read_text(encoding="utf-8").count("\n")
    assert lines <= LINE_LIMIT, f"{SOURCE.name} has {lines} lines; the limit is {LINE_LIMIT}"


def test_the_makefile_check_target_builds_it_with_gcc_and_clang() -> None:
    """`make -C native check` is the gate: both compilers, warnings as errors, both helpers."""
    make = shutil.which("make")
    if make is None or shutil.which("clang") is None or shutil.which("gcc") is None:
        pytest.skip("make, gcc and clang are needed for the Makefile gate")
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [make, "-C", str(NATIVE), "check"], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert "ginn-watch.c" in run.stdout


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--once"],
        ["/d"],
        ["/d", "10", "20"],
        ["relative", "10", "20", "cmd"],
        ["/d/../e", "10", "20", "cmd"],
        ["/d/..", "10", "20", "cmd"],
        ["/d", "0", "20", "cmd"],
        ["/d", "abc", "20", "cmd"],
        ["/d", "-5", "20", "cmd"],
        ["/d", " 10", "20", "cmd"],
        ["/d", "10", "700000", "cmd"],
        ["/d\nx", "10", "20", "cmd"],
        ["--bogus", "/d", "10", "20", "cmd"],
    ],
)
def test_bad_argv_exits_2_with_usage(helper: Path, args: list[str]) -> None:
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), *args], capture_output=True, text=True, check=False, timeout=WAIT_S
    )
    assert run.returncode == 2, run.stderr
    assert "usage: ginn-watch" in run.stderr


@pytest.mark.fr("FR-044")
def test_a_directory_that_cannot_be_watched_is_an_error(helper: Path, tmp_path: Path) -> None:
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), str(tmp_path / "missing"), "10", "20", "true"],
        capture_output=True,
        text=True,
        check=False,
        timeout=WAIT_S,
    )
    assert run.returncode == 1
    assert "inotify_add_watch" in run.stderr


@pytest.mark.fr("FR-044")
def test_creating_a_desktop_file_runs_the_command_once_with_its_argv(
    helper: Path, tmp_path: Path
) -> None:
    watched = _watched(tmp_path)
    script, log = _stub(tmp_path)
    proc = _start(helper, watched, [str(script), "_sync", "arch"], once=True)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    assert lines.until("watching ").endswith(str(watched))
    (watched / "firefox.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    # IN_CREATE and IN_CLOSE_WRITE of one file. A stall between them may split them into two runs.
    # The first run is the one that counts, so the test takes the first run's changes.
    assert int(lines.until("event ").split()[1]) >= 1
    code, _err = _finish(proc)
    assert code == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["_sync", "arch"]


@pytest.mark.fr("FR-044")
def test_changes_to_other_names_do_not_run_the_command(helper: Path, tmp_path: Path) -> None:
    watched = _watched(tmp_path)
    script, log = _stub(tmp_path)
    proc = _start(helper, watched, [str(script)])
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    (watched / "notes.txt").write_text("x", encoding="utf-8")
    (watched / "icon.png").write_bytes(b"x")
    assert lines.next(QUIET_MS / 1000 * 4) is None
    assert not log.exists()
    (watched / "gimp.desktop").write_text("x", encoding="utf-8")
    assert int(lines.until("event ").split()[1]) >= 1
    _stop(proc)


@pytest.mark.fr("FR-044")
def test_a_burst_of_changes_is_one_run(helper: Path, tmp_path: Path) -> None:
    """Thirty files in a quick burst: one run that counts all sixty changes (create, close)."""
    watched = _watched(tmp_path)
    script, log = _stub(tmp_path)
    proc = _start(helper, watched, [str(script)], quiet=1000, maximum=6000)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    for i in range(30):
        (watched / f"app{i:02d}.desktop").write_text("x", encoding="utf-8")
    assert int(lines.until("event ").split()[1]) == 60  # the whole burst is in the first run
    _stop(proc)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1  # one run, one log line


@pytest.mark.fr("FR-044")
@pytest.mark.fr("NFR-009")
def test_a_steady_stream_still_runs_the_command_within_the_max_latency(
    helper: Path, tmp_path: Path
) -> None:
    """Changes every 50 ms never let the quiet period pass. The max latency still runs the command:
    the first run comes about MAX_MS after the first change, far inside NFR-009's 5 s."""
    watched = _watched(tmp_path)
    script, _log = _stub(tmp_path)
    proc = _start(helper, watched, [str(script)], quiet=5000, maximum=MAX_MS)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    stop = threading.Event()

    def stream() -> None:
        i = 0
        while not stop.is_set():
            (watched / f"s{i}.desktop").write_text("x", encoding="utf-8")
            i += 1
            time.sleep(0.05)

    writer = threading.Thread(target=stream)
    started = time.monotonic()
    writer.start()
    try:
        lines.until("event ", timeout=3.0)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        writer.join()
        _stop(proc)
    assert elapsed < 2.5, f"first run after {elapsed:.2f} s; the max latency is {MAX_MS} ms"


@pytest.mark.fr("FR-044")
def test_the_status_of_the_command_is_reported(helper: Path, tmp_path: Path) -> None:
    watched = _watched(tmp_path)
    script, _log = _stub(tmp_path, status=5)
    proc = _start(helper, watched, [str(script)], once=True)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    (watched / "a.desktop").write_text("x", encoding="utf-8")
    assert lines.until("event ").split()[2] == "5"
    assert _finish(proc)[0] == 0


@pytest.mark.fr("FR-044")
def test_a_command_that_cannot_run_reports_127(helper: Path, tmp_path: Path) -> None:
    watched = _watched(tmp_path)
    proc = _start(helper, watched, ["/nonexistent/ginn-no-such-command"], once=True)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    (watched / "a.desktop").write_text("x", encoding="utf-8")
    assert lines.until("event ").split()[2] == "127"
    assert "exec /nonexistent/ginn-no-such-command" in _finish(proc)[1]


@pytest.mark.fr("FR-044")
def test_stdout_holds_only_watcher_lines_and_the_command_writes_to_stderr(
    helper: Path, tmp_path: Path
) -> None:
    watched = _watched(tmp_path)
    chatty = tmp_path / "chatty.sh"
    chatty.write_text("#!/bin/sh\necho from-command\n", encoding="utf-8")
    chatty.chmod(0o755)
    proc = _start(helper, watched, [str(chatty)], once=True)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    (watched / "a.desktop").write_text("x", encoding="utf-8")
    lines.until("event ")
    code, err = _finish(proc)
    assert code == 0
    assert "from-command" in err


@pytest.mark.fr("FR-044")
def test_the_command_gets_its_arguments_as_an_argv_not_a_shell_line(
    helper: Path, tmp_path: Path
) -> None:
    watched = _watched(tmp_path)
    script, log = _stub(tmp_path)
    marker = tmp_path / "injected"
    hostile = f"x; touch {marker}"
    proc = _start(
        helper, watched, [str(script), hostile, "$(touch " + str(marker) + ")"], once=True
    )
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    (watched / "a.desktop").write_text("x", encoding="utf-8")
    lines.until("event ")
    _finish(proc)
    assert log.read_text(encoding="utf-8").splitlines() == [hostile, "$(touch " + str(marker) + ")"]
    assert not marker.exists()


@pytest.mark.fr("FR-045")
def test_removing_a_desktop_file_runs_the_command(helper: Path, tmp_path: Path) -> None:
    watched = _watched(tmp_path)
    (watched / "gone.desktop").write_text("x", encoding="utf-8")
    script, log = _stub(tmp_path)
    proc = _start(helper, watched, [str(script)], once=True)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    (watched / "gone.desktop").unlink()
    assert lines.until("event ").split()[1] == "1"
    _finish(proc)
    assert log.exists()


@pytest.mark.fr("FR-045")
def test_a_removed_directory_runs_the_command_once_and_exits_3(
    helper: Path, tmp_path: Path
) -> None:
    watched = _watched(tmp_path)
    script, log = _stub(tmp_path)
    proc = _start(helper, watched, [str(script)])
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    shutil.rmtree(watched)
    lines.until("event ")
    code, err = _finish(proc)
    assert code == 3
    assert "is gone" in err
    assert log.exists()


@pytest.mark.fr("FR-044")
def test_a_loop_keeps_watching_after_a_run(helper: Path, tmp_path: Path) -> None:
    watched = _watched(tmp_path)
    script, _log = _stub(tmp_path)
    proc = _start(helper, watched, [str(script)])
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    for name in ("one", "two"):
        (watched / f"{name}.desktop").write_text("x", encoding="utf-8")
        lines.until("event ")
    assert proc.poll() is None
    _stop(proc)


@pytest.mark.fr("FR-044")
@pytest.mark.fr("FR-045")
def test_touching_an_existing_desktop_name_runs_the_command(helper: Path, tmp_path: Path) -> None:
    """The package-manager hooks of ADR-016 run `touch` on one stamp that already exists. touch
    opens the file for writing and closes it, so IN_CLOSE_WRITE fires without a new name."""
    watched = _watched(tmp_path)
    stamp = watched / "transaction.desktop"
    stamp.write_bytes(b"")
    script, log = _stub(tmp_path)
    proc = _start(helper, watched, [str(script), "x"])
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    touch = shutil.which("touch")
    assert touch is not None
    for _ in range(2):
        subprocess.run([touch, str(stamp)], check=True, timeout=WAIT_S)  # noqa: S603
        assert int(lines.until("event ").split()[1]) >= 1
    assert log.read_text(encoding="utf-8").splitlines() == ["x", "x"]
    _stop(proc)


@pytest.mark.fr("FR-044")
def test_exit_with_ends_the_watch_when_that_process_is_gone(helper: Path, tmp_path: Path) -> None:
    watched = _watched(tmp_path)
    owner = subprocess.Popen(["sleep", "60"])  # noqa: S603, S607 - fixed argv
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [str(helper), "--exit-with", str(owner.pid), str(watched), "100", "200", "true"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _STARTED.append(proc)
        _Lines(proc.stdout.fileno()).until("watching ")  # type: ignore[union-attr]
        assert proc.poll() is None
        owner.kill()
        owner.wait(timeout=WAIT_S)
        gone = time.monotonic()
        code, _err = _finish(proc)
        assert code == 0
        assert time.monotonic() - gone < 0.9  # a pidfd wakes the poll; no one-second tick
    finally:
        owner.kill()
        owner.wait(timeout=WAIT_S)


@pytest.mark.fr("FR-044")
@pytest.mark.parametrize("args", [["--exit-with"], ["--exit-with", "abc"], ["--exit-with", "0"]])
def test_exit_with_needs_a_pid(helper: Path, args: list[str]) -> None:
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), *args, "/d", "10", "20", "cmd"],
        capture_output=True,
        text=True,
        check=False,
        timeout=WAIT_S,
    )
    assert run.returncode == 2
    assert "usage: ginn-watch" in run.stderr


@pytest.mark.fr("FR-044")
def test_a_failed_run_is_tried_again_up_to_four_times(helper: Path, tmp_path: Path) -> None:
    """A realm busy with another operation refuses `ginn _sync`. The watch retries, then stops."""
    watched = _watched(tmp_path)
    script, log = _stub(tmp_path, status=1)
    proc = _start(helper, watched, [str(script)], quiet=100, maximum=200)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    (watched / "a.desktop").write_text("x", encoding="utf-8")
    for _ in range(5):
        assert lines.until("event ").endswith(" 1")
    assert lines.next(1.0) is None
    assert log.read_text(encoding="utf-8").count("\n") == 5  # the first run and four retries
    _stop(proc)


@pytest.mark.fr("NFR-001")
def test_successful_runs_that_follow_each_other_closely_are_spaced_further_and_further(
    helper: Path, tmp_path: Path
) -> None:
    """A realm that touches the stamp without end cannot keep `ginn _sync` busy (ADR-016).

    The first twelve runs are free of delay. The thirteenth waits two max-latencies after the
    twelfth.
    """
    watched = _watched(tmp_path)
    script, _log = _stub(tmp_path)
    proc = _start(helper, watched, [str(script)], quiet=20, maximum=300)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    stamp = watched / "a.desktop"
    waits = []
    for _ in range(13):
        stamp.write_text("x", encoding="utf-8")
        started = time.monotonic()
        lines.until("event ", timeout=10.0)
        waits.append(time.monotonic() - started)
    _stop(proc)
    assert max(waits[:12]) < 0.5
    assert waits[12] >= 0.55  # max << 1 = 600 ms after the twelfth run


@pytest.mark.fr("NFR-009")
def test_failed_retries_never_count_toward_the_spacing(helper: Path, tmp_path: Path) -> None:
    """A realm busy with an update refuses `ginn _sync`: each failure and retry is not a run that
    spaces the next. The next transaction runs at once (well under 5 s)."""
    watched = _watched(tmp_path)
    ok = tmp_path / "ok"
    script = tmp_path / "flaky.sh"
    script.write_text(f"#!/bin/sh\n[ -e '{ok}' ]\n", encoding="utf-8")
    script.chmod(0o755)
    proc = _start(helper, watched, [str(script)], quiet=20, maximum=100)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    stamp = watched / "a.desktop"
    for _ in range(3):  # 3 x 5 failed runs: more than the free runs
        stamp.write_text("x", encoding="utf-8")
        for _run in range(5):
            assert lines.until("event ", timeout=10.0).endswith(" 1")
    ok.write_text("", encoding="utf-8")
    stamp.write_text("y", encoding="utf-8")
    started = time.monotonic()
    assert lines.until("event ", timeout=10.0).endswith(" 0")
    _stop(proc)
    assert time.monotonic() - started < 1.0


@pytest.mark.fr("CR-006")
def test_an_inherited_directory_descriptor_is_closed_once_it_is_watched(
    helper: Path, tmp_path: Path
) -> None:
    """The command that ginn-watch runs must not inherit the O_PATH descriptor (ADR-016)."""
    watched = _watched(tmp_path)
    fd = os.open(watched, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    fd = os.dup2(fd, 150, inheritable=False)  # a number the shell does not use for itself
    log = tmp_path / "fds.log"
    script = tmp_path / "fds.sh"
    script.write_text(f"#!/bin/sh\nls /proc/$$/fd >> '{log}'\n", encoding="utf-8")
    script.chmod(0o755)
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(helper), f"/proc/self/fd/{fd}", "50", "100", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(fd,),
    )
    _STARTED.append(proc)
    try:
        lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
        lines.until("watching ")
        (watched / "a.desktop").write_text("x", encoding="utf-8")  # the watch works through it
        lines.until("event ")
    finally:
        os.close(fd)
        _stop(proc)
    assert str(fd) not in log.read_text(encoding="utf-8").split()


@pytest.mark.fr("FR-045")
def test_a_removed_directory_run_that_fails_is_tried_again_then_exits_3(
    helper: Path, tmp_path: Path
) -> None:
    """The run on a removed directory is retried like any run (a realm busy with an update)."""
    watched = _watched(tmp_path)
    script, log = _stub(tmp_path, status=1)
    proc = _start(helper, watched, [str(script)], quiet=50, maximum=100)
    lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
    lines.until("watching ")
    watched.rmdir()
    code, err = _finish(proc)
    assert code == 3
    assert "is gone" in err
    assert log.read_text(encoding="utf-8").count("\n") == 5  # the first run and four retries


@pytest.mark.fr("CR-006")
def test_an_fd_path_with_trailing_junk_closes_nothing(helper: Path, tmp_path: Path) -> None:
    """`/proc/self/fd/150x` is not a number: the descriptor 150 stays open (no atoi prefix)."""
    watched = _watched(tmp_path)
    fd = os.open(watched, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    fd = os.dup2(fd, 150, inheritable=False)
    log = tmp_path / "fds.log"
    script = tmp_path / "fds.sh"
    script.write_text(f"#!/bin/sh\nls /proc/$$/fd >> '{log}'\n", encoding="utf-8")
    script.chmod(0o755)
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(helper), f"/proc/self/fd/{fd}/.", "50", "100", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(fd,),
    )
    _STARTED.append(proc)
    try:
        lines = _Lines(proc.stdout.fileno())  # type: ignore[union-attr]
        lines.until("watching ")
        (watched / "a.desktop").write_text("x", encoding="utf-8")
        lines.until("event ")
    finally:
        os.close(fd)
        _stop(proc)
    assert str(fd) in log.read_text(encoding="utf-8").split()
