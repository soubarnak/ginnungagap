# SPDX-License-Identifier: Apache-2.0
"""Fake `ginn` executable for the Control Plane backend tests (FR-100..107).

The fake is a standalone Python script that the tests write into a temporary directory. It
prints canned `--json` output in the shapes that src/ginn/cli/output.py documents. It also streams
progress lines when `--verbose` is set, traps SIGINT, and records every argv it receives in
`calls.jsonl`. Nothing imports the ginn package, so the fake never touches a container runtime.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ginn.gui.backend import CliRunner

FAKE_GINN_SOURCE = r"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ["FAKE_GINN_DIR"])


def record(argv):
    with (STATE / "calls.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")


def out(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
    sys.stdout.flush()


def err(obj):
    sys.stderr.write(json.dumps(obj, sort_keys=True) + "\n")
    sys.stderr.flush()


def fail(code, message, remedy, exit_code, details=None):
    err({"error": {"code": code, "message": message, "remedy": remedy,
                   "exit_code": exit_code, "details": details or {}, "notes": []}})
    sys.exit(exit_code)


def status(name, state, tier, services, disk, error=None):
    return {
        "name": name, "state": state, "container_state": None, "state_mismatch": False,
        "distro": name, "tier": tier, "services": services, "generation": 1,
        "image_digest": "sha256:abc", "created_at": "2026-10-13T09:00:00Z",
        "updated_at": "2026-10-13T09:00:00Z", "restart_required": False,
        "share_skipped": [], "disk": disk, "exported_apps": None, "error": error,
    }


REALMS = {
    "arch": status("arch", "stopped", "basic", False,
                   {"realm_home_bytes": 1610612736, "realm_home_partial": False,
                    "container_bytes": None}),
    "debian": status("debian", "running", "development", True,
                     {"realm_home_bytes": 2048, "realm_home_partial": True,
                      "container_bytes": 4096}),
    "fedora": status("fedora", "broken", "basic", False, None),
}

argv = sys.argv[1:]
record(argv)
if not argv or argv[0] != "--json":
    sys.stderr.write("usage: ginn [--json] COMMAND\n")
    sys.exit(2)
rest = argv[1:]
verbose = "--verbose" in rest
rest = [item for item in rest if item != "--verbose"]
if not rest:
    fail("GINN-E-USAGE", "missing command", "run ginn --help", 2)
command, args = rest[0], rest[1:]

if command == "list":
    out({"realms": [REALMS[name] for name in ("arch", "debian", "fedora")]})
elif command == "status":
    name = args[0]
    if name not in REALMS:
        fail("GINN-E-NO-SUCH-REALM", f"no realm named {name}",
             "run 'ginn list' to see the realms", 3, {"name": name})
    out(REALMS[name])
elif command == "tier":
    name = args[0]
    tier = args[1] if len(args) > 1 else ("custom" if name == "lab" else "basic")
    out({"name": name, "state": "stopped", "tier": tier,
         "toggles": ["downloads", "gpu"] if tier == "custom" else ["display"],
         "shares": [{"path": "/home/tester/projects", "mode": "ro"}],
         "restart_required": False})
elif command == "perms":
    out({"name": args[0], "state": "stopped", "tier": "custom", "toggles": [],
         "shares": [], "restart_required": False})
elif command == "doctor":
    out({"schema": 1, "user": "tester", "uid": 1000, "ok": False,
         "counts": {"PASS": 1, "WARN": 0, "FAIL": 1, "INFO": 0},
         "checks": [
             {"name": "subuid", "status": "FAIL", "message": "no subuid entry",
              "fixes": ["ginn host-setup --user tester"]},
             {"name": "podman", "status": "PASS", "message": "rootless podman works",
              "fixes": []},
         ]})
    fail("GINN-E-DOCTOR-FAILED", "doctor found 1 failed check",
         "run the fix commands above", 1)
elif command == "integrity":
    paths = ["/etc/hosts", "/usr/bin/tool"]
    if "--ack" in args:
        out({"status": "acknowledged", "changed": paths, "pending": None, "discarded": {}})
    else:
        out({"status": "changed", "changed": paths, "pending": paths,
             "warnings": [{"code": "GINN-W-HOST-CHANGED",
                           "message": "host files changed since baseline: " + ", ".join(paths),
                           "remedy": "review the listed paths, then run 'ginn integrity --ack'"}]})
elif command == "create":
    if verbose:
        err({"verbose": "step: pull image"})
        err({"progress": {"message": "step: install integration set"}})
    out({"name": "arch", "distro": "arch", "state": "stopped", "tier": "basic",
         "services": False, "generation": 1, "image_digest": "sha256:abc",
         "locale_note": None, "gpu_note": None})
elif command == "group":
    # Mimics a podman step: ginn ignores SIGINT while a helper runs, and the helper shares the
    # process group. Both record their pid and pgid, so the test can check the group.
    def ignore(signum, frame):
        return None

    signal.signal(signal.SIGINT, ignore)
    helper = subprocess.Popen([sys.executable, os.environ["FAKE_GINN_HELPER"], str(STATE)])
    (STATE / "group-parent").write_text(f"{os.getpid()} {os.getpgrp()}", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not (STATE / "group-helper").exists():
        if time.monotonic() > deadline or helper.poll() is not None:
            fail("GINN-E-INTERNAL", "helper did not start", "see the fake", 70)
        time.sleep(0.01)
    if verbose:
        err({"verbose": "ready"})
    helper.wait()
    fail("GINN-E-INTERRUPTED", "operation interrupted",
         "the partial realm was rolled back", 130)
elif command == "ghost":
    sys.exit(4)
elif command == "bare":
    sys.stderr.write("usage: ginn [--json] COMMAND\n")
    sys.exit(2)
else:
    fail("GINN-E-USAGE", f"unknown command {command}", "run ginn --help", 2)
"""


FAKE_HELPER_SOURCE = """
import os
import signal
import sys
import time
from pathlib import Path

STATE = Path(sys.argv[1])


def on_sigint(signum, frame):
    (STATE / "helper-interrupted").write_text("SIGINT", encoding="utf-8")
    os._exit(0)


signal.signal(signal.SIGINT, on_sigint)
(STATE / "group-helper").write_text(f"{os.getpid()} {os.getpgrp()}", encoding="utf-8")
while True:
    time.sleep(0.05)
"""


@dataclass(frozen=True)
class FakeGinn:
    """The fake executable, its helper, and the directory it records its calls in."""

    script: Path
    directory: Path
    helper: Path

    def runner(self) -> CliRunner:
        env = {
            **os.environ,
            "FAKE_GINN_DIR": str(self.directory),
            "FAKE_GINN_HELPER": str(self.helper),
        }
        return CliRunner((sys.executable, str(self.script)), env=env)

    def calls(self) -> list[list[str]]:
        log = self.directory / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def helper_interrupted(self) -> bool:
        return (self.directory / "helper-interrupted").exists()

    def pids(self, name: str) -> tuple[int, int]:
        """The (pid, pgid) that the fake's `group-parent` or `group-helper` file recorded."""
        pid, pgid = (self.directory / name).read_text(encoding="utf-8").split()
        return int(pid), int(pgid)


def make_fake_ginn(tmp_path: Path) -> FakeGinn:
    """Write the fake script and an empty state directory under `tmp_path`."""
    directory = tmp_path / "fake-ginn-state"
    directory.mkdir()
    script = tmp_path / "fake_ginn.py"
    script.write_text(FAKE_GINN_SOURCE, encoding="utf-8")
    helper = tmp_path / "group_helper.py"
    helper.write_text(FAKE_HELPER_SOURCE, encoding="utf-8")
    return FakeGinn(script=script, directory=directory, helper=helper)
