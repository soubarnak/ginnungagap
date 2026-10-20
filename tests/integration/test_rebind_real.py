# SPDX-License-Identifier: Apache-2.0
"""Host gate for the session sockets after a real logout (T7.8): CL-5, IR-025.

A realm keeps running across a logout, and the display and audio sockets that were bound into it at
start belong to the old session. After the next login the host has new sockets at the same names.
ADR-008 and its amendment decide that the next `ginn exec` or `ginn enter` re-attaches them without
a restart (ginn-rebind, `needs_reattach` / `reattach --force`). S-04 and S-08 could not prove
this on a real logout, because the host created no elogind session; the simulated runs (S-04s,
S-08s, S-08t) stand in. This gate is the real run.

A real logout ends the pytest process, so the gate has two phases and a state directory that
survives the logout. Nothing runs unless GINN_REBIND_PHASE says so, so a full `pytest
tests/integration` run never leaves a realm behind:

  1. `GINN_REBIND_PHASE=1 pytest tests/integration/test_rebind_real.py -v -s`, in the graphical
     session whose sockets are to go stale. It creates and starts the realm, checks that each
     session socket in the realm is the host socket (the inode matches), and records the state to
     `$GINN_REBIND_STATE_DIR` (default /tmp/ginn-t78-<uid>, mode 0700). The realm is left running
     on purpose.
  2. Log out of the session and log in again (a real logout, not `su` or a new shell: the old
     session's /run/user/<uid> must be torn down and recreated). Do not reboot.
  3. `GINN_REBIND_PHASE=2 pytest tests/integration/test_rebind_real.py -v -s`, in the new session.
     It checks that a logout happened, that the realm survived it with the same init, that one
     `ginn exec` re-attaches every socket that the new session has (inside inode equals the new host
     inode, a connect works), that a socket the new session lacks is gone from the realm, and that
     the init did not restart. It then deletes the realm and the state directory, pass or fail.
  `GINN_REBIND_PHASE=cleanup` deletes both without checking anything (an abandoned phase 1).

`GINN_REBIND_SERVICES=1` adds a second realm in phase 1: an arch realm with services on and
autostart on, supervised by the user's runit (the ADR-008 amendment: a services-on start attaches
no session grant, and the first exec after login attaches them; FR-084 is not claimed). The
runit supervisor runs from boot, not from login, so phase 2 checks that the realm is still up under
it after the logout. That the supervisor starts the realm at its own start is proven by
test_services_real.py.

The realms are `t78-...` on the user's real Owned Paths with the T7.7 launcher, since the claim
under test is the real runtime root (/run/ginn/<uid>, ADR-004) across a real logout. Nothing touches
~/.local/share/containers: every podman call goes through the product adapter or `podman()` of
test_services_real.py, which pass --root, --runroot and --tmpdir.

Skipped, with a reason, when the phase is unset, the host is not set up for ginn, or the session
has no Wayland, PipeWire or PulseAudio socket (an ssh login).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ginn.errors import GinnError
from ginn.services._exec_lock import exec_lock_dir
from ginn.services.exec_relay import exec_service_dir, exec_socket
from ginn.store.journal import process_start_ticks
from ginn.store.paths import OwnedPaths
from tests.integration.conftest import REPO, run_tool, tool
from tests.integration.test_export_real import Target, run_ginn
from tests.integration.test_services_real import (
    CGROUP_ROOT,
    _retire,
    container_id,
    pid_of,
    podman,
    remove_exec_leftovers,
    sv_status,
    wait_run_with_init,
)
from tests.integration.test_services_real import write_launcher as services_launcher

PREFIX = "t78-"
PHASE = os.environ.get("GINN_REBIND_PHASE", "")
WITH_SERVICES = os.environ.get("GINN_REBIND_SERVICES", "") == "1"
UID = os.getuid()
STATE_DIR = Path(
    os.environ.get("GINN_REBIND_STATE_DIR")
    or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "ginn-gate-t78"
)
"""Survives the logout, so it is not under /tmp or the runtime directory. See `state_dir()`."""
STATE_FILE = STATE_DIR / "state.json"
REALM_RUNTIME = f"/run/user/{UID}"
CONNECT_SCRIPT = (
    "perl -e 'use Socket; socket(S, PF_UNIX, SOCK_STREAM, 0) or die qq(socket: $!); "
    'connect(S, sockaddr_un($ARGV[0])) or die qq(connect: $!); print qq(connected\\n)\' "$1"'
)
"""Connects to the unix socket named by $1, in the realm. Exit 127 means the realm has no perl."""
IDLE_SEAM = r"""
import dataclasses as _dc
_w1f_lifecycle_hooks = _context.lifecycle_hooks
def _lifecycle_hooks(*a, **k):
    hooks = _w1f_lifecycle_hooks(*a, **k)
    return _dc.replace(hooks, idle=_dc.replace(hooks.idle, helper=NATIVE + '/ginn-idle'))
_context.lifecycle_hooks = _lifecycle_hooks
"""
"""Appended to the launcher: the idle watcher (FR-018) is the one of native/build, so the gate does
not depend on an installed /usr/libexec/ginn/ginn-idle."""


@dataclass(frozen=True)
class Case:
    name: str
    distro: str
    services: bool


CASES = [Case(f"{PREFIX}rebind", "debian", False)] + (
    [Case(f"{PREFIX}rebind-svc", "arch", True)] if WITH_SERVICES else []
)


@dataclass
class Rebind(Target):
    paths: OwnedPaths = None  # type: ignore[assignment]


def state_dir() -> Path:
    """Create STATE_DIR (mode 0700) when absent, and check that it is safe to trust.

    It must be a real directory (not a symlink), owned by this user, with no group or other access,
    since phase 2 reads container ids and pids from it."""
    STATE_DIR.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        STATE_DIR.mkdir(mode=0o700)
    info = os.lstat(STATE_DIR)
    assert stat.S_ISDIR(info.st_mode), f"{STATE_DIR} is not a real directory"
    assert info.st_uid == UID, f"{STATE_DIR} is owned by uid {info.st_uid}, not {UID}"
    assert stat.S_IMODE(info.st_mode) == 0o700, f"{STATE_DIR} mode is {oct(info.st_mode)}"
    return STATE_DIR


def make_target() -> Rebind:
    """The launcher in the state directory and the user's real Owned Paths."""
    try:
        paths = OwnedPaths.from_env()
    except GinnError as exc:
        pytest.skip(f"the host is not set up for ginn ({exc.code})")
    run_tool([tool("make"), "-C", str(REPO / "native")])
    bin_dir = state_dir() / "bin"
    bin_dir.mkdir(mode=0o700, exist_ok=True)
    script = services_launcher(bin_dir)
    text = script.read_text(encoding="utf-8")
    script.write_text(
        text.replace("if __name__ == '__main__':", IDLE_SEAM + "if __name__ == '__main__':"),
        encoding="utf-8",
    )
    return Rebind(
        launcher=script,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        paths=paths,
    )


def session_runtime() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR", "")
    return Path(value) if value.startswith("/") else Path(REALM_RUNTIME)


def is_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def session_sockets() -> dict[str, Path]:
    """The session sockets that ginn binds (runtime/env.py): realm path -> host path."""
    runtime = session_runtime()
    found: dict[str, Path] = {}
    display = os.environ.get("WAYLAND_DISPLAY", "")
    if display:
        host = Path(display) if display.startswith("/") else runtime / display
        if is_socket(host):
            found[f"{REALM_RUNTIME}/wayland-0"] = host
    for name in ("pipewire-0", "pulse/native"):
        if is_socket(runtime / name):
            found[f"{REALM_RUNTIME}/{name}"] = runtime / name
    return found


def realm_inode(target: Rebind, name: str, realm_path: str) -> int | None:
    """The inode of `realm_path` in the realm, through `ginn exec` (which re-attaches), or None."""
    ran = run_ginn(
        target, "exec", name, "--", "stat", "-c", "%i", realm_path, json_mode=False, timeout=300.0
    )
    return int(ran.out.strip()) if ran.rc == 0 and ran.out.strip().isdigit() else None


def realm_connect(target: Rebind, name: str, realm_path: str) -> bool | None:
    """True when a connect from inside the realm works, False when it fails, None without perl."""
    ran = run_ginn(
        target, "exec", name, "--", "sh", "-c", CONNECT_SCRIPT, "sh", realm_path, json_mode=False
    )
    if ran.rc == 127:
        return None
    return ran.rc == 0 and "connected" in ran.out


def podman_inode(target: Rebind, name: str, realm_path: str) -> int | None:
    """The inode in the realm read with `podman exec` directly, which re-attaches nothing."""
    try:
        out = podman(
            target.paths,
            ["exec", container_id(target.paths, name), "stat", "-c", "%i", realm_path],
        )
    except Exception:  # noqa: BLE001 - the probe reports "cannot read", nothing more
        return None
    return int(out.strip()) if out.strip().isdigit() else None


def identity(target: Rebind, case: Case) -> dict[str, Any]:
    """The facts that tell one running init from another (pid and start time), and its container."""
    pid = pid_of(target.paths, case.name)
    cid = container_id(target.paths, case.name)
    started = podman(target.paths, ["inspect", "--format", "{{.State.StartedAt}}", cid]).strip()
    return {
        "container": cid,
        "pid": pid,
        "start_ticks": process_start_ticks(pid),
        "started_at": started,
    }


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def saved_flags() -> dict[str, bool]:
    """Whether `exec/` and the relay service existed before phase 1 (the state file says; a missing
    file means "yes", so a cleanup never removes what it cannot prove it created)."""
    try:
        data = json.loads((STATE_DIR / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"exec_existed": True, "relay_existed": True}
    return {
        "exec_existed": bool(data.get("exec_existed", True)),
        "relay_existed": bool(data.get("relay_existed", True)),
    }


def saved_names() -> list[str]:
    """The realm names that phase 1 recorded, or [] when there is no readable state."""
    try:
        data = json.loads((STATE_DIR / "state.json").read_text(encoding="utf-8"))
        return [str(c["name"]) for c in data.get("cases", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def drop(target: Rebind, *, exec_existed: bool, relay_existed: bool) -> None:
    """Delete every `t78-` realm with its Realm Home and what it left in the user's supervisor
    tree (the exec lock and, when this gate made it, the relay service; as test_services_real.py
    does), then the state directory."""
    names = saved_names()
    for row in run_ginn(target, "list").body.get("realms", []):
        name = str(row.get("name", ""))
        if name.startswith(PREFIX):
            names.append(name)
            run_ginn(target, "delete", name, "--purge-home", "--yes", timeout=600.0)
    paths = target.paths
    remove_exec_leftovers(paths, names, exec_existed=exec_existed)
    if not relay_existed:
        _retire(exec_service_dir(paths))
        for stale in sorted(paths.sv.glob("._exec.*")):
            shutil.rmtree(stale, ignore_errors=True)
        with contextlib.suppress(OSError):
            (paths.sv / ".exec-relay.lock").unlink()
        with contextlib.suppress(OSError):
            exec_socket(paths).unlink()
    for leaf in CGROUP_ROOT.glob(f"{PREFIX}*"):
        with contextlib.suppress(OSError):
            if leaf.is_dir() and not (leaf / "cgroup.procs").read_text(encoding="utf-8").split():
                leaf.rmdir()
    shutil.rmtree(STATE_DIR, ignore_errors=True)


def hold_idle_watcher_off(target: Rebind, case: Case) -> None:
    """Keep a user process in a services-off realm, so the idle watcher (FR-018, 60 s) does not
    stop it before the operator is back. A detached `ginn exec <realm> -- sleep infinity`; it
    dies with the realm. Services-on realms are supervised and never idle-stopped."""
    if case.services:
        return
    holder = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(target.launcher), "exec", case.name, "--", "sleep", "infinity"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=target.env,
        start_new_session=True,
        close_fds=True,
    )
    time.sleep(3.0)
    assert holder.poll() is None, f"the holder process for {case.name} exited at once"


def record(item: str, verdict: str, **data: Any) -> None:
    """One JSON line to the file named by GINN_T49_RESULTS, as conftest.py does. Unset: nothing."""
    path = os.environ.get("GINN_T49_RESULTS")
    if path:
        line = {"item": item, "verdict": verdict, "at": round(time.time(), 3), **data}
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True, default=str) + "\n")


def say(text: str) -> None:
    """Write to the real stderr, so the operator sees it even when pytest captures output."""
    sys.__stderr__.write(text + "\n")  # type: ignore[union-attr]
    sys.__stderr__.flush()  # type: ignore[union-attr]


def create_case(target: Rebind, case: Case) -> None:
    args = ["--services", "on"] if case.services else []
    made = run_ginn(target, "create", case.distro, "--name", case.name, *args, timeout=1800.0)
    assert made.rc == 0, made.error_text()
    if case.services:
        auto = run_ginn(target, "autostart", case.name, "on")
        assert auto.rc == 0, auto.error_text()
    started = run_ginn(target, "start", case.name)
    assert started.rc == 0, started.error_text()
    if case.services:
        wait_run_with_init(target.paths, case.name)


@pytest.mark.fr("IR-025")
def test_phase1_records_the_attached_session_sockets() -> None:
    """Phase 1: each session socket in the realm is the host socket. The realm is left running."""
    if PHASE != "1":
        pytest.skip(
            "two-phase test: run with GINN_REBIND_PHASE=1, log out and in, then GINN_REBIND_PHASE=2"
        )
    sockets = session_sockets()
    if not sockets:
        pytest.skip(
            "no Wayland, PipeWire or PulseAudio socket in this session "
            f"({session_runtime()}): run phase 1 in a graphical login, not over ssh"
        )
    target = make_target()
    drop(target, **saved_flags())  # a stale realm or state file of an abandoned phase 1
    target = make_target()
    flags = {
        "exec_existed": exec_lock_dir(target.paths).is_dir(),
        "relay_existed": (target.paths.sv / "_exec").exists(),
    }
    state: dict[str, Any] = {
        **flags,
        "boot_id": read_text("/proc/sys/kernel/random/boot_id"),
        "sessionid": read_text("/proc/self/sessionid"),
        "xdg_session_id": os.environ.get("XDG_SESSION_ID", ""),
        "runtime_inode": os.stat(session_runtime()).st_ino,
        "cases": [],
    }
    try:
        for case in CASES:
            create_case(target, case)
            hold_idle_watcher_off(target, case)
            recorded: dict[str, Any] = {}
            for realm_path, host in sockets.items():
                inside = realm_inode(target, case.name, realm_path)
                host_inode = os.stat(host).st_ino
                assert inside == host_inode, (
                    f"{case.name}: {realm_path} is inode {inside} in the realm, "
                    f"{host_inode} on the host ({host})"
                )
                recorded[realm_path] = {"host": str(host), "inode": host_inode}
            state["cases"].append(
                {
                    "name": case.name,
                    "distro": case.distro,
                    "services": case.services,
                    "sockets": recorded,
                }
                | identity(target, case)
            )
    except BaseException:
        drop(target, **flags)
        raise
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    STATE_FILE.chmod(0o600)
    record("t78-phase1", "PASS", cases=[c["name"] for c in state["cases"]])
    say(
        "\nT7.8 phase 1 done. The realm(s) "
        + ", ".join(c.name for c in CASES)
        + f" keep running. State: {STATE_FILE}.\n"
        "Now log out of this session and log in again (no reboot), then run:\n"
        "  GINN_INTEGRATION=1 GINN_REBIND_PHASE=2 .venv/bin/pytest "
        "tests/integration/test_rebind_real.py -v -s\n"
        "To abandon: GINN_REBIND_PHASE=cleanup with the same command.\n"
    )


@pytest.mark.fr("IR-025")
def test_phase2_exec_reattaches_the_new_session_sockets() -> None:
    """Phase 2, in the new session: no restart, and every socket of the new session reaches the
    realm after one `ginn exec` (CL-5, ADR-008). Deletes the realms and the state in every case."""
    if PHASE != "2":
        pytest.skip(
            "phase 2 of the two-phase test: set GINN_REBIND_PHASE=2 after phase 1 and a login"
        )
    if not STATE_FILE.is_file():
        pytest.fail(f"no phase 1 state at {STATE_FILE}: run GINN_REBIND_PHASE=1 first")
    state_dir()  # lstat check of the directory the state file is read from
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    seen, changed = logout_seen(state)
    if not seen:
        pytest.skip(
            "no logout detected: the session id, the runtime directory and every recorded socket "
            "inode are unchanged. Log out of the graphical session and in again, then rerun "
            "phase 2. The realms and the state are kept."
        )
    target = make_target()
    flags = {
        "exec_existed": bool(state.get("exec_existed", True)),
        "relay_existed": bool(state.get("relay_existed", True)),
    }
    try:
        _check_phase2(target, state, changed)
    finally:
        drop(target, **flags)


def logout_seen(state: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Whether the session changed since phase 1, and the recorded sockets whose inode changed."""
    changed = {
        realm_path: info
        for case in state["cases"][:1]
        for realm_path, info in case["sockets"].items()
        if is_socket(Path(info["host"])) and os.stat(info["host"]).st_ino != info["inode"]
    }
    new_session = read_text("/proc/self/sessionid") != state["sessionid"]
    return bool(changed) or new_session or _runtime_recreated(state), changed


def _check_phase2(target: Rebind, state: dict[str, Any], changed: dict[str, Any]) -> None:
    assert read_text("/proc/sys/kernel/random/boot_id") == state["boot_id"], (
        "the host rebooted since phase 1, so the realm did not survive a logout: rerun both phases"
    )
    now = session_sockets()
    for saved in state["cases"]:
        case = Case(saved["name"], saved.get("distro", "debian"), bool(saved["services"]))
        _check_case(target, case, saved, now)
    record("t78-phase2", "PASS", cases=[c["name"] for c in state["cases"]], changed=sorted(changed))


def _runtime_recreated(state: dict[str, Any]) -> bool:
    """True when /run/user/<uid> is a new inode (elogind recreated it at login, CL-1)."""
    try:
        return os.stat(session_runtime()).st_ino != state["runtime_inode"]
    except OSError:
        return False


def _check_case(target: Rebind, case: Case, saved: dict[str, Any], now: dict[str, Path]) -> None:
    name = case.name
    listed = run_ginn(target, "status", name)
    assert listed.rc == 0, listed.error_text()
    assert listed.body.get("state") == "running", (
        f"{name} is {listed.body.get('state')} after the logout; the logout stopped the realm. "
        "Check KillUserProcesses in /etc/elogind/logind.conf (S-04)"
    )
    if case.services:
        assert sv_status(target.paths, name).startswith("run:"), sv_status(target.paths, name)
    before = identity(target, case)
    for key in ("container", "pid", "start_ticks", "started_at"):
        assert before[key] == saved[key], (
            f"{name}: {key} changed across the logout: {before[key]!r}"
        )
    facts: dict[str, Any] = {}
    for realm_path, info in saved["sockets"].items():
        host = now.get(realm_path)
        if host is None:
            continue
        new_inode = os.stat(host).st_ino
        stale = None if case.services else podman_inode(target, name, realm_path)
        facts[realm_path] = {"old": info["inode"], "new": new_inode, "stale_view": stale}
    # One exec re-attaches every grant whose host identity changed (ADR-008).
    first = run_ginn(target, "exec", name, "--", "true", json_mode=False, timeout=300.0)
    assert first.rc == 0, first.err[-600:]
    for realm_path, fact in facts.items():
        inside = realm_inode(target, name, realm_path)
        assert inside == fact["new"], (
            f"{name}: {realm_path} is inode {inside} in the realm after the exec; the new "
            f"session's socket is {fact['new']} "
            f"(before the exec the realm showed {fact['stale_view']})"
        )
        worked = realm_connect(target, name, realm_path)
        assert worked is not False, f"{name}: connect to {realm_path} failed after the re-attach"
        fact["connect"] = worked
    for realm_path in saved["sockets"]:
        if realm_path in now:
            continue
        gone = run_ginn(
            target,
            "exec",
            name,
            "--",
            "sh",
            "-c",
            'test ! -e "$1"',
            "sh",
            realm_path,
            json_mode=False,
        )
        assert gone.rc == 0, f"{name}: {realm_path} is still in the realm; the new session lacks it"
    after = identity(target, case)
    for key in ("container", "pid", "start_ticks", "started_at"):
        assert after[key] == saved[key], f"{name}: the exec restarted the realm ({key})"
    record(f"t78-reattach-{name}", "PASS", sockets=facts)


@pytest.mark.fr("IR-025")
def test_cleanup_abandoned_phase_one() -> None:
    """GINN_REBIND_PHASE=cleanup: delete the `t78-` realms and the state directory."""
    if PHASE != "cleanup":
        pytest.skip("only with GINN_REBIND_PHASE=cleanup, after an abandoned phase 1")
    target = make_target()
    drop(target, **saved_flags())
    target = make_target()  # the launcher lived in the state directory that drop() removed
    left = [
        r.get("name")
        for r in run_ginn(target, "list").body.get("realms", [])
        if str(r.get("name", "")).startswith(PREFIX)
    ]
    assert left == [], f"t78 realms remain: {left}"
    shutil.rmtree(STATE_DIR, ignore_errors=True)
