# SPDX-License-Identifier: Apache-2.0
"""Host gate for ADR-015 (T7.27, phase A: items 1, 2, 3, 6 and 7), on real rootless Podman.

Run with `GINN_INTEGRATION=1 .venv/bin/pytest tests/integration/test_adr015_real.py -v -s` on a
Void glibc host with the ginn supervisor running. The verdicts and numbers are in
spikes/results/T7.27-adr015-real.md.

The gate drives the product CLI through the launcher of the T7.7 gate (test_services_real.py):
ginn from this checkout, helpers from native/build. Every realm is named `t727-...`, and the module
removes them at the start and at the end. Nothing here names the user's own storage or /run/user.

In-realm commands run as `ginn exec --root`, the product path. On a services-on realm the product
sends them through the supervisor relay (ADR-014), so this module starts no runit helper service.

The T7.7 known gaps do not apply. ADR-015 installs systemd at create, so debian, fedora and kali
create services-on realms. This module has no strict xfail marks: a failure is a failure.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import statistics
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
import tempfile
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ginn.runtime.spec import SERVICES_MASKED_UNITS, SERVICES_USERDBD_UNITS
from ginn.services._exec_lock import exec_lock_dir
from ginn.services.exec_relay import exec_service_dir, exec_socket
from ginn.services.supervisor import parse_status
from ginn.store.paths import OwnedPaths
from tests.integration.conftest import REPO, run_tool, tool
from tests.integration.test_services_real import (
    Ran,
    _retire,
    container_id,
    host_memory,
    podman,
    realm_rss,
    relay_listener_rss,
    remove_exec_leftovers,
    rss_kib,
    sv_status,
    wait_run_with_init,
    write_launcher,
)
from tests.integration.test_services_real import ginn as run_ginn

PREFIX = "t727-"
DISTROS = ("debian", "fedora", "kali", "arch")
FROZEN_SRC = os.environ.get("GINN_T727_SRC", "")
"""A frozen copy of the committed `src/` (a `git archive` extract). Empty: the worktree's src."""
CREATE_TIMEOUT = 1800.0
"""Provisioning and replay take minutes (ADR-015 Decision 2)."""
CGROUP_ROOT = Path("/sys/fs/cgroup/ginn") / str(os.getuid())
"""The user's cgroup root, /sys/fs/cgroup/ginn/<uid> (ADR-003). Empty realm leaves are removed."""
CRON = {
    "debian": ("cron", "cron"),
    "kali": ("cron", "cron"),
    "fedora": ("cronie", "crond"),
    "arch": ("cronie", "cronie"),
}
"""The cron package and its unit on each distribution. Item 1 enables the unit."""
QUERY = {
    "debian": "dpkg -s",
    "kali": "dpkg -s",
    "fedora": "rpm -q",
    "arch": "pacman -Q",
}
"""The package query of each distribution. Its exit status is 0 when the package is installed."""
_CREATED: list[str] = []
"""The realms this module asked `create` for, so its teardown can remove their exec files."""


@dataclass
class Svc:
    """A services-on realm of one distribution, started by the module fixture."""

    name: str
    distro: str


def init_set(distro: str) -> list[str]:
    """The init set of a services-on realm (ADR-015 Decision 2), from the distribution data.

    Arch lists no init packages, because its base image ships systemd. So systemd is checked there.
    """
    data = tomllib.loads(
        (REPO / "src" / "ginn" / "distros" / "data" / f"{distro}.toml").read_text(encoding="utf-8")
    )
    packages: list[str] = list(data.get("services_packages", []))
    return packages or ["systemd"]


def exec_root(
    launcher: Path, name: str, script: str, timeout: float = 600.0
) -> tuple[int, str, str]:
    """`ginn exec --root NAME -- sh -c SCRIPT`: the product path into the realm as container root.

    On a services-on realm the product enters through the supervisor relay (ADR-014). The script is
    a constant of this module, and only package and unit names go in it. Returns (rc, out, err).
    """
    env = {**os.environ, "PATH": f"{launcher.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(launcher), "exec", "--root", name, "--", "sh", "-c", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd="/tmp",  # noqa: S108 - a working directory, not a temp file
        timeout=timeout,
        check=False,
    )
    return done.returncode, done.stdout, done.stderr


def exec_ok(launcher: Path, name: str, script: str) -> str:
    """Run `script` in the realm and return its stdout. A non-zero status fails the item."""
    rc, out, err = exec_root(launcher, name, script)
    assert rc == 0, f"exec failed (rc {rc}): {err.strip()[-300:]}"
    return out


def realm_state(launcher: Path, name: str) -> tuple[str, int | None]:
    """The record state and generation of the realm, from `ginn status`."""
    body = run_ginn(launcher, "status", name).body
    generation = body.get("generation")
    return str(body.get("state")), generation if isinstance(generation, int) else None


def container_image_id(paths: OwnedPaths, name: str) -> str:
    """The image ID the realm's container runs from, read from the top-level `Image` of inspect.

    `ginn status --json` does not expose the derived image ID (`init_image_id` is only on the
    record), so the container is read instead (ADR-015 Decision 9).
    """
    return podman(paths, ["inspect", "--format", "{{.Image}}", container_id(paths, name)]).strip()


def boot_facts(launcher: Path, name: str) -> dict[str, Any]:
    """PID 1, the system state and the failed units of the realm, read in one exec."""
    out = exec_ok(
        launcher,
        name,
        "cat /proc/1/comm; systemctl is-system-running; "
        "systemctl --failed --no-legend --plain | cut -d ' ' -f1",
    )
    lines = out.splitlines()
    return {
        "pid1": lines[0].strip() if lines else "",
        "system": lines[1].strip() if len(lines) > 1 else "",
        "failed": [line.strip() for line in lines[2:] if line.strip()],
    }


def settled_facts(launcher: Path, name: str, timeout: float = 120.0) -> dict[str, Any]:
    """`boot_facts` once the system has left `starting` and `initializing`, or after `timeout`."""
    began = time.monotonic()
    facts = boot_facts(launcher, name)
    while facts["system"] in ("starting", "initializing") and time.monotonic() - began < timeout:
        time.sleep(2)
        facts = boot_facts(launcher, name)
    facts["settle_s"] = round(time.monotonic() - began, 2)
    return facts


USERDB_DISTROS = ("fedora", "arch")
"""The distributions whose systemd uses the userdb helpers (ADR-007 D9). Item 1 checks that no unit
hard-requires the masked userdb pair there."""


def userdb_facts(launcher: Path, name: str) -> dict[str, Any]:
    """The LoadState of the userdb pair, the unit files that hard-require it, and the nsswitch
    passwd and group lines, all read in the realm (ADR-007 D9, FR-080)."""
    load_state = {
        unit: exec_ok(launcher, name, f"systemctl show -p LoadState --value {unit}").strip()
        for unit in SERVICES_USERDBD_UNITS
    }
    # The userdb pair's own unit files are left out of the search: they are masked, so their own
    # Requires= line (the service on its socket) never runs. Any other file that matches counts.
    hard = exec_ok(
        launcher,
        name,
        "grep -rlE '^(Requires|Requisite|BindsTo)=.*systemd-userdb' "
        "/usr/lib/systemd/system /etc/systemd/system 2>/dev/null "
        "| grep -v -E '/systemd-userdbd[.](service|socket)$'; true",
    ).split()
    own = exec_ok(
        launcher,
        name,
        "grep -nE '^(Requires|Requisite|BindsTo)=' "
        "/usr/lib/systemd/system/systemd-userdbd.service "
        "/usr/lib/systemd/system/systemd-userdbd.socket 2>/dev/null; true",
    ).splitlines()
    nss = exec_ok(
        launcher, name, "grep -E '^(passwd|group):' /etc/nsswitch.conf 2>/dev/null; true"
    ).splitlines()
    return {"load_state": load_state, "hard_requirers": hard, "own_lines": own, "nsswitch": nss}


def package_installed(launcher: Path, name: str, distro: str, package: str) -> bool:
    """True when the package query of the distribution finds `package` in the realm."""
    rc, _out, _err = exec_root(launcher, name, f"{QUERY[distro]} {package} >/dev/null 2>&1")
    return rc == 0


def sweep(launcher: Path, paths: OwnedPaths) -> list[str]:
    """Delete every realm whose name starts with PREFIX, with its Realm Home. Returns what is left.

    Empty cgroup leaves of this module are removed too (finding F9 of the T7.7 gate). The images and
    containers of the module are checked by `leftovers` (ADR-015 Decision 13).
    """
    for realm in run_ginn(launcher, "list").body.get("realms", []):
        name = str(realm.get("name", ""))
        if name.startswith(PREFIX):
            run_ginn(launcher, "delete", name, "--purge-home", "--yes")
    for leaf in CGROUP_ROOT.glob(f"{PREFIX}*"):
        # podman and `ginn delete` leave the empty realm cgroup behind (F9 of the T7.7 gate).
        if leaf.is_dir() and not (leaf / "cgroup.procs").read_text(encoding="utf-8").split():
            with contextlib.suppress(OSError):
                leaf.rmdir()
    return [
        str(r.get("name"))
        for r in run_ginn(launcher, "list").body.get("realms", [])
        if str(r.get("name", "")).startswith(PREFIX)
    ]


def leftovers(paths: OwnedPaths) -> dict[str, list[str]]:
    """Images and containers of this module left in ginn's storage (ADR-015 Decisions 10, 13)."""
    images = podman(paths, ["images", "--format", "{{.Repository}}:{{.Tag}}"]).split()
    containers = podman(paths, ["ps", "-a", "--format", "{{.Names}}"]).split()
    return {
        "images": [i for i in images if PREFIX in i],
        "containers": [c for c in containers if PREFIX in c],
    }


DEFERRAL = "GINN-W-REBUILD-DEFERRED"
"""The warning of a lazy rebuild that failed. The start then ran the old root (ops/lifecycle.py)."""


def recorded(launcher: Path, label: str, *args: str, timeout: float = 900.0) -> Ran:
    """Run a ginn start or rebuild and print every warning code it returned (ADR-015, FR-021 AC-2).

    The codes are read from the result's `warnings` list, which the output channel adds to a dict
    result. They are printed as `T727_CODES <label>`, so the run log records each start and rebuild.
    """
    ran = run_ginn(launcher, *args, timeout=timeout)
    print(f"T727_CODES {label} rc={ran.rc} codes={ran.codes()}")
    return ran


def assert_clean(ran: Ran, label: str) -> None:
    """The command succeeded, and it did not defer its rebuild to the old root (FR-066, FR-083).

    A deferred rebuild is GINN-W-REBUILD-DEFERRED on a start that still returns rc 0. The gate
    treats it as a failure: the realm would run the old root, not the generation it asked for.
    """
    assert ran.rc == 0, f"{label}: {ran.error_text()}"
    assert DEFERRAL not in ran.codes(), f"{label}: {DEFERRAL} (the start ran the old root)"


@pytest.fixture(scope="module")
def paths() -> OwnedPaths:
    return OwnedPaths.from_env()


@pytest.fixture(scope="module")
def launcher(paths: OwnedPaths) -> Iterator[Path]:
    """The gate's ginn command (the T7.7 launcher), with the relay service of this gate tracked."""
    run_tool([tool("make"), "-C", str(REPO / "native")])
    base = Path(tempfile.mkdtemp(prefix="ginn-t727-", dir="/tmp"))  # noqa: S108 - mkdtemp, 0700
    bin_dir = base / "bin"
    bin_dir.mkdir(mode=0o700)
    script = write_launcher(bin_dir)
    if FROZEN_SRC:
        # Run the product from the frozen copy of the committed code, not the worktree's src.
        text = script.read_text(encoding="utf-8")
        script.write_text(text.replace(str(REPO / "src"), FROZEN_SRC), encoding="utf-8")
    exec_existed = exec_lock_dir(paths).is_dir()
    relay_existed = (paths.sv / "_exec").exists()
    _CREATED.clear()
    # A realm or image of an earlier run would collide with this run's names, so none may remain.
    kept = sweep(script, paths)
    assert kept == [], f"t727 realms from an earlier run could not be removed: {kept}"
    assert leftovers(paths) == {"images": [], "containers": []}, "t727 leftovers before the gate"
    try:
        yield script
    finally:
        left = sweep(script, paths)
        remaining = leftovers(paths)
        remove_exec_leftovers(paths, _CREATED, exec_existed=exec_existed)
        if not relay_existed:
            # The relay `_exec` service is made by the first services-on start (ADR-014). It goes
            # with this module when it was absent before, as in the T7.7 gate.
            _retire(exec_service_dir(paths))
            for stale in sorted(paths.sv.glob("._exec.*")):
                shutil.rmtree(stale, ignore_errors=True)
            with contextlib.suppress(OSError):
                (paths.sv / ".exec-relay.lock").unlink()
            with contextlib.suppress(OSError):
                exec_socket(paths).unlink()
        shutil.rmtree(base, ignore_errors=True)
        assert left == [], "t727 realms could not be removed after the gate"
        assert remaining == {"images": [], "containers": []}, f"t727 leftovers: {remaining}"


@pytest.fixture(scope="module", params=DISTROS)
def svc(request: pytest.FixtureRequest, launcher: Path, paths: OwnedPaths) -> Iterator[Svc]:
    """One services-on realm per distribution, created and started once for items 7, 1, 2 and 6.

    The items run in file order on this realm, so each reads the state the earlier items left.
    """
    distro = str(request.param)
    name = f"{PREFIX}{distro}-svc"
    _CREATED.append(name)
    created = run_ginn(
        launcher, "create", distro, "--name", name, "--services", "on", timeout=CREATE_TIMEOUT
    )
    assert created.rc == 0, created.error_text()
    try:
        started = recorded(
            launcher, f"start {distro} fixture", "start", name, timeout=CREATE_TIMEOUT
        )
        assert_clean(started, f"start {distro} fixture")
        yield Svc(name=name, distro=distro)
    finally:
        run_ginn(launcher, "stop", name)
        run_ginn(launcher, "delete", name, "--purge-home", "--yes")


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-080")
def test_fr021_empty_manifest_rebuild_boots_services_on(
    launcher: Path, paths: OwnedPaths, svc: Svc
) -> None:
    """Item 7, the empty manifest: a services-on rebuild with no user package, boot check only.

    The rebuild replays nothing. The new generation must start, with systemd as PID 1 and no failed
    unit. Its success also runs the boot check of ADR-015 Decision 5 (GINN-E-REALM-BOOT on failure).
    """
    _state, before = realm_state(launcher, svc.name)
    image_before = container_image_id(paths, svc.name)
    ran = recorded(
        launcher, f"rebuild {svc.distro} empty", "rebuild", svc.name, timeout=CREATE_TIMEOUT
    )
    assert_clean(ran, f"rebuild {svc.distro} empty")
    assert not ran.body.get("replay_missing"), ran.body.get("replay_missing")
    state, generation = realm_state(launcher, svc.name)
    assert state == "running", f"after the rebuild the realm reads {state}"
    assert before is not None and generation == before + 1, (before, generation)
    image_after = container_image_id(paths, svc.name)
    facts = settled_facts(launcher, svc.name)
    print(
        f"T727_ITEM7B {svc.distro} {json.dumps(facts, sort_keys=True)} gen {before}->{generation} "
        f"image {image_before[:12]}->{image_after[:12]} changed={image_before != image_after}"
    )
    assert facts["pid1"] == "systemd", facts
    assert facts["system"] == "running" and facts["failed"] == [], facts


@pytest.mark.fr("FR-080")
def test_fr080_ac1_systemd_is_pid1_and_enable_now_works(
    launcher: Path, paths: OwnedPaths, svc: Svc
) -> None:
    """FR-080 AC-1 and AC-2 on the product path: systemd is PID 1, the system is `running` with no
    failed unit, and `systemctl enable --now` of cron works. The unit is active again after a stop
    and a start. The derived image is named by its bare ID (ADR-015 Decision 4).
    """
    package, unit = CRON[svc.distro]
    installed = run_ginn(launcher, "install", svc.name, package, timeout=CREATE_TIMEOUT)
    assert installed.rc == 0, installed.error_text()
    out = exec_ok(
        launcher,
        svc.name,
        f"systemctl enable --now {unit} >/dev/null 2>&1; systemctl is-active {unit}",
    )
    assert out.split()[-1:] == ["active"], out[-300:]
    facts = settled_facts(launcher, svc.name)
    print(f"T727_ITEM1 {svc.distro} {json.dumps(facts, sort_keys=True)} unit {unit}")
    assert facts["pid1"] == "systemd", facts
    assert facts["system"] == "running" and facts["failed"] == [], facts
    if svc.distro in USERDB_DISTROS:
        userdb = userdb_facts(launcher, svc.name)
        print(f"T727_ITEM1_USERDB {svc.distro} {json.dumps(userdb, sort_keys=True)}")
        assert userdb["load_state"] == dict.fromkeys(SERVICES_USERDBD_UNITS, "masked"), userdb
        assert userdb["hard_requirers"] == [], userdb
    cid = container_id(paths, svc.name)
    argv = json.loads(podman(paths, ["inspect", "--format", "{{json .Config.CreateCommand}}", cid]))
    assert len(argv[-1]) == 64 and "@sha256:" not in argv[-1], argv[-1]
    assert run_ginn(launcher, "stop", svc.name).rc == 0
    restarted = recorded(
        launcher, f"start {svc.distro} after stop", "start", svc.name, timeout=CREATE_TIMEOUT
    )
    assert_clean(restarted, f"start {svc.distro} after stop")
    out = exec_ok(launcher, svc.name, f"systemctl is-active {unit}")
    assert out.strip() == "active", out[-300:]


@pytest.mark.fr("FR-081")
@pytest.mark.fr("NFR-033")
def test_fr081_ac2_killed_init_restarts_within_10_s(
    launcher: Path, paths: OwnedPaths, svc: Svc
) -> None:
    """Item 2, FR-081 AC-2 (NFR-033): kill the container init. Within 10 s of the kill, runsv has
    started `run` with a new pid, a new container init runs, and `ginn status` reads running.

    The clock starts at the kill. The method is the T7.7 gate's (test_services_real.py).
    """
    old_init = wait_run_with_init(paths, svc.name)
    old_run = parse_status(sv_status(paths, svc.name)).pid
    os.kill(old_init, signal.SIGKILL)
    began = time.monotonic()
    new_run = None
    while time.monotonic() - began < 30:
        status = parse_status(sv_status(paths, svc.name))
        if status.pid is not None and status.pid != old_run and status.state == "run":
            new_run = status.pid
            break
        time.sleep(0.25)
    assert new_run is not None, "runsv did not restart run within 30 s"
    new_init = wait_run_with_init(paths, svc.name, not_pid=old_init, timeout=30.0)
    # The record reads failed while the crash is recorded and the restart's start runs (F13 of the
    # T7.7 gate), so the state is polled inside the window.
    state = ""
    elapsed = 0.0
    while True:
        state, _generation = realm_state(launcher, svc.name)
        elapsed = time.monotonic() - began
        if state == "running" or elapsed >= 10:
            break
        time.sleep(0.5)
    print(
        f"T727_ITEM2 {svc.distro} restart_s={elapsed:.2f} old_run={old_run} new_run={new_run} "
        f"new_init={new_init} state={state}"
    )
    assert state == "running", f"ginn status reads {state} within 10 s of the kill"
    assert elapsed <= 10, f"restart took {elapsed:.1f} s"


@pytest.mark.fr("FR-080")
def test_adr007_all_masks_read_masked_and_inactive(
    launcher: Path, paths: OwnedPaths, svc: Svc
) -> None:
    """Item 6, ADR-007 masks: each unit of SERVICES_MASKED_UNITS (ginn.runtime.spec) reads
    LoadState `masked` and ActiveState `inactive` inside the realm. The list holds the thirteen
    units of ADR-007 mitigation b, the userdb pair included (T7.32)."""
    script = "; ".join(
        f"echo {unit} $(systemctl show -p LoadState --value {unit})"
        f" $(systemctl show -p ActiveState --value {unit})"
        for unit in SERVICES_MASKED_UNITS
    )
    rows = [line.split() for line in exec_ok(launcher, svc.name, script).splitlines() if line]
    print(f"T727_ITEM6 {svc.distro} " + json.dumps(rows, sort_keys=True))
    expected = [[unit, "masked", "inactive"] for unit in SERVICES_MASKED_UNITS]
    assert rows == expected, rows


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-049")
def test_fr021_services_on_rebuild_replays_the_user_manifest_through_the_relay(
    launcher: Path, paths: OwnedPaths, svc: Svc
) -> None:
    """Item 7, the user manifest: the services-on realm holds cron in its Install Manifest (item 1).
    A rebuild replays it into generation N+1 through the supervisor relay (ADR-014), with no
    replay-missing warning. The new generation boots with systemd as PID 1 and no failed unit.
    """
    package, _unit = CRON[svc.distro]
    _state, before = realm_state(launcher, svc.name)
    image_before = container_image_id(paths, svc.name)
    ran = recorded(
        launcher, f"rebuild {svc.distro} manifest", "rebuild", svc.name, timeout=CREATE_TIMEOUT
    )
    assert_clean(ran, f"rebuild {svc.distro} manifest")
    assert "GINN-W-REPLAY-MISSING" not in ran.codes(), ran.codes()
    assert not ran.body.get("replay_missing"), ran.body.get("replay_missing")
    state, generation = realm_state(launcher, svc.name)
    assert state == "running", f"after the rebuild the realm reads {state}"
    assert before is not None and generation == before + 1, (before, generation)
    image_after = container_image_id(paths, svc.name)
    installed = package_installed(launcher, svc.name, svc.distro, package)
    facts = settled_facts(launcher, svc.name)
    print(
        f"T727_ITEM7A {svc.distro} {package} replayed={installed} "
        f"{json.dumps(facts, sort_keys=True)} gen {before}->{generation} "
        f"image {image_before[:12]}->{image_after[:12]} changed={image_before != image_after}"
    )
    assert installed, f"{package} is not installed in generation {generation}"
    assert facts["pid1"] == "systemd", facts
    assert facts["system"] == "running" and facts["failed"] == [], facts


@pytest.mark.parametrize("distro", DISTROS)
@pytest.mark.fr("FR-083")
def test_fr083_on_off_on_keeps_htop_and_the_init_set(
    launcher: Path, paths: OwnedPaths, distro: str
) -> None:
    """Item 3, FR-083 AC-1: htop is installed in a services-off realm. Services are turned on, off
    and on again. When on, systemd is PID 1. When off, htop and the init set are still installed.
    """
    name = f"{PREFIX}{distro}-tog"
    _CREATED.append(name)
    created = run_ginn(
        launcher, "create", distro, "--name", name, "--services", "off", timeout=CREATE_TIMEOUT
    )
    assert created.rc == 0, created.error_text()
    try:
        started = recorded(
            launcher, f"start {distro} services=off", "start", name, timeout=CREATE_TIMEOUT
        )
        assert_clean(started, f"start {distro} services=off")
        installed = run_ginn(launcher, "install", name, "htop", timeout=CREATE_TIMEOUT)
        assert installed.rc == 0, installed.error_text()
        for mode in ("on", "off", "on"):
            assert run_ginn(launcher, "stop", name).rc == 0
            assert run_ginn(launcher, "services", name, mode).rc == 0
            ran = recorded(
                launcher, f"start {distro} services={mode}", "start", name, timeout=CREATE_TIMEOUT
            )
            assert_clean(ran, f"start {distro} services={mode}")
            state, generation = realm_state(launcher, name)
            assert state == "running", f"services {mode} realm reads {state}"
            pid1 = exec_ok(launcher, name, "cat /proc/1/comm").strip()
            htop = package_installed(launcher, name, distro, "htop")
            init = {pkg: package_installed(launcher, name, distro, pkg) for pkg in init_set(distro)}
            print(
                f"T727_ITEM3 {distro} services={mode} gen={generation} pid1={pid1} "
                f"htop={htop} init={json.dumps(init, sort_keys=True)}"
            )
            assert htop, f"htop is missing with services {mode}"
            assert all(init.values()), f"the init set is missing with services {mode}: {init}"
            if mode == "on":
                assert pid1 == "systemd", pid1
            else:
                assert pid1 != "systemd", pid1
    finally:
        run_ginn(launcher, "stop", name)
        run_ginn(launcher, "delete", name, "--purge-home", "--yes")


# ----------------------------------------------------------------------------------------------
# Phase B (T7.27 items 4, 12 and 13): idle RSS (NFR-003, NFR-021) and disk use (NFR-008)
# ----------------------------------------------------------------------------------------------

MIB = 1024 * 1024
NFR003_BUDGET_MIB = 40.0
"""NFR-003: at most 40 MiB RSS total for an idle running realm, services on (SRS, Must)."""
NFR003_RUNS = int(os.environ.get("GINN_T727_NFR003_RUNS", "10"))
"""NFR-003 verification: the median of 10 runs (the SRS method, shared with NFR-002). A regression
re-run may set GINN_T727_NFR003_RUNS to a smaller count; the SRS verdict is the default of ten."""
SETTLE_S = 60.0
"""NFR-002 and NFR-003 verification: the RSS is read after 60 s idle."""
NFR021_REALMS = 5
"""NFR-021: five concurrently running realms (SRS, Should)."""
NFR021_SAMPLES = 3
NFR021_SAMPLE_GAP_S = 30.0
"""NFR-021 samples: three, 30 s apart, from the moment the settle time ends."""
NFR008_MARGIN_BYTES = 200 * MIB
"""NFR-008: the disk use of a new realm is at most the official image size plus 200 MiB (SRS)."""


@dataclass
class IdleSet:
    """Five services-on realms of one distribution, created by the module fixture (NFR-021)."""

    distro: str
    names: list[str]
    disk_after_create: dict[str, Any]


def disk_use(launcher: Path, paths: OwnedPaths, name: str) -> dict[str, Any]:
    """The disk use of one realm, as NFR-008 counts it (ADR-015 Decision 11).

    The product total is the derived image (its size, as podman reports it), the writable layer of
    the realm's container, and the Realm Home (`ginn status`, FR-020). The official image size is
    the size of the pinned base the derived image was built on. The base is the local image whose
    manifest digest is the derived image's `io.ginn.base-digest` label (ADR-015 Decision 10). The
    target is that size plus 200 MiB. `bound_bytes` adds the base once more, as an upper bound for
    the case where podman's derived size leaves out the base layers.
    """
    derived = container_image_id(paths, name)
    derived_size = int(
        podman(paths, ["image", "inspect", "--format", "{{.Size}}", derived]).strip()
    )
    labels = json.loads(
        podman(paths, ["image", "inspect", "--format", "{{json .Labels}}", derived])
    )
    digest = str(labels.get("io.ginn.base-digest", ""))
    base_id: str | None = None
    if digest:
        listing = podman(paths, ["image", "ls", "--digests", "--format", "{{.ID}} {{.Digest}}"])
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == digest:
                base_id = parts[0]
                break
    base_size = None
    if base_id is not None:
        base_size = int(
            podman(paths, ["image", "inspect", "--format", "{{.Size}}", base_id]).strip()
        )
    size_text = podman(
        paths,
        ["container", "inspect", "--size", "--format", "{{.SizeRw}}", container_id(paths, name)],
    ).strip()
    writable = int(size_text) if size_text.isdigit() else 0
    disk = run_ginn(launcher, "status", name).body.get("disk") or {}
    home = int(disk.get("realm_home_bytes") or 0)
    total = derived_size + writable + home
    target = None if base_size is None else base_size + NFR008_MARGIN_BYTES
    bound = None if base_size is None else total + base_size
    return {
        "base_digest": digest,
        "derived_bytes": derived_size,
        "writable_bytes": writable,
        "home_bytes": home,
        "total_bytes": total,
        "base_bytes": base_size,
        "target_bytes": target,
        "bound_bytes": bound,
    }


def process_table(pids: list[int], top: int = 12) -> list[dict[str, Any]]:
    """The processes of the realm's attribution set, largest VmRSS first (item 12)."""
    rows: list[dict[str, Any]] = []
    for pid in pids:
        try:
            vm = rss_kib(pid)["VmRSS"]
            comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except (OSError, ValueError):
            continue
        rows.append(
            {"pid": pid, "comm": comm, "VmRSS_KiB": vm, "cmd": cmd.decode("utf-8", "replace")[:100]}
        )
    rows.sort(key=lambda row: row["VmRSS_KiB"], reverse=True)
    return rows[:top]


USERDB_COMM = ("systemd-userdb", "systemd-userw")
"""The comm prefixes of the userdb helpers: systemd-userdbd, and systemd-userwork, which the kernel
cuts to 15 bytes."""


def userdb_processes(pids: list[int]) -> list[str]:
    """The comm names of the attribution set that are systemd user database helpers (ADR-007 D9)."""
    found: list[str] = []
    for pid in pids:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if comm.startswith(USERDB_COMM):
            found.append(comm)
    return found


def unit_profile(launcher: Path, name: str) -> dict[str, Any]:
    """The active and the failed units of the realm, read in the realm (item 12)."""
    active = exec_ok(
        launcher, name, "systemctl list-units --state=active --no-legend --plain | cut -d ' ' -f1"
    ).split()
    failed = exec_ok(
        launcher, name, "systemctl --failed --no-legend --plain | cut -d ' ' -f1"
    ).split()
    return {"active_count": len(active), "active_units": active, "failed_units": failed}


@pytest.fixture(scope="module", params=DISTROS)
def idle_set(
    request: pytest.FixtureRequest, launcher: Path, paths: OwnedPaths
) -> Iterator[IdleSet]:
    """Five services-on realms of one distribution, created once for NFR-003, NFR-008 and NFR-021.

    The tests run in file order, so NFR-003 (ten start and stop cycles on the first realm) and
    NFR-008 read the first realm before NFR-021 starts all five together. All five are deleted at
    the end, with their Realm Homes.
    """
    distro = str(request.param)
    names = [f"{PREFIX}{distro}-idle{i}" for i in range(NFR021_REALMS)]
    _CREATED.extend(names)
    try:
        for name in names:
            created = run_ginn(
                launcher,
                "create",
                distro,
                "--name",
                name,
                "--services",
                "on",
                timeout=CREATE_TIMEOUT,
            )
            assert created.rc == 0, created.error_text()
        disk = disk_use(launcher, paths, names[0])
        print(f"T727_NFR008_CREATE {distro} {json.dumps(disk, sort_keys=True)}")
        yield IdleSet(distro=distro, names=names, disk_after_create=disk)
    finally:
        for name in names:
            run_ginn(launcher, "stop", name)
            run_ginn(launcher, "delete", name, "--purge-home", "--yes")


@pytest.mark.fr("NFR-003")
def test_nfr003_idle_rss_services_on_per_distribution(
    launcher: Path, paths: OwnedPaths, idle_set: IdleSet
) -> None:
    """NFR-003 (item 4): the idle RSS of one services-on realm, median of ten runs.

    Each run starts the realm, idles it for 60 s (the settle time), and reads the attribution of
    realm_rss (cgroup tree, conmon, runsv, ginn-wait and the pause, as ADR-007 and the T7.7 gate).
    The median is of VmRSS, and PSS is recorded beside it. The last run also profiles the realm's
    processes and units (item 12).
    """
    distro = idle_set.distro
    name = idle_set.names[0]
    rows: list[dict[str, Any]] = []
    profile: dict[str, Any] = {}
    for run in range(1, NFR003_RUNS + 1):
        label = f"start {distro} nfr003 run {run}"
        started = recorded(launcher, label, "start", name, timeout=CREATE_TIMEOUT)
        assert_clean(started, label)
        returned = time.monotonic()
        time.sleep(SETTLE_S)
        state = realm_state(launcher, name)[0]
        assert state == "running", f"run {run}: the realm reads {state} after the settle time"
        settle = time.monotonic() - returned
        host = host_memory()
        attribution = realm_rss(paths, name)
        kib = attribution["kib"]
        rows.append(
            {
                "run": run,
                "settle_s": round(settle, 2),
                "VmRSS_KiB": kib["VmRSS"],
                "Pss_KiB": kib["Pss"],
                "RssAnon_KiB": kib["RssAnon"],
                "RssFile_KiB": kib["RssFile"],
                "pids": len(attribution["pids"]),
                "pss_unreadable": attribution["pss_unreadable"],
                "MemAvailable_KiB": host["MemAvailable_kib"],
                "userdb_procs": userdb_processes(attribution["pids"]),
            }
        )
        if run == NFR003_RUNS:
            profile = {
                "processes": process_table(attribution["pids"]),
                "units": unit_profile(launcher, name),
            }
        stopped = run_ginn(launcher, "stop", name)
        assert stopped.rc == 0, stopped.error_text()
    median_mib = statistics.median(row["VmRSS_KiB"] / 1024 for row in rows)
    pss_mib = statistics.median(row["Pss_KiB"] / 1024 for row in rows)
    verdict = "PASS" if median_mib <= NFR003_BUDGET_MIB else "FAIL"
    print(
        f"T727_NFR003 {distro} median_vmrss_mib={median_mib:.2f} median_pss_mib={pss_mib:.2f} "
        f"budget_mib={NFR003_BUDGET_MIB:.0f} verdict={verdict} settle_s={SETTLE_S:.0f}"
    )
    print("T727_NFR003_ROWS " + json.dumps({"distro": distro, "rows": rows}, sort_keys=True))
    print("T727_PROFILE " + json.dumps({"distro": distro, **profile}, sort_keys=True))
    assert median_mib <= NFR003_BUDGET_MIB, (
        f"NFR-003: VmRSS median {median_mib:.2f} MiB is over the {NFR003_BUDGET_MIB:.0f} MiB budget"
    )


@pytest.mark.parametrize("distro", DISTROS)
@pytest.mark.fr("NFR-008")
def test_nfr008_disk_use_of_a_new_realm_per_distribution(
    launcher: Path, paths: OwnedPaths, distro: str
) -> None:
    """NFR-008 (item 13): the disk use of a new realm, no --with, is at most the official image size
    plus 200 MiB. The verdict is the storage after create (the SRS verification). The steady state,
    after 60 s idle in a running realm, is recorded and judged as well. The realm is its own, and it
    is deleted at the end with its Realm Home.
    """
    name = f"{PREFIX}{distro}-disk"
    _CREATED.append(name)
    steady: dict[str, Any] = {}
    try:
        created = run_ginn(
            launcher, "create", distro, "--name", name, "--services", "on", timeout=CREATE_TIMEOUT
        )
        assert created.rc == 0, created.error_text()
        after = disk_use(launcher, paths, name)
        print(f"T727_NFR008_CREATE {distro} {json.dumps(after, sort_keys=True)}")
        label = f"start {distro} nfr008"
        started = recorded(launcher, label, "start", name, timeout=CREATE_TIMEOUT)
        assert_clean(started, label)
        time.sleep(SETTLE_S)
        assert realm_state(launcher, name)[0] == "running", (
            "the realm is not running at steady state"
        )
        steady = disk_use(launcher, paths, name)
        print(f"T727_NFR008_STEADY {distro} {json.dumps(steady, sort_keys=True)}")
    finally:
        run_ginn(launcher, "stop", name)
        run_ginn(launcher, "delete", name, "--purge-home", "--yes")
    assert after["target_bytes"] is not None, "NFR-008: the realm's pinned base is not found"
    verdict_after = "PASS" if after["total_bytes"] <= after["target_bytes"] else "FAIL"
    verdict_steady = "PASS" if steady["total_bytes"] <= steady["target_bytes"] else "FAIL"
    print(
        f"T727_NFR008 {distro} after_create_total={after['total_bytes']} "
        f"steady_total={steady['total_bytes']} target={after['target_bytes']} "
        f"verdict_after_create={verdict_after} verdict_steady={verdict_steady}"
    )
    assert after["total_bytes"] <= after["target_bytes"], (
        f"NFR-008 after create: {after['total_bytes']} bytes is over {after['target_bytes']}"
    )
    assert steady["total_bytes"] <= steady["target_bytes"], (
        f"NFR-008 steady state: {steady['total_bytes']} bytes is over {steady['target_bytes']}"
    )


@pytest.mark.fr("NFR-021")
def test_nfr021_five_concurrent_idle_realms_per_distribution(
    launcher: Path, paths: OwnedPaths, idle_set: IdleSet
) -> None:
    """NFR-021 (item 4 and 13): five services-on realms of one distribution run together. The
    criterion is at least five concurrently running realms, each within NFR-002/NFR-003 (40 MiB
    VmRSS). Each realm's median over three samples, 30 s apart, from 60 s after the last start, is
    compared with the budget. The relay listener is recorded beside the five (ADR-014).
    """
    distro = idle_set.distro
    names = idle_set.names
    for name in names:
        label = f"start {distro} nfr021 {name}"
        started = recorded(launcher, label, "start", name, timeout=CREATE_TIMEOUT)
        assert_clean(started, label)
    last_returned = time.monotonic()
    samples: list[dict[str, Any]] = []
    for index in range(NFR021_SAMPLES):
        due = last_returned + SETTLE_S + index * NFR021_SAMPLE_GAP_S
        time.sleep(max(0.0, due - time.monotonic()))
        states = {name: realm_state(launcher, name)[0] for name in names}
        vm = {name: realm_rss(paths, name)["kib"]["VmRSS"] for name in names}
        relay = relay_listener_rss()
        samples.append(
            {
                "at_s": round(time.monotonic() - last_returned, 2),
                "states": states,
                "VmRSS_KiB": vm,
                "relay": relay,
            }
        )
    per_realm = {
        name: statistics.median(sample["VmRSS_KiB"][name] / 1024 for sample in samples)
        for name in names
    }
    total = statistics.median(
        (sum(sample["VmRSS_KiB"].values()) + sample["relay"]["VmRSS"]) / 1024 for sample in samples
    )
    all_running = all(
        state == "running" for sample in samples for state in sample["states"].values()
    )
    worst = max(per_realm.values())
    verdict = "PASS" if all_running and worst <= NFR003_BUDGET_MIB else "FAIL"
    print(
        f"T727_NFR021 {distro} concurrent={len(names)} all_running={all_running} "
        f"median_total_vmrss_mib={total:.2f} worst_realm_median_mib={worst:.2f} "
        f"budget_mib={NFR003_BUDGET_MIB:.0f} verdict={verdict} settle_s={SETTLE_S:.0f}"
    )
    print(
        "T727_NFR021_SAMPLES " + json.dumps({"distro": distro, "samples": samples}, sort_keys=True)
    )
    assert all_running, f"NFR-021: not all five realms ran in every sample: {samples[-1]['states']}"
    assert worst <= NFR003_BUDGET_MIB, (
        f"NFR-021: a realm's median VmRSS {worst:.2f} MiB is over {NFR003_BUDGET_MIB:.0f} MiB"
    )
