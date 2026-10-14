# SPDX-License-Identifier: Apache-2.0
"""tools/trace-no-xbps.sh (T10.3): the FR-004 process trace, run on fake xbps tools.

The fakes are scripts in a temporary directory, never the xbps packages. Each traced command runs
with a PATH that holds only the fakes and a toolbin of links to the few tools the trace script
needs. No test can reach a real xbps-* executable on the host. The tests that trace skip when
strace is missing or cannot trace here (ptrace blocked).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "trace-no-xbps.sh"

TOOLBIN_NAMES = ("true", "mktemp", "grep", "sed", "wc", "rm")
FAKE_OK = "#!/bin/sh\nexit 0\n"
FAKE_FAILS = "#!/bin/sh\nexit 4\n"


@pytest.fixture(scope="module")
def strace(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The strace binary, after a probe that it can trace an exec here. Skips otherwise."""
    found = [
        os.environ.get("STRACE", ""),
        shutil.which("strace") or "",
        "/usr/sbin/strace",
    ]
    binary = next((c for c in found if c and os.access(c, os.X_OK)), None)
    if binary is None:
        pytest.skip("strace is not installed")
    probe = tmp_path_factory.mktemp("probe") / "probe.log"
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [binary, "-f", "-e", "trace=execve", "-o", str(probe), "--", "/bin/sh", "-c", "exit 0"],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0 or "execve(" not in probe.read_text(encoding="utf-8"):
        pytest.skip("strace cannot trace a process here (ptrace blocked?)")
    return binary


@pytest.fixture
def toolbin(tmp_path: Path) -> Path:
    """A directory of links to the tools the trace script runs. It holds no xbps-* name."""
    directory = tmp_path / "toolbin"
    directory.mkdir()
    for name in TOOLBIN_NAMES:
        found = shutil.which(name)
        assert found is not None, f"the trace script needs {name}"
        (directory / name).symlink_to(found)
    assert not [p for p in directory.iterdir() if p.name.startswith("xbps")]
    return directory


def make_fake(directory: Path, name: str, body: str = FAKE_OK) -> Path:
    """Write an executable fake tool `name` in `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def run_trace(
    strace: str,
    tmp_path: Path,
    command: list[str],
    *,
    path: str,
    strace_env: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run trace-no-xbps.sh -- COMMAND with the given PATH. TMPDIR is under tmp_path."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    env = {
        "PATH": path,
        "STRACE": strace if strace_env is None else strace_env,
        "TMPDIR": str(work),
        "LC_ALL": "C",
    }
    argv = ["/bin/sh", str(TOOL), "--", *command]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.fr("FR-004")
def test_a_clean_command_exits_zero(strace: str, toolbin: Path, tmp_path: Path) -> None:
    result = run_trace(strace, tmp_path, ["/bin/sh", "-c", "exit 0"], path=str(toolbin))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 of them xbps-*" in result.stdout


@pytest.mark.fr("FR-004")
def test_an_absolute_call_to_a_fake_xbps_install_fails_the_check(
    strace: str, toolbin: Path, tmp_path: Path
) -> None:
    fake = make_fake(tmp_path / "bin", "xbps-install")
    result = run_trace(strace, tmp_path, [str(fake), "-S", "ginn"], path=str(toolbin))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "xbps-* exec attempt" in result.stderr
    assert str(fake) in result.stderr
    assert "FR-004 violated" in result.stderr


@pytest.mark.fr("FR-004")
def test_a_path_search_that_finds_a_fake_xbps_install_fails_the_check(
    strace: str, toolbin: Path, tmp_path: Path
) -> None:
    fakebin = tmp_path / "fakebin"
    make_fake(fakebin, "xbps-install")
    path = os.pathsep.join([str(fakebin), str(toolbin)])
    result = run_trace(strace, tmp_path, ["/bin/sh", "-c", "xbps-install -S ginn"], path=path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "xbps-install" in result.stderr


@pytest.mark.fr("FR-004")
def test_an_exec_the_kernel_refuses_counts_as_an_attempt(
    strace: str, toolbin: Path, tmp_path: Path
) -> None:
    # The file exists but is not executable, so the kernel refuses the exec (EACCES). The refusal
    # is still an attempt to run xbps-query.
    not_executable = tmp_path / "noexec" / "xbps-query"
    not_executable.parent.mkdir()
    not_executable.write_text(FAKE_OK, encoding="utf-8")
    not_executable.chmod(0o644)
    result = run_trace(strace, tmp_path, [str(not_executable), "-l"], path=str(toolbin))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "xbps-query" in result.stderr
    assert "EACCES" in result.stderr


@pytest.mark.fr("FR-004")
def test_a_path_search_that_finds_no_xbps_tool_launches_nothing(
    strace: str, toolbin: Path, tmp_path: Path
) -> None:
    # dash checks PATH with stat and never calls execve here, so the trace has no attempt. The
    # shell fails with 127 and nothing is launched, which FR-004 allows.
    result = run_trace(strace, tmp_path, ["/bin/sh", "-c", "xbps-query -l"], path=str(toolbin))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "0 of them xbps-*" in result.stdout


@pytest.mark.fr("FR-004")
def test_an_exec_of_a_long_path_is_read_in_full(strace: str, toolbin: Path, tmp_path: Path) -> None:
    # strace cuts strings at 32 characters by default. The basename sits past that cut here.
    deep = tmp_path / ("a" * 60) / ("b" * 60) / "bin"
    fake = make_fake(deep, "xbps-remove")
    assert len(str(fake)) > 120
    result = run_trace(strace, tmp_path, [str(fake)], path=str(toolbin))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "xbps-remove" in result.stderr


@pytest.mark.fr("FR-004")
def test_a_child_process_of_the_command_is_traced(
    strace: str, toolbin: Path, tmp_path: Path
) -> None:
    fake = make_fake(tmp_path / "bin", "xbps-reconfigure")
    # The shell forks for the first command, so the fake runs in a child process.
    result = run_trace(
        strace,
        tmp_path,
        ["/bin/sh", "-c", '"$1"; true', "sh", str(fake)],
        path=str(toolbin),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "xbps-reconfigure" in result.stderr


@pytest.mark.fr("FR-004")
def test_a_name_that_only_contains_xbps_is_not_an_xbps_tool(
    strace: str, toolbin: Path, tmp_path: Path
) -> None:
    fake = make_fake(tmp_path / "bin", "notxbps-install")
    result = run_trace(strace, tmp_path, [str(fake)], path=str(toolbin))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.fr("FR-004")
def test_a_failed_command_without_an_attempt_exits_three(
    strace: str, toolbin: Path, tmp_path: Path
) -> None:
    result = run_trace(strace, tmp_path, ["/bin/sh", "-c", "exit 4"], path=str(toolbin))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "COMMAND exited 4" in result.stderr


@pytest.mark.fr("FR-004")
def test_an_attempt_wins_over_the_status_of_a_failed_command(
    strace: str, toolbin: Path, tmp_path: Path
) -> None:
    fake = make_fake(tmp_path / "bin", "xbps-install", FAKE_FAILS)
    result = run_trace(strace, tmp_path, [str(fake), "-S"], path=str(toolbin))
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.fr("FR-004")
def test_a_missing_strace_is_a_usage_error(toolbin: Path, tmp_path: Path) -> None:
    result = run_trace(
        "/nonexistent/strace",
        tmp_path,
        ["/bin/sh", "-c", "exit 0"],
        path=str(toolbin),
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "strace is not available" in result.stderr


@pytest.mark.fr("FR-004")
def test_a_blocked_tracer_is_a_usage_error_not_a_clean_run(toolbin: Path, tmp_path: Path) -> None:
    # /bin/false exits 1 without tracing anything, as a blocked strace would.
    result = run_trace(
        "/bin/false",
        tmp_path,
        ["/bin/sh", "-c", "exit 0"],
        path=str(toolbin),
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot trace" in result.stderr


@pytest.mark.fr("FR-004")
@pytest.mark.parametrize("argv", [["/bin/true"], ["--"]])
def test_usage_errors_exit_two(argv: list[str], toolbin: Path, tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(TOOL), *argv],
        env={"PATH": str(toolbin), "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage:" in result.stderr
