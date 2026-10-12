# SPDX-License-Identifier: Apache-2.0
"""native/ginn-rebind.c: it compiles clean, stays within 200 lines, and validates its argv.

Most cases run only the argument checks and one unknown pid, which fail before any namespace is
touched. The namespace cases run the helper inside a private user and mount namespace
(`unshare -Urm`), with scratch paths only: a half-done attach is finished, a forced grant with a
gone file source is unmounted, a missing placeholder is made, and an unmount group detaches. They
are skipped where that namespace is not allowed. The attach on a real host is proven by T4.9
(ADR-008, the host gate). Nothing here runs podman.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "native" / "ginn-rebind.c"
WARNINGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2"]
LINE_LIMIT = 200

pytestmark = [pytest.mark.fr("FR-061"), pytest.mark.fr("NFR-SEC-005")]

NAMESPACE_PREAMBLE = """\
#!/bin/sh
# Scratch paths only, run inside `unshare -Urm`. The container is a sleep in its own mount ns.
set -eu
H="$1"; S="$2"
mkdir -p "$S/src" "$S/realm"
unshare -m --propagation private sleep 300 &
C=$!
trap 'kill "$C" 2>/dev/null || true' EXIT
i=0
while [ "$(readlink /proc/$C/ns/mnt)" = "$(readlink /proc/$$/ns/mnt)" ] && [ $i -lt 100 ]; do
    sleep 0.05; i=$((i + 1))
done
"""
HALF_DONE_BODY = """\
echo data > "$S/src/f"
: > "$S/realm/f"
"$H" "$C" "$S/src/f" "$S/realm/f"
nsenter -t "$C" -m umount "$S/realm/f"
"$H" "$C" "$S/src/f" "$S/realm/f"
nsenter -t "$C" -m cat "$S/realm/f"
"""
FORCED_GONE_BODY = """\
echo data > "$S/src/f"
: > "$S/realm/f"
"$H" "$C" "$S/src/f" "$S/realm/f"
rm "$S/src/f"
"$H" "$C" --force "$S/src/f" "$S/realm/f"
echo "EMPTY $(nsenter -t "$C" -m cat "$S/realm/f" | wc -c | tr -d ' ')"
"""
MISSING_PLACEHOLDER_BODY = """\
echo data > "$S/src/f"
mkdir -p "$S/src/d"; echo inner > "$S/src/d/inner"
"$H" "$C" "$S/src/f" "$S/realm/newfile"
"$H" "$C" "$S/src/d" "$S/realm/newdir"
echo "FILE $(nsenter -t "$C" -m cat "$S/realm/newfile")"
echo "DIR $(nsenter -t "$C" -m ls "$S/realm/newdir")"
"""
UNMOUNT_BODY = """\
echo data > "$S/src/f"
: > "$S/realm/f"
"$H" "$C" "$S/src/f" "$S/realm/f"
"$H" "$C" --unmount "$S/realm/f"
echo "EMPTY $(nsenter -t "$C" -m cat "$S/realm/f" | wc -c | tr -d ' ')"
"""


def _compiler() -> str | None:
    return shutil.which(os.environ.get("CC", "cc"))


def _private_namespaces_usable() -> bool:
    """True when this process may create a user and mount namespace, with unshare and nsenter."""
    unshare = shutil.which("unshare")
    if unshare is None or shutil.which("nsenter") is None:
        return False
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [unshare, "-Urm", "true"], capture_output=True, check=False
    )
    return probe.returncode == 0


needs_namespaces = pytest.mark.skipif(
    not _private_namespaces_usable(), reason="no private user and mount namespace here"
)


@pytest.fixture(scope="module")
def helper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cc = _compiler()
    if cc is None:
        pytest.skip("no C compiler on PATH")
    out = tmp_path_factory.mktemp("rebind") / "ginn-rebind"
    built = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [cc, *WARNINGS, "-o", str(out), str(SOURCE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    return out


def _run_namespace(helper: Path, tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Run `body` in a private namespace, after the container stand-in is up."""
    script = tmp_path / "namespace.sh"
    script.write_text(NAMESPACE_PREAMBLE + body, encoding="utf-8")
    unshare = shutil.which("unshare")
    assert unshare is not None
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [unshare, "-Urm", "sh", str(script), str(helper), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_source_stays_within_the_line_limit() -> None:
    lines = SOURCE.read_text(encoding="utf-8").count("\n")
    assert lines <= LINE_LIMIT, f"{SOURCE.name} has {lines} lines; the limit is {LINE_LIMIT}"


def test_compiles_clean_with_warnings_as_errors(helper: Path) -> None:
    assert helper.is_file()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["4242"],
        ["4242", "/src"],
        ["4242", "--force", "/src"],
        ["4242", "/src", "/dst", "--force"],
        ["4242", "relative", "/dst"],
        ["4242", "/src", "relative"],
        ["4242", "--bogus", "/src", "/dst"],
        ["x", "/src", "/dst"],
        ["0", "/src", "/dst"],
        ["1", "/src", "/dst"],
        ["-3", "/src", "/dst"],
        ["4242", "/src/../etc", "/dst"],
        ["4242", "/src", "/dst/.."],
        ["4242", "--unmount", "/a/../b"],
        ["4242", "--unmount"],
        ["4242", "--unmount", "--force", "/r"],
        ["4242", "--force", "--unmount", "/r"],
        ["4242", "--unmount", "--unmount", "/r"],
        ["4242", "--unmount", "--ro", "/r"],
        ["4242", "--ro", "--unmount", "/r"],
    ],
)
def test_bad_argv_exits_2_with_usage(helper: Path, args: list[str]) -> None:
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), *args], capture_output=True, text=True, check=False
    )
    assert run.returncode == 2, run.stderr
    assert "usage: ginn-rebind" in run.stderr


def test_a_pid_with_no_namespace_fails_before_any_mount(helper: Path) -> None:
    """The namespace is opened before any grant is cloned, so a bad pid touches nothing."""
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(helper), "2147483000", "/src", "/dst"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 1, run.stderr
    assert "/proc/2147483000/ns/mnt" in run.stderr


@pytest.mark.fr("FR-066")
@needs_namespaces
def test_a_half_done_attach_is_finished_by_the_next_run(helper: Path, tmp_path: Path) -> None:
    """Unmounting the attached target leaves the state a crash between umount2 and move_mount does.

    The placeholder is then an empty file with no mount. The next run must find nothing mounted
    (EINVAL on the detach loop), move the tree in, and report it attached (ADR-008, T4.10 notes).
    """
    run = _run_namespace(helper, tmp_path, HALF_DONE_BODY)
    assert run.returncode == 0, run.stderr
    attached = re.findall(r"^0 attached \d+ \d+$", run.stdout, flags=re.MULTILINE)
    assert len(attached) == 2, run.stdout
    assert run.stdout.rstrip().endswith("data")


@pytest.mark.fr("FR-064")
@needs_namespaces
def test_a_forced_file_grant_whose_source_is_gone_is_unmounted(
    helper: Path, tmp_path: Path
) -> None:
    """The target of a plain umount2 is a file with no open fd, so the detach must not be EBUSY."""
    run = _run_namespace(helper, tmp_path, FORCED_GONE_BODY)
    assert run.returncode == 0, run.stderr
    assert "0 missing" in run.stdout.splitlines()
    assert "EMPTY 0" in run.stdout.splitlines()


@pytest.mark.fr("FR-061")
@needs_namespaces
def test_the_helper_makes_a_missing_placeholder_as_a_file_or_a_directory(
    helper: Path, tmp_path: Path
) -> None:
    run = _run_namespace(helper, tmp_path, MISSING_PLACEHOLDER_BODY)
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert sum(1 for line in lines if re.fullmatch(r"0 attached \d+ \d+", line)) == 2
    assert "FILE data" in lines
    assert "DIR inner" in lines


@pytest.mark.fr("FR-066")
@needs_namespaces
def test_an_unmount_group_detaches_the_realm_path(helper: Path, tmp_path: Path) -> None:
    run = _run_namespace(helper, tmp_path, UNMOUNT_BODY)
    assert run.returncode == 0, run.stderr
    assert "0 missing" in run.stdout.splitlines()
    assert "EMPTY 0" in run.stdout.splitlines()
