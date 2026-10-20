# SPDX-License-Identifier: Apache-2.0
"""T10.5 host gate: NFR-SEC-010 network capture against the allow-list (AS-022, release gate 6).

ginn sends no telemetry and connects only to image registries, distribution repositories and the
AUR. This gate runs a defined workload under tools/net-capture-check.py and fails on any DNS name
or destination outside tools/net-allowlist.txt.

    GINN_INTEGRATION=1 pytest tests/integration/test_network_allowlist_real.py -q -rs
    # optional: GINN_NET_EXTRA_ALLOWLIST=<file> adds names of the host's background traffic

Needs network access, tcpdump and `timeout`, with root or passwordless `sudo -n`. Without a usable
tcpdump the tests are skipped (NOT RUN): the `ss` sampling of the tool sees too little to be a
verdict. The capture is host-wide, so run it on a quiet host.

PARTIAL EVIDENCE. The SRS verifies NFR-SEC-010 by a capture during the full test suite. This gate
is one workload only, so on its own it does not close NFR-SEC-010. The gate-6 run wraps the suite:

    tools/net-capture-check.py run --allowlist tools/net-allowlist.txt --keep <cap.txt> \\
        --max-seconds 14400 -- env GINN_INTEGRATION=1 .venv/bin/pytest tests/integration -q

The tool sets GINN_NET_CAPTURE_ACTIVE=1 for that command, and the workload test then skips, since
the outer capture covers it. The control skips too, because its `.invalid` lookup would be
reported by the outer capture.

The workload is the product CLI, in the gate's isolated storage (conftest.py): create a Debian realm
by the pinned digest (registry), start it, `install htop` (apt mirror), `update --check`, `update`,
`exec`, stop, delete. The Arch/AUR path (`install --aur`) and the Fedora and Kali legs are not part
of this workload and stay open. A control run looks up a reserved `.invalid` name and must be
reported, which proves the tool can fail. The control runs first, and the workload test depends on
it, so a capture that parses nothing can never pass. An empty capture is NOT RUN (exit 2).
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
import sys
import textwrap
from pathlib import Path

import pytest

from tests.integration.conftest import REPO, Host, gate_item
from tests.integration.test_lifecycle_real import (  # noqa: F401 - the mount-root hide is autouse
    _hide_real_mount_roots,
)
from tests.integration.test_tiers_real import _drop

TOOL = REPO / "tools" / "net-capture-check.py"
ALLOWLIST = REPO / "tools" / "net-allowlist.txt"
EXTRA_ENV = "GINN_NET_EXTRA_ALLOWLIST"
REALM = "netcap"
CONTROL_NAME = "ginn-net-control.invalid"  # RFC 6761: never resolves, never leaves the resolver
WORKLOAD_TIMEOUT = 3600
WORKLOAD_CAPTURE_S = 3300  # the privileged tcpdump ends by itself, before the test timeout
CONTROL_CAPTURE_S = 120
ACTIVE_ENV = "GINN_NET_CAPTURE_ACTIVE"

WORKLOAD = '''\
"""Workload of test_network_allowlist_real.py: one Debian realm through the product CLI."""
import sys

sys.path.insert(0, {repo!r})
sys.path.insert(0, {src!r})
from pathlib import Path
from ginn.__main__ import main
from ginn.cli import _context
from ginn.runtime import attach
from tests.integration.test_lifecycle_real import HIDDEN_MOUNT_ROOTS, _gate_exists

real_identity = attach.read_identity


def identity(host, *, mountinfo=attach.MOUNTINFO):
    return None if host in HIDDEN_MOUNT_ROOTS else real_identity(host, mountinfo=mountinfo)


attach.read_identity = identity
seams = _context.Seams(root_dir=Path({root_dir!r}), helper={helper!r}, exists=_gate_exists)
_context.seams = lambda: seams
STEPS = [
    ["create", "debian", "--name", {realm!r}],
    ["start", {realm!r}],
    ["install", {realm!r}, "htop"],
    ["update", "--check", {realm!r}],
    ["update", {realm!r}],
    ["exec", {realm!r}, "--", "true"],
    ["stop", {realm!r}],
    ["delete", {realm!r}, "--yes"],
]
status = 0
for step in STEPS:
    code = main(step)
    print("step", step[0], "->", code, flush=True)
    if code != 0:
        status = code
        if step[0] in ("create", "start"):
            break
sys.exit(status)
'''


def _require_tcpdump() -> None:
    """Skip unless tcpdump and timeout can run with root or `sudo -n`. ss alone is not a verdict."""
    if shutil.which("tcpdump") is None or shutil.which("timeout") is None:
        pytest.skip("tcpdump and timeout are needed; the ss fallback is not a verdict (NOT RUN)")
    # The same argv shape the tool uses: `sudo -n timeout ... tcpdump`. A sudoers rule that allows
    # only tcpdump does not cover it, and the test then skips instead of failing midway.
    asroot = [] if os.geteuid() == 0 else [shutil.which("sudo") or "sudo", "-n"]
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            *asroot,
            shutil.which("timeout") or "timeout",
            "-s",
            "INT",
            "5",
            shutil.which("tcpdump") or "tcpdump",
            "-D",
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip(f"tcpdump cannot run as `sudo -n timeout ... tcpdump` ({probe.stderr.strip()})")


def _tool(
    extra: list[str],
    keep: Path | None,
    command: list[str],
    env: dict[str, str],
    max_seconds: int,
) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(TOOL), "run", "--allowlist", str(ALLOWLIST)]
    for path in extra:
        argv += ["--allowlist", path]
    argv += ["--max-seconds", str(max_seconds)]
    if keep is not None:
        argv += ["--keep", str(keep)]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*argv, "--", *command],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd=str(REPO),
        check=False,
        timeout=WORKLOAD_TIMEOUT,
    )


def _extra() -> list[str]:
    path = os.environ.get(EXTRA_ENV, "")
    if path and not Path(path).is_file():
        pytest.fail(f"{EXTRA_ENV}={path!r} is not a file")
    return [path] if path else []


def _workload_env(host: Host) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(host.host_home),
        "XDG_DATA_HOME": str(host.base / "data"),
        "XDG_CONFIG_HOME": str(host.base / "config"),
        "XDG_STATE_HOME": str(host.base / "state"),
        "GINN_RUNTIME_DIR": str(host.xdg),
        "XDG_RUNTIME_DIR": str(host.session),
    }


@pytest.fixture(scope="module")
def control(host: Host, tmp_path_factory: pytest.TempPathFactory) -> None:
    """The tool can fail: a lookup of a reserved name must give exit 1 and name the host.

    The workload test needs this to have passed, so an empty or unparsed capture cannot pass it.
    """
    if os.environ.get(ACTIVE_ENV) == "1":
        pytest.skip("an outer capture runs (gate-6 suite run); the control would trip it")
    _require_tcpdump()
    code = (
        "import socket\n"
        "try:\n"
        f"    socket.getaddrinfo({CONTROL_NAME!r}, 80)\n"
        "except OSError:\n"
        "    pass\n"
    )
    keep = tmp_path_factory.mktemp("netcontrol") / "control.txt"
    env = {k: v for k, v in os.environ.items() if k != ACTIVE_ENV}
    ran = _tool(_extra(), keep, [sys.executable, "-c", code], env, CONTROL_CAPTURE_S)
    with gate_item(host.results, "NFR-SEC-010-control", {"rc": ran.returncode}):
        assert ran.returncode == 1, f"stdout:\n{ran.stdout}\nstderr:\n{ran.stderr}"
        assert CONTROL_NAME in ran.stdout


@pytest.mark.fr("NFR-SEC-010")
def test_nfr_sec_010_control_a_lookup_outside_the_list_is_reported(control: None) -> None:
    """The control fixture holds the assertions; this test makes its verdict visible."""


@pytest.mark.fr("NFR-SEC-010")
def test_nfr_sec_010_a_full_realm_workload_talks_only_to_the_allow_list(
    host: Host, tmp_path: Path, control: None
) -> None:
    """One workload only: partial evidence for NFR-SEC-010 (see the module doc)."""
    script = tmp_path / "workload.py"
    script.write_text(
        textwrap.dedent(WORKLOAD).format(
            repo=str(REPO),
            src=str(REPO / "src"),
            root_dir=str(host.root_dir),
            helper=host.helper,
            realm=REALM,
        ),
        encoding="utf-8",
    )
    keep = tmp_path / "capture.txt"
    try:
        ran = _tool(
            _extra(), keep, [sys.executable, str(script)], _workload_env(host), WORKLOAD_CAPTURE_S
        )
    finally:
        _drop(host, REALM)  # the workload deletes it; this covers a workload that stopped early
    facts = {"method": "tcpdump", "rc": ran.returncode, "scope": "single workload (partial)"}
    with gate_item(host.results, "NFR-SEC-010-workload", facts):
        assert ran.returncode == 0, f"stdout:\n{ran.stdout}\nstderr:\n{ran.stderr}"
        assert "clean" in ran.stdout
