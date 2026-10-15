# SPDX-License-Identifier: Apache-2.0
"""native/ginn-exec-worker.c: the checks of one request, and the exec of ginn-exec-run (ADR-014).

The worker takes the client socket on fd 3, as ginn-exec-relay starts it, so these tests need no
listener and no socket path. It execs ginn-exec-run from its own directory, so the fixture builds
that helper too, and the chain runs for every request that passes. Podman is a shell stand-in or the
podman shim (tests/fakes/podman_bin/podman, which replays a fixture and logs its argv). Nothing here
runs real podman, and nothing touches /run/user/<uid> or ~/.local/share/containers. The lock, the
signal, the EOF and the token kill are in test_native_exec_run.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "native"
HEADER = NATIVE / "ginn-exec-frame.h"
SHIM = REPO / "tests" / "fakes" / "podman_bin" / "podman"
WARNINGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2"]
LINE_LIMIT = 200
WAIT_S = 15.0
CID = "b" * 64
REALM = "tst-exec"
TOKEN_ENV = "GINN_EXEC_TOKEN"  # noqa: S105 - an environment name, not a secret
STDIO = (0, 1, 2)
REQ, ACK, REJECT, EXIT = 1, 2, 3, 5
MUTATING = 4
_RUNNING: list[subprocess.Popen[bytes]] = []
"""Every worker and stand-in a test started, so a failing test leaves none running."""


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        pytest.skip("no C compiler on PATH")
    out = tmp_path_factory.mktemp("exec-worker-bin")
    for name in ("ginn-exec-worker", "ginn-exec-run", "ginn-exec-kill"):
        run = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [cc, *WARNINGS, "-o", str(out / name), str(NATIVE / f"{name}.c")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, run.stderr
    return out


@pytest.fixture(autouse=True)
def _nothing_left_running() -> Iterator[None]:
    yield
    while _RUNNING:
        proc = _RUNNING.pop()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=WAIT_S)


def _globals(root: Path) -> list[str]:
    return [
        "--remote=false",
        "--root",
        str(root / "root"),
        "--runroot",
        str(root / "rr"),
        "--tmpdir",
        str(root / "t"),
    ]


def _ginn_env(root: Path) -> dict[str, str]:
    """The ginn-owned keys that the run script gives the worker, so the worker can pass them on."""
    return {
        "PATH": os.environ["PATH"],
        "XDG_RUNTIME_DIR": str(root),
        "CONTAINERS_CONF": str(root / "containers.conf"),
        "CONTAINERS_STORAGE_CONF": str(root / "storage.conf"),
        "TMPDIR": str(root / "t"),
    }


def _s16(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _req(
    argv: Sequence[str] | None = None,
    *,
    realm: str = REALM,
    cid: str = CID,
    flags: int = 0,
    pass_env: Sequence[str] = (),
    nfds: int | None = None,
    bind: bool = True,
) -> bytes:
    """A REQ record in the layout of ADR-014. The default argv is a valid exec of `true`. With
    `bind`, the client's bare `--env NAME` for each pass-through name is added, as it sends them."""
    tail = ["exec", "--workdir", "/", "--", CID, "true"] if argv is None else list(argv)
    if bind:
        binds = [part for entry in pass_env for part in ("--env", entry.split("=", 1)[0])]
        tail = [tail[0], *binds, *tail[1:]]
    count = (5 if flags & 8 else 3) if nfds is None else nfds
    body = bytes([REQ, 1, flags, count]) + _s16(realm) + cid.encode("ascii")
    body += struct.pack(">H", len(pass_env)) + b"".join(_s16(p) for p in pass_env)
    body += struct.pack(">H", len(tail)) + b"".join(_s16(a) for a in tail)
    return body


def _shim_env(fixtures: Path, log: Path) -> list[str]:
    """Pass-through values for the podman shim: its replay directory and its call log."""
    return [f"GINN_FAKE_PODMAN_FIXTURES={fixtures}", f"GINN_FAKE_PODMAN_LOG={log}"]


def _fixtures(tmp: Path, doc: dict[str, object]) -> Path:
    fixtures = tmp / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "exec.json").write_text(json.dumps(doc), encoding="utf-8")
    return fixtures


def _stand_in(tmp: Path, *, body: str = "", status: int = 0) -> Path:
    """A shell stand-in for podman: it writes its pid and argv, runs `body`, exits with `status`."""
    script = tmp / "podman-sh"
    lines = [
        "#!/bin/sh",
        f"echo $$ > '{tmp / 'pid'}'",
        f"printf '%s\\n' \"$@\" > '{tmp / 'argv'}'",
        body,
    ]
    lines.append(f"exit {status}")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _worker(
    bin_dir: Path, root: Path, podman: Path, child: socket.socket, env: dict[str, str]
) -> subprocess.Popen[bytes]:
    """Start the worker with `child` on fd 3, the descriptor that ginn-exec-relay gives it."""
    fd = child.fileno()
    # preexec_fn runs before close_fds, which would close the fd 3 it made. So close_fds is off.
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(bin_dir / "ginn-exec-worker"), str(root), str(podman), *_globals(root)],
        env=env,
        close_fds=False,
        preexec_fn=lambda: os.dup2(fd, 3, inheritable=True),  # noqa: PLW1509 - single-threaded test
        stderr=subprocess.PIPE,
    )
    _RUNNING.append(proc)
    return proc


def _pair() -> tuple[socket.socket, socket.socket]:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.settimeout(WAIT_S)
    return parent, child


def _finish(proc: subprocess.Popen[bytes]) -> tuple[int, str]:
    try:
        _out, err = proc.communicate(timeout=WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=WAIT_S)
        pytest.fail("the worker did not exit on its own")
    return proc.returncode, err.decode("utf-8")


def _recv(sock: socket.socket) -> bytes:
    data = sock.recv(65536)
    assert data, "the worker closed the client before it sent a frame"
    return data


def _pid_of(tmp: Path) -> int:
    """The pid the stand-in wrote. ACK goes out before the shell starts, so wait for the file."""
    pidfile = tmp / "pid"
    deadline = time.monotonic() + WAIT_S
    while not (pidfile.exists() and pidfile.read_text(encoding="utf-8").endswith("\n")):
        assert time.monotonic() < deadline, "the stand-in never wrote its pid"
        time.sleep(0.02)
    return int(pidfile.read_text(encoding="utf-8"))


def _malformed(shim: list[str]) -> dict[str, bytes]:
    """Each fault of the allowlist (ADR-014, Validation). The shim's pass-through is in every case,
    so a request that wrongly passed would run the shim and write the log."""
    good = ["exec", "--workdir", "/", "--", CID, "true"]
    ok = _req(good, pass_env=shim)
    return {
        "version-2": bytes([REQ, 2]) + ok[2:],
        "type-not-req": bytes([9]) + ok[1:],
        "unknown-flag": _req(good, flags=0x10, pass_env=shim),
        "nfds-without-captured": _req(good, nfds=5, pass_env=shim),
        "realm-uppercase": _req(good, realm="ARCH", pass_env=shim),
        "realm-path": _req(good, realm="../x", pass_env=shim),
        "cid-not-hex": _req(good, cid="G" * 64, pass_env=shim),
        "pass-without-equals": _req(good, pass_env=[*shim, "NOEQUALS"]),
        "pass-token": _req(good, pass_env=[*shim, f"{TOKEN_ENV}=x"]),
        "pass-value-oversized": _req(good, pass_env=[*shim, "BIG=" + "x" * 4097]),
        "argv0-not-exec": _req(["run", *good[1:]], pass_env=shim),
        "global-flag-before-exec": _req(["--root", "/x", *good], pass_env=shim),
        "privileged": _req(["exec", "--privileged", "--", CID, "true"], pass_env=shim),
        "bad-env-name": _req(["exec", "--env", "1X=v", "--", CID, "true"], pass_env=shim),
        "client-token": _req(
            ["exec", "--env", f"{TOKEN_ENV}=abc", "--", CID, "true"], pass_env=shim
        ),
        "user-not-digits": _req(["exec", "--user", "root", "--", CID, "true"], pass_env=shim),
        "flag-after-dashdash": _req(["exec", "--", "--privileged", "true"], pass_env=shim),
        "cid-mismatch": _req(["exec", "--", "c" * 64, "true"], pass_env=shim),
        "record-oversized": ok + b"\x00" * 70000,
        "missing-descriptors": _req(good, nfds=3, pass_env=shim),
        "six-descriptors": ok,
        "pass-unbound": _req(
            [
                "exec",
                "--env",
                "GINN_FAKE_PODMAN_FIXTURES",
                "--env",
                "GINN_FAKE_PODMAN_LOG",
                "--workdir",
                "/",
                "--",
                CID,
                "true",
            ],
            pass_env=[*shim, "SECRET=x"],
            bind=False,
        ),
        "pass-ld-preload": _req(
            ["exec", "--env", "LD_PRELOAD", "--workdir", "/", "--", CID, "true"],
            pass_env=["LD_PRELOAD=/x.so"],
            bind=False,
        ),
        "pass-containers-conf-override": _req(
            ["exec", "--env", "CONTAINERS_CONF_OVERRIDE", "--workdir", "/", "--", CID, "true"],
            pass_env=["CONTAINERS_CONF_OVERRIDE=/x"],
            bind=False,
        ),
        "pass-container-host": _req(
            ["exec", "--env", "CONTAINER_HOST", "--workdir", "/", "--", CID, "true"],
            pass_env=["CONTAINER_HOST=unix:///x"],
            bind=False,
        ),
        "pass-podman-prefix": _req(
            ["exec", "--env", "PODMAN_REMOTE", "--workdir", "/", "--", CID, "true"],
            pass_env=["PODMAN_REMOTE=1"],
            bind=False,
        ),
        "pass-underscore-containers": _req(
            ["exec", "--env", "_CONTAINERS_STATE", "--workdir", "/", "--", CID, "true"],
            pass_env=["_CONTAINERS_STATE=1"],
            bind=False,
        ),
        "pass-storage-prefix": _req(good, pass_env=[*shim, "STORAGE_DRIVER=vfs"]),
        "pass-xdg-prefix": _req(good, pass_env=[*shim, "XDG_DATA_HOME=/x"]),
        "pass-buildah-prefix": _req(good, pass_env=[*shim, "BUILDAH_FORMAT=oci"]),
        "pass-godebug": _req(good, pass_env=[*shim, "GODEBUG=x=1"]),
        "pass-gomemlimit": _req(good, pass_env=[*shim, "GOMEMLIMIT=1GiB"]),
        "pass-registry-prefix": _req(good, pass_env=[*shim, "REGISTRY_AUTH_FILE=/x"]),
        "bind-after-container-id": _req(
            [
                "exec",
                "--env",
                "GINN_FAKE_PODMAN_FIXTURES",
                "--env",
                "GINN_FAKE_PODMAN_LOG",
                "--workdir",
                "/",
                "--",
                CID,
                "env",
                "--env",
                "SECRET",
            ],
            pass_env=[*shim, "SECRET=x"],
            bind=False,
        ),
        "tty-flag-without-option": _req(good, flags=0x01, pass_env=shim),
        "tty-option-without-flag": _req(
            ["exec", "--tty", "--workdir", "/", "--", CID, "true"], pass_env=shim
        ),
        "interactive-flag-without-option": _req(good, flags=0x02, pass_env=shim),
    }


@pytest.mark.fr("NFR-SEC-005")
@pytest.mark.parametrize(
    "case",
    [
        "version-2",
        "type-not-req",
        "unknown-flag",
        "nfds-without-captured",
        "realm-uppercase",
        "realm-path",
        "cid-not-hex",
        "pass-without-equals",
        "pass-token",
        "pass-value-oversized",
        "argv0-not-exec",
        "global-flag-before-exec",
        "privileged",
        "bad-env-name",
        "client-token",
        "user-not-digits",
        "flag-after-dashdash",
        "cid-mismatch",
        "record-oversized",
        "missing-descriptors",
        "six-descriptors",
        "pass-unbound",
        "pass-ld-preload",
        "pass-containers-conf-override",
        "pass-container-host",
        "pass-podman-prefix",
        "pass-underscore-containers",
        "tty-flag-without-option",
        "tty-option-without-flag",
        "interactive-flag-without-option",
        "pass-storage-prefix",
        "pass-xdg-prefix",
        "pass-buildah-prefix",
        "pass-godebug",
        "pass-gomemlimit",
        "pass-registry-prefix",
        "bind-after-container-id",
    ],
)
def test_a_malformed_request_gets_reject_and_podman_never_runs(
    built: Path, tmp_path: Path, case: str
) -> None:
    log = tmp_path / "podman.log"
    fixtures = _fixtures(tmp_path, {"exit": 0})
    body = _malformed(_shim_env(fixtures, log))[case]
    parent, child = _pair()
    proc = _worker(built, tmp_path, SHIM, child, _ginn_env(tmp_path))
    child.close()
    if case == "missing-descriptors":
        parent.send(body)
    elif (
        case == "six-descriptors"
    ):  # the kernel delivers six: the worker keeps five and rejects (ADR-014)
        socket.send_fds(parent, [body], [*STDIO, *STDIO])
    else:
        socket.send_fds(parent, [body], list(STDIO))
    frame = _recv(parent)
    assert frame[:2] == bytes([REJECT, 1]), frame
    assert frame[4:].startswith(b"GINN-E-RELAY-REJECTED"), frame
    code, err = _finish(proc)
    assert code == 0, err
    assert not log.exists(), "podman ran for a rejected request"


@pytest.mark.fr("FR-049")
def test_a_valid_request_is_exec_d_through_run_and_its_exit_status_comes_back(
    built: Path, tmp_path: Path
) -> None:
    podman = _stand_in(tmp_path, status=3)
    parent, child = _pair()
    proc = _worker(built, tmp_path, podman, child, _ginn_env(tmp_path))
    child.close()
    socket.send_fds(parent, [_req()], list(STDIO))
    ack = _recv(parent)
    assert ack[0] == ACK
    assert int.from_bytes(ack[1:5], "big") == _pid_of(tmp_path)
    assert _recv(parent) == bytes([EXIT, 0, 3])
    assert _finish(proc)[0] == 0
    argv = (tmp_path / "argv").read_text(encoding="utf-8").splitlines()
    assert not any(TOKEN_ENV in arg for arg in argv), "a non-mutating request must carry no token"


@pytest.mark.fr("FR-049")
def test_a_mutating_request_puts_the_token_before_the_first_dashdash(
    built: Path, tmp_path: Path
) -> None:
    podman = _stand_in(tmp_path)
    parent, child = _pair()
    proc = _worker(built, tmp_path, podman, child, _ginn_env(tmp_path))
    child.close()
    socket.send_fds(parent, [_req(flags=MUTATING)], list(STDIO))
    assert _recv(parent)[0] == ACK
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert _finish(proc)[0] == 0
    argv = (tmp_path / "argv").read_text(encoding="utf-8").splitlines()
    dashdash = argv.index("--")
    assert argv[dashdash - 2] == "--env"
    assert re.fullmatch(rf"{TOKEN_ENV}=[0-9a-f]{{32}}", argv[dashdash - 1]), argv


@pytest.mark.fr("NFR-SEC-005")
def test_podman_gets_the_ginn_keys_and_the_pass_through_values_and_no_secret_on_argv(
    built: Path, tmp_path: Path
) -> None:
    secret = "s3cr3t-value-7f"  # noqa: S105 - a test value that must not reach argv
    env_dump = tmp_path / "env"
    podman = _stand_in(tmp_path, body=f"env > '{env_dump}'")
    env = dict(_ginn_env(tmp_path), HOME="/home/not-for-podman", LANG="C.UTF-8")
    parent, child = _pair()
    proc = _worker(built, tmp_path, podman, child, env)
    child.close()
    socket.send_fds(parent, [_req(pass_env=[f"SECRET={secret}", "TMPDIR=/evil"])], list(STDIO))
    assert _recv(parent)[0] == ACK
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert _finish(proc)[0] == 0
    seen = dict(line.split("=", 1) for line in env_dump.read_text(encoding="utf-8").splitlines())
    assert seen["SECRET"] == secret
    assert seen["TMPDIR"] == str(tmp_path / "t"), "a ginn-owned key overrides the pass-through"
    assert "HOME" not in seen and "LANG" not in seen
    assert {"PATH", "XDG_RUNTIME_DIR", "CONTAINERS_CONF", "CONTAINERS_STORAGE_CONF"} <= set(seen)
    assert secret not in (tmp_path / "argv").read_text(encoding="utf-8")


@pytest.mark.fr("FR-049")
def test_the_worker_with_the_header_stays_within_200_lines() -> None:
    """The lines target counts the shared header toward each relay helper (ADR-014)."""
    header = len(HEADER.read_text(encoding="utf-8").splitlines())
    own = len((NATIVE / "ginn-exec-worker.c").read_text(encoding="utf-8").splitlines())
    total = own + header
    assert total <= LINE_LIMIT, f"ginn-exec-worker.c with the header has {total} lines"


@pytest.mark.fr("NFR-SEC-005")
def test_a_name_that_only_shares_a_refused_prefix_or_stem_reaches_podman(
    built: Path, tmp_path: Path
) -> None:
    """GOOGLE_* and GOGCX are not refused; only the exact Go names and listed prefixes are."""
    env_dump = tmp_path / "env"
    podman = _stand_in(tmp_path, body=f"env > '{env_dump}'")
    parent, child = _pair()
    proc = _worker(built, tmp_path, podman, child, _ginn_env(tmp_path))
    child.close()
    socket.send_fds(parent, [_req(pass_env=["GOOGLE_API_KEY=k1", "GOGCX=2"])], list(STDIO))
    assert _recv(parent)[0] == ACK
    assert _recv(parent) == bytes([EXIT, 0, 0])
    assert _finish(proc)[0] == 0
    seen = dict(line.split("=", 1) for line in env_dump.read_text(encoding="utf-8").splitlines())
    assert seen["GOOGLE_API_KEY"] == "k1" and seen["GOGCX"] == "2"
