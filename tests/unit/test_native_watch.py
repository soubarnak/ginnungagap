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
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


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
