# SPDX-License-Identifier: Apache-2.0
"""Fake `ginn` executable for the Control Plane backend tests (FR-100..107).

This file is the fake. Run as `python <this file> --json COMMAND ...` it prints canned `--json`
output in the shapes that src/ginn/cli/output.py documents. Like the CLI, it prints a `progress`
line after each step of create and delete (FR-107) without any flag, and a `verbose` line only
with `--verbose`. It traps SIGINT and appends every argv it receives to `calls.jsonl` in the state
directory. Run as `python <this file> --helper STATE`, it is the helper that a held command starts,
and it shares that command's process group. Nothing here loads the ginn package when it runs, so
the fake never touches a container runtime.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, TextIO

if TYPE_CHECKING:
    from ginn.gui.backend import CliRunner

HELPER_FLAG = "--helper"
THIS_FILE = Path(__file__).resolve()


def _say(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, sort_keys=True) + "\n")
    stream.flush()


def _quit(
    code: str,
    message: str,
    remedy: str,
    exit_code: int,
    details: dict[str, Any] | None = None,
) -> NoReturn:
    error = {
        "code": code,
        "message": message,
        "remedy": remedy,
        "exit_code": exit_code,
        "details": details or {},
        "notes": [],
    }
    _say(sys.stderr, {"error": error})
    sys.exit(exit_code)


def _state_dir() -> Path:
    return Path(os.environ["FAKE_GINN_DIR"])


def _log_call(argv: list[str]) -> None:
    with (_state_dir() / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")


def _realm(
    name: str,
    state: str,
    tier: str,
    services: bool,
    disk: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "name": name,
        "state": state,
        "container_state": None,
        "state_mismatch": False,
        "distro": name,
        "tier": tier,
        "services": services,
        "generation": 1,
        "image_digest": "sha256:abc",
        "created_at": "2026-10-13T09:00:00Z",
        "updated_at": "2026-10-13T09:00:00Z",
        "restart_required": False,
        "share_skipped": [],
        "disk": disk,
        "exported_apps": None,
        "error": None,
    }


_REALMS: dict[str, dict[str, Any]] = {
    "arch": _realm(
        "arch",
        "stopped",
        "basic",
        False,
        {"realm_home_bytes": 1610612736, "realm_home_partial": False, "container_bytes": None},
    ),
    "debian": _realm(
        "debian",
        "running",
        "development",
        True,
        {"realm_home_bytes": 2048, "realm_home_partial": True, "container_bytes": 4096},
    ),
    "fedora": _realm("fedora", "broken", "basic", False, None),
}


def _run_helper(state: Path) -> None:
    """The process a held command starts. It records its pid and group, then waits for SIGINT."""

    def on_sigint(_signum: int, _frame: object) -> None:
        (state / "helper-interrupted").write_text("SIGINT", encoding="utf-8")
        os._exit(0)

    signal.signal(signal.SIGINT, on_sigint)
    (state / "group-helper").write_text(f"{os.getpid()} {os.getpgrp()}", encoding="utf-8")
    while True:
        time.sleep(0.05)


def _hold() -> NoReturn:
    """Stand in for a podman step: ignore SIGINT while a helper runs, then wait for the helper.

    The helper is started in this process group, so one group SIGINT reaches both processes. Each
    records its pid and group, and the test reads them back to check the group.
    """
    state = _state_dir()
    signal.signal(signal.SIGINT, lambda _signum, _frame: None)
    helper = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, os.environ["FAKE_GINN_HELPER"], HELPER_FLAG, str(state)]
    )
    (state / "group-parent").write_text(f"{os.getpid()} {os.getpgrp()}", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not (state / "group-helper").exists():
        if time.monotonic() > deadline or helper.poll() is not None:
            _quit("GINN-E-INTERNAL", "helper did not start", "see the fake", 70)
        time.sleep(0.01)
    _say(sys.stderr, {"progress": {"step": "ready", "message": "ready"}})
    helper.wait()
    _quit("GINN-E-INTERRUPTED", "operation interrupted", "the partial realm was rolled back", 130)


def _list(command: str, args: list[str], verbose: bool) -> None:
    _say(sys.stdout, {"realms": list(_REALMS.values())})


def _status(command: str, args: list[str], verbose: bool) -> None:
    name = args[0]
    if name not in _REALMS:
        _quit(
            "GINN-E-NO-SUCH-REALM",
            f"no realm named {name}",
            "run 'ginn list' to see the realms",
            3,
            {"name": name},
        )
    _say(sys.stdout, _REALMS[name])


def _tier(command: str, args: list[str], verbose: bool) -> None:
    name = args[0]
    tier = args[1] if len(args) > 1 else ("custom" if name == "lab" else "basic")
    toggles = ["downloads", "gpu"] if tier == "custom" else ["display"]
    shares = [{"path": "/home/tester/projects", "mode": "ro"}]
    payload = {
        "name": name,
        "state": "stopped",
        "tier": tier,
        "toggles": toggles,
        "shares": shares,
        "restart_required": False,
    }
    _say(sys.stdout, payload)


def _perms(command: str, args: list[str], verbose: bool) -> None:
    payload = {
        "name": args[0],
        "state": "stopped",
        "tier": "custom",
        "toggles": [],
        "shares": [],
        "restart_required": False,
    }
    _say(sys.stdout, payload)


def _doctor(command: str, args: list[str], verbose: bool) -> None:
    checks = [
        {
            "name": "subuid",
            "status": "FAIL",
            "message": "no subuid entry",
            "fixes": ["ginn host-setup --user tester"],
        },
        {"name": "podman", "status": "PASS", "message": "rootless podman works", "fixes": []},
    ]
    report = {
        "schema": 1,
        "user": "tester",
        "uid": 1000,
        "ok": False,
        "counts": {"PASS": 1, "WARN": 0, "FAIL": 1, "INFO": 0},
        "checks": checks,
    }
    _say(sys.stdout, report)
    _quit("GINN-E-DOCTOR-FAILED", "doctor found 1 failed check", "run the fix commands above", 1)


def _integrity(command: str, args: list[str], verbose: bool) -> None:
    paths = ["/etc/hosts", "/usr/bin/tool"]
    if "--ack" in args:
        _say(
            sys.stdout,
            {"status": "acknowledged", "changed": paths, "pending": None, "discarded": {}},
        )
        return
    warning = {
        "code": "GINN-W-HOST-CHANGED",
        "message": "host files changed since baseline: " + ", ".join(paths),
        "remedy": "review the listed paths, then run 'ginn integrity --ack'",
    }
    _say(
        sys.stdout,
        {"status": "changed", "changed": paths, "pending": paths, "warnings": [warning]},
    )


def _estimate() -> None:
    # Marker files in the state directory change the answer, as the tests set them up. A
    # `estimate-fails` file makes the estimate refuse for lack of space (FR-121); an
    # `estimate-blocks` file holds the estimate until the user cancels it (FR-107).
    marks = _state_dir()
    if (marks / "estimate-fails").exists():
        _quit(
            "GINN-E-DISK-SPACE",
            "not enough free space for the everything set",
            "free some space and try again",
            2,
            {"needed": "13359 bytes"},
        )
    if (marks / "estimate-blocks").exists():
        _hold()
    _say(
        sys.stderr, {"progress": {"step": "estimated", "message": "estimate: step estimated done"}}
    )
    _say(
        sys.stdout,
        {
            "distro": "kali",
            "everything": ["kali-linux-everything"],
            "packages": 3,
            "download_bytes": 6000,
            "installed_bytes": 6144,
            "required_bytes": 13359,
        },
    )


def _create(command: str, args: list[str], verbose: bool) -> None:
    if "--estimate" in args:
        _estimate()
        return
    if verbose:
        _say(sys.stderr, {"verbose": "create: verbose detail"})
    _say(sys.stderr, {"progress": {"step": "pulled", "message": "create: step pulled done"}})
    _say(
        sys.stderr,
        {"progress": {"step": "integrated", "message": "create: step integrated done"}},
    )
    _say(
        sys.stdout,
        {
            "name": "arch",
            "distro": "arch",
            "state": "stopped",
            "tier": "basic",
            "services": False,
            "generation": 1,
            "image_digest": "sha256:abc",
            "locale_note": None,
            "gpu_note": None,
        },
    )


def _delete(command: str, args: list[str], verbose: bool) -> None:
    _say(sys.stderr, {"progress": {"step": "deleting", "message": "delete: step deleting done"}})
    _say(
        sys.stdout,
        {"realm": args[0], "home": "kept", "purge_declined": False, "warnings": []},
    )


def _echo_action(command: str, args: list[str], verbose: bool) -> None:
    _say(sys.stdout, {"realm": args[0], "action": command, "argv": args[1:]})


def _group(command: str, args: list[str], verbose: bool) -> None:
    _hold()


def _ghost(command: str, args: list[str], verbose: bool) -> None:
    sys.exit(4)


def _bare(command: str, args: list[str], verbose: bool) -> None:
    sys.stderr.write("usage: ginn [--json] COMMAND\n")
    sys.exit(2)


def _unknown(command: str, args: list[str], verbose: bool) -> None:
    _quit("GINN-E-USAGE", f"unknown command {command}", "run ginn --help", 2)


_COMMANDS = {
    "list": _list,
    "status": _status,
    "tier": _tier,
    "perms": _perms,
    "doctor": _doctor,
    "integrity": _integrity,
    "create": _create,
    "delete": _delete,
    "group": _group,
    "ghost": _ghost,
    "bare": _bare,
    **dict.fromkeys(("start", "stop", "services", "autostart", "update"), _echo_action),
}


def _main(argv: list[str]) -> None:
    if argv[:1] == [HELPER_FLAG]:
        _run_helper(Path(argv[1]))
        return
    _log_call(argv)
    if not argv or argv[0] != "--json":
        sys.stderr.write("usage: ginn [--json] COMMAND\n")
        sys.exit(2)
    rest = argv[1:]
    verbose = "--verbose" in rest
    rest = [item for item in rest if item != "--verbose"]
    if not rest:
        _quit("GINN-E-USAGE", "missing command", "run ginn --help", 2)
    command, args = rest[0], rest[1:]
    _COMMANDS.get(command, _unknown)(command, args, verbose)


@dataclass(frozen=True)
class FakeGinn:
    """The fake executable, its helper, and the directory it records its calls in.

    The executable and the helper are the same file, THIS_FILE, run with different arguments.
    """

    script: Path
    directory: Path
    helper: Path

    def runner(self) -> CliRunner:
        # Imported here, not at module level, so that a run of the fake never loads ginn.
        from ginn.gui import backend

        env = {
            **os.environ,
            "FAKE_GINN_DIR": str(self.directory),
            "FAKE_GINN_HELPER": str(self.helper),
        }
        return backend.CliRunner((sys.executable, str(self.script)), env=env)

    def calls(self) -> list[list[str]]:
        log = self.directory / "calls.jsonl"
        if not log.is_file():
            return []
        text = log.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def helper_interrupted(self) -> bool:
        return (self.directory / "helper-interrupted").exists()

    def pids(self, name: str) -> tuple[int, int]:
        """The (pid, pgid) that the fake's `group-parent` or `group-helper` file recorded."""
        pid, pgid = (self.directory / name).read_text(encoding="utf-8").split()
        return int(pid), int(pgid)


def make_fake_ginn(tmp_path: Path) -> FakeGinn:
    """Create an empty state directory under `tmp_path` and return the fake that uses it."""
    directory = tmp_path / "fake-ginn-state"
    directory.mkdir()
    return FakeGinn(script=THIS_FILE, directory=directory, helper=THIS_FILE)


if __name__ == "__main__":
    _main(sys.argv[1:])
