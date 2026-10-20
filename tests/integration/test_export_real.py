# SPDX-License-Identifier: Apache-2.0
"""Host gate for the desktop export, the unexport and the bin export (M5), on real rootless Podman.

Run with `GINN_INTEGRATION=1 pytest tests/integration/test_export_real.py -v -s` on a Void host.
This file also holds the harness that test_aur_real.py and test_rebuild_real.py import (`World`,
`world`, `run_ginn`, `STEP_SEAM`, `realm`, ...), the way test_cli_real.py imports from the
lifecycle gate.

Harness. The product CLI runs as a subprocess through a launcher this module writes. The launcher
imports ginn from this checkout, points the helper constants at native/build, hides the user's real
mount roots from every realm (as test_lifecycle_real.py does) and appends STEP_SEAM. The storage is
the conftest `host` base: HOME, XDG_*_HOME, GINN_RUNTIME_DIR and XDG_RUNTIME_DIR point under it, so
nothing here reads or writes ~/.local/share/containers, the user's Owned Paths or /run/user/<uid>.
The session display, agent and bus variables are dropped from the CLI environment, so no realm of
this gate binds a session socket. A create runs the product's integration step (`integrate=True`,
the product path, network needed).

Because each CLI call is a separate process, a test can SIGKILL or interrupt a create or a rebuild
at a chosen journal step (STEP_SEAM) and look at what the next ginn command does.

What this file proves, and what it leaves out:
- FR-044 AC-1 and AC-2, FR-045 AC-1: `ginn install` and `ginn remove` leave the host menu right when
  they return. The native path (`ginn exec --root -- apt-get ...`) is a separate item. It is a
  strict xfail (F-WATCH) while no trigger runs `ginn _sync` after a native transaction (no watcher
  is wired in src/ginn; native/ginn-watch.c is not started by any command).
- NFR-009: 20 timed `ginn _sync` runs, interpreter start included, each on a removed host entry.
  The 5 s bound is read at the sync, since a package transaction itself is not timed by ginn.
- Launch prefix (no FR marker): the host entry runs `<ginn> exec <realm> -- <argv>`, and that
  command runs in the realm.
  The renderer, audio and GPU legs (FR-046 AC-1, AC-2) need a Wayland or X11 session and were proven
  by S-06 and T6.6; they stay a manual walkthrough and are not claimed here.
- FR-047 AC-1: export, run through the launcher, unexport.

Every realm here is named `w1f-...`. The module deletes its realms in a finally block.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import shlex
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ginn.store.paths import OwnedPaths
from ginn.store.records import RealmStore
from tests.integration.conftest import REPO, Host, gate_item, p95

PREFIX = "w1f-"
CREATE_TIMEOUT = 1800.0
ENTRY_WAIT_S = 5.0
"""NFR-009: the host menu follows a package transaction within this many seconds."""
SYNC_RUNS = 20
"""NFR-009 names 20 runs."""
EXPORT_REALM = f"{PREFIX}exp"
EXPORT_REALM_2 = f"{PREFIX}exp2"
DESKTOP_PKG = "xterm"
DESKTOP_APP = "xterm"
NATIVE_PKG = "rxvt-unicode"
BIN_PKG = "ripgrep"
BIN_NAME = "rg"
SESSION_DROP = (
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "SSH_AUTH_SOCK",
    "GPG_AGENT_INFO",
)
"""Variables of the user's session that the gate's CLI does not see."""
KILL_ENV = "GINN_W1F_KILL_AT"
CANCEL_ENV = "GINN_W1F_CANCEL_AT"
STEP_LOG_ENV = "GINN_W1F_STEP_LOG"

STEP_SEAM = r"""
import dataclasses as _dc
import os as _os
import signal as _signal
def _w1f_wrap(orig):
    def build(*a, **k):
        hooks = orig(*a, **k)
        log = _os.environ.get("GINN_W1F_STEP_LOG", "")
        kill = _os.environ.get("GINN_W1F_KILL_AT", "")
        cancel = _os.environ.get("GINN_W1F_CANCEL_AT", "")
        if not (log or kill or cancel):
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
            if cancel and step == cancel:
                _os.kill(_os.getpid(), _signal.SIGINT)
        return _dc.replace(hooks, on_step=on_step)
    return build
_context.create_hooks = _w1f_wrap(_context.create_hooks)
_context.rebuild_hooks = _w1f_wrap(_context.rebuild_hooks)
"""
"""Appended to a launcher before its main guard. It wraps `_context.create_hooks` and
`_context.rebuild_hooks`. With GINN_W1F_KILL_AT=<step> the process is SIGKILLed right after that
journal step is recorded (a crash). With GINN_W1F_CANCEL_AT=<step> it gets SIGINT there (a cancel,
AS-018). GINN_W1F_STEP_LOG=<file> appends each step name. All three unset, it changes nothing."""


@dataclass
class Ran:
    """One finished ginn command: its exit status, its output and every JSON object it printed."""

    rc: int
    out: str
    err: str
    objs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def body(self) -> dict[str, Any]:
        """The last result object (progress, warning and verbose lines skipped)."""
        skip = ("progress", "warning", "verbose")
        return next((o for o in reversed(self.objs) if not any(k in o for k in skip)), {})

    @property
    def codes(self) -> list[str]:
        found: list[str] = []
        for o in self.objs:
            if isinstance(o.get("warning"), dict):
                found.append(str(o["warning"].get("code")))
            found.extend(str(w.get("code")) for w in o.get("warnings", []) if isinstance(w, dict))
        return found

    @property
    def steps(self) -> list[dict[str, Any]]:
        return [o["progress"] for o in self.objs if isinstance(o.get("progress"), dict)]

    def error_text(self) -> str:
        err = self.body.get("error")
        if isinstance(err, dict):
            return f"{err.get('code')}: {err.get('message')}"
        return f"exit {self.rc}: {self.err.strip()[-400:]}"


@dataclass
class Target:
    """A ginn launcher and the environment its commands run in."""

    launcher: Path
    env: dict[str, str]


@dataclass
class World(Target):
    """The launcher on the conftest storage base, with the stores a test reads."""

    host: Host = None  # type: ignore[assignment]

    @property
    def paths(self) -> OwnedPaths:
        return self.host.paths

    @property
    def apps_dir(self) -> Path:
        """The host menu directory, `$XDG_DATA_HOME/applications` (FR-044)."""
        return self.host.base / "data" / "applications"

    def store(self) -> RealmStore:
        return RealmStore(self.host.paths, self.host.env.clock)


def hidden_mount_roots() -> tuple[str, ...]:
    return ("/media", "/mnt", f"/run/media/{getpass.getuser()}")


def run_ginn(
    target: Target,
    *args: str,
    json_mode: bool = True,
    timeout: float = CREATE_TIMEOUT,
    env_extra: Mapping[str, str] | None = None,
) -> Ran:
    """Run one ginn command through the launcher, with no shell. `--json` is the default."""
    argv = [str(target.launcher), *(["--json"] if json_mode else []), *args]
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**target.env, **(env_extra or {})},
        cwd="/tmp",  # noqa: S108 - a working directory, not a temp file
        timeout=timeout,
        check=False,
    )
    objs: list[dict[str, Any]] = []
    for line in (done.stdout + "\n" + done.stderr).splitlines():
        if line.startswith("{"):
            with contextlib.suppress(ValueError):
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    objs.append(parsed)
    return Ran(done.returncode, done.stdout, done.stderr, objs)


def in_realm(target: Target, name: str, script: str, *, root: bool = True) -> Ran:
    """Run `sh -c script` in the realm through `ginn exec` (container root by default)."""
    flags = ["--root"] if root else []
    return run_ginn(target, "exec", *flags, name, "--", "sh", "-c", script, json_mode=False)


def journal_step(paths: OwnedPaths, name: str) -> str | None:
    """The step of the realm's journal entry, or None when there is none (FR-122)."""
    try:
        data = json.loads((paths.journal / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    step = data.get("step") if isinstance(data, dict) else None
    return step if isinstance(step, str) else None


def write_launcher(directory: Path, host: Host) -> Path:
    """Write the gate's `ginn` into `directory` (harness only) and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    src = str(REPO / "src")
    native = str(REPO / "native" / "build")
    text = (
        f"#!{sys.executable}\n"
        "# Host-gate launcher (harness only): ginn from this checkout, helpers from native/build.\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {src!r})\n"
        "import ginn\n"
        f"assert ginn.__file__.startswith({src!r}), ginn.__file__\n"
        "from ginn.__main__ import main\n"
        "from ginn.cli import _context\n"
        "from ginn.runtime import attach\n"
        f"HIDDEN = frozenset({hidden_mount_roots()!r})\n"
        "_real_identity = attach.read_identity\n"
        "def _identity(host, **kw):\n"
        "    return None if host in HIDDEN else _real_identity(host, **kw)\n"
        "attach.read_identity = _identity\n"
        "def _exists(path):\n"
        "    return path not in HIDDEN and os.path.exists(path)\n"
        "_context.seams = lambda: _context.Seams(\n"
        f"    root_dir=Path({str(host.root_dir)!r}),\n"
        f"    helper={native + '/ginn-rebind'!r},\n"
        f"    wait_helper={native + '/ginn-wait'!r},\n"
        "    exists=_exists,\n"
        ")\n" + STEP_SEAM + "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    script = directory / "ginn"
    script.write_text(text, encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture(scope="module")
def world(host: Host) -> World:
    """The product CLI on the conftest storage base."""
    script = write_launcher(host.base / "launcher", host)
    env = {k: v for k, v in os.environ.items() if k not in SESSION_DROP}
    env.update(
        {
            "HOME": str(host.host_home),
            "XDG_DATA_HOME": str(host.base / "data"),
            "XDG_CONFIG_HOME": str(host.base / "config"),
            "XDG_STATE_HOME": str(host.base / "state"),
            "GINN_RUNTIME_DIR": str(host.xdg),
            "XDG_RUNTIME_DIR": str(host.session),
            "PATH": f"{script.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        }
    )
    return World(launcher=script, env=env, host=host)


@contextlib.contextmanager
def realm(
    target: Target, distro: str, name: str, *create_args: str, start: bool = True
) -> Iterator[str]:
    """Create (and start) a realm; delete it with its Realm Home when the block ends."""
    try:
        made = run_ginn(target, "create", distro, "--name", name, *create_args)
        assert made.rc == 0, made.error_text()
        if start:
            started = run_ginn(target, "start", name)
            assert started.rc == 0, started.error_text()
        yield name
    finally:
        run_ginn(target, "delete", name, "--purge-home", "--yes", timeout=600.0)


def read_entry(path: Path) -> dict[str, str]:
    """The keys of the `[Desktop Entry]` group of a host entry (first occurrence of each key)."""
    keys: dict[str, str] = {}
    group = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            group = line.strip()
        elif group == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            keys.setdefault(key.strip(), value.strip())
    return keys


def entry_path(world: World, realm_name: str, app: str) -> Path:
    return world.apps_dir / f"ginn-{realm_name}-{app}.desktop"


def wait_for(check: Any, timeout: float) -> float | None:
    """Poll `check()` every 50 ms. Return the seconds it took to turn true, or None on timeout."""
    began = time.monotonic()
    while time.monotonic() - began <= timeout:
        if check():
            return time.monotonic() - began
        time.sleep(0.05)
    return None


@pytest.fixture(scope="module")
def exp_realm(world: World) -> Iterator[str]:
    """One running Debian realm for the export items. The items install into it as they need."""
    with realm(world, "debian", EXPORT_REALM) as name:
        yield name


def ensure_installed(world: World, name: str, pkg: str, app: str) -> None:
    """Install `pkg` with `ginn install` when its host entry is absent (the items stand alone)."""
    if entry_path(world, name, app).exists():
        return
    ran = run_ginn(world, "install", name, pkg)
    assert ran.rc == 0, ran.error_text()


@pytest.mark.fr("FR-044")
def test_install_exports_entry_and_launch_prefix(world: World, exp_realm: str) -> None:
    """FR-044 AC-1: after `ginn install`, the host entry exists with the realm in its name and the
    icon copied under Owned Paths. Its Exec runs `ginn exec <realm> --` in the
    realm (the launch prefix only; the FR-046 render and audio legs are manual, see the module
    docstring, so no FR-046 marker is claimed)."""
    ran = run_ginn(world, "install", exp_realm, DESKTOP_PKG)
    assert ran.rc == 0, ran.error_text()
    entry = entry_path(world, exp_realm, DESKTOP_APP)
    assert entry.is_file(), (
        f"no host entry {entry}; menu: {sorted(p.name for p in world.apps_dir.glob('*'))}"
    )
    keys = read_entry(entry)
    assert keys["Name"].endswith(f"({exp_realm})"), keys["Name"]
    assert keys["X-Ginn-Realm"] == exp_realm
    icon = keys.get("Icon")
    if icon and icon.startswith("/"):
        assert icon.startswith(str(world.paths.icons)), f"icon outside Owned Paths: {icon}"
        assert Path(icon).is_file(), icon
    argv = shlex.split(keys["Exec"])
    assert "--" in argv, keys["Exec"]
    assert Path(argv[0]).resolve() == world.launcher.resolve(), argv[0]
    assert argv[1:3] == ["exec", exp_realm], argv
    prefix = argv[: argv.index("--") + 1]
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*prefix, "cat", "/etc/os-release"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=world.env,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "Debian" in done.stdout, done.stdout


@pytest.mark.fr("NFR-009")
@pytest.mark.fr("FR-044")
def test_sync_latency_over_20_runs(world: World, exp_realm: str) -> None:
    """NFR-009: with the host entry removed, `ginn _sync` brings it back within 5 s, 20 runs.

    Each run times the whole process, interpreter start included. Every run is written to the
    gate's results file (GINN_T49_RESULTS) as MEASURED."""
    ensure_installed(world, exp_realm, DESKTOP_PKG, DESKTOP_APP)
    entry = entry_path(world, exp_realm, DESKTOP_APP)
    timings: list[float] = []
    facts: dict[str, Any] = {"runs": SYNC_RUNS, "bound_s": ENTRY_WAIT_S}
    with gate_item(world.host.results, "nfr009-sync-20", facts):
        for _ in range(SYNC_RUNS):
            entry.unlink()
            began = time.monotonic()
            ran = run_ginn(world, "_sync", exp_realm, timeout=120.0)
            elapsed = time.monotonic() - began
            assert ran.rc == 0, ran.error_text()
            assert entry.is_file(), "the sync did not restore the removed entry"
            timings.append(elapsed)
        facts.update(p95=round(p95(timings), 3), max=round(max(timings), 3), timings=timings)
        assert max(timings) <= ENTRY_WAIT_S, f"sync took {max(timings):.2f} s: {timings}"


class KnownGapF14(AssertionError):
    """Raised only at the menu-entry assertion of the native items (the known gap F14)."""


XFAIL_WATCH = pytest.mark.xfail(
    strict=True,
    raises=KnownGapF14,
    reason=(
        "F14: no trigger syncs the menu after native package transactions (FR-044/045); "
        "fix in ADR-016"
    ),
)
"""The native-transaction items fail until a later task wires a watcher to `_sync`. strict: an
unexpected pass makes the run fail, so the marker is removed when the product fix lands."""


def native_entries(world: World, name: str) -> list[Path]:
    """The host entries rxvt-unicode would get, named from the package's own desktop files.

    `dpkg -L` lists them in the realm (the package metadata), so no file name is assumed here."""
    ran = in_realm(
        world,
        name,
        f"dpkg -L {NATIVE_PKG} 2>/dev/null | grep '/applications/.*[.]desktop$' || true",
    )
    stems = [Path(line).stem for line in ran.out.split() if line.endswith(".desktop")]
    return [entry_path(world, name, stem) for stem in stems]


@XFAIL_WATCH
@pytest.mark.fr("FR-044")
@pytest.mark.fr("NFR-009")
def test_native_install_reaches_the_menu_within_5s(world: World, exp_realm: str) -> None:
    """FR-044 AC-1, native path: `apt-get install` inside the realm, then the entry within 5 s.

    Nothing here runs `ginn _sync`. If no ginn trigger runs after a native transaction, this fails
    with the entry missing, the known finding (strict xfail, see the module docstring)."""
    ran = in_realm(
        world,
        exp_realm,
        f"apt-get update -qq && apt-get install -y --no-install-recommends {NATIVE_PKG}",
    )
    if ran.rc != 0:
        pytest.fail(f"native install failed: {ran.err[-600:]}")
    expected = native_entries(world, exp_realm)
    if not expected:
        pytest.fail(f"{NATIVE_PKG} ships no desktop file in the realm: pick another NATIVE_PKG")
    began = time.monotonic()
    seen = wait_for(lambda: any(e.is_file() for e in expected), ENTRY_WAIT_S)
    if seen is None:
        raise KnownGapF14(
            f"no host entry within {ENTRY_WAIT_S:.0f} s of the native install "
            f"({time.monotonic() - began:.1f} s waited); menu: "
            f"{sorted(p.name for p in world.apps_dir.glob('*'))}"
        )


@XFAIL_WATCH
@pytest.mark.fr("FR-045")
@pytest.mark.fr("NFR-009")
def test_native_removal_unexports_within_5s(world: World, exp_realm: str) -> None:
    """FR-045 AC-1, native path: after `apt-get remove` the entry and its icon copy are gone within
    5 s, with no `ginn _sync` run by the test. Same gap note as the install item."""
    installed = in_realm(
        world,
        exp_realm,
        f"apt-get update -qq && apt-get install -y --no-install-recommends {NATIVE_PKG}",
    )
    if installed.rc != 0:
        pytest.fail(f"native install failed: {installed.err[-600:]}")
    expected = native_entries(world, exp_realm)
    if not expected:
        pytest.fail(f"{NATIVE_PKG} ships no desktop file in the realm: pick another NATIVE_PKG")
    # Export by hand so the removal is real, whatever the install item left.
    synced = run_ginn(world, "_sync", exp_realm)
    if synced.rc != 0:
        pytest.fail(f"manual sync failed: {synced.error_text()}")
    present = [e for e in expected if e.is_file()]
    if not present:
        pytest.fail("the native package left no exportable entry to remove")
    icons = [k["Icon"] for k in map(read_entry, present) if k.get("Icon", "").startswith("/")]
    ran = in_realm(world, exp_realm, f"apt-get remove -y {NATIVE_PKG}")
    if ran.rc != 0:
        pytest.fail(f"native remove failed: {ran.err[-600:]}")
    gone = wait_for(
        lambda: not any(e.exists() for e in expected) and not any(Path(i).exists() for i in icons),
        ENTRY_WAIT_S,
    )
    if gone is None:
        raise KnownGapF14(f"entry or icon still present {ENTRY_WAIT_S:.0f} s after the removal")


@pytest.mark.fr("FR-045")
def test_remove_unexports_entry_and_icon(world: World, exp_realm: str) -> None:
    """FR-045 AC-1 through `ginn remove`: the entry and its copied icon are gone when it returns."""
    ensure_installed(world, exp_realm, DESKTOP_PKG, DESKTOP_APP)
    entry = entry_path(world, exp_realm, DESKTOP_APP)
    icon = read_entry(entry).get("Icon", "")
    ran = run_ginn(world, "remove", exp_realm, DESKTOP_PKG)
    assert ran.rc == 0, ran.error_text()
    assert wait_for(lambda: not entry.exists(), ENTRY_WAIT_S) is not None, f"{entry} remains"
    if icon.startswith("/"):
        assert not Path(icon).exists(), f"icon copy remains: {icon}"


@pytest.mark.fr("FR-044")
def test_same_app_in_two_realms_gives_two_entries(world: World, exp_realm: str) -> None:
    """FR-044 AC-2: the same application from two realms is two distinct menu entries."""
    ensure_installed(world, exp_realm, DESKTOP_PKG, DESKTOP_APP)
    with realm(world, "debian", EXPORT_REALM_2) as second:
        ran = run_ginn(world, "install", second, DESKTOP_PKG)
        assert ran.rc == 0, ran.error_text()
        first_entry = entry_path(world, exp_realm, DESKTOP_APP)
        second_entry = entry_path(world, second, DESKTOP_APP)
        assert first_entry.is_file() and second_entry.is_file()
        names = {read_entry(first_entry)["Name"], read_entry(second_entry)["Name"]}
        assert len(names) == 2, names
        assert any(n.endswith(f"({exp_realm})") for n in names)
        assert any(n.endswith(f"({second})") for n in names)


@pytest.mark.fr("FR-047")
def test_bin_export_runs_the_binary_in_the_realm(world: World, exp_realm: str) -> None:
    """FR-047 AC-1: `export --bin` writes a launcher, running it on the host runs rg in the realm,
    `unexport --bin` removes it. Nothing is exported by the install alone."""
    ran = run_ginn(world, "install", exp_realm, BIN_PKG)
    assert ran.rc == 0, ran.error_text()
    launcher = world.paths.bin / BIN_NAME
    assert not launcher.exists(), "a binary was exported without `export --bin`"
    exported = run_ginn(world, "export", "--bin", exp_realm, BIN_NAME)
    assert exported.rc == 0, exported.error_text()
    try:
        assert launcher.is_file() and os.access(launcher, os.X_OK)
        env = {**world.env, "PATH": f"{world.paths.bin}{os.pathsep}{world.env['PATH']}"}
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [BIN_NAME, "--version"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=300,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert "ripgrep" in done.stdout, done.stdout
    finally:
        gone = run_ginn(world, "unexport", "--bin", exp_realm, BIN_NAME)
        assert gone.rc == 0, gone.error_text()
    assert not launcher.exists()
