# SPDX-License-Identifier: Apache-2.0
"""Which ginn an exported launcher runs (FR-046, FR-047): `ginn_program` in the CLI context.

The lookup must give an absolute path, must not pick a copy inside the ginn bin directory (a
launcher would then run another launcher), and falls back to sys.argv[0] only when that is absolute.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ginn.cli import _context
from ginn.cli.output import Output
from tests.unit.test_podman_cli import host_paths


def _ginn(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "ginn"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.mark.fr("FR-046")
def test_the_first_absolute_ginn_on_path_is_used_as_a_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _ginn(tmp_path / "usr-bin")
    monkeypatch.setenv("PATH", str(tmp_path / "usr-bin"))
    assert _context.ginn_program(host_paths(tmp_path)) == os.path.realpath(script)


@pytest.mark.fr("FR-046")
def test_a_relative_path_entry_is_never_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ginn(tmp_path / "relative-dir")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "relative-dir")
    monkeypatch.setattr(sys, "argv", ["ginn"])
    assert _context.ginn_program(host_paths(tmp_path)) == "ginn"


@pytest.mark.fr("FR-047")
def test_a_copy_inside_the_ginn_bin_directory_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = host_paths(tmp_path)
    _ginn(paths.bin)
    other = _ginn(tmp_path / "usr-bin")
    monkeypatch.setenv("PATH", f"{paths.bin}{os.pathsep}{tmp_path / 'usr-bin'}")
    assert _context.ginn_program(paths) == os.path.realpath(other)


@pytest.mark.fr("FR-047")
def test_a_symbolic_link_into_the_bin_directory_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = host_paths(tmp_path)
    target = _ginn(paths.bin)
    link_dir = tmp_path / "link-dir"
    link_dir.mkdir()
    (link_dir / "ginn").symlink_to(target)
    monkeypatch.setenv("PATH", str(link_dir))
    monkeypatch.setattr(sys, "argv", ["ginn"])
    assert _context.ginn_program(paths) == "ginn"


@pytest.mark.fr("FR-046")
def test_an_absolute_argv0_is_the_fallback_when_path_has_no_ginn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _ginn(tmp_path / "venv-bin")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(sys, "argv", [str(script)])
    assert _context.ginn_program(host_paths(tmp_path)) == os.path.realpath(script)


@pytest.mark.fr("FR-046")
def test_the_bare_name_is_the_last_resort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(sys, "argv", ["ginn"])
    assert _context.ginn_program(host_paths(tmp_path)) == "ginn"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.fr("NFR-SEC-005")
def test_podman_resolves_to_the_absolute_path_outside_the_bin_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = host_paths(tmp_path)
    _executable(paths.bin / "podman")
    real = _executable(tmp_path / "usr-bin" / "podman")
    monkeypatch.setenv("PATH", f"{paths.bin}{os.pathsep}{tmp_path / 'usr-bin'}")
    assert _context.podman_program(paths) == os.path.realpath(real)


@pytest.mark.fr("NFR-SEC-005")
def test_podman_with_no_copy_on_path_falls_back_to_the_bare_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert _context.podman_program(host_paths(tmp_path)) == "podman"


@pytest.mark.fr("NFR-SEC-005")
def test_the_podman_adapter_runs_the_resolved_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = host_paths(tmp_path)
    paths.ensure()
    real = _executable(tmp_path / "usr-bin" / "podman")
    monkeypatch.setenv("PATH", str(tmp_path / "usr-bin"))
    cli = _context.podman(paths, Output(json_mode=False, verbose=False))
    assert cli.binary == os.path.realpath(real)


@pytest.mark.fr("FR-069")
def test_every_podman_call_names_the_owned_cdi_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI adapter passes `--cdi-spec-dir <owned cdi dir>` on every call, so `podman start`
    resolves `nvidia.com/gpu=all` as `podman create` does (ADR-012)."""
    paths = host_paths(tmp_path)
    paths.ensure()
    _executable(tmp_path / "usr-bin" / "podman")
    monkeypatch.setenv("PATH", str(tmp_path / "usr-bin"))
    cli = _context.podman(paths, Output(json_mode=False, verbose=False))
    flags = cli._global_flags()
    assert flags[-2:] == ["--cdi-spec-dir", str(paths.cdi)]
    assert flags.count("--cdi-spec-dir") == 1
