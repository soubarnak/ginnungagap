# SPDX-License-Identifier: Apache-2.0
"""A bounded podman exec: a timeout and an output cap (FR-044, FR-049 hygiene).

The realm is untrusted, so a listing that hangs or floods its output must not hold the realm lock
or fill memory. The tests run the real PodmanCli against a stub `podman` script, a fake binary of
the test suite (the same seam as tests/fakes/podman_bin). No real podman runs.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from ginn.errors import GinnError
from ginn.podman.adapter import ExecTarget
from ginn.podman.cli import PodmanCli
from tests.unit.test_podman_cli import configured_paths

DIRECT_TARGET = ExecTarget(realm="test", services=False, mutating=False)


def _stub(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "podman"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _cli(tmp_path: Path, body: str) -> PodmanCli:
    binary = _stub(tmp_path, body)
    return PodmanCli.from_paths(configured_paths(tmp_path), binary=str(binary))


@pytest.mark.fr("FR-044")
def test_a_bounded_run_returns_its_output_within_the_limits(tmp_path: Path) -> None:
    cli = _cli(tmp_path, 'echo "listed"')
    result = cli._run_bounded(["ps"], 10.0, 4096)
    assert (result.returncode, result.stdout) == (0, "listed\n")


@pytest.mark.fr("FR-044")
def test_a_run_that_hangs_is_killed_at_the_timeout(tmp_path: Path) -> None:
    cli = _cli(tmp_path, "sleep 30")
    started = time.monotonic()
    with pytest.raises(GinnError) as err:
        cli.exec("c0ffee", ["ls"], capture=True, timeout_s=0.3, target=DIRECT_TARGET)
    assert time.monotonic() - started < 10.0
    assert err.value.code == "GINN-E-INTERNAL"
    assert "timed out" in str(err.value.details["reason"])


@pytest.mark.fr("FR-044")
def test_a_run_that_prints_past_the_cap_is_killed(tmp_path: Path) -> None:
    cli = _cli(tmp_path, "dd if=/dev/zero bs=65536 count=64 2>/dev/null")
    with pytest.raises(GinnError) as err:
        cli.exec("c0ffee", ["ls"], capture=True, max_output=1024 * 1024, target=DIRECT_TARGET)
    assert "printed more than" in str(err.value.details["reason"])


@pytest.mark.fr("FR-044")
def test_an_unbounded_exec_is_unchanged(tmp_path: Path) -> None:
    cli = _cli(tmp_path, 'echo "still here"')
    result = cli._run(["ps"])
    assert result.stdout == "still here\n"


@pytest.mark.fr("FR-043")
def test_a_bounded_run_keeps_the_error_line_of_stderr(tmp_path: Path) -> None:
    cli = _cli(tmp_path, 'echo "==> ERROR: build failed" >&2; exit 2')
    result = cli._run_bounded(["ps"], 10.0, 4096)
    assert result.returncode == 2
    assert result.stderr.strip().endswith("==> ERROR: build failed")


@pytest.mark.fr("FR-043")
def test_a_flood_of_stderr_keeps_only_its_last_bytes(tmp_path: Path) -> None:
    from ginn.podman.cli import STDERR_TAIL_BYTES

    # The flood goes to stderr only. `2>&1 >&2` would send both streams to stdout, where the cap is.
    cli = _cli(tmp_path, "dd if=/dev/zero bs=65536 count=64 >&2; echo last >&2")
    result = cli._run_bounded(["ps"], 10.0, 4096)
    assert len(result.stderr) <= STDERR_TAIL_BYTES
    assert result.stderr.endswith("last\n")


@pytest.mark.fr("FR-044")
def test_a_timeout_alone_stops_a_run_that_prints_without_pause(tmp_path: Path) -> None:
    """Output that never stops used to keep the wait going, because the deadline was only read
    when the pipe was silent. With no output cap, only the timeout can end it."""
    cli = _cli(tmp_path, "yes")
    started = time.monotonic()
    with pytest.raises(GinnError) as err:
        cli._run_bounded(["ps"], 0.5, None)
    assert time.monotonic() - started < 10.0
    assert "timed out" in str(err.value.details["reason"])


@pytest.mark.fr("FR-044")
def test_a_bounded_read_returns_binary_bytes_exactly(tmp_path: Path) -> None:
    """A `cat` of a PNG header: the bytes survive the text channel, since stdout is decoded with
    surrogateescape and encoding it back restores them."""
    header = b"\x89PNG\r\n\x1a\n"
    octal = "".join(f"\\{b:03o}" for b in header)
    cli = _cli(tmp_path, f"printf '{octal}'")
    result = cli.exec(
        "c0ffee",
        ["cat", "--", "/x.png"],
        capture=True,
        timeout_s=10.0,
        max_output=64,
        target=DIRECT_TARGET,
    )
    assert result.stdout.encode("utf-8", "surrogateescape") == header


@pytest.mark.fr("FR-043")
def test_a_stdout_tail_keeps_only_the_end_and_the_cap_counts_all_of_it(tmp_path: Path) -> None:
    from ginn.podman.cli import STDOUT_TAIL_BYTES

    cli = _cli(tmp_path, "dd if=/dev/zero bs=65536 count=32 2>/dev/null; echo done")
    result = cli._run_bounded(["ps"], 10.0, 8 * 1024 * 1024, True)
    assert len(result.stdout) <= STDOUT_TAIL_BYTES
    assert result.stdout.endswith("done\n")
    with pytest.raises(GinnError) as err:
        cli._run_bounded(["ps"], 10.0, 4096, True)
    assert "printed more than" in str(err.value.details["reason"])


@pytest.mark.fr("FR-043")
def test_the_tail_mode_still_returns_the_exit_status(tmp_path: Path) -> None:
    cli = _cli(tmp_path, "echo build failed; exit 2")
    result = cli._run_bounded(["ps"], 10.0, 4096, True)
    assert (result.returncode, result.stdout) == (2, "build failed\n")


@pytest.mark.fr("FR-044")
def test_a_bounded_run_works_with_a_descriptor_above_1024(tmp_path: Path) -> None:
    import os
    import resource

    target = 1100
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < target + 64:
        pytest.skip("the hard descriptor limit is too low for this check")
    if soft < target + 64:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target + 64, hard))
    opened: list[int] = []
    try:
        while True:
            fd = os.dup(0)
            opened.append(fd)
            if fd >= target:
                break
        cli = _cli(tmp_path, "echo above")
        result = cli._run_bounded(["ps"], 10.0, 4096)
        assert result.stdout == "above\n"
    finally:
        for fd in opened:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


@pytest.mark.fr("FR-047")
def test_the_ginn_bin_directory_is_not_on_the_path_of_a_podman_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = configured_paths(tmp_path)
    cli = PodmanCli.from_paths(paths, binary=str(_stub(tmp_path, "true")))
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", str(paths.bin), "/bin"]))
    env = cli._env()
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/bin"])
    monkeypatch.setenv("PATH", os.pathsep.join([str(paths.bin) + "/", "/usr/bin"]))
    assert cli._env()["PATH"] == "/usr/bin"
