# SPDX-License-Identifier: Apache-2.0
"""tools/host-diff.sh (T10.2): the host write check of FR-005, run on temporary trees.

Every run passes --root, so the tool walks a temporary directory and never the real host. HOME and
the XDG and GINN_RUNTIME_DIR paths name places inside that tree. The tests never run the tool on /.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from ginn.store.paths import OwnedPaths

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "host-diff.sh"

WRITE = "import sys; open(sys.argv[1], 'w', encoding='utf-8').write(sys.argv[2])"
KEEP_TIME_REWRITE = (
    "import os, sys; p = sys.argv[1]; st = os.stat(p); "
    "open(p, 'w', encoding='utf-8').write(sys.argv[2]); "
    "os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))"
)
FAIL_WITH = "import sys; sys.exit(int(sys.argv[1]))"


def python_argv(code: str, *args: str) -> list[str]:
    """An argv that runs a Python one-liner with `args`. No shell is involved."""
    return [sys.executable, "-I", "-c", code, *args]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small fake host: etc and usr with files, and the directories the exclusions name."""
    root = tmp_path / "root"
    for sub in ("etc", "usr/lib", "home/u", "tmp", "var/cache", "var/log", "srv/xdg", "opt"):
        (root / sub).mkdir(parents=True)
    (root / "etc" / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    (root / "usr" / "lib" / "libx.so").write_text("library\n", encoding="utf-8")
    return root


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """TMPDIR for the tool, outside the fake host, so its snapshots never join the walk."""
    path = tmp_path / "work"
    path.mkdir()
    return path


def run_tool(
    root: Path,
    work: Path,
    command: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    extra: Sequence[str] = (),
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run host-diff --root ROOT over COMMAND. The environment is minimal and set by the test."""
    base_env = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "TMPDIR": str(work), "LC_ALL": "C"}
    if env:
        base_env.update(env)
    argv = ["/bin/sh", str(TOOL), "--root", str(root), *extra, "--", *command]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, env=base_env, capture_output=True, text=True, check=False, cwd=cwd
    )


@pytest.mark.fr("FR-005")
def test_unchanged_tree_exits_zero(tree: Path, work: Path) -> None:
    result = run_tool(tree, work, python_argv("pass"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no change outside the exclusions" in result.stdout


@pytest.mark.fr("FR-005")
def test_new_file_under_etc_is_reported_with_its_path(tree: Path, work: Path) -> None:
    target = tree / "etc" / "new.conf"
    result = run_tool(tree, work, python_argv(WRITE, str(target), "x\n"))
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"+ {target}\t" in result.stdout
    assert str(target) in result.stdout


@pytest.mark.fr("FR-005")
def test_content_change_with_same_size_and_mtime_is_caught_by_the_hash(
    tree: Path, work: Path
) -> None:
    hosts = tree / "etc" / "hosts"
    result = run_tool(
        tree, work, python_argv(KEEP_TIME_REWRITE, str(hosts), "127.0.0.1 localhosu\n")
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"+ {hosts}\tsha256\t" in result.stdout
    assert f"{hosts}\tf\t" not in result.stdout  # the metadata line is unchanged


@pytest.mark.fr("FR-005")
def test_writes_under_tmp_var_cache_home_and_log_are_excluded(tree: Path, work: Path) -> None:
    writes = [
        tree / "tmp" / "t",
        tree / "var" / "cache" / "c",
        tree / "var" / "log" / "l",
        tree / "home" / "u" / "f",
    ]
    code = "import sys\nfor p in sys.argv[1:]:\n    open(p, 'w').write('x')\n"
    result = run_tool(tree, work, python_argv(code, *map(str, writes)))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.fr("FR-005")
def test_owned_paths_under_an_xdg_override_are_excluded_but_other_desktop_files_are_not(
    tree: Path, work: Path
) -> None:
    owned = tree / "srv" / "xdg" / "ginn" / "state.json"
    owned.parent.mkdir(parents=True)
    desktop = tree / "srv" / "xdg" / "applications"
    desktop.mkdir()
    env = {"XDG_DATA_HOME": "/srv/xdg"}  # a host path: the tool maps it under --root
    quiet = run_tool(
        tree,
        work,
        python_argv(WRITE, str(owned), "{}"),
        env=env,
    )
    assert quiet.returncode == 0, quiet.stdout + quiet.stderr
    entries = [desktop / "ginn-firefox.desktop", desktop / "other.desktop"]
    touch_all = "import sys\nfor p in sys.argv[1:]:\n    open(p, 'w').write('x')\n"
    both = run_tool(tree, work, python_argv(touch_all, *map(str, entries)), env=env)
    assert both.returncode == 1, both.stdout + both.stderr
    assert str(entries[1]) in both.stdout
    assert str(entries[0]) not in both.stdout


@pytest.mark.fr("FR-005")
def test_the_runtime_dir_override_is_excluded(tree: Path, work: Path) -> None:
    runtime = tree / "opt" / "ginn-run"
    runtime.mkdir()
    target = runtime / "podman" / "x"
    target.parent.mkdir()
    result = run_tool(
        tree,
        work,
        python_argv(WRITE, str(target), "x"),
        env={"GINN_RUNTIME_DIR": "/opt/ginn-run"},  # a host path under --root
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.fr("FR-005")
def test_exclude_option_is_taken_under_root_and_only_for_its_path(tree: Path, work: Path) -> None:
    resolv = tree / "etc" / "resolv.conf"
    with_exclude = run_tool(
        tree,
        work,
        python_argv(WRITE, str(resolv), "nameserver 192.0.2.1\n"),
        extra=["--exclude", "/etc/resolv.conf"],
    )
    assert with_exclude.returncode == 0, with_exclude.stdout + with_exclude.stderr
    without = run_tool(tree, work, python_argv(WRITE, str(resolv), "nameserver 192.0.2.2\n"))
    assert without.returncode == 1, without.stdout + without.stderr


@pytest.mark.fr("FR-005")
def test_command_status_is_passed_on_without_a_change(tree: Path, work: Path) -> None:
    result = run_tool(tree, work, python_argv(FAIL_WITH, "4"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "COMMAND exited 4" in result.stdout


@pytest.mark.fr("FR-005")
def test_a_change_wins_over_a_failed_command(tree: Path, work: Path) -> None:
    target = tree / "etc" / "failed.conf"
    code = "import sys; open(sys.argv[1], 'w').write('x'); sys.exit(4)"
    result = run_tool(tree, work, python_argv(code, str(target)))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "COMMAND exited 4" in result.stdout


@pytest.mark.fr("FR-005")
def test_the_command_is_passed_as_an_argv_not_a_shell_string(tree: Path, work: Path) -> None:
    # If the tool evaluated its arguments, $(touch x) would create x in the working directory.
    cwd = tree.parent / "cwd"
    cwd.mkdir()
    name = tree / "etc" / "a b;c $(touch x)"
    result = run_tool(tree, work, python_argv(WRITE, str(name), "x"), cwd=cwd)
    assert result.returncode == 1, result.stdout + result.stderr
    assert str(name) in result.stdout
    assert list(cwd.iterdir()) == []


@pytest.mark.fr("FR-005")
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_an_unreadable_directory_is_recorded_and_the_check_goes_on(tree: Path, work: Path) -> None:
    locked = tree / "usr" / "locked"
    locked.mkdir()
    (locked / "inside").write_text("x", encoding="utf-8")
    locked.chmod(0)
    try:
        result = run_tool(tree, work, python_argv("pass"))
    finally:
        locked.chmod(0o755)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "(1 unreadable)" in result.stdout


@pytest.mark.fr("FR-005")
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_an_unreadable_file_under_usr_is_recorded_with_no_hash(tree: Path, work: Path) -> None:
    secret = tree / "usr" / "lib" / "secret"
    secret.write_text("x", encoding="utf-8")
    secret.chmod(0)
    try:
        result = run_tool(tree, work, python_argv("pass"))
    finally:
        secret.chmod(0o644)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "(1 unreadable)" in result.stdout


@pytest.mark.fr("FR-005")
def test_snapshots_are_removed_after_a_clean_run(tree: Path, work: Path) -> None:
    result = run_tool(tree, work, python_argv("pass"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(work.iterdir()) == []


@pytest.mark.fr("FR-005")
def test_snapshots_are_kept_when_the_check_finds_a_change(tree: Path, work: Path) -> None:
    result = run_tool(tree, work, python_argv(WRITE, str(tree / "etc" / "k"), "x"))
    assert result.returncode == 1
    kept = [p for p in work.iterdir() if p.name.startswith("ginn-host-diff.")]
    assert len(kept) == 1
    assert f"snapshots kept in {kept[0]}" in result.stdout


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--root", "/nonexistent-ginn-root", "--", "true"],
        ["--root", "", "--", "true"],
        ["--bogus", "--", "true"],
    ],
)
def test_usage_errors_exit_two(argv: list[str], tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(TOOL), *argv],
        env={"PATH": "/usr/bin:/bin", "HOME": "/home/u", "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr


@pytest.mark.fr("FR-005")
def test_a_command_is_required_after_the_separator(tree: Path, work: Path) -> None:
    result = run_tool(tree, work, [])
    assert result.returncode == 2, result.stdout + result.stderr


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize(
    "env",
    [
        {"GINN_RUNTIME_DIR": "relative/dir"},
        {"HOME": "relative-home"},
    ],
)
def test_relative_runtime_or_home_is_a_usage_error(
    env: dict[str, str], tree: Path, work: Path
) -> None:
    result = run_tool(tree, work, python_argv("pass"), env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "absolute" in result.stderr


@pytest.mark.fr("FR-005")
def test_print_exclusions_lists_the_owned_paths_of_store_paths(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": "relative-config",
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "GINN_RUNTIME_DIR": str(runtime),
    }
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", str(TOOL), "--print-exclusions"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    printed = set(result.stdout.splitlines())
    paths = OwnedPaths.from_env(env, uid=os.getuid())
    owned = {
        str(paths.data),
        str(paths.config),
        str(paths.state),
        str(paths.runtime),
        str(paths.applications_dir / paths.desktop_entry_glob),
    }
    assert owned <= printed
    assert str(tmp_path / "home") in printed
    system = {"/proc", "/sys", "/dev", "/run", "/var/cache", "/var/log"}
    # The temporary-directory names are exclusions, not files this test writes.
    temporary = {"/tmp", "/var/tmp"}  # noqa: S108
    assert system | temporary <= printed
    assert str(paths.config) == str(tmp_path / "home" / ".config" / "ginn")
