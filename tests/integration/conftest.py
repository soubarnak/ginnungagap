# SPDX-License-Identifier: Apache-2.0
"""Host gate fixtures for tests/integration (T4.9): real rootless Podman in storage the test owns.

Every test here is skipped unless GINN_INTEGRATION=1 (AGENTS.md). A `host_musl` test is skipped
unless the host is musl. Nothing here names the user's own storage. The temp base holds the graph
root, the runroot, the podman tmpdir, XDG_RUNTIME_DIR, the Owned Paths, the Realm Homes and the
host home stand-in. The session removes all of it. Every podman call goes through `PodmanCli`,
which passes --root, --runroot and --tmpdir from that base (AGENTS.md, ADR-004).

The base lives under /tmp, not under the repository or the scratchpad, because conmon creates unix
sockets under the runroot and a socket path is limited to 108 bytes.

Each gate item writes one JSON line to the file named by GINN_T49_RESULTS, when it is set. The
verdict is PASS, FAIL or NOT RUN. The markdown result is written from those lines.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import math
import os
import shutil
import signal
import subprocess  # noqa: S404 - the host gate runs fixed argv lists only, never a shell
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ginn.errors import GinnError
from ginn.ops._mutating import OpEnv
from ginn.podman.cli import PodmanCli
from ginn.store.paths import OwnedPaths
from ginn.trust.manifest import pull_reference

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "native"
GATE_SWITCH = "GINN_INTEGRATION"
RESULTS_ENV = "GINN_T49_RESULTS"
DISTRO = "debian"
ROOT_USER = "0:0"
PAUSE_COMM = "catatonit"
TOOLS = ("fusermount3", "nsenter", "umount", "rm", "true")


def run_tool(argv: Sequence[str], *, check: bool = True, timeout: float = 300) -> Any:
    """Run a fixed argv with no shell (NFR-SEC-005). Output is captured as text."""
    return subprocess.run(  # noqa: S603 - argv list built here, no shell
        list(argv),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=check,
        timeout=timeout,
    )


def tool(name: str) -> str:
    """The absolute path of a host tool, or a failure: the gate needs it."""
    found = shutil.which(name)
    if found is None:
        pytest.fail(f"the host gate needs {name} on PATH")
    return found


def is_musl_host() -> bool:
    """True when the dynamic loader of musl sits in a system library directory."""
    libs = ("/lib", "/usr/lib")
    return any(next(Path(d).glob("ld-musl-*.so.1"), None) is not None for d in libs)


def p95(values: Sequence[float]) -> float:
    """The nearest-rank 95th percentile (the method S-02 used)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


class Results:
    """Appends one JSON line per gate item. Nothing is written when GINN_T49_RESULTS is unset."""

    def __init__(self, path: Path | None) -> None:
        self._path = path

    def record(self, item: str, verdict: str, **data: Any) -> None:
        if self._path is None:
            return
        line = {"item": item, "verdict": verdict, "at": round(time.time(), 3), **data}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True, default=str) + "\n")


@contextlib.contextmanager
def gate_item(results: Results, item: str, facts: dict[str, Any] | None = None) -> Iterator[None]:
    """Record PASS with `facts` when the block finishes, or FAIL with the error, and re-raise it."""
    data = {} if facts is None else facts
    try:
        yield
    except BaseException as exc:
        results.record(item, "FAIL", error=f"{type(exc).__name__}: {exc}"[:800], **data)
        raise
    results.record(item, "PASS", **data)


@dataclass
class Host:
    """The storage and the tools one session uses. Every path is under `base`."""

    base: Path
    uid: int
    gid: int
    user: str
    xdg: Path
    session: Path
    paths: OwnedPaths
    cli: PodmanCli
    env: OpEnv
    helper: str
    host_home: Path
    mount_root: Path
    root_dir: Path
    image: str
    results: Results
    fuse: list[Path] = field(default_factory=list)
    pause_mounts: list[Path] = field(default_factory=list)

    def session_env(self) -> dict[str, str]:
        """The host session of the gate: an empty runtime dir, never the user's /run/user/<uid>."""
        return {"XDG_RUNTIME_DIR": str(self.session)}

    def realm_home(self, name: str) -> Path:
        return self.paths.homes / name

    @staticmethod
    def pause_comm(pid: int) -> str | None:
        """The process name of `pid`, or None when it is gone."""
        try:
            return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def pause_pid(self) -> int | None:
        """The rootless pause process of this store, or None. It must still be catatonit.

        The pid comes from the store's pause.pid file, and the process name is checked too, so a
        recycled pid is never signalled (the caller checks again right before a signal).
        """
        pid_file = self.xdg / "libpod" / "tmp" / "pause.pid"
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return None
        return pid if comm == PAUSE_COMM else None


def _build_host(base: Path, helper: str, results: Results) -> Host:
    uid, gid = os.getuid(), os.getgid()
    xdg = base / "x"
    session = base / "session"
    host_home = base / "home" / "hosthome"
    mount_root = base / "media" / "user"
    root_dir = base / "root"
    for path in (xdg, session, base / "g", base / "r", base / "t", host_home, mount_root, root_dir):
        path.mkdir(parents=True, exist_ok=True)
    xdg.chmod(0o700)
    session.chmod(0o700)
    paths = OwnedPaths.from_env(
        {
            "HOME": str(host_home),
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_STATE_HOME": str(base / "state"),
            "GINN_RUNTIME_DIR": str(xdg),
        },
        uid=uid,
    )
    # `from_paths` passes --cdi-spec-dir on every call (S-06), as the product adapter does.
    # So `start` resolves the NVIDIA spec that `create` writes there.
    cli = PodmanCli.from_paths(paths, binary=tool("podman"), oci_runtime="crun")
    env = OpEnv.for_paths(paths, getpass.getuser(), root_dir=root_dir, clock=time.time)
    image = pull_reference(DISTRO)
    return Host(
        base=base,
        uid=uid,
        gid=gid,
        user=getpass.getuser(),
        xdg=xdg,
        session=session,
        paths=paths,
        cli=cli,
        env=env,
        helper=helper,
        host_home=host_home,
        mount_root=mount_root,
        root_dir=root_dir,
        image=image,
        results=results,
    )


def _teardown(host: Host | None, base: Path, tools: dict[str, str]) -> list[str]:
    """Remove what this session made. Returns the problems, so the session can report them.

    `tools` was resolved before teardown started, so no lookup can fail inside the cleanup. Every
    external call has a timeout. The store's runtime dir (`host.xdg`) is removed last: it holds
    the pause pid file, which is read again after the namespace removal and checked by name just
    before the signal. A pid that no longer names catatonit is never signalled.
    """
    problems: list[str] = []
    if host is None:
        shutil.rmtree(base, ignore_errors=True)
        return problems
    errors = (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError)
    for mount in host.fuse:
        with contextlib.suppress(*errors):
            run_tool([tools["fusermount3"], "-u", str(mount)], check=False, timeout=60)
    with contextlib.suppress(GinnError, OSError, subprocess.TimeoutExpired):
        for info in host.cli.ps():
            with contextlib.suppress(GinnError):
                host.cli.rm(info.id, force=True)
    pause = host.pause_pid()
    if pause is not None:
        # Mounts this session made in the store namespace (nsenter -t <pause>), while it lives.
        for mount in host.pause_mounts:
            with contextlib.suppress(*errors):
                run_tool(
                    [
                        tools["nsenter"], "-t", str(pause), "-U", "-m", "--",
                        tools["umount"], str(mount),
                    ],
                    check=False,
                    timeout=60,
                )
    # Start the store's pause process if none runs, so its pid file exists before the removal.
    try:
        host.cli.unshare([tools["true"]])
    except (GinnError, subprocess.TimeoutExpired, OSError) as exc:
        problems.append(f"podman unshare (pause start) failed: {exc}")
    # The subordinate-uid files need the store's namespace to remove (AGENTS.md, ADR-004). The
    # runtime dir is left for last, so the pause pid file survives this call.
    others = [str(entry) for entry in base.iterdir() if entry != host.xdg]
    if others:
        try:
            host.cli.unshare([tools["rm"], "-rf", "--one-file-system", *others])
        except (GinnError, subprocess.TimeoutExpired, OSError) as exc:
            problems.append(f"podman unshare rm failed: {exc}")
    pause = host.pause_pid()  # read again after the removal, and checked by name below
    if pause is not None:
        with contextlib.suppress(OSError):
            os.kill(pause, signal.SIGTERM)
        time.sleep(1)
        if host.pause_comm(pause) == PAUSE_COMM:
            # Still the store's pause process after SIGTERM: the name was checked just now.
            with contextlib.suppress(OSError):
                os.kill(pause, signal.SIGKILL)
            time.sleep(1)
        alive = host.pause_comm(pause) == PAUSE_COMM
        host.results.record("teardown-pause", "MEASURED", pid=pause, alive_after_kill=alive)
        if alive:
            problems.append(f"pause process {pause} survived SIGTERM and SIGKILL")
    else:
        host.results.record("teardown-pause", "MEASURED", pid=None, alive_after_kill=False)
    shutil.rmtree(base, ignore_errors=True)
    if base.exists():
        problems.append(f"temp base still exists: {base}")
    return problems


def _resolve_tools() -> dict[str, str]:
    """The absolute paths of the cleanup tools. Called before the session body, not in cleanup."""
    return {name: tool(name) for name in TOOLS}


@pytest.fixture(scope="session")
def ginn_rebind_helper() -> str:
    """Build native/ginn-rebind with `make` (native/Makefile) and return its absolute path."""
    run_tool([tool("make"), "-C", str(NATIVE)])
    path = NATIVE / "build" / "ginn-rebind"
    if not os.access(path, os.X_OK):
        pytest.fail(f"make did not build {path}")
    return str(path)


@pytest.fixture(scope="session")
def host(ginn_rebind_helper: str) -> Iterator[Host]:
    """One storage base for the whole gate. The session removes it at the end."""
    tools = _resolve_tools()  # before the base exists, so a missing tool leaks nothing
    base = Path(tempfile.mkdtemp(prefix="ginn-it-", dir="/tmp"))  # noqa: S108 - mkdtemp, mode 0700
    base.chmod(0o700)
    results_path = os.environ.get(RESULTS_ENV)
    results = Results(Path(results_path) if results_path else None)
    built: Host | None = None
    try:
        built = _build_host(base, ginn_rebind_helper, results)
        yield built
    finally:
        problems = _teardown(built, base, tools)
        for problem in problems:
            results.record("teardown", "FAIL", error=problem)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the gate unless GINN_INTEGRATION=1, and skip host_musl tests off a musl host."""
    gate_on = os.environ.get(GATE_SWITCH) == "1"
    musl = is_musl_host()
    for item in items:
        if "tests/integration/" not in str(item.path).replace(os.sep, "/"):
            continue
        if not gate_on:
            item.add_marker(pytest.mark.skip(reason=f"set {GATE_SWITCH}=1 for the host gate"))
        elif item.get_closest_marker("host_musl") is not None and not musl:
            item.add_marker(pytest.mark.skip(reason="no musl host (S-03 has not run here)"))
