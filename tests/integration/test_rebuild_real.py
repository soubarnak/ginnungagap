# SPDX-License-Identifier: Apache-2.0
"""Host gate for rebuild, update and create --resume (M8), on real rootless Podman.

Run with `GINN_INTEGRATION=1 pytest tests/integration/test_rebuild_real.py -v -s` on a Void host
with network access. Two harnesses are used:

- The services-off items run the product CLI as a subprocess on the conftest storage base, with the
  launcher, `run_ginn` and `realm` of test_export_real.py (nothing under the user's own storage).
- The services-on items (`t83-` realms) need the user's real runit supervisor, which only watches
  `$XDG_STATE_HOME/ginn/sv`. They run on the user's real Owned Paths with the T7.7 launcher, as
  test_services_real.py does, and delete what they create. The distribution is arch, since the
  debian and fedora images have no /sbin/init (F1).

Crashes and cancels are made at a chosen journal step by STEP_SEAM (test_export_real.py): the
launcher kills itself with SIGKILL right after the step is journaled (a crash, FR-122), or sends
itself SIGINT (a cancel, AS-018). The next ginn command then runs recovery.

Items, and the open points of the T8.3 roadmap bullet that they close:
- FR-021 AC-1 (services-off): home file unchanged, manifest replayed, tier and services kept, one
  container left, exported entry regenerated. FR-121: the `space-checked` step reports a figure and
  the free space is at least 1.10 times it.
- FR-022 AC-1: a manifest package that the upstream does not have gets GINN-W-REPLAY-MISSING and the
  swap completes. The unavailable name is added to the realm record by the test, because no real
  package can be made to vanish.
- FR-021 AC-2, NFR-031: a cancel (SIGINT) before the swap leaves the old realm and its generation.
- FR-122, NFR-030: a SIGKILL before and at the swap is settled by the next command
  (GINN-W-RECOVERED), the realm has one container of one generation, and it starts.
- FR-023 AC-2, FR-024: `update --check` changes nothing, `update` upgrades and is logged.
- FR-122 AC-1: a create killed after `homed` or `created` is `broken` with both recovery commands,
  and `create --resume` finishes it and keeps the Realm Home.
- Open for the M8 host gate: a services-on replay under runit, a killed rebuild on a host, and the
  rollback `sv down` path. The first two are items below. The rollback is covered for the cancel
  that raises KeyboardInterrupt inside the build, which takes the `sv down` of gN+1 on the success
  path of that call. The path where `sv down` fails with an error other than "gone" has no fault
  that a host can inject, so it stays untested here.

Not covered: FR-120 (network failure retries and rollback) needs a controlled network cut, and
FR-121 AC-1 (a refusal at low free space) needs a controlled small filesystem. Neither is done in a
host test that must not touch the user's disks or network. FR-023 AC-1 (nothing changes in 30 days)
is a time rule and is not testable in a run.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import RealmName
from ginn.domain.packages import InstallMethod, InstallSource, ManifestEntry
from ginn.errors import GinnError
from ginn.services._exec_lock import exec_lock_dir
from ginn.services.exec_relay import exec_service_dir, exec_socket
from ginn.services.runit_user import service_dir
from ginn.store.paths import OwnedPaths
from tests.integration import test_export_real as harness
from tests.integration.conftest import REPO, run_tool, tool
from tests.integration.test_aur_real import AUR_PACKAGE
from tests.integration.test_export_real import (
    CANCEL_ENV,
    CREATE_TIMEOUT,
    DESKTOP_ID,
    DESKTOP_PKG,
    KILL_ENV,
    PREFIX,
    STEP_SEAM,
    Target,
    World,
    entry_path,
    in_realm,
    journal_step,
    realm,
    run_ginn,
)
from tests.integration.test_services_real import (
    _CREATED,
    CGROUP_ROOT,
    _retire,
    leaf_exec,
    podman_cli,
    remove_exec_leftovers,
    sv_status,
    wait_run_with_init,
)
from tests.integration.test_services_real import write_launcher as services_launcher

world = harness.world
"""The storage base and launcher fixture of the export gate (a fixture, reused by name)."""

RB_REALM = f"{PREFIX}rb"
RESUME_REALM = f"{PREFIX}rs"
ARCH_REALM = f"{PREFIX}rba"
REAL_PREFIX = "t83-"
SVC_REALM = f"{REAL_PREFIX}svc"
REPO_PKG = "htop"
RECOVER_LIST_S = 5.0
"""NFR-030: the command that settles a crash returns within this many seconds."""
NOTES = "kept across the rebuild\n"
FAKE_PKG = "ginn-gate-no-such-package"
LABEL_REALM = "io.ginn.realm"
LABEL_GENERATION = "io.ginn.generation"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def status(target: Target, name: str) -> dict[str, Any]:
    ran = run_ginn(target, "status", name)
    assert ran.rc == 0, ran.error_text()
    return ran.body


def generations(paths: OwnedPaths, name: str) -> list[str]:
    """The generation labels of the realm's containers, from the product's own podman adapter."""
    return sorted(
        str(c.labels.get(LABEL_GENERATION)) for c in podman_cli(paths).ps({LABEL_REALM: name})
    )


def audit_ops(paths: OwnedPaths, name: str, op: str) -> list[dict[str, Any]]:
    """The audit entries of one operation on one realm, oldest first (FR-124)."""
    found: list[dict[str, Any]] = []
    try:
        lines = paths.audit_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        with contextlib.suppress(ValueError):
            entry = json.loads(line)
            if entry.get("realm") == name and entry.get("op") == op:
                found.append(entry)
    return found


@pytest.fixture(scope="module")
def rb(world: World) -> Iterator[str]:
    """A running Debian realm with htop (create --with), xterm (install) and a file in its home."""
    with realm(world, "debian", RB_REALM, "--with", REPO_PKG) as name:
        ran = run_ginn(world, "install", name, DESKTOP_PKG)
        assert ran.rc == 0, ran.error_text()
        (world.host.realm_home(name) / "notes.txt").write_text(NOTES, encoding="utf-8")
        yield name


def timed_list(target: Target) -> tuple[Any, float]:
    """`ginn list` (it runs recovery first) and the seconds it took, interpreter start included."""
    began = time.monotonic()
    listed = run_ginn(target, "list")
    return listed, time.monotonic() - began


def run_ginn_when_idle(target: Target, *args: str, wait_s: float = 60.0) -> Any:
    """`run_ginn`, retried while a watcher-started `ginn _sync` holds the realm (GINN-E-REALM-BUSY).

    The install hook touches the stamp, and the host watcher then syncs the realm for a moment."""
    deadline = time.monotonic() + wait_s
    while True:
        ran = run_ginn(target, *args)
        if ran.rc != 75 or time.monotonic() > deadline:
            return ran
        time.sleep(1.0)


def ensure_running(world: World, name: str) -> None:
    ran = run_ginn(world, "start", name)
    assert ran.rc == 0, ran.error_text()


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-121")
@pytest.mark.fr("DR-034")
def test_rebuild_keeps_home_manifest_and_settings(world: World, rb: str) -> None:
    """FR-021 AC-1 (services-off): generation + 1, the packages are back, `notes.txt` is unchanged,
    tier and services are kept, the removed menu entries (xterm, and htop when it had one) are
    regenerated, one container remains."""
    ensure_running(world, rb)
    before = status(world, rb)
    gen = before["generation"]
    notes = world.host.realm_home(rb) / "notes.txt"
    digest = sha(notes)
    entry = entry_path(world, rb, DESKTOP_ID)
    htop_entry = entry_path(world, rb, REPO_PKG)
    entry.unlink(missing_ok=True)
    had_htop = htop_entry.exists()
    htop_entry.unlink(missing_ok=True)
    ran = run_ginn_when_idle(world, "rebuild", rb)
    assert ran.rc == 0, ran.error_text()
    body = ran.body
    assert body["generation"] == gen + 1, body
    assert body["replay_missing"] == [] and "GINN-W-REPLAY-MISSING" not in ran.codes
    assert body["old_removed"] is True
    space = [s for s in ran.steps if s.get("step") == "space-checked"]
    assert space, f"no space-checked step in {[s.get('step') for s in ran.steps]}"
    need, free = space[0]["required_bytes"], space[0]["free_bytes"]
    assert need > 0 and free >= need * 1.10, (need, free)
    after = status(world, rb)
    for key in ("state", "tier", "services", "distro"):
        assert after[key] == before[key], (key, before[key], after[key])
    assert after["generation"] == gen + 1
    assert sha(notes) == digest
    pkgs = in_realm(world, rb, f"dpkg -s {REPO_PKG} >/dev/null && dpkg -s {DESKTOP_PKG} >/dev/null")
    assert pkgs.rc == 0, "a manifest package is missing after the rebuild"
    assert entry.is_file(), "the exported entry was not regenerated"
    if had_htop:
        assert htop_entry.is_file(), "the replayed package's host entry was not regenerated"
    assert generations(world.paths, rb) == [str(gen + 1)]


@pytest.mark.fr("FR-022")
def test_rebuild_reports_a_package_the_upstream_lacks(world: World, rb: str) -> None:
    """FR-022 AC-1: the other packages install, the swap completes, and GINN-W-REPLAY-MISSING names
    the missing package, which leaves the manifest."""
    assert run_ginn(world, "stop", rb).rc == 0
    store = world.store()
    record = store.load(RealmName(rb))
    fake = ManifestEntry(FAKE_PKG, InstallSource.REPO, InstallMethod.GINN)
    store.save(replace(record, manifest=[*record.manifest, fake]))
    ran = run_ginn(world, "rebuild", rb)
    assert ran.rc == 0, ran.error_text()
    assert "GINN-W-REPLAY-MISSING" in ran.codes, ran.codes
    assert FAKE_PKG in ran.body["replay_missing"]
    names = {e.name for e in store.load(RealmName(rb)).manifest}
    assert FAKE_PKG not in names and REPO_PKG in names
    assert in_realm(world, rb, f"dpkg -s {REPO_PKG} >/dev/null").rc == 0


@pytest.mark.fr("FR-021")
@pytest.mark.fr("NFR-031")
def test_rebuild_cancelled_before_the_swap_keeps_the_old_realm(world: World, rb: str) -> None:
    """FR-021 AC-2, FR-107 cancel: SIGINT after the replay. The old generation is still the realm:
    same generation, one container, no journal entry, not broken, usable, home file intact."""
    ensure_running(world, rb)
    gen = status(world, rb)["generation"]
    ran = run_ginn(world, "rebuild", rb, env_extra={CANCEL_ENV: "replayed"})
    assert ran.rc != 0, "a cancelled rebuild reported success"
    assert journal_step(world.paths, rb) is None, "the cancel left a journal entry"
    after = status(world, rb)
    assert after["generation"] == gen
    assert after["state"] in ("running", "stopped"), after["state"]
    assert generations(world.paths, rb) == [str(gen)]
    assert run_ginn(world, "exec", rb, "--", "true", json_mode=False).rc == 0
    assert (world.host.realm_home(rb) / "notes.txt").read_text(encoding="utf-8") == NOTES
    assert in_realm(world, rb, f"dpkg -s {REPO_PKG} >/dev/null").rc == 0


@pytest.mark.fr("FR-122")
@pytest.mark.fr("NFR-030")
@pytest.mark.fr("NFR-031")
@pytest.mark.parametrize("point", ["replayed", "swapping"])
def test_killed_rebuild_is_settled_by_the_next_command(world: World, rb: str, point: str) -> None:
    """A SIGKILL right after the journal step `point`. The next command (`list`) recovers: the
    journal is gone, the record names one generation (gN, or gN+1 only at the swap), the realm has
    one container of that generation, and it starts and runs a command."""
    ensure_running(world, rb)
    gen = status(world, rb)["generation"]
    killed = run_ginn(world, "rebuild", rb, env_extra={KILL_ENV: point})
    assert killed.rc == -signal.SIGKILL, (
        f"not killed at {point}: rc {killed.rc} {killed.err[-300:]}"
    )
    assert journal_step(world.paths, rb) == point
    listed, took = timed_list(world)
    assert listed.rc == 0, listed.error_text()
    assert "GINN-W-RECOVERED" in listed.codes, listed.codes
    assert took <= RECOVER_LIST_S, f"recovery took {took:.1f} s (NFR-030 bound {RECOVER_LIST_S} s)"
    assert journal_step(world.paths, rb) is None
    after = status(world, rb)
    allowed = {gen} if point == "replayed" else {gen, gen + 1}
    assert after["generation"] in allowed, after
    assert after["state"] in ("running", "stopped"), after["state"]
    assert generations(world.paths, rb) == [str(after["generation"])]
    ensure_running(world, rb)
    assert run_ginn(world, "exec", rb, "--", "true", json_mode=False).rc == 0
    assert in_realm(world, rb, f"dpkg -s {REPO_PKG} >/dev/null").rc == 0
    assert (world.host.realm_home(rb) / "notes.txt").read_text(encoding="utf-8") == NOTES


@pytest.mark.fr("FR-023")
@pytest.mark.fr("FR-024")
@pytest.mark.fr("FR-124")
def test_update_check_changes_nothing_and_update_is_logged(world: World, rb: str) -> None:
    """FR-024: `update --check` reports a status and changes nothing. FR-023 AC-2: `update` runs
    the native full upgrade (nothing is left to upgrade after it) and the audit log has it."""
    ensure_running(world, rb)
    before = status(world, rb)
    checked = run_ginn(world, "update", "--check", rb)
    assert checked.rc == 0, checked.error_text()
    rows = checked.body["realms"]
    assert [r["realm"] for r in rows] == [rb], rows
    assert rows[0]["status"] in ("updates", "none"), rows[0]
    assert status(world, rb)["updated_at"] == before["updated_at"]
    assert audit_ops(world.paths, rb, "update") == []
    updated = run_ginn(world, "update", rb)
    assert updated.rc == 0, updated.error_text()
    row = updated.body["realms"][0]
    assert row["status"] == "updated", row
    logged = audit_ops(world.paths, rb, "update")
    assert len(logged) == 1 and logged[0]["outcome"] == "ok", logged
    left = in_realm(world, rb, "apt-get -s upgrade")
    assert left.rc == 0 and re.search(r"^0 upgraded,", left.out, re.M), left.out[-400:]
    rechecked = run_ginn(world, "update", "--check", rb)
    assert rechecked.body["realms"][0]["status"] == "none", rechecked.body


@pytest.mark.fr("FR-122")
@pytest.mark.fr("FR-010")
@pytest.mark.parametrize("point", ["homed", "created"])
def test_create_killed_is_broken_then_resumed(world: World, point: str) -> None:
    """FR-122 AC-1: a create killed after `homed` (before the image is pulled) or after `created`
    reads `broken`, the text names both recovery
    commands, a file in the Realm Home survives `create --resume`, and the resumed realm runs."""
    name = RESUME_REALM
    try:
        killed = run_ginn(world, "create", "debian", "--name", name, env_extra={KILL_ENV: point})
        assert killed.rc == -signal.SIGKILL, f"not killed: rc {killed.rc} {killed.err[-300:]}"
        assert journal_step(world.paths, name) == point
        text = run_ginn(world, "list", json_mode=False)
        assert text.rc == 0, text.err[-400:]
        assert f"ginn create --resume {name}" in text.out, text.out
        assert f"ginn delete {name}" in text.out, text.out
        assert status(world, name)["state"] == "broken"
        marker = world.host.realm_home(name) / "marker.txt"
        marker.write_text("home kept\n", encoding="utf-8")
        resumed = run_ginn(world, "create", "--resume", name)
        assert resumed.rc == 0, resumed.error_text()
        assert resumed.body.get("resumed") is True, resumed.body
        assert status(world, name)["state"] == "stopped"
        assert marker.read_text(encoding="utf-8") == "home kept\n"
        ensure_running(world, name)
        ran = run_ginn(world, "exec", name, "--", "echo", "ok", json_mode=False)
        assert ran.rc == 0 and ran.out == "ok\n", ran.out
    finally:
        run_ginn(world, "delete", name, "--purge-home", "--yes", timeout=600.0)


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-043")
def test_rebuild_arch_replays_repo_and_aur_packages(world: World) -> None:
    """FR-021 AC-1 with an AUR package: arch realm with a repository package and the AUR test
    package (`GINN_AUR_PACKAGE`, see test_aur_real.py); after `ginn rebuild`, both are installed,
    the AUR entry is still `aur` in the manifest, the home file is unchanged."""
    with realm(world, "arch", ARCH_REALM, "--with", REPO_PKG) as name:
        built = run_ginn(world, "install", name, "--aur", AUR_PACKAGE)
        assert built.rc == 0, built.error_text()
        notes = world.host.realm_home(name) / "notes.txt"
        notes.write_text(NOTES, encoding="utf-8")
        before = status(world, name)
        ran = run_ginn(world, "rebuild", name)
        assert ran.rc == 0, ran.error_text()
        assert ran.body["generation"] == before["generation"] + 1
        assert "GINN-W-REPLAY-MISSING" not in ran.codes, ran.body
        for pkg in (REPO_PKG, AUR_PACKAGE):
            assert in_realm(world, name, f"pacman -Q {pkg}").rc == 0, f"{pkg} missing after rebuild"
        entries = {e.name: e for e in world.store().load(RealmName(name)).manifest}
        assert entries[AUR_PACKAGE].source is InstallSource.AUR
        assert entries[REPO_PKG].source is InstallSource.REPO
        assert notes.read_text(encoding="utf-8") == NOTES


# ---- services-on, on the user's real supervisor (T8.3 open items) ----


@dataclass
class Real(Target):
    """The T7.7 launcher on the user's real Owned Paths, with the step seam appended."""

    paths: OwnedPaths = None  # type: ignore[assignment]


def sweep_real(real: Real) -> list[str]:
    """Delete every realm named `t83-...` with its Realm Home. Return the names that remain."""
    for row in run_ginn(real, "list").body.get("realms", []):
        name = str(row.get("name", ""))
        if name.startswith(REAL_PREFIX):
            run_ginn(real, "delete", name, "--purge-home", "--yes", timeout=600.0)
    for leaf in CGROUP_ROOT.glob(f"{REAL_PREFIX}*"):
        # podman and `ginn delete` leave the empty realm cgroup behind (finding F9). Only this
        # module's own empty directories are removed.
        with contextlib.suppress(OSError):
            if leaf.is_dir() and not (leaf / "cgroup.procs").read_text(encoding="utf-8").split():
                leaf.rmdir()
    return [
        str(r.get("name"))
        for r in run_ginn(real, "list").body.get("realms", [])
        if str(r.get("name", "")).startswith(REAL_PREFIX)
    ]


@pytest.fixture(scope="module")
def real() -> Iterator[Real]:
    """The launcher on the user's real storage. Skipped when the host is not set up for ginn."""
    try:
        paths = OwnedPaths.from_env()
    except GinnError as exc:
        pytest.skip(f"the host is not set up for ginn ({exc.code}): services-on items need it")
    run_tool([tool("make"), "-C", str(REPO / "native")])
    base = Path(tempfile.mkdtemp(prefix="ginn-w1f-", dir="/tmp"))  # noqa: S108 - mkdtemp, 0700
    bin_dir = base / "bin"
    bin_dir.mkdir(mode=0o700)
    script = services_launcher(bin_dir)
    text = script.read_text(encoding="utf-8")
    script.write_text(
        text.replace("if __name__ == '__main__':", STEP_SEAM + "if __name__ == '__main__':"),
        encoding="utf-8",
    )
    target = Real(
        launcher=script,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        paths=paths,
    )
    exec_existed = exec_lock_dir(paths).is_dir()
    relay_existed = (paths.sv / "_exec").exists()
    _CREATED.clear()
    try:
        assert sweep_real(target) == [], "stale t83 realms could not be removed before the gate"
        yield target
    finally:
        left = sweep_real(target)
        remove_exec_leftovers(paths, [SVC_REALM], exec_existed=exec_existed)
        if not relay_existed:
            _retire(exec_service_dir(paths))
            for stale in sorted(paths.sv.glob("._exec.*")):
                shutil.rmtree(stale, ignore_errors=True)
            with contextlib.suppress(OSError):
                (paths.sv / ".exec-relay.lock").unlink()
            with contextlib.suppress(OSError):
                exec_socket(paths).unlink()
        shutil.rmtree(base, ignore_errors=True)
        assert left == [], f"t83 realms could not be removed after the gate: {left}"


@pytest.fixture(scope="module")
def svc(real: Real) -> Iterator[str]:
    """A running services-on arch realm with htop, supervised by the user's runit.

    A services-on create refuses `--with` (GINN-E-NOT-SUPPORTED-YET, ops/_create_plan.py, ADR-003),
    so htop goes in with `ginn install` after the first supervised start, through the ADR-014 relay.
    """
    made = run_ginn(real, "create", "arch", "--name", SVC_REALM, "--services", "on")
    assert made.rc == 0, made.error_text()
    started = run_ginn(real, "start", SVC_REALM)
    assert started.rc == 0, started.error_text()
    wait_run_with_init(real.paths, SVC_REALM)
    added = run_ginn(real, "install", SVC_REALM, REPO_PKG, timeout=CREATE_TIMEOUT)
    assert added.rc == 0, added.error_text()
    assert_runs_htop(real, SVC_REALM)
    yield SVC_REALM


def settle_running(real: Real, name: str) -> None:
    """Start the realm if it is not running, and wait for its container init under runit."""
    ran = run_ginn(real, "start", name)
    assert ran.rc == 0, ran.error_text()
    wait_run_with_init(real.paths, name)
    assert sv_status(real.paths, name).startswith("run:"), sv_status(real.paths, name)


def assert_runs_htop(real: Real, name: str) -> None:
    rc, out = leaf_exec(real.paths, name, f"pacman -Q {REPO_PKG}")
    assert rc == 0 and out.startswith(REPO_PKG), out


@pytest.mark.fr("FR-021")
@pytest.mark.fr("FR-081")
def test_services_on_rebuild_replays_under_runit(real: Real, svc: str) -> None:
    """FR-021 AC-1, services-on: a running realm is rebuilt through runit. Generation + 1, htop is
    replayed (through the supervisor relay, ADR-014), runit supervises gN+1, one container remains,
    and the runit directory of the realm still exists."""
    settle_running(real, svc)
    gen = status(real, svc)["generation"]
    ran = run_ginn(real, "rebuild", svc, timeout=CREATE_TIMEOUT)
    assert ran.rc == 0, ran.error_text()
    assert ran.body["generation"] == gen + 1, ran.body
    assert ran.body["replay_missing"] == [] and "GINN-W-REPLAY-MISSING" not in ran.codes
    settle_running(real, svc)
    assert status(real, svc)["services"] is True
    assert service_dir(real.paths, RealmName(svc)).is_dir()
    assert generations(real.paths, svc) == [str(gen + 1)]
    assert_runs_htop(real, svc)


@pytest.mark.fr("FR-021")
@pytest.mark.fr("NFR-031")
def test_services_on_cancelled_rebuild_rolls_back(real: Real, svc: str) -> None:
    """FR-021 AC-2, services-on: SIGINT once gN+1 is started under runit. The rollback takes gN+1
    down through `sv down` and removes it. The old realm runs again: same generation, one container,
    no journal entry, runit up, htop present. Only the successful `sv down` path is reached."""
    settle_running(real, svc)
    gen = status(real, svc)["generation"]
    ran = run_ginn(real, "rebuild", svc, env_extra={CANCEL_ENV: "started"}, timeout=CREATE_TIMEOUT)
    assert ran.rc != 0, "a cancelled rebuild reported success"
    assert journal_step(real.paths, svc) is None
    assert status(real, svc)["generation"] == gen
    assert generations(real.paths, svc) == [str(gen)]
    settle_running(real, svc)
    assert_runs_htop(real, svc)


@pytest.mark.fr("FR-122")
@pytest.mark.fr("NFR-030")
def test_services_on_killed_rebuild_is_settled(real: Real, svc: str) -> None:
    """FR-122, services-on: SIGKILL after the replay. The next command settles it: no gN+1
    container (runsv was told to stop it before removal), one generation, and the realm starts."""
    settle_running(real, svc)
    gen = status(real, svc)["generation"]
    killed = run_ginn(
        real, "rebuild", svc, env_extra={KILL_ENV: "replayed"}, timeout=CREATE_TIMEOUT
    )
    assert killed.rc == -signal.SIGKILL, f"not killed: rc {killed.rc} {killed.err[-300:]}"
    assert journal_step(real.paths, svc) == "replayed"
    listed, took = timed_list(real)
    assert listed.rc == 0, listed.error_text()
    assert "GINN-W-RECOVERED" in listed.codes, listed.codes
    assert took <= RECOVER_LIST_S, f"recovery took {took:.1f} s (NFR-030 bound {RECOVER_LIST_S} s)"
    assert journal_step(real.paths, svc) is None
    assert status(real, svc)["generation"] == gen
    assert generations(real.paths, svc) == [str(gen)]
    settle_running(real, svc)
    assert_runs_htop(real, svc)
