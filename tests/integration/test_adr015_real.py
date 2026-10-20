# SPDX-License-Identifier: Apache-2.0
"""Host gate for ADR-015 (T7.27, phase A: items 1, 2, 3, 6 and 7), on real rootless Podman.

Run with `GINN_INTEGRATION=1 .venv/bin/pytest tests/integration/test_adr015_real.py -v -s` on a
Void glibc host with the ginn supervisor running. The verdicts and numbers are in
spikes/results/T7.27-adr015-real.md.

Phase C (items 5, 9, 10, 11 and 14) runs as its own invocation, `-k phasec`, and phase D as
`-k phased`, as run 7 did. A full-module run skips arch for items 5, 9, 10 and 11: the module-scoped
svc and idle_set arch realms are still alive when build[arch] starts, so the pinned base is present,
and the build fixture skips that distribution. No fixture change is made for this.

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
import re
import shutil
import signal
import statistics
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from ginn.distros import load as load_distro
from ginn.ops.disk import estimate_required
from ginn.runtime.provision import LABEL_BASE_DIGEST, LABEL_INIT_SET, LABEL_PROVISION
from ginn.runtime.spec import SERVICES_MASKED_UNITS, SERVICES_USERDBD_UNITS
from ginn.services._exec_lock import exec_lock_dir
from ginn.services.exec_relay import exec_service_dir, exec_socket
from ginn.services.supervisor import parse_status
from ginn.store.paths import OwnedPaths
from ginn.trust import pull_reference
from tests.integration.conftest import REPO, run_tool, tool
from tests.integration.test_services_real import (
    Ran,
    _retire,
    container_id,
    host_memory,
    podman,
    podman_cli,
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


HOME_NAME = re.compile(r"t727-[a-z0-9-]+")
"""The exact shape of a t727 realm name, and so of its Realm Home. Only such a name is removed."""


def orphan_homes(paths: OwnedPaths, listed: Iterable[str]) -> list[Path]:
    """The Realm Homes of t727 names that no realm in `listed` (`ginn list`) owns.

    A delete without `--purge-home` drops the realm record and keeps the home (run 8 left
    t727-fedora-off this way), and create refuses a name whose home exists (GINN-E-NAME-TAKEN,
    ops/create.py). Only the top level of the homes directory is read. An entry counts when its name
    is an exact t727 name, it is a real directory (not a link) and this user owns it.
    """
    try:
        entries = list(os.scandir(paths.homes))
    except FileNotFoundError:
        return []
    known = set(listed)
    found: list[Path] = []
    for entry in entries:
        if entry.name in known or not HOME_NAME.fullmatch(entry.name):
            continue
        try:
            owner = entry.stat(follow_symlinks=False).st_uid
        except FileNotFoundError:
            continue  # removed since the listing: nothing to remove
        if entry.is_dir(follow_symlinks=False) and owner == os.getuid():
            found.append(Path(entry.path))
    return found


def mounted_under(path: Path) -> bool:
    """True when a mount point is `path` or lies under it, read from this process's mount table."""
    prefix = str(path)
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        # The mount point is the fifth field. The kernel escapes a space, tab, newline and backslash
        # as a backslash and three octal digits.
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), line.split()[4])
        if mount == prefix or mount.startswith(prefix + "/"):
            return True
    return False


def remove_orphan_home(home: Path) -> str | None:
    """Remove one orphan Realm Home. Return None when it is gone, else the reason it stays.

    Nothing is removed while a mount point lies under it: a bind mount would send the removal to the
    files it shows. `shutil.rmtree` does not follow links.
    """
    if mounted_under(home):
        return "a mount point lies under it"
    try:
        shutil.rmtree(home)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"[:200]
    print(f"T727_HOME_REMOVED {home.name}")
    return None


def listed_realms(launcher: Path) -> tuple[list[str] | None, str]:
    """The realm names of `ginn list`, or None with the reason the listing cannot be trusted.

    A failed call, or an output without a `realms` key, must not read as an empty list: an empty
    list would make every t727 Realm Home an orphan, and the sweep would remove homes whose realms
    still have records.
    """
    ran = run_ginn(launcher, "list")
    if ran.rc == 0 and "realms" in ran.body:
        return [str(realm.get("name", "")) for realm in ran.body["realms"]], ""
    return None, (ran.error_text() if ran.rc != 0 else "its output has no realms key")


def sweep(launcher: Path, paths: OwnedPaths) -> list[str]:
    """Delete every realm whose name starts with PREFIX, with its Realm Home, and every orphan Realm
    Home of a PREFIX name (`orphan_homes`). Returns what is left.

    A `ginn list` that fails removes nothing: the sweep returns "ginn list failed: ..." in what is
    left, so the pre-run and the teardown asserts fail. Empty cgroup leaves of this module are
    removed too (finding F9 of the T7.7 gate). The images and containers of the module are checked
    by `leftovers` (ADR-015 Decision 13).
    """
    listed, failure = listed_realms(launcher)
    if listed is None:
        return [f"ginn list failed: {failure}"]
    for name in listed:
        if name.startswith(PREFIX):
            run_ginn(launcher, "delete", name, "--purge-home", "--yes")
    for leaf in CGROUP_ROOT.glob(f"{PREFIX}*"):
        # podman and `ginn delete` leave the empty realm cgroup behind (F9 of the T7.7 gate).
        if leaf.is_dir() and not (leaf / "cgroup.procs").read_text(encoding="utf-8").split():
            with contextlib.suppress(OSError):
                leaf.rmdir()
    listed, failure = listed_realms(launcher)
    if listed is None:
        return [f"ginn list failed: {failure}"]
    left = [name for name in listed if name.startswith(PREFIX)]
    for home in orphan_homes(paths, listed):
        reason = remove_orphan_home(home)
        if reason is not None:
            left.append(f"Realm Home {home.name}: {reason}")
    return left


def leftovers(paths: OwnedPaths) -> dict[str, list[str]]:
    """Images and containers of this module left in ginn's storage (ADR-015 Decisions 10, 13).

    It reads the product environment, as `heal_leftovers` does, so both see the same storage.
    """
    images = product_podman(paths, ["images", "--format", "{{.Repository}}:{{.Tag}}"]).split()
    containers = product_podman(paths, ["ps", "-a", "--format", "{{.Names}}"]).split()
    return {
        "images": [i for i in images if PREFIX in i],
        "containers": [c for c in containers if PREFIX in c],
    }


PROBE = "t727-change-probe"
"""The item 14 scratch container (the `commit --change` probe). A crashed run leaves it behind."""
PROBE_IMAGE = "localhost/t727-change-probe:1"
"""The scratch image of PROBE. Item 14 removes it, and the launcher removes it after a crash."""


def heal_leftovers(paths: OwnedPaths) -> None:
    """Remove what a crashed earlier run of this module left: exact t727 names only (item 4).

    The probe container and image of item 14, then the provisioning containers and the derived
    images of the t727 realms. A container goes before its image, since a container holds it. Each
    removal ignores its status: the leftovers check after this call is what proves the removal.
    """
    product_run(paths, ["rm", "-f", PROBE])
    product_run(paths, ["rmi", "-f", PROBE_IMAGE])
    containers = product_podman(paths, ["ps", "-a", "--format", "{{.Names}}"]).split()
    for container in containers:
        if container.startswith(f"ginn_provision_{PREFIX}"):
            product_run(paths, ["rm", "-f", container])
    images = product_podman(paths, ["images", "--format", "{{.Repository}}:{{.Tag}}"]).split()
    for image in images:
        if image.startswith(f"localhost/ginn-init-{PREFIX}"):
            product_run(paths, ["rmi", "-f", image])


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


def run_ginn_env(
    launcher: Path, extra: Mapping[str, str], *args: str, timeout: float = 900.0
) -> Ran:
    """`ginn --json` as `ginn()` runs it, with `extra` in the environment of this one call only.

    The test process's own environment is not changed, so every later call and every cleanup runs
    without `extra`. The JSON reading is that of `ginn()`.
    """
    env = {
        **os.environ,
        "PATH": f"{launcher.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        **extra,
    }
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(launcher), "--json", *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd="/tmp",  # noqa: S108 - a working directory, not a temp file
        timeout=timeout,
        check=False,
    )
    lines = (done.stdout + "\n" + done.stderr).splitlines()
    objects = [json.loads(line) for line in lines if line.startswith("{")]
    body: dict[str, Any] = next((o for o in reversed(objects) if "progress" not in o), {})
    return Ran(done.returncode, body)


def ginn_steps(
    launcher: Path, *args: str, timeout: float = 900.0
) -> tuple[Ran, list[dict[str, Any]]]:
    """`ginn --json` as `ginn()` runs it, and the progress objects it printed, in the order printed.

    A finished step of an operation prints `{"progress": {"step", "message"}}` on stderr (FR-107,
    cli/output.py). The fields of a step are read from the objects returned here. `ginn()` and
    `run_ginn_env` drop them. The environment is read at call time, so a `mock.patch.dict` around
    the call reaches the command.
    """
    env = {**os.environ, "PATH": f"{launcher.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(launcher), "--json", *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd="/tmp",  # noqa: S108 - a working directory, not a temp file
        timeout=timeout,
        check=False,
    )
    lines = (done.stdout + "\n" + done.stderr).splitlines()
    objects = [json.loads(line) for line in lines if line.startswith("{")]
    body: dict[str, Any] = next((o for o in reversed(objects) if "progress" not in o), {})
    steps = [o["progress"] for o in objects if isinstance(o.get("progress"), dict)]
    return Ran(done.returncode, body), steps


SPACE_STEP = "space-checked"
"""The progress step where a create, a resume or a rebuild compares free space with its figure."""


def space_checked(label: str, steps: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """The product's `required_bytes` and `free_bytes`, from the command's `space-checked` step.

    The T7.35 contract: the object `{"progress": {"step": "space-checked", "required_bytes": N,
    "free_bytes": M, ...}}` of `ginn --json` carries the figure the product compares with free
    space, with the 1.10 margin applied. The gate reads that figure and does not re-derive it. A
    path that checks nothing reports `required_bytes` 0 with a `reason`, and that fails here: there
    is no figure to bound the measured peak by.
    """
    found = [step for step in steps if step.get("step") == SPACE_STEP]
    assert len(found) == 1, (
        f"{label}: {len(found)} `{SPACE_STEP}` progress steps, expected 1; steps: {list(steps)}"
    )
    step = found[0]
    assert "required_bytes" in step and "free_bytes" in step, (
        f"{label}: the `{SPACE_STEP}` step lacks required_bytes or free_bytes (T7.35 contract): "
        f"{step}"
    )
    required, free = step["required_bytes"], step["free_bytes"]
    # A step that checks nothing reports required_bytes 0 (or none) with free_bytes None and a
    # reason, so the reason is read before the free figure is required to be an integer.
    assert isinstance(required, int) and required > 0, (
        f"{label}: required_bytes is {required!r}, reason {step.get('reason')!r}: this path "
        "checked nothing, so the measured peak has no figure to be bounded by"
    )
    assert isinstance(free, int), f"{label}: free_bytes is {free!r} in the `{SPACE_STEP}` step"
    print(f"T727_SPACE {label} {json.dumps(step, sort_keys=True)}")
    return required, free


def teardown_step(
    failed: list[str], label: str, step: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Run one teardown step. A failure is recorded in `failed`, so the steps after it still run."""
    try:
        return step(*args, **kwargs)
    except Exception as exc:
        failed.append(f"{label}: {exc!r}"[:300])
        return None


@pytest.fixture(scope="module")
def paths() -> OwnedPaths:
    return OwnedPaths.from_env()


@pytest.fixture(scope="module")
def launcher(paths: OwnedPaths) -> Iterator[Path]:
    """The gate's ginn command (the T7.7 launcher), with the relay service of this gate tracked."""
    run_tool([tool("make"), "-C", str(REPO / "native")])
    base = Path(tempfile.mkdtemp(prefix="ginn-t727-", dir="/tmp"))  # noqa: S108 - mkdtemp, 0700
    exec_existed = exec_lock_dir(paths).is_dir()
    relay_existed = (paths.sv / "_exec").exists()
    failed: list[str] = []
    script: Path | None = None
    try:
        bin_dir = base / "bin"
        bin_dir.mkdir(mode=0o700)
        script = write_launcher(bin_dir)
        if FROZEN_SRC:
            # Run the product from the frozen copy of the committed code, not the worktree's src.
            text = script.read_text(encoding="utf-8")
            script.write_text(text.replace(str(REPO / "src"), FROZEN_SRC), encoding="utf-8")
        # The step seam (Phases C and D): each create step is appended to STEP_LOG_ENV when that is
        # set, and the process is SIGKILLed after the step named by KILL_ENV. Unset, it changes
        # nothing.
        text = script.read_text(encoding="utf-8")
        script.write_text(
            text.replace("if __name__ == '__main__':", SEAM_PATCH + "if __name__ == '__main__':"),
            encoding="utf-8",
        )
        _CREATED.clear()
        # A realm, image or Realm Home of an earlier run would collide with this run's names, so
        # none may remain. The sweep removes the realms and their homes, and heal_leftovers the
        # crashed run's probe.
        kept = sweep(script, paths)
        assert kept == [], (
            f"t727 realms or Realm Homes from an earlier run could not be removed: {kept}"
        )
        heal_leftovers(paths)
        assert leftovers(paths) == {"images": [], "containers": []}, (
            "t727 leftovers before the gate"
        )
        yield script
    finally:
        # Every step runs, even after one fails. The failures are asserted at the end.
        left: list[str] = []
        remaining: dict[str, list[str]] = {"images": [], "containers": []}
        if script is not None:
            left = teardown_step(failed, "sweep", sweep, script, paths) or []
            remaining = teardown_step(failed, "leftovers", leftovers, paths) or remaining
        teardown_step(
            failed,
            "remove exec leftovers",
            remove_exec_leftovers,
            paths,
            _CREATED,
            exec_existed=exec_existed,
        )
        if not relay_existed:
            # The relay `_exec` service is made by the first services-on start (ADR-014). It goes
            # with this module when it was absent before, as in the T7.7 gate.
            teardown_step(failed, "retire relay service", _retire, exec_service_dir(paths))
            for stale in sorted(paths.sv.glob("._exec.*")):
                teardown_step(
                    failed, "stale relay directory", shutil.rmtree, stale, ignore_errors=True
                )
            with contextlib.suppress(OSError):
                (paths.sv / ".exec-relay.lock").unlink()
            with contextlib.suppress(OSError):
                exec_socket(paths).unlink()
        teardown_step(failed, "remove the gate directory", shutil.rmtree, base, ignore_errors=True)
        assert failed == [], f"t727 teardown steps failed: {failed}"
        assert left == [], f"t727 realms or Realm Homes could not be removed after the gate: {left}"
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
    derived = realm_image(paths, name)
    derived_size = int(
        product_podman(paths, ["image", "inspect", "--format", "{{.Size}}", derived]).strip()
    )
    digest = str(image_labels(paths, derived).get(LABEL_BASE_DIGEST, ""))
    base_id = image_id_of_digest(paths, digest) if digest else None
    base_size = None
    if base_id is not None:
        base_size = int(
            product_podman(paths, ["image", "inspect", "--format", "{{.Size}}", base_id]).strip()
        )
    size_text = product_podman(
        paths,
        ["container", "inspect", "--size", "--format", "{{.SizeRw}}", realm_cid(paths, name)],
    ).strip()
    assert size_text.isdigit(), f"podman reports no SizeRw for {name}: {size_text!r}"
    writable = int(size_text)
    disk = run_ginn(launcher, "status", name).body.get("disk") or {}
    home_text = disk.get("realm_home_bytes")
    assert home_text is not None, f"ginn status reports no realm_home_bytes for {name}: {disk}"
    home = int(home_text)
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


def assert_attributed(attribution: dict[str, Any], label: str) -> None:
    """A sample counts only when its attribution found the realm's cgroup processes, its runsv and
    its ginn-wait. An empty role means the attribution missed part of the realm (item 6), so the
    sample fails.
    """
    helpers = attribution["helpers"]
    missing = [role for role in ("cgroup", "runsv", "ginn-wait") if not helpers[role]]
    assert not missing, f"{label}: no process found for {missing}; the sample is not attributed"


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
        assert_attributed(attribution, f"{distro} nfr003 run {run}")
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
    assert steady["target_bytes"] is not None, (
        "NFR-008: the realm's pinned base is not found at steady state"
    )
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
        attributed = {name: realm_rss(paths, name) for name in names}
        for name, attribution in attributed.items():
            assert_attributed(attribution, f"{distro} {name} nfr021 sample {index + 1}")
        vm = {name: attributed[name]["kib"]["VmRSS"] for name in names}
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


# ----------------------------------------------------------------------------------------------
# Phase C (T7.27 items 5, 9, 11 and 14) and Phase D (items 8 and 10). Run as two invocations:
# `-k phasec` (the base is pulled by the first create of each distribution), then `-k phased`.
# Every podman call here passes the product's global flags and environment (product_run).
# ----------------------------------------------------------------------------------------------

PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
"""The proxy variables that the product passes to podman (podman/cli.py, _PASSTHROUGH_ENV)."""
DUMMY_PROXY = "http://proxy.invalid:3128"
"""A dummy proxy for item 14. Nothing listens there, so a pull under it would fail. Item 14 creates
only from a base that is already present."""
PROXY_ENV = {
    "HTTP_PROXY": DUMMY_PROXY,
    "HTTPS_PROXY": DUMMY_PROXY,
    "http_proxy": DUMMY_PROXY,
    "https_proxy": DUMMY_PROXY,
    "NO_PROXY": "*",
    "no_proxy": "*",
}
"""The environment of item 14's one create, for that call only. NO_PROXY=* sends its pull direct."""
STEP_LOG_ENV = "GINN_T727_STEP_LOG"
"""When set, every create step is appended to this file, one step per line (the step seam)."""
KILL_ENV = "GINN_T727_KILL_AT"
"""When set, the create is SIGKILLed right after this journal step is recorded (the step seam)."""
SEAM_PATCH = r"""
import dataclasses as _dataclasses
import os as _os
import signal as _signal
_orig_create_hooks = _context.create_hooks
def _create_hooks_with_seams(paths=None, output=None, *, operation="create"):
    hooks = _orig_create_hooks(paths, output, operation=operation)
    log = _os.environ.get("GINN_T727_STEP_LOG", "")
    kill = _os.environ.get("GINN_T727_KILL_AT", "")
    if not log and not kill:
        return hooks
    prev = hooks.on_step
    def on_step(step):
        if prev is not None:
            prev(step)
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(step + "\n")
        if kill and step == kill:
            _os.kill(_os.getpid(), _signal.SIGKILL)
    return _dataclasses.replace(hooks, on_step=on_step)
_context.create_hooks = _create_hooks_with_seams
"""
"""Appended to the launcher before its main guard. It only wraps `_context.create_hooks`."""
KILL_POINTS = (
    "provision-created",
    "init-installed",
    "ips-installed",
    "stage-committed",
    "reset-created",
    "image-committed",
    "reset-removed",
    "stage-removed",
    "provision-removed",
)
"""The provisioning steps of Decision 13, in order, each a kill point (NFR-030). Phase C's step log
shows the order a real create takes (see the Phase C section of the results file)."""
VAR_TMP = Path("/var/tmp")  # noqa: S108 - listed only, never written or removed
"""The default image copy directory of podman (ADR-015 Decision 11, Open question 10)."""


def product_run(
    paths: OwnedPaths, args: Sequence[str], timeout: float = 1800.0
) -> subprocess.CompletedProcess[str]:
    """One podman call with the product's global flags and environment (podman/cli.py, `_env`).

    The allowlist mirrors the adapter: XDG_RUNTIME_DIR, the two config files and TMPDIR are ginn's,
    the proxy variables pass through as the product passes them, and nothing else reaches podman.
    """
    env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": os.environ.get("HOME", ""),
        "XDG_RUNTIME_DIR": str(paths.runtime),
        "CONTAINERS_CONF": str(paths.podman_containers_conf),
        "CONTAINERS_STORAGE_CONF": str(paths.podman_storage_conf),
        "TMPDIR": str(paths.image_tmp),
        **{key: os.environ[key] for key in PROXY_KEYS if key in os.environ},
    }
    return subprocess.run(  # noqa: S603 - fixed argv list, no shell (NFR-SEC-005)
        [*podman_cli(paths).global_argv(), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def product_podman(paths: OwnedPaths, args: Sequence[str]) -> str:
    """The stdout of one product-environment podman call, which must succeed."""
    done = product_run(paths, args)
    assert done.returncode == 0, (
        f"podman {args[0]} failed (rc {done.returncode}): {done.stderr.strip()[-300:]}"
    )
    return done.stdout


DU_ALLOCATED = ("-sxB1",)
"""The `du` flags of the storage peak: allocated bytes, on one filesystem (`-x`), so a running
container's merged mount is not counted. T7.33 measured these flags (ADR-015 Decision 11)."""
DU_APPARENT = ("-sbx",)
"""The `du` flags of the apparent bytes, on one filesystem. Recorded, and not asserted."""


DU_RETRIES = 1
"""How many times a failed `du` count is tried again. One retry covers the race of run 8: podman
renaming or removing an entry while `du` walks the tree (`fts_read` ENOENT)."""
DU_RETRY_S = 0.5
"""The pause before a `du` count is tried again."""


def _du_once(paths: OwnedPaths, path: str | Path, flags: Sequence[str]) -> int:
    done = product_run(paths, ["unshare", "/usr/bin/du", *flags, "--", str(path)], timeout=900.0)
    fields = done.stdout.split()
    if done.returncode != 0 or not fields or not fields[0].isdigit():
        raise ValueError(
            f"du under podman unshare failed (rc {done.returncode}): {done.stderr.strip()[-200:]}"
        )
    return int(fields[0])


def du_bytes(paths: OwnedPaths, path: str | Path, flags: Sequence[str] = DU_ALLOCATED) -> int:
    """The bytes under `path`, counted inside `podman unshare`, so keep-id layers can be read.

    The default is allocated bytes. `flags` takes `DU_APPARENT` for apparent bytes. A non-zero exit
    is a failure even when du printed a total: a partial total is an under-count (an entry that
    vanished during the walk), and a peak must not rest on one. A failed count is tried again
    DU_RETRIES times, DU_RETRY_S apart, and the last failure is raised. The retry is here, so every
    caller gets it: the storage watch and `realm_upper`, which reads a running realm.
    """
    for _ in range(DU_RETRIES):
        try:
            return _du_once(paths, path, flags)
        except ValueError:
            time.sleep(DU_RETRY_S)
    return _du_once(paths, path, flags)


def realm_cid(paths: OwnedPaths, name: str) -> str:
    """The id of the realm's container, found by its io.ginn.realm label (product environment)."""
    listing = product_podman(
        paths, ["ps", "-a", "--filter", f"label=io.ginn.realm={name}", "--format", "{{.ID}}"]
    )
    return listing.split()[0]


def realm_image(paths: OwnedPaths, name: str) -> str:
    """The image ID the realm's container runs from (product environment)."""
    return product_podman(
        paths, ["inspect", "--format", "{{.Image}}", realm_cid(paths, name)]
    ).strip()


def image_labels(paths: OwnedPaths, image: str) -> dict[str, str]:
    """The labels of an image, as podman reads them (product environment).

    podman prints `null` for an unlabelled image, so the `or {}` keeps a `.get` on the result safe.
    """
    raw = product_podman(paths, ["image", "inspect", "--format", "{{json .Labels}}", image])
    labels: dict[str, str] = json.loads(raw) or {}
    return labels


def image_env(paths: OwnedPaths, image: str) -> list[str]:
    """The `Env` of an image's config (product environment)."""
    env: list[str] = json.loads(
        product_podman(paths, ["image", "inspect", "--format", "{{json .Config.Env}}", image])
    )
    return env


def image_id_of_digest(paths: OwnedPaths, digest: str) -> str | None:
    """The short ID of the local image whose manifest digest is `digest`, or None."""
    listing = product_podman(paths, ["image", "ls", "--digests", "--format", "{{.ID}} {{.Digest}}"])
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == digest:
            return parts[0]
    return None


def base_id_of(paths: OwnedPaths, derived: str) -> str | None:
    """The local image whose manifest digest is the derived image's `io.ginn.base-digest`."""
    digest = str(image_labels(paths, derived).get(LABEL_BASE_DIGEST, ""))
    if not digest:
        return None
    return image_id_of_digest(paths, digest)


def pinned_base_id(paths: OwnedPaths, distro: str) -> str | None:
    """The local image of the pinned base of `distro`: the digest of its Trust Manifest pin."""
    return image_id_of_digest(paths, pull_reference(distro).rpartition("@")[2])


def base_holder(launcher: Path, paths: OwnedPaths, base_id: str) -> str | None:
    """The name of a realm that runs the base, or a derived image built on it, when one is known.

    The IDs are compared by their first twelve characters: `ps` and `inspect` print the full ID.
    """
    for realm in run_ginn(launcher, "list").body.get("realms", []):
        name = str(realm.get("name", ""))
        try:
            image = realm_image(paths, name)
        except IndexError:
            continue  # the realm has no container
        if image[:12] == base_id[:12]:
            return name
        built_on = base_id_of(paths, image) or ""
        if built_on[:12] == base_id[:12]:
            return name
    return None


def provision_leftovers(paths: OwnedPaths, name: str) -> dict[str, list[str]]:
    """The provisioning containers and the derived or stage images of one realm, by name prefix."""
    containers = product_podman(paths, ["ps", "-a", "--format", "{{.Names}}"]).split()
    images = product_podman(paths, ["images", "--format", "{{.Repository}}:{{.Tag}}"]).split()
    return {
        "containers": [c for c in containers if c.startswith(f"ginn_provision_{name}_")],
        "images": [i for i in images if i.startswith(f"localhost/ginn-init-{name}:")],
    }


def tmp_entries(directory: Path) -> dict[str, tuple[int, int, int]]:
    """Each entry of `directory` as name -> (owner uid, mtime in ns, size), links not followed.

    Read only (scandir and lstat). An entry removed after it was listed is skipped.
    """
    found: dict[str, tuple[int, int, int]] = {}
    with os.scandir(directory) as scan:
        for entry in scan:
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            found[entry.name] = (st.st_uid, st.st_mtime_ns, st.st_size)
    return found


def named_for_ginn(name: str) -> bool:
    """True when an entry name carries a hint of podman's, buildah's or ginn's storage."""
    return any(
        hint in name for hint in ("podman", "containers", "storage", "ginn", "libpod", "buildah")
    )


def ginn_changes(
    before: dict[str, tuple[int, int, int]],
    after: dict[str, tuple[int, int, int]],
    sampled: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """The entries that appeared or changed in the window and that this user owns (ginn's uid).

    `sampled` names the owned entries that a StorageWatch listing saw during the window. An entry
    that appeared and went again between the two listings is found only that way. Entries of other
    owners are not ginn's: the system and other users write to /var/tmp too.
    """
    uid = os.getuid()
    changes: dict[str, dict[str, Any]] = {}
    for name, value in after.items():
        if value[0] != uid:
            continue
        if name not in before or before[name] != value:
            changes[name] = {
                "new": name not in before,
                "uid": value[0],
                "size": value[2],
                "named_for_ginn": named_for_ginn(name),
                "sampled_only": False,
            }
    for name in sampled:
        if name not in before and name not in changes:
            changes[name] = {
                "new": True,
                "uid": uid,
                "size": None,
                "named_for_ginn": named_for_ginn(name),
                "sampled_only": True,
            }
    return changes


def image_copy_dir(paths: OwnedPaths) -> str:
    """The image copy directory that podman reports under the product environment (`store`)."""
    info = json.loads(product_podman(paths, ["info", "--format", "json"]))
    return str(info.get("store", {}).get("imageCopyTmpDir", ""))


MIN_SAMPLES = 3
"""The fewest counted storage samples a peak may rest on. The watch samples every second, so a build
that yields fewer than three has no measured peak, and its zero must not pass (item 2)."""
MAX_DROP_FRACTION = 0.10
"""The largest share of storage samples a watch may drop after its retry (item 5). The services-on
create runs its rm, rmi and rm within about 1.1 s, so a sample that lands on them can fail twice.
The peak rests on the others, and the sample after the return (`final_ok`) is checked on its own."""


def assert_sampled(
    label: str,
    samples: int,
    elapsed_s: float,
    errors: Sequence[str],
    dropped: int,
    final_ok: bool,
) -> None:
    """Fail when the watch's peak cannot be trusted (items 2 and 5).

    The peak rests on at least MIN_SAMPLES counted samples, and on the sample taken after the build
    returned (`final_ok`), which holds the storage the build left. A missing final sample fails the
    leg outright. Otherwise the watch may drop at most MAX_DROP_FRACTION of its attempts. A zero
    peak from no samples is not a measurement, so it must not pass.
    """
    attempts = samples + dropped
    assert samples >= MIN_SAMPLES, (
        f"{label}: {samples} storage samples in {elapsed_s:.1f} s, below {MIN_SAMPLES}; "
        f"errors: {list(errors)[:3]}"
    )
    assert final_ok, (
        f"{label}: the storage sample after the build returned was not counted; "
        f"errors: {list(errors)[:3]}"
    )
    assert dropped <= MAX_DROP_FRACTION * attempts, (
        f"{label}: {dropped} of {attempts} storage samples dropped after a retry, over "
        f"{MAX_DROP_FRACTION:.0%}; errors: {list(errors)[:3]}"
    )


APPARENT_EVERY = 5
"""Apparent bytes are counted on every fifth allocated sample. Each count is a second `du` pass over
the same tree, so counting them on every sample would halve the allocated sampling rate."""


class StorageWatch:
    """Samples the podman storage size, the image tmpdir and the owned entries of /var/tmp every
    `interval` seconds (one second) of a build, and at both of its ends.

    The first sample is the baseline, taken in `__enter__` before the build. The last is taken in
    `__exit__`, after the command has returned, so the storage the build leaves is counted. A timer
    sample alone can miss the growth of a create's last seconds (run 8 of T7.27: debian services-on
    at 3 s, 86 MB short of the end state that T7.35 measured).

    The size is counted inside `podman unshare` with `du -x` (du_bytes), so the keep-id layers are
    included and a running container's merged mount is not. Allocated bytes are the figure the peak
    is taken from, and they are counted on every sample. The peak is the largest allocated sample
    less the allocated size before the build (`peak`, asserted). Apparent bytes are counted on every
    `APPARENT_EVERY`th allocated sample (`peak_apparent`) and not asserted. A count is retried once
    inside du_bytes. A count that still fails is recorded in `errors` and counted in `dropped`, and
    it adds to no list. `final_ok` says whether the sample after the return was
    counted. `var_known` holds the /var/tmp names there were before the build. Each other entry that
    this user owns and that a sample sees goes into `var_tmp_seen` (read only, nothing is removed).
    """

    def __init__(
        self,
        paths: OwnedPaths,
        interval: float = 1.0,
        var_known: frozenset[str] = frozenset(),
    ) -> None:
        self.paths = paths
        self.interval = interval
        self.baseline = 0
        self.apparent_baseline = 0
        self.sizes: list[int] = []
        self.apparent_sizes: list[int] = []
        self.errors: list[str] = []
        self.dropped = 0
        self.final_ok = False
        self.image_tmp_seen: set[str] = set()
        self.var_known = var_known
        self.var_tmp_seen: set[str] = set()
        self.var_listings = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> StorageWatch:
        allocated = self._count(DU_ALLOCATED, "allocated")
        apparent = self._count(DU_APPARENT, "apparent")
        if allocated is None or apparent is None:
            raise ValueError(f"the storage baseline could not be counted: {self.errors[-2:]}")
        # The baseline is the first sample, so a build that ends at once still has one.
        self.baseline, self.apparent_baseline = allocated, apparent
        self.sizes.append(allocated)
        self.apparent_sizes.append(apparent)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=300)
        # The sample after the command returned: it holds the storage the build left.
        self.final_ok = self._sample()

    def _run(self) -> None:
        # Event.wait returns True once __exit__ has set the flag, so the thread ends with no sample
        # of its own. __exit__ takes the last one, after the join.
        while not self._stop.wait(self.interval):
            self._sample()

    def _count(self, flags: Sequence[str], name: str) -> int | None:
        """One `du` count of the podman root, or None when it failed. `du_bytes` retries it once
        (DU_RETRIES), so None means the count failed twice. The failure is recorded in `errors`.
        """
        try:
            return du_bytes(self.paths, self.paths.podman_root, flags)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.errors.append(f"{name} dropped: {type(exc).__name__}: {exc}"[:200])
            return None

    def _sample(self) -> bool:
        """One sample: the allocated count (the asserted figure, taken first), the apparent count on
        every `APPARENT_EVERY`th allocated sample, and the listings. Return whether the allocated
        count was counted.
        """
        size = self._count(DU_ALLOCATED, "allocated")
        if size is None:
            self.dropped += 1
        else:
            self.sizes.append(size)
            if len(self.sizes) % APPARENT_EVERY == 0:
                apparent = self._count(DU_APPARENT, "apparent")
                if apparent is not None:
                    self.apparent_sizes.append(apparent)
        with contextlib.suppress(OSError):
            self.image_tmp_seen.update(os.listdir(self.paths.image_tmp))
        with contextlib.suppress(OSError):
            listed = tmp_entries(VAR_TMP)
            self.var_listings += 1
            uid = os.getuid()
            self.var_tmp_seen.update(
                name
                for name, value in listed.items()
                if value[0] == uid and name not in self.var_known
            )
        return size is not None

    def peak(self) -> int:
        """The largest allocated growth over the baseline. Callers assert `assert_sampled` first."""
        return max(self.sizes, default=self.baseline) - self.baseline

    def peak_apparent(self) -> int:
        """The largest apparent growth over its baseline. Recorded, not asserted."""
        return max(self.apparent_sizes, default=self.apparent_baseline) - self.apparent_baseline


def realm_upper(paths: OwnedPaths, name: str) -> dict[str, Any]:
    """The realm container's writable layer: podman's SizeRw and the du of its upper directory."""
    cid = realm_cid(paths, name)
    upper = product_podman(
        paths, ["container", "inspect", "--format", "{{.GraphDriver.Data.UpperDir}}", cid]
    ).strip()
    size_text = product_podman(
        paths, ["container", "inspect", "--size", "--format", "{{.SizeRw}}", cid]
    ).strip()
    return {
        "upper": upper,
        "size_rw": int(size_text) if size_text.isdigit() else None,
        # Apparent bytes, as before: SizeRw and writable_layer_bytes are apparent figures too.
        "upper_bytes": du_bytes(paths, upper, DU_APPARENT) if upper else None,
    }


@dataclass
class Measure:
    """One services-off create from no base, with its storage watch and its progress objects, from
    which `space_checked` reads the product's own figure."""

    name: str
    elapsed_s: float
    progress: list[dict[str, Any]]
    peak: int
    peak_apparent: int
    samples: int
    dropped: int
    final_ok: bool
    watch_errors: list[str]


@dataclass
class Build:
    """One services-on create of a distribution, with its storage and its tmpdir measured, and the
    services-off create from no base that ran before it (`off`, item 5). When that services-off
    create failed, `off` is None and `off_failure` names the failure."""

    distro: str
    name: str
    rc: int
    elapsed_s: float
    base_bytes: int
    progress: list[dict[str, Any]]
    baseline: int
    peak: int
    peak_apparent: int
    samples: int
    dropped: int
    final_ok: bool
    watch_errors: list[str]
    image_tmp: str
    image_tmp_seen: list[str]
    image_copy_dir: str
    var_changes: dict[str, dict[str, Any]]
    var_other_changes: int
    var_listings: int
    steps: list[str]
    disk: dict[str, Any]
    off: Measure | None
    off_failure: str | None


@pytest.fixture(scope="module")
def gate_dir() -> Iterator[Path]:
    """A 0700 directory for the step logs of this gate. It is removed at the module's end."""
    base = Path(tempfile.mkdtemp(prefix="ginn-t727-steps-", dir="/tmp"))  # noqa: S108 - mkdtemp, 0700
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def delete_failure(launcher: Path, name: str) -> str:
    """Delete a t727 realm with its Realm Home, as `ginn --json delete --purge-home --yes`.

    Return "" when the delete returned 0. Otherwise return its rc and the tail of its stderr (or of
    its stdout when stderr is empty). A refused delete keeps the realm record and its pinned base,
    so the caller must fail on it (run 8: GINN-E-HOST-UNACKED, with the leg reporting no failure).
    """
    env = {**os.environ, "PATH": f"{launcher.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(launcher), "--json", "delete", name, "--purge-home", "--yes"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd="/tmp",  # noqa: S108 - a working directory, not a temp file
        timeout=900.0,
        check=False,
    )
    if done.returncode == 0:
        return ""
    output = done.stderr.strip() or done.stdout.strip()
    return f"delete of {name} failed (rc {done.returncode}): {output[-400:]}"


def services_off_leg(launcher: Path, paths: OwnedPaths, gate_dir: Path, distro: str) -> Measure:
    """The services-off create of `distro` from no base, measured (item 5, services off).

    `build` runs this before its services-on create, so that create also starts from no base. The
    realm is deleted here, and a delete removes the pinned base when no realm uses it. The delete's
    rc is checked: a refused delete fails this leg, with its stderr, whether the create succeeded or
    not (defect 2 of run 8). `build` then checks that the base is gone, because a base that stays
    would make the services-on create measure a create over a base in storage. The progress objects
    are kept, and the item 5 test reads the figure of this create's `space-checked` step
    (`space_checked`).
    """
    name = f"{PREFIX}{distro}-off"
    _CREATED.append(name)
    steps_file = gate_dir / f"{distro}-off-steps.txt"
    began = time.monotonic()
    measured: Measure | None = None
    error: Exception | None = None
    try:
        # Sampled every second: a services-off create can be as short as about 20 s (T7.33).
        with (
            StorageWatch(paths) as watch,
            mock.patch.dict(os.environ, {STEP_LOG_ENV: str(steps_file)}),
        ):
            created, progress = ginn_steps(
                launcher,
                "create",
                distro,
                "--name",
                name,
                "--services",
                "off",
                timeout=CREATE_TIMEOUT,
            )
        elapsed = time.monotonic() - began
        assert created.rc == 0, created.error_text()
        measured = Measure(
            name=name,
            elapsed_s=round(elapsed, 1),
            progress=progress,
            peak=watch.peak(),
            peak_apparent=watch.peak_apparent(),
            samples=len(watch.sizes),
            dropped=watch.dropped,
            final_ok=watch.final_ok,
            watch_errors=watch.errors[:5],
        )
    except Exception as exc:  # noqa: BLE001 - re-raised below, after the delete has been checked
        error = exc
    finally:
        run_ginn(launcher, "stop", name)
        refused = delete_failure(launcher, name)
    if refused:
        detail = f" (after {type(error).__name__}: {error})" if error is not None else ""
        raise AssertionError(f"{refused}{detail}")
    if error is not None:
        raise error
    assert measured is not None
    return measured


@pytest.fixture(scope="module", params=DISTROS)
def build(
    request: pytest.FixtureRequest, launcher: Path, paths: OwnedPaths, gate_dir: Path
) -> Iterator[Build]:
    """A services-off, then a services-on create of one distribution, both from no base (a real
    pull and a real commit each time).

    The services-off create runs first (`services_off_leg`), and it is deleted with its base. The
    services-on create runs under StorageWatch and the step log. /var/tmp is listed before and after
    it, and during it by each sample. The services-on realm is deleted at the module's end, with its
    Realm Home. When the pinned base is already in ginn storage, the distribution is skipped: its
    items need a real pull, and the skip names the realm that holds the base when one is known. When
    the services-off leg itself leaves the base, the distribution fails: that base is the gate's own
    leftover (a refused delete, for one), and a skip would hide it.
    """
    distro = str(request.param)
    base_id = pinned_base_id(paths, distro)
    if base_id is not None:
        holder = base_holder(launcher, paths, base_id)
        pytest.skip(
            f"{distro}: the pinned base {base_id} is already in ginn storage, held by realm "
            f"{holder or 'unknown'}. Items 5, 9 and the item 8 shared-base guard need a real pull."
        )
    off: Measure | None = None
    off_failure: str | None = None
    try:
        off = services_off_leg(launcher, paths, gate_dir, distro)
    except Exception as exc:  # noqa: BLE001 - recorded on the Build, and item 5 (off) fails on it
        off_failure = f"{type(exc).__name__}: {exc}"
    left = pinned_base_id(paths, distro)
    if left is not None:
        # The base is the gate's own leftover, so this is a failure. A skip would hide it (run 8).
        cause = off_failure or "the leg's create and delete both reported success"
        pytest.fail(
            f"{distro}: the services-off leg left the pinned base {left} in ginn storage "
            f"({cause}), so the services-on create would not start from no base",
            pytrace=False,
        )
    name = f"{PREFIX}{distro}-prov"
    _CREATED.append(name)
    steps_file = gate_dir / f"{distro}-steps.txt"
    var_before = tmp_entries(VAR_TMP)
    image_copy_before = image_copy_dir(paths)
    began = time.monotonic()
    with (
        StorageWatch(paths, var_known=frozenset(var_before)) as watch,
        mock.patch.dict(os.environ, {STEP_LOG_ENV: str(steps_file)}),
    ):
        created, progress = ginn_steps(
            launcher, "create", distro, "--name", name, "--services", "on", timeout=CREATE_TIMEOUT
        )
    elapsed = time.monotonic() - began
    try:
        var_after = tmp_entries(VAR_TMP)
        assert created.rc == 0, created.error_text()
        disk = disk_use(launcher, paths, name)
        base = disk["base_bytes"]
        assert base is not None, "the pinned base is not found by its io.ginn.base-digest"
        changes = ginn_changes(var_before, var_after, watch.var_tmp_seen)
        others = sum(
            1
            for key, value in var_after.items()
            if value[0] != os.getuid() and var_before.get(key) != value
        )
        steps = steps_file.read_text(encoding="utf-8").split() if steps_file.exists() else []
        yield Build(
            distro=distro,
            name=name,
            rc=created.rc,
            elapsed_s=round(elapsed, 1),
            base_bytes=base,
            progress=progress,
            baseline=watch.baseline,
            peak=watch.peak(),
            peak_apparent=watch.peak_apparent(),
            samples=len(watch.sizes),
            dropped=watch.dropped,
            final_ok=watch.final_ok,
            watch_errors=watch.errors[:5],
            image_tmp=str(paths.image_tmp),
            image_tmp_seen=sorted(watch.image_tmp_seen),
            image_copy_dir=image_copy_before,
            var_changes=changes,
            var_other_changes=others,
            var_listings=watch.var_listings,
            steps=steps,
            disk=disk,
            off=off,
            off_failure=off_failure,
        )
    finally:
        run_ginn(launcher, "stop", name)
        run_ginn(launcher, "delete", name, "--purge-home", "--yes")


@pytest.mark.fr("FR-121")
def test_phasec_dry_podman_env_and_unshare(launcher: Path, paths: OwnedPaths) -> None:
    """Preflight, no realm: the product environment reaches podman, `podman unshare` reads the
    storage, and the image copy directory is the one ginn configured (ADR-015 Decision 11)."""
    assert os.path.isfile(paths.podman_containers_conf), "ginn's containers.conf is missing"
    conf = paths.podman_containers_conf.read_text(encoding="utf-8")
    assert "image_copy_tmp_dir" in conf and str(paths.image_tmp) in conf, conf
    size = du_bytes(paths, paths.podman_root)
    reported = image_copy_dir(paths)
    print(
        f"T727_PHASEC_DRY storage_bytes={size} imageCopyTmpDir={reported!r} tmp={paths.image_tmp}"
    )
    assert reported == str(paths.image_tmp), reported
    assert product_podman(paths, ["unshare", "true"]) == ""
    assert run_ginn(launcher, "list").rc == 0


@pytest.mark.fr("FR-121")
def test_phasec_item9_image_copy_stays_out_of_var_tmp(build: Build) -> None:
    """Item 9: during a real pull and commit, no entry that ginn or podman names lands in /var/tmp.

    The product env sets TMPDIR and containers.conf to paths.image_tmp, and `podman info` reports
    that directory. /var/tmp is listed before and after the create, and by every storage sample
    during it. Only entries owned by this user are compared, since the system and the user's other
    programs write to /var/tmp too. The test fails only on a new owned entry whose name carries a
    podman, buildah or ginn hint. Every other new owned entry is printed as a T727_VARTMP_OTHER line
    for a manual check, and it does not fail. The sampled listing finds an entry that appeared and
    went again between the two listings, but not one that lived less than a sample interval (1 s).
    Nothing is deleted there.
    """
    named = {name: change for name, change in build.var_changes.items() if change["named_for_ginn"]}
    other = {
        name: change for name, change in build.var_changes.items() if not change["named_for_ginn"]
    }
    for name, change in sorted(other.items()):
        print(f"T727_VARTMP_OTHER {build.distro} {name} {json.dumps(change, sort_keys=True)}")
    print(
        f"T727_ITEM9 {build.distro} info_copy_dir={build.image_copy_dir} "
        f"image_tmp={build.image_tmp} seen_in_image_tmp={build.image_tmp_seen} "
        f"named_changes={json.dumps(named, sort_keys=True)} other_changes={len(other)} "
        f"other_owner_changes={build.var_other_changes} var_listings={build.var_listings} "
        f"elapsed_s={build.elapsed_s}"
    )
    assert build.image_copy_dir == build.image_tmp, build.image_copy_dir
    assert named == {}, named


@pytest.mark.fr("FR-121")
def test_phasec_item5_formula_against_measured_peak(build: Build) -> None:
    """Item 5, services on: the product's FR-121 figure for this create, against the measured peak.

    The figure is `required_bytes` of the create's own `space-checked` step (T7.35 contract, read
    by `space_checked`). The gate does not re-derive it. The peak is the largest allocated growth of
    the podman root over its baseline during the create, counted inside `podman unshare` with
    `du -x`. The verdict is PASS when the peak is within the figure. The apparent peak is printed
    beside it and not asserted. Arch is asserted like the others: T7.33 bounds it (ADR-015
    Decision 11).
    """
    required, free = space_checked(f"{build.distro} services-on create", build.progress)
    verdict = "PASS" if build.peak <= required else "FAIL"
    print(
        f"T727_ITEM5 {build.distro} base={build.base_bytes} baseline={build.baseline} "
        f"required={required} free={free} peak={build.peak} "
        f"peak_apparent={build.peak_apparent} samples={build.samples} dropped={build.dropped} "
        f"final_ok={build.final_ok} elapsed_s={build.elapsed_s} "
        f"watch_errors={build.watch_errors[:2]} verdict={verdict}"
    )
    assert_sampled(
        f"{build.distro} item 5",
        build.samples,
        build.elapsed_s,
        build.watch_errors,
        build.dropped,
        build.final_ok,
    )
    assert build.peak <= required, (
        f"{build.distro}: measured allocated peak {build.peak} is over the product's "
        f"required_bytes {required}"
    )


@pytest.mark.fr("FR-121")
def test_phasec_item5_services_off_against_measured_peak(build: Build) -> None:
    """Item 5, services off: a services-off create from no base, against its own `required_bytes`.

    `build` runs `services_off_leg` before the services-on create, and that leg is measured the same
    way: the allocated growth of the podman root with `du -x`, against the figure of its own
    `space-checked` step. T7.35 extends the storage model to the services-off create.
    """
    if build.off is None:
        pytest.fail(
            f"{build.distro}: the services-off leg did not measure: {build.off_failure}",
            pytrace=False,
        )
    off = build.off
    required, free = space_checked(f"{build.distro} services-off create", off.progress)
    verdict = "PASS" if off.peak <= required else "FAIL"
    print(
        f"T727_ITEM5_OFF {build.distro} name={off.name} required={required} free={free} "
        f"peak={off.peak} peak_apparent={off.peak_apparent} samples={off.samples} "
        f"dropped={off.dropped} final_ok={off.final_ok} elapsed_s={off.elapsed_s} "
        f"watch_errors={off.watch_errors[:2]} verdict={verdict}"
    )
    assert_sampled(
        f"{build.distro} item 5 services off",
        off.samples,
        off.elapsed_s,
        off.watch_errors,
        off.dropped,
        off.final_ok,
    )
    assert off.peak <= required, (
        f"{build.distro}: services-off allocated peak {off.peak} is over the product's "
        f"required_bytes {required}"
    )


@pytest.mark.fr("FR-121")
@pytest.mark.fr("NFR-008")
def test_phasec_item11_labels_names_and_deferred_term(build: Build, paths: OwnedPaths) -> None:
    """Item 11, on the final image of each distribution: the realm label is cleared, the stage and
    reset leftovers are gone, and the deferred current disk use term (gN root plus derived image)
    is sampled inside `podman unshare`. The term is recorded, not asserted (ADR-015 Decision 11).
    """
    derived = realm_image(paths, build.name)
    labels = image_labels(paths, derived)
    upper = realm_upper(paths, build.name)
    left = provision_leftovers(paths, build.name)
    deferred = build.disk["derived_bytes"] + (upper["upper_bytes"] or 0)
    print(
        f"T727_ITEM11 {build.distro} labels={json.dumps(labels, sort_keys=True)} "
        f"leftovers={json.dumps(left, sort_keys=True)} upper={upper} "
        f"derived_bytes={build.disk['derived_bytes']} deferred_term_bytes={deferred} "
        f"product_disk={json.dumps(build.disk, sort_keys=True)}"
    )
    assert labels.get(LABEL_PROVISION, "") == "", labels
    assert LABEL_INIT_SET in labels and LABEL_BASE_DIGEST in labels, labels
    assert "io.ginn.realm" not in labels, labels
    final = f"localhost/ginn-init-{build.name}:g1"
    assert left == {"containers": [], "images": [final]}, left


@pytest.mark.fr("FR-121")
def test_phasec_reuse_and_pinned_terms_debian(
    build: Build, launcher: Path, paths: OwnedPaths
) -> None:
    """Item 11 and 5, the other two image sources, on debian (ADR-015 Decision 11, D7).

    Reuse: services off then on, with the derived image kept. The lazy start is a reuse build (no
    provisioning). htop is installed in the services-off container, as S-11 measured the writable
    layer, and the writable layer is compared with the term of the distribution data. Pinned: a
    services-off realm with no derived image, rebuilt while its pinned base is in storage. The
    figure is the rebuild's own `required_bytes` (its `space-checked` step, read by
    `space_checked`), and the peak is the allocated growth of the rebuild. This covers a pin already
    in storage only. A pin rotation pulls and copies the base again; the product models that (T7.35)
    and this gate does not run it, because it needs a trust manifest edit on this host.
    """
    if build.distro != "debian":
        pytest.skip("reuse and pinned are measured on debian only (ADR-015 Decision 11)")
    name = build.name
    assert run_ginn(launcher, "stop", name).rc == 0
    off = run_ginn(launcher, "services", name, "off")
    assert off.rc == 0, off.error_text()
    started = recorded(launcher, "start debian reuse off", "start", name, timeout=CREATE_TIMEOUT)
    assert_clean(started, "start debian reuse off")
    installed = run_ginn(launcher, "install", name, "htop", timeout=CREATE_TIMEOUT)
    assert installed.rc == 0, installed.error_text()
    writable = realm_upper(paths, name)
    definition = load_distro("debian")
    term = definition.writable_layer_bytes
    term_formula = estimate_required(term)
    print(
        f"T727_REUSE debian writable={writable} term={term} formula_term={term_formula} "
        f"verdict={'PASS' if (writable['upper_bytes'] or 0) <= term_formula else 'FAIL'}"
    )
    assert (writable["upper_bytes"] or 0) <= term_formula, writable
    assert run_ginn(launcher, "stop", name).rc == 0
    assert run_ginn(launcher, "services", name, "on").rc == 0
    back = recorded(launcher, "start debian reuse on", "start", name, timeout=CREATE_TIMEOUT)
    assert_clean(back, "start debian reuse on")
    facts = settled_facts(launcher, name)
    print(f"T727_REUSE_BOOT debian {json.dumps(facts, sort_keys=True)}")
    assert facts["pid1"] == "systemd" and facts["failed"] == [], facts
    assert run_ginn(launcher, "stop", name).rc == 0

    pin = f"{PREFIX}debian-pin"
    _CREATED.append(pin)
    created = run_ginn(launcher, "create", "debian", "--name", pin, "--services", "off")
    assert created.rc == 0, created.error_text()
    try:
        base = build.base_bytes
        began = time.monotonic()
        # The pinned rebuild is the shortest build, so its watch samples every second.
        with StorageWatch(paths) as watch:
            rebuilt, rebuild_steps = ginn_steps(launcher, "rebuild", pin, timeout=CREATE_TIMEOUT)
        elapsed = time.monotonic() - began
        assert rebuilt.rc == 0, rebuilt.error_text()
        required, free = space_checked("debian pinned rebuild", rebuild_steps)
        print(
            f"T727_PINNED debian base={base} required={required} free={free} peak={watch.peak()} "
            f"peak_apparent={watch.peak_apparent()} samples={len(watch.sizes)} "
            f"dropped={watch.dropped} final_ok={watch.final_ok} "
            f"elapsed_s={elapsed:.1f} codes={rebuilt.codes()} "
            f"verdict={'PASS' if watch.peak() <= required else 'FAIL'}"
        )
        assert_sampled(
            "pinned rebuild",
            len(watch.sizes),
            elapsed,
            watch.errors,
            watch.dropped,
            watch.final_ok,
        )
        assert watch.peak() <= required, (
            f"pinned rebuild: allocated peak {watch.peak()} is over the product's required_bytes "
            f"{required}"
        )
    finally:
        run_ginn(launcher, "stop", pin)
        run_ginn(launcher, "delete", pin, "--purge-home", "--yes")


@pytest.mark.fr("FR-013")
def test_phasec_item10_arch_clean_empties_the_cache(
    build: Build, launcher: Path, paths: OwnedPaths
) -> None:
    """Item 10: the arch `clean` argv (`find /var/cache/pacman/pkg -mindepth 1 -delete`) empties the
    package cache, keeps the directory and the sync databases, and a later install returns rc 0.

    The clean ran in the provisioning stage, so the derived image holds no cached package. The
    realm is started, the cache and the sync databases are read in it, then htop is installed.
    """
    if build.distro != "arch":
        pytest.skip("the clean verb of the arch data is item 10")
    name = build.name
    started = recorded(launcher, "start arch item10", "start", name, timeout=CREATE_TIMEOUT)
    assert_clean(started, "start arch item10")
    out = exec_ok(
        launcher,
        name,
        "echo CACHE $(find /var/cache/pacman/pkg -mindepth 1 | wc -l); "
        "test -d /var/cache/pacman/pkg && echo DIR yes; "
        "echo SYNC $(ls /var/lib/pacman/sync 2>/dev/null | wc -l)",
    )
    print(f"T727_ITEM10 arch {out.split()}")
    fields = dict(zip(out.split()[0::2], out.split()[1::2], strict=False))
    assert fields.get("CACHE") == "0", out
    assert "DIR" in out and "yes" in out, out
    assert int(fields.get("SYNC", "0")) >= 1, out
    installed = run_ginn(launcher, "install", name, "htop", timeout=CREATE_TIMEOUT)
    assert installed.rc == 0, installed.error_text()
    assert package_installed(launcher, name, "arch", "htop"), "htop is not installed after install"


@pytest.mark.fr("FR-048")
def test_phasec_item14_host_proxy_absent_and_change_commas(
    launcher: Path, paths: OwnedPaths
) -> None:
    """Item 14: the derived image's Env does not hold a host proxy variable that is set in the
    run's environment (ADR-015 Decisions 2b and 2g), and the commas of a `--change` value are
    probed.

    A holder creates the base first, with no proxy in the environment. Then the proxy variables are
    set to a dummy address in the environment of the one create that follows (run_ginn_env). The
    test process's own environment is never changed. Every create runs `podman pull` (the digest
    check of ops/create.py), so NO_PROXY=* sends that pull direct: the dummy is set, and it is never
    used. The comma probe commits a scratch image in ginn's storage with an ENV and a LABEL value
    that hold a comma, and records what podman stores. Everything is removed afterwards.
    """
    hold = f"{PREFIX}debian-proxyhold"
    name = f"{PREFIX}debian-proxy"
    _CREATED.extend([hold, name])
    held = run_ginn(launcher, "create", "debian", "--name", hold, "--services", "off")
    assert held.rc == 0, held.error_text()
    probe = PROBE
    probe_image = PROBE_IMAGE
    try:
        base_id = realm_image(paths, hold)
        created_probe = product_run(paths, ["create", "--name", probe, base_id, "true"])
        assert created_probe.returncode == 0, created_probe.stderr
        committed = product_run(
            paths,
            [
                "commit",
                "--change",
                "ENV T727_PROBE=a,b",
                "--change",
                "LABEL t727.probe=x,y",
                probe,
                probe_image,
            ],
        )
        if committed.returncode == 0:
            probe_env = [
                e for e in image_env(paths, probe_image) if "T727_PROBE" in e or e in ("a", "b")
            ]
            probe_labels = {
                k: v for k, v in image_labels(paths, probe_image).items() if "t727" in k
            }
        else:
            probe_env, probe_labels = [], {"commit_error": committed.stderr.strip()[-200:]}
        print(f"T727_ITEM14_PROBE env={probe_env} labels={probe_labels}")
        created = run_ginn_env(
            launcher,
            PROXY_ENV,
            "create",
            "debian",
            "--name",
            name,
            "--services",
            "on",
            timeout=CREATE_TIMEOUT,
        )
        assert created.rc == 0, created.error_text()
        derived_env = image_env(paths, realm_image(paths, name))
        cid = realm_cid(paths, name)
        container_env = json.loads(
            product_podman(paths, ["container", "inspect", "--format", "{{json .Config.Env}}", cid])
        )
        leaked = [
            e for e in derived_env + container_env if DUMMY_PROXY in e or "proxy" in e.lower()
        ]
        print(f"T727_ITEM14 derived_env={derived_env} container_env_proxy={leaked}")
        assert leaked == [], leaked
    finally:
        product_run(paths, ["rm", "-f", probe])
        product_run(paths, ["rmi", "-f", probe_image])
        run_ginn(launcher, "stop", name)
        run_ginn(launcher, "delete", name, "--purge-home", "--yes")
        run_ginn(launcher, "stop", hold)
        run_ginn(launcher, "delete", hold, "--purge-home", "--yes")


@pytest.mark.fr("FR-121")
def test_phasec_item5_commit_only_with_base_present(launcher: Path, paths: OwnedPaths) -> None:
    """Item 5, the commit alone: a services-on create over a base that is already in storage.

    The base is held by a services-off realm, so this create transfers no base layer. The create
    has its own `space-checked` step, which carries the product's figure for the base-in-storage
    branch of Decision 11 (ops/_create_plan.py `storage_required` with `base_in_storage`). The gate
    reads that figure (`space_checked`) and does not re-derive it. The growth of the create is
    measured with no base transfer in it, which separates the commit from the pull that the
    provision test includes.
    """
    hold = f"{PREFIX}debian-commit-hold"
    name = f"{PREFIX}debian-commit"
    _CREATED.extend([hold, name])
    held = run_ginn(
        launcher, "create", "debian", "--name", hold, "--services", "off", timeout=CREATE_TIMEOUT
    )
    assert held.rc == 0, held.error_text()
    try:
        began = time.monotonic()
        with StorageWatch(paths) as watch:
            created, steps = ginn_steps(
                launcher,
                "create",
                "debian",
                "--name",
                name,
                "--services",
                "on",
                timeout=CREATE_TIMEOUT,
            )
        elapsed = time.monotonic() - began
        assert created.rc == 0, created.error_text()
        required, free = space_checked("debian commit only", steps)
        peak = watch.peak()
        verdict = "PASS" if peak <= required else "FAIL"
        print(
            f"T727_ITEM5_COMMIT debian base_present=True required={required} free={free} "
            f"peak={peak} peak_apparent={watch.peak_apparent()} samples={len(watch.sizes)} "
            f"dropped={watch.dropped} final_ok={watch.final_ok} "
            f"elapsed_s={elapsed:.1f} errors={watch.errors[:2]} verdict={verdict}"
        )
        assert_sampled(
            "commit only",
            len(watch.sizes),
            elapsed,
            watch.errors,
            watch.dropped,
            watch.final_ok,
        )
        assert peak <= required, (
            f"commit-only allocated peak {peak} is over the product's required_bytes {required}"
        )
    finally:
        run_ginn(launcher, "stop", name)
        run_ginn(launcher, "delete", name, "--purge-home", "--yes")
        run_ginn(launcher, "stop", hold)
        run_ginn(launcher, "delete", hold, "--purge-home", "--yes")


def journal_step(paths: OwnedPaths, name: str) -> str | None:
    """The `step` of the realm's journal entry (store/journal.py), or None when there is none."""
    journal = paths.journal / f"{name}.json"
    if not journal.is_file():
        return None
    return str(json.loads(journal.read_text(encoding="utf-8")).get("step"))


def image_present(paths: OwnedPaths, image: str) -> bool:
    """True when podman finds the image by ID or name, in ginn's storage."""
    return product_run(paths, ["image", "exists", image]).returncode == 0


@pytest.fixture(scope="module")
def hold_debian(launcher: Path) -> Iterator[str]:
    """A services-off debian realm that holds the pinned base while the kill tests run, so each
    create after a kill re-provisions from the base and does not pull it again."""
    name = f"{PREFIX}debian-hold"
    _CREATED.append(name)
    created = run_ginn(launcher, "create", "debian", "--name", name, "--services", "off")
    assert created.rc == 0, created.error_text()
    try:
        yield name
    finally:
        run_ginn(launcher, "delete", name, "--purge-home", "--yes")


@pytest.mark.fr("FR-122")
@pytest.mark.fr("NFR-030")
@pytest.mark.parametrize("point", KILL_POINTS)
def test_phased_kill_then_resume_at_each_provisioning_point(
    launcher: Path,
    paths: OwnedPaths,
    gate_dir: Path,
    hold_debian: str,
    point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 8, FR-122 AC-1: SIGKILL the create right after one provisioning step is journaled, then
    `create --resume`. The journal names the step, the leftovers are named, the resume finishes with
    rc 0, the leftovers are gone, and the realm boots with systemd as PID 1.

    The designed stage tag and reset container (Decisions 2f and 2g, item 11) are checked where the
    step means they exist: the stage tag from stage-committed, and the reset container, with its
    label, from reset-created until reset-removed. The reset container is read by its ID from one
    listing, taken before anything else touches the realm. The realm is removed in every case.
    """
    name = f"{PREFIX}debian-kill"
    _CREATED.append(name)
    steps_file = gate_dir / f"kill-{point}.txt"
    monkeypatch.setenv(STEP_LOG_ENV, str(steps_file))
    monkeypatch.setenv(KILL_ENV, point)
    try:
        killed = run_ginn(
            launcher, "create", "debian", "--name", name, "--services", "on", timeout=CREATE_TIMEOUT
        )
    finally:
        monkeypatch.delenv(KILL_ENV, raising=False)
    try:
        assert killed.rc != 0, f"the create was not killed at {point}"
        killed_steps = steps_file.read_text(encoding="utf-8").split() if steps_file.exists() else []
        step = journal_step(paths, name)
        listing = product_podman(
            paths, ["ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}"]
        ).split("\n")
        reset = f"ginn_provision_{name}_g1_reset"
        reset_ids = [line.split()[0] for line in listing if line.split()[1:] == [reset]]
        before = provision_leftovers(paths, name)
        reset_label = None
        if reset_ids:
            reset_label = json.loads(
                product_podman(
                    paths,
                    ["container", "inspect", "--format", "{{json .Config.Labels}}", reset_ids[0]],
                )
            ).get(LABEL_PROVISION)
        state = run_ginn(launcher, "status", name).body.get("state")
        stage_tag = f"localhost/ginn-init-{name}:g1-stage"
        print(
            f"T727_KILL {point} rc={killed.rc} journal_step={step} state={state} "
            f"leftovers={json.dumps(before, sort_keys=True)} reset_label={reset_label}"
        )
        assert step == point, f"the journal reads step {step!r}, not {point!r}"
        # Decision 2g's reset container lives from reset-created until reset-removed, and the stage
        # tag from stage-committed until stage-removed (Decision 13 names the order of removals).
        if point in ("reset-created", "image-committed"):
            assert reset_ids, f"no reset container listed at {point}: {before}"
            assert reset_label == f"{name}:1", reset_label
        if point == "reset-removed":
            assert not reset_ids, f"the reset container is still listed at {point}: {before}"
        if point in ("stage-committed", "reset-created", "image-committed", "reset-removed"):
            assert stage_tag in before["images"], before
        if point in ("stage-removed", "provision-removed"):
            assert stage_tag not in before["images"], before
        if point == "provision-removed":
            assert before["containers"] == [], before
        resumed = run_ginn(launcher, "create", "--resume", name, timeout=CREATE_TIMEOUT)
        assert resumed.rc == 0, resumed.error_text()
        resumed_steps = steps_file.read_text(encoding="utf-8").split()[len(killed_steps) :]
        print(f"T727_STEPS {point} killed={killed_steps} resumed={resumed_steps}")
        after = provision_leftovers(paths, name)
        final = f"localhost/ginn-init-{name}:g1"
        print(f"T727_RESUME {point} rc={resumed.rc} codes={resumed.codes()} after={after}")
        assert after == {"containers": [], "images": [final]}, after
        labels = image_labels(paths, realm_image(paths, name))
        assert labels.get(LABEL_PROVISION, "") == "" and LABEL_BASE_DIGEST in labels, labels
        started = recorded(launcher, f"start {point}", "start", name, timeout=CREATE_TIMEOUT)
        assert_clean(started, f"start {point}")
        facts = settled_facts(launcher, name)
        print(f"T727_BOOT {point} {json.dumps(facts, sort_keys=True)}")
        assert facts["pid1"] == "systemd" and facts["failed"] == [], facts
    finally:
        run_ginn(launcher, "stop", name)
        run_ginn(launcher, "delete", name, "--purge-home", "--yes")
        leftover = provision_leftovers(paths, name)
        print(f"T727_KILL_CLEANUP {point} leftover={leftover}")


def ginn_progress(launcher: Path, *args: str, timeout: float = 900.0) -> tuple[Ran, list[str]]:
    """`ginn --json` as the other helpers run it, and the progress lines it printed, in order.

    Each step of an operation prints one progress object (`on_step`, FR-107). The lines are read in
    the order they were printed, so the step order of a delete can be asserted. `ginn_steps` reads
    them; this returns each one as text.
    """
    ran, steps = ginn_steps(launcher, *args, timeout=timeout)
    return ran, [str(step) for step in steps]


@pytest.mark.fr("FR-019")
@pytest.mark.fr("FR-122")
def test_phased_delete_order_and_shared_base_guard(launcher: Path, paths: OwnedPaths) -> None:
    """Item 8, the delete order and the shared-base guard (ADR-015 Decision 13, Open question 13).

    Two services-on debian realms A and B share one base. B is rebuilt to generation 2, as in the
    T7.30 observation. A is deleted first: its own derived image goes, and the base stays, since
    B's derived image is built on it. B is deleted next: its derived image goes, and then the base.
    The image IDs, digests, parents and labels are recorded before the deletes. The image presence
    checks after each delete are the evidence of the order and of the guard. For delete A only, the
    order of its progress lines is also checked (a progress-step-order check, not an order of
    removals). A note that says a derived image is kept is read as what it names: the guard keeps
    the base, which is the image that `delete` removes last.
    """
    a = f"{PREFIX}debian-a"
    b = f"{PREFIX}debian-b"
    _CREATED.extend([a, b])
    try:
        for realm in (a, b):
            created = run_ginn(
                launcher,
                "create",
                "debian",
                "--name",
                realm,
                "--services",
                "on",
                timeout=CREATE_TIMEOUT,
            )
            assert created.rc == 0, created.error_text()
        rebuilt = run_ginn(launcher, "rebuild", b, timeout=CREATE_TIMEOUT)
        assert rebuilt.rc == 0, rebuilt.error_text()
        a_image = realm_image(paths, a)
        b_image = realm_image(paths, b)
        base = base_id_of(paths, a_image)
        assert base is not None, "the shared base is not found from A's base-digest label"

        def describe(image: str) -> dict[str, Any]:
            data: dict[str, Any] = json.loads(
                product_podman(paths, ["image", "inspect", "--format", "{{json .}}", image])
            )
            labels = data.get("Labels") or {}
            return {
                "id": data.get("Id"),
                "digest": data.get("Digest"),
                "repo_digests": data.get("RepoDigests"),
                "parent": data.get("Parent"),
                "base_digest_label": labels.get(LABEL_BASE_DIGEST),
            }

        before = {
            "a_derived": describe(a_image),
            "b_derived": describe(b_image),
            "base": describe(base),
        }
        print("T727_SHARED_BEFORE " + json.dumps(before, sort_keys=True))
        delete_a, steps_a = ginn_progress(launcher, "delete", a, "--purge-home", "--yes")
        body_a = json.dumps(delete_a.body, sort_keys=True)[:1500]
        print(f"T727_DELETE_A rc={delete_a.rc} steps={steps_a} body={body_a}")
        assert delete_a.rc == 0, delete_a.error_text()
        after_a = {
            "a_derived": image_present(paths, a_image),
            "b_derived": image_present(paths, b_image),
            "base": image_present(paths, base),
        }
        print(f"T727_SHARED_AFTER_A {after_a}")
        assert not after_a["a_derived"], "A's own derived image is kept after its delete"
        assert after_a["b_derived"], "B's derived image went with A's delete"
        assert after_a["base"], "the base went while B's derived image is built on it"
        # A progress-step-order check, for delete A only. The image presence checks above are the
        # evidence that the derived image went and the base stayed.
        init_step_at = next(i for i, step in enumerate(steps_a) if "init-image-removed" in step)
        base_step_at = next(
            i for i, step in enumerate(steps_a) if "image-removed" in step and "init" not in step
        )
        assert init_step_at < base_step_at, f"progress steps out of order: {steps_a}"
        delete_b, steps_b = ginn_progress(launcher, "delete", b, "--purge-home", "--yes")
        body_b = json.dumps(delete_b.body, sort_keys=True)[:1500]
        print(f"T727_DELETE_B rc={delete_b.rc} steps={steps_b} body={body_b}")
        assert delete_b.rc == 0, delete_b.error_text()
        after_b = {
            "b_derived": image_present(paths, b_image),
            "base": image_present(paths, base),
        }
        print(f"T727_SHARED_AFTER_B {after_b}")
        assert not after_b["b_derived"] and not after_b["base"], after_b
    finally:
        for realm in (a, b):
            run_ginn(launcher, "delete", realm, "--purge-home", "--yes")
