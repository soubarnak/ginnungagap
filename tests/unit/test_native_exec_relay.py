# SPDX-License-Identifier: Apache-2.0
"""native/ginn-exec-relay.c and its frame header: the listener, the wire constants, the exec lock
and the token kill's launch path (ADR-014, FR-049, FR-123, AS-028).

Each test starts the listener under a private runtime root (mode 0700). Requests go to the podman
shim (tests/fakes/podman_bin/podman, which replays fixtures and logs its argv) or to a shell
stand-in that waits for a sentinel file, so a test decides when podman exits. Nothing here runs real
podman, and nothing touches /run/user/<uid> or ~/.local/share/containers. The peer uid and user
namespace checks are not exercised: one test process has one uid and one namespace.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "native"
HEADER = NATIVE / "ginn-exec-frame.h"
SHIM = REPO / "tests" / "fakes" / "podman_bin" / "podman"
WARNINGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2"]
HELPERS = ("ginn-exec-relay", "ginn-exec-worker", "ginn-exec-run", "ginn-exec-kill")
LINE_LIMIT = 200
WAIT_S = 15.0
CID = "a" * 64
REALM = "arch"
STDIO = (0, 1, 2)
_DEFINE = re.compile(r'^#define GINN_EXEC_(\w+) (0x[0-9A-Fa-f]+|\d+|"[^"]*")$', re.MULTILINE)
_GROUPS: list[int] = []
"""The process group of every listener a test started (it starts a session of its own), so a
test that fails part way kills the listener and every worker and stand-in under it."""


def _constants() -> dict[str, int | str]:
    """The #define lines of the frame header, parsed as the client (T7.10) parses them."""
    out: dict[str, int | str] = {}
    for name, raw in _DEFINE.findall(HEADER.read_text(encoding="utf-8")):
        out[name] = raw[1:-1] if raw.startswith('"') else int(raw, 0)
    return out


C = _constants()


def _n(name: str) -> int:
    value = C[name]
    assert isinstance(value, int), name
    return value


REQ, ACK, REJECT, SIG, EXIT = (_n(k) for k in ("REQ", "ACK", "REJECT", "SIG", "EXIT"))
FL_TTY, FL_MUTATING, FL_CAPTURED = (_n(k) for k in ("FL_TTY", "FL_MUTATING", "FL_CAPTURED"))


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    out = tmp_path_factory.mktemp("exec-relay-bin")
    for name in HELPERS:
        run = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [cc, *WARNINGS, "-o", str(out / name), str(NATIVE / f"{name}.c")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, run.stderr
    return out


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    """A 0700 runtime root. The socket path must fit sun_path (108 bytes), so a long basetemp
    falls back to a short directory of the same kind."""
    if len(str(tmp_path / "exec.sock")) < 100:
        tmp_path.chmod(0o700)
        yield tmp_path
        return
    shm = "/dev/shm" if Path("/dev/shm").is_dir() else None  # noqa: S108 - a short socket dir
    short = Path(tempfile.mkdtemp(prefix="ginn-exec-", dir=shm))
    short.chmod(0o700)
    try:
        yield short
    finally:
        shutil.rmtree(short, ignore_errors=True)


@pytest.fixture(autouse=True)
def _nothing_left_running() -> Iterator[None]:
    """Kill every listener group the test started, with the workers and stand-ins in it."""
    yield
    for group in _GROUPS:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)
    _GROUPS.clear()


def _globals(root: Path) -> list[str]:
    return [
        "--remote=false",
        "--root",
        str(root / "root"),
        "--runroot",
        str(root / "runroot"),
        "--tmpdir",
        str(root / "tmp"),
        "--runtime",
        "crun",
    ]


def _env(root: Path) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(root)}


def _start_relay(bin_dir: Path, root: Path, podman: Path) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(bin_dir / "ginn-exec-relay"), str(root), str(podman), *_globals(root)],
        env=_env(root),
        start_new_session=True,
        stderr=subprocess.PIPE,
    )
    _GROUPS.append(proc.pid)
    deadline = time.monotonic() + WAIT_S
    while not (root / "exec.sock").exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            pytest.fail(f"the listener did not bind: {proc.stderr.read() if proc.stderr else ''!r}")
        time.sleep(0.02)
    return proc


def _connect(root: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock.settimeout(WAIT_S)
    sock.connect(str(root / "exec.sock"))
    return sock


def _s16(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _req(
    argv: list[str] | None = None,
    *,
    realm: str = REALM,
    cid: str = CID,
    flags: int = 0,
    pass_env: tuple[str, ...] = (),
    nfds: int | None = None,
) -> bytes:
    """A REQ record in the layout of ADR-014 (Wire protocol). Pass a bad field to build a fault."""
    tail = ["exec", "--workdir", "/", "--", CID, "true"] if argv is None else argv
    binds = [part for entry in pass_env for part in ("--env", entry.split("=", 1)[0])]
    tail = [tail[0], *binds, *tail[1:]]  # the client's bare `--env NAME` for each pass-through name
    count = (5 if flags & FL_CAPTURED else 3) if nfds is None else nfds
    body = bytes([REQ, 1, flags, count]) + _s16(realm) + cid.encode("ascii")
    body += struct.pack(">H", len(pass_env)) + b"".join(_s16(p) for p in pass_env)
    body += struct.pack(">H", len(tail)) + b"".join(_s16(a) for a in tail)
    return body


def _shim_env(fixtures: Path, log: Path) -> tuple[str, ...]:
    """Pass-through values for the podman shim: its replay directory and its call log."""
    return (f"GINN_FAKE_PODMAN_FIXTURES={fixtures}", f"GINN_FAKE_PODMAN_LOG={log}")


def _fixtures(tmp: Path, doc: dict[str, object]) -> Path:
    """A shim replay directory whose `exec` case is `doc` (an envelope with an exit status)."""
    fixtures = tmp / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "exec.json").write_text(json.dumps(doc), encoding="utf-8")
    return fixtures


def _sentinel_podman(tmp: Path, *, wait_for: Path | None = None, status: int = 0) -> Path:
    """A shell stand-in for podman: it writes its pid and argv, waits for `wait_for` when given,
    and exits with `status`. The pid is the child the worker forks, so ACK carries it."""
    script = tmp / "podman-sh"
    lines = [
        "#!/bin/sh",
        f"echo $$ > '{tmp / 'pid'}'",
        f"printf '%s\\n' \"$@\" > '{tmp / 'argv'}'",
    ]
    if wait_for is not None:
        lines.append(f"while [ ! -e '{wait_for}' ]; do sleep 0.05; done")
    lines.append(f"exit {status}")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _pid_of(tmp: Path) -> int:
    """The pid the stand-in wrote. The worker sends ACK before the shell has started, so wait."""
    pidfile = tmp / "pid"
    deadline = time.monotonic() + WAIT_S
    while not (pidfile.exists() and pidfile.read_text(encoding="utf-8").endswith("\n")):
        assert time.monotonic() < deadline, "the stand-in never wrote its pid"
        time.sleep(0.02)
    return int(pidfile.read_text(encoding="utf-8"))


def _next(sock: socket.socket) -> bytes:
    data = sock.recv(65536)
    assert data, "the worker closed the connection before it sent a frame"
    return data


def _open_request(root: Path, body: bytes) -> socket.socket:
    sock = _connect(root)
    socket.send_fds(sock, [body], list(STDIO))
    return sock


def _run(root: Path, body: bytes) -> list[bytes]:
    """Send one request and collect frames up to REJECT or EXIT."""
    sock = _open_request(root, body)
    frames: list[bytes] = []
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                return frames
            frames.append(data)
            if data[0] in (REJECT, EXIT):
                return frames
    finally:
        sock.close()


def _logged_argv(log: Path) -> list[list[str]]:
    return [json.loads(line)["argv"] for line in log.read_text(encoding="utf-8").splitlines()]


def _lock_busy(path: Path) -> bool:
    """The check of ADR-014 (CLI check): a shared, non-blocking flock. EWOULDBLOCK means held."""
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _wait_free(path: Path) -> None:
    deadline = time.monotonic() + WAIT_S
    while _lock_busy(path):
        assert time.monotonic() < deadline, "the exec lock was still held after EXIT"
        time.sleep(0.02)


@pytest.mark.fr("FR-049")
def test_the_frame_constants_are_the_adr_014_values() -> None:
    """The client reads these #define lines, so their values are pinned here."""
    assert (REQ, ACK, REJECT, SIG, EXIT) == (1, 2, 3, 4, 5)
    assert (FL_TTY, C["FL_INTERACTIVE"], FL_MUTATING, FL_CAPTURED) == (1, 2, 4, 8)
    limits = (_n("CID_LEN"), _n("MAX_RECORD"), _n("MAX_ITEMS"), _n("MAX_VALUE"))
    assert limits == (64, 65536, 256, 4096)
    assert (_n("CLIENT_FD"), _n("REJ_REJECTED"), _n("REJ_LOCKED"), _n("REJ_BUSY")) == (3, 1, 2, 3)
    assert C["E_REJECTED"] == "GINN-E-RELAY-REJECTED"
    assert C["E_BUSY"] == "GINN-E-RELAY-BUSY"
    assert C["W_ORPHAN"] == "GINN-W-RELAY-ORPHAN"


@pytest.mark.fr("FR-049")
def test_the_relay_with_the_header_and_the_kill_helper_alone_stay_within_200_lines() -> None:
    """The lines target counts the shared header toward each helper that includes it (ADR-014).

    The kill helper does not include the header, so it is counted alone. It spells out the
    warning define itself."""
    header = len(HEADER.read_text(encoding="utf-8").splitlines())
    relay = len((NATIVE / "ginn-exec-relay.c").read_text(encoding="utf-8").splitlines())
    assert relay + header <= LINE_LIMIT, (
        f"ginn-exec-relay.c with the header: {relay + header} lines"
    )
    kill_source = (NATIVE / "ginn-exec-kill.c").read_text(encoding="utf-8")
    assert '#include "ginn-exec-frame.h"' not in kill_source
    kill = len(kill_source.splitlines())
    assert kill <= LINE_LIMIT, f"ginn-exec-kill.c has {kill} lines"
    match = re.search(r'^#define GINN_EXEC_W_ORPHAN "([^"]*)"$', kill_source, re.MULTILINE)
    assert match is not None and match.group(1) == C["W_ORPHAN"], "kill's warning text drifted"


@pytest.mark.fr("FR-049")
def test_make_lines_passes_for_every_helper() -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is needed for the lines target")
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [make, "-C", str(NATIVE), "lines"], capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.fr("FR-049")
def test_the_listener_relays_a_request_and_the_exit_status_comes_back(
    built: Path, root: Path, tmp_path: Path
) -> None:
    log = tmp_path / "podman.log"
    fixtures = _fixtures(tmp_path, {"exit": 7})
    relay = _start_relay(built, root, SHIM)
    assert stat.S_IMODE((root / "exec.sock").stat().st_mode) == 0o600
    frames = _run(root, _req(pass_env=_shim_env(fixtures, log)))
    assert len(frames[0]) == 5 and frames[0][0] == ACK
    assert int.from_bytes(frames[0][1:], "big") > 0
    assert frames[-1] == bytes([EXIT, 0, 7])
    assert relay.poll() is None


@pytest.mark.fr("FR-049")
def test_only_a_mutating_request_gets_a_token_before_the_first_dashdash(
    built: Path, root: Path, tmp_path: Path
) -> None:
    log = tmp_path / "podman.log"
    fixtures = _fixtures(tmp_path, {"exit": 0})
    _start_relay(built, root, SHIM)
    _run(root, _req(flags=FL_MUTATING, pass_env=_shim_env(fixtures, log)))
    argv = _logged_argv(log)[-1]
    dashdash = argv.index("--")
    assert argv[dashdash - 2] == "--env"
    assert re.fullmatch(r"GINN_EXEC_TOKEN=[0-9a-f]{32}", argv[dashdash - 1])
    _run(root, _req(pass_env=_shim_env(fixtures, log)))
    assert not any("GINN_EXEC_TOKEN" in arg for arg in _logged_argv(log)[-1])


@pytest.mark.fr("FR-123")
def test_a_held_exec_lock_refuses_a_mutating_request_and_a_free_one_runs(
    built: Path, root: Path, tmp_path: Path
) -> None:
    lock = root / "exec" / f"{REALM}.lock"
    log = tmp_path / "podman.log"
    fixtures = _fixtures(tmp_path, {"exit": 0})
    _start_relay(built, root, SHIM)
    lock.parent.mkdir(mode=0o700, exist_ok=True)
    holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        refused = _run(root, _req(flags=FL_MUTATING, pass_env=_shim_env(fixtures, log)))
        assert refused[0][:2] == bytes([REJECT, 2]), refused
        assert refused[0][4:].startswith(b"GINN-E-REALM-BUSY"), refused
    finally:
        os.close(holder)
    later = _run(root, _req(flags=FL_MUTATING, pass_env=_shim_env(fixtures, log)))
    assert later[-1] == bytes([EXIT, 0, 0])
    _wait_free(lock)


@pytest.mark.fr("FR-123")
def test_the_lock_is_held_while_podman_runs_and_free_after_it_exits(
    built: Path, root: Path, tmp_path: Path
) -> None:
    gate = tmp_path / "go"
    lock = root / "exec" / f"{REALM}.lock"
    _start_relay(built, root, _sentinel_podman(tmp_path, wait_for=gate))
    sock = _open_request(root, _req(flags=FL_MUTATING))
    assert _next(sock)[0] == ACK
    assert _lock_busy(lock), "the exec lock is free while podman runs"
    second = _run(root, _req(flags=FL_MUTATING))
    assert second[0][:2] == bytes([REJECT, 2]), second
    gate.touch()
    assert _next(sock) == bytes([EXIT, 0, 0])
    sock.close()
    _wait_free(lock)


@pytest.mark.fr("AS-028")
def test_a_worker_outlives_the_listener_that_started_it(
    built: Path, root: Path, tmp_path: Path
) -> None:
    gate = tmp_path / "go"
    relay = _start_relay(built, root, _sentinel_podman(tmp_path, wait_for=gate))
    sock = _open_request(root, _req())
    assert _next(sock)[0] == ACK
    podman_pid = _pid_of(tmp_path)
    relay.send_signal(signal.SIGKILL)
    relay.wait(timeout=WAIT_S)
    with pytest.raises(ConnectionRefusedError):
        _connect(root)
    os.kill(podman_pid, 0)
    gate.touch()
    assert _next(sock) == bytes([EXIT, 0, 0])
    sock.close()


@pytest.mark.fr("FR-049")
def test_the_listener_refuses_a_33rd_worker_with_reject_busy(
    built: Path, root: Path, tmp_path: Path
) -> None:
    gate = tmp_path / "go"
    _start_relay(built, root, _sentinel_podman(tmp_path, wait_for=gate))
    held = [_open_request(root, _req()) for _ in range(32)]
    for sock in held:
        assert _next(sock)[0] == ACK
    overflow = _run(root, _req())
    assert overflow[0][:2] == bytes([REJECT, 3])
    assert overflow[0][4:].startswith(b"GINN-E-RELAY-BUSY")
    gate.touch()
    for sock in held:
        assert _next(sock) == bytes([EXIT, 0, 0])
        sock.close()


@pytest.mark.fr("DR-010")
def test_the_listener_refuses_a_runtime_root_that_is_not_private(built: Path, root: Path) -> None:
    root.chmod(0o755)
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(built / "ginn-exec-relay"), str(root), str(SHIM), *_globals(root)],
        env=_env(root),
        capture_output=True,
        text=True,
        check=False,
        timeout=WAIT_S,
    )
    assert run.returncode == 3, run.stderr
    assert "0700" in run.stderr
    assert not (root / "exec.sock").exists()
