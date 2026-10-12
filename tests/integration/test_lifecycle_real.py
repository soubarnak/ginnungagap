# SPDX-License-Identifier: Apache-2.0
"""Host gate for the realm lifecycle and the grant attachment (T4.9), on real rootless Podman.

Run with `GINN_INTEGRATION=1 pytest tests/integration -q` on a Void host. The gate items and their
verdicts are in spikes/results/T4.9-lifecycle-real.md. Each item writes one JSON line to the file
named by GINN_T49_RESULTS (see conftest.py).

Gate 3 (no package install, no refresh): the gate realm is created with
`CreateHooks(integrate=False)`.
Its realm user step (`useradd`) is skipped there and measured on its own by test_d, because that
step fails on this podman (keep-id writes the passwd entry itself).

`admit` is "admit everything" in the attach calls of this file. The DR-004 rules are unit-tested in
tests/unit/test_runtime_attach.py. The sources here sit under the temp base, and a mount root needs
an allowed root that a test cannot create without root.
"""

from __future__ import annotations

import contextlib
import dataclasses
import getpass
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ginn.domain import DistroId, RealmName, RealmState
from ginn.domain.grants import GrantRecord
from ginn.domain.paths import Mode
from ginn.errors import GinnError
from ginn.ops import create as create_mod
from ginn.ops.create import USERADD_FLAGS, CreateHooks, CreateRequest, create_realm
from ginn.ops.delete import delete_realm
from ginn.ops.lifecycle import HostUser, LifecycleHooks, start_realm, stop_realm
from ginn.podman.adapter import CreateSpec
from ginn.podman.cli import PodmanCli
from ginn.runtime import attach as attach_mod
from ginn.runtime.attach import AttachResult, attach_at_start, needs_reattach, reattach
from ginn.runtime.enter import EnterHooks, enter_realm
from ginn.runtime.env import SessionEnv
from ginn.runtime.spec import create_spec, realm_spec
from ginn.store.records import RealmRecord, RealmStore
from tests.integration.conftest import (
    DISTRO,
    ROOT_USER,
    Host,
    gate_item,
    p95,
    run_tool,
    tool,
)

GATE = "gate"
BOX = "attbox"
RUN_DIR = "/run/user/{uid}"


@dataclass
class Gate:
    name: str
    record: RealmRecord
    elapsed: float
    image_present_before: bool
    pulled: bool
    removed_image: bool


HIDDEN_MOUNT_ROOTS = frozenset({"/media", "/mnt", f"/run/media/{getpass.getuser()}"})
"""The user's real mount roots. The gate's exists probe reads them as absent, so no realm of the
gate binds them read-write (decision recorded in the T4.9 result file, section 6)."""


def _gate_exists(path: str) -> bool:
    """The `exists` probe of the gate's hooks: the user's real mount roots read as absent."""
    return path not in HIDDEN_MOUNT_ROOTS and os.path.exists(path)


@pytest.fixture(scope="module", autouse=True)
def _hide_real_mount_roots() -> Iterator[None]:
    """Keep the user's real mount roots out of every gate realm.

    `attach` reads source identities with os.stat (not through a hook), so `exists` alone does not
    stop a skipped mount root from being attached. Its identity is read as None here, which is how
    a missing source reads. The unit tests patch the same function for the same reason.
    """
    real = attach_mod.read_identity

    def identity(host: str, *, mountinfo: Path = attach_mod.MOUNTINFO) -> Any:
        if host in HIDDEN_MOUNT_ROOTS:
            return None
        return real(host, mountinfo=mountinfo)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(attach_mod, "read_identity", identity)
        yield


def _hidden_mounts(host: Host, cid: str) -> list[str]:
    """The hidden mount roots that the realm has mounted now (empty when the probe held)."""
    text = sh(host, cid, "cat /proc/self/mountinfo").stdout.splitlines()
    fields = [line.split() for line in text]
    return sorted(f[4] for f in fields if len(f) > 4 and f[4] in HIDDEN_MOUNT_ROOTS)


@dataclass
class Box:
    name: str
    cid: str


class _PasswdTrue:
    """A PodmanCli that sends `passwd=True` to `create`, so no `--passwd=false` reaches podman.

    `podman create` on podman 5.8.8 has no `--passwd` flag (only `podman run` has one). test_d
    shows that, so this wrapper builds the realm with the same argv otherwise.
    """

    def __init__(self, inner: PodmanCli) -> None:
        self._inner = inner

    def create(self, spec: CreateSpec) -> str:
        return self._inner.create(dataclasses.replace(spec, passwd=True))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _skip_user_step(self: create_mod._Create) -> None:
    """Gate 3 seam: the useradd step is measured by test_d on its own. This skips it."""
    self._step("user")


def _admit_all(grant: GrantRecord) -> None:
    """The `admit` of the attach calls in this file. None means admitted (see the module doc)."""
    return None


def _proc(name: str) -> str:
    return Path(f"/proc/{name}").read_text(encoding="utf-8").strip()


def _loadavg() -> list[str]:
    return Path("/proc/loadavg").read_text(encoding="utf-8").split()[:3]


def sh(host: Host, cid: str, script: str, *args: str, user: str = ROOT_USER) -> Any:
    """Run `script` in the box with /bin/sh. Paths go in `args` ($1, $2, ...), not in the script."""
    return host.cli.exec(cid, ["/bin/sh", "-c", script, "sh", *args], user=user)


def grant(path: Path, realm: str, *, mode: Mode = "rw", is_dir: bool = False) -> GrantRecord:
    return GrantRecord(kind="share", host=str(path), realm=realm, mode=mode, is_dir=is_dir)


def attach(host: Host, box: Box, grants: list[GrantRecord]) -> AttachResult:
    return attach_at_start(
        host.cli,
        box.cid,
        pid=host.cli.inspect(box.cid).pid,
        grants=grants,
        session=SessionEnv(env={}, binds=[]),
        realm_home=host.realm_home(box.name),
        host_home=host.host_home,
        admit=_admit_all,
        helper=host.helper,
    )


def lazy(host: Host, box: Box, records: list[GrantRecord]) -> tuple[bool, AttachResult | None]:
    """The ADR-008 pre-check, then the re-attach when it asks for one. Returns (asked, result)."""
    session = SessionEnv(env={}, binds=[])
    if not needs_reattach(records, session, admit=_admit_all):
        return False, None
    return True, reattach(
        host.cli,
        box.cid,
        pid=host.cli.inspect(box.cid).pid,
        grants=records,
        session=session,
        realm_home=host.realm_home(box.name),
        host_home=host.host_home,
        admit=_admit_all,
        helper=host.helper,
    )


def reattach_outcome(host: Host, box: Box, records: list[GrantRecord]) -> dict[str, Any]:
    """Run the re-attach and report what happened, without raising a GinnError."""
    try:
        result = reattach(
            host.cli,
            box.cid,
            pid=host.cli.inspect(box.cid).pid,
            grants=records,
            session=SessionEnv(env={}, binds=[]),
            realm_home=host.realm_home(box.name),
            host_home=host.host_home,
            admit=_admit_all,
            helper=host.helper,
        )
    except GinnError as err:
        return {"raised": err.code, "detail": str(err.details)[:400]}
    return {"raised": None, "records": len(result.records)}


def holder(host: Host, box: Box, script: str, *args: str) -> str:
    """Start `script` in the background inside the box and return its pid. It keeps its fds."""
    return sh(host, box.cid, f"{script} >/dev/null 2>&1 & echo $!", *args).stdout.strip()


def unholder(host: Host, box: Box, pid: str) -> None:
    sh(host, box.cid, 'kill "$1" 2>/dev/null; true', pid)


def mount_options(host: Host, box: Box, realm_path: str) -> str:
    """The per-mount options of `realm_path` in the box, or '' when it is not a mount point."""
    for line in sh(host, box.cid, "cat /proc/self/mountinfo").stdout.splitlines():
        fields = line.split()
        if len(fields) > 5 and fields[4] == realm_path:
            return fields[5]
    return ""


def pause_pid(host: Host) -> int:
    """The rootless pause process. `podman unshare` starts it when it is not running."""
    host.cli.unshare([tool("true")])
    pid = host.pause_pid()
    if pid is None:
        pytest.fail("no rootless pause process after podman unshare")
    return pid


def pause_mount(host: Host, target: Path) -> None:
    """A tmpfs mounted inside the store's namespace (nsenter -t <pause>), as the owner uid may."""
    pid = pause_pid(host)
    run_tool(
        [tool("nsenter"), "-t", str(pid), "-U", "-m", "--", tool("mount"), "-t", "tmpfs", "tmpfs",
         str(target)]
    )
    host.pause_mounts.append(target)


class _Pong:
    """A host unix socket server. Each connection gets one line, `pong`."""

    def __init__(self, path: Path) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"pong\n")

    def close(self) -> None:
        self._sock.close()


@pytest.fixture(scope="module")
def box(host: Host) -> Iterator[Box]:
    """A keep-id box, built from the runtime's spec, then started by `podman start`. No packages."""
    home = host.realm_home(BOX)
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    spec = realm_spec(
        RealmName(BOX),
        DistroId(DISTRO),
        uid=host.uid,
        gid=host.gid,
        host_home=host.host_home,
        realm_home=home,
        owned_paths=host.paths.all_owned_roots(),
        env={},
    )
    cid = host.cli.create(dataclasses.replace(create_spec(spec), passwd=True))
    host.cli.start(cid)
    yield Box(BOX, cid)


@pytest.fixture(scope="module")
def gate(host: Host) -> Iterator[Gate]:
    """The gate realm, created by the product create with the Gate 3 seams (see the module doc)."""
    request = CreateRequest(
        name=RealmName(GATE),
        distro=DISTRO,
        uid=host.uid,
        gid=host.gid,
        host_home=host.host_home,
        host_env=host.session_env(),
    )
    # Each session starts from an empty store, but test_d pulls the image first. It is removed here,
    # so the create below pulls it and the timing includes the pull.
    removed_image = host.cli.image_exists(host.image)
    if removed_image:
        host.cli.rmi(host.image)
    image_present_before = host.cli.image_exists(host.image)
    mark = len(host.cli.history)
    started = time.perf_counter()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(create_mod._Create, "_make_user", _skip_user_step)
        result = create_realm(
            host.env,
            _PasswdTrue(host.cli),  # type: ignore[arg-type]
            request,
            hooks=CreateHooks(helper=host.helper, integrate=False, exists=_gate_exists),
        )
    elapsed = time.perf_counter() - started
    pulled = any("pull" in argv for argv in host.cli.history[mark:])
    yield Gate(GATE, result.record, elapsed, image_present_before, pulled, removed_image)


def _container_of(host: Host, name: str) -> str:
    return RealmStore(host.paths, time.time).load(RealmName(name)).container_id or ""


def _start_gate(host: Host) -> str:
    start_realm(
        host.env,
        host.cli,
        RealmName(GATE),
        session=HostUser(uid=host.uid, host_home=host.host_home, host_env=host.session_env()),
        hooks=LifecycleHooks(helper=host.helper, exists=_gate_exists),
    )
    return _container_of(host, GATE)


def _stop_gate(host: Host) -> None:
    stop_realm(host.env, host.cli, RealmName(GATE), hooks=LifecycleHooks(helper=host.helper))


@pytest.mark.fr("NFR-004")
def test_env_podman_runtime(host: Host) -> None:
    """The runtime podman uses on this host. ADR-010 asks for the exec numbers on crun."""
    info = host.cli.info()
    host.results.record(
        "env-podman-runtime",
        "MEASURED",
        oci_runtime=info.oci_runtime,
        podman=info.version,
        graph_driver=info.graph_driver,
        rootless=info.rootless,
        cgroup_manager=info.cgroup_manager,
        cgroup_version=info.cgroup_version,
        kernel=os.uname().release,
        store_root=str(host.base / "data" / "podman"),
    )
    assert info.oci_runtime == "crun"


# ------------------------------------------------------------------- d: create and keep-id passwd


@pytest.mark.fr("FR-010")
def test_d_create_with_the_product_argv(host: Host) -> None:
    """The product create with its own argv. Only integrate=False is set (Gate 3)."""
    request = CreateRequest(
        name=RealmName("rej"),
        distro=DISTRO,
        uid=host.uid,
        gid=host.gid,
        host_home=host.host_home,
        host_env=host.session_env(),
    )
    facts: dict[str, Any] = {}
    with gate_item(host.results, "d-create-product-argv", facts):
        try:
            create_realm(
                host.env, host.cli, request, hooks=CreateHooks(helper=host.helper, integrate=False)
            )
        except GinnError as err:
            facts.update(code=err.code, details={k: str(v)[:300] for k, v in err.details.items()})
            raise
        finally:
            # The argv the product create sent, read back from the recorded podman calls.
            facts["argv_has_passwd_false"] = any(
                "--passwd=false" in argv and "create" in argv for argv in host.cli.history
            )
        facts["created"] = True


@pytest.mark.fr("FR-010")
def test_d_keep_id_passwd_injection_and_useradd(host: Host) -> None:
    """Keep-id writes a passwd entry for uid 1000 at create. The realm useradd then clashes."""
    name = "ctl2"
    home = host.realm_home(name)
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    spec = realm_spec(
        RealmName(name),
        DistroId(DISTRO),
        uid=host.uid,
        gid=host.gid,
        host_home=host.host_home,
        realm_home=home,
        owned_paths=host.paths.all_owned_roots(),
        env={},
    )
    cid = host.cli.create(dataclasses.replace(create_spec(spec), passwd=True))
    facts: dict[str, Any] = {"passwd_false_flag": "unknown to podman create (test_d-create)"}
    try:
        host.cli.start(cid)
        passwd = sh(host, cid, 'getent passwd "$1"', str(host.uid))
        facts["passwd_before_useradd"] = passwd.stdout.strip()
        facts["group_entries"] = sh(host, cid, 'getent group "$1"', str(host.uid)).stdout.strip()
        argv = ["useradd", *USERADD_FLAGS, "-m", "-d", str(host.host_home), "-u", str(host.uid),
                host.user]
        step = host.cli.exec(cid, argv, user=ROOT_USER)
        facts.update(
            useradd_argv=argv,
            useradd_exit=step.exit_code,
            useradd_stderr=step.stderr.strip()[-300:],
            passwd_after_useradd=sh(host, cid, 'getent passwd "$1"', str(host.uid)).stdout.strip(),
        )
        with gate_item(host.results, "d-useradd-under-keep-id", facts):
            assert step.exit_code == 0, f"useradd exit {step.exit_code}"
    finally:
        with contextlib.suppress(GinnError):
            host.cli.rm(cid, force=True)


# ----------------------------------------------------------- create timing, start and stop


@pytest.mark.fr("NFR-007")
def test_c_create_timing(gate: Gate, host: Host) -> None:
    """NFR-007 timing: one run, zero packages, no refresh. Not the 3-run, 100 Mbps measurement."""
    host.results.record(
        "c-create-timing",
        "MEASURED",
        elapsed_s=round(gate.elapsed, 3),
        state=str(gate.record.state),
        distro=DISTRO,
        packages="none (Gate 3)",
        runs=1,
        image_present_before_create=gate.image_present_before,
        image_removed_first=gate.removed_image,
        pull_included=gate.pulled,
    )
    assert gate.record.state == RealmState.STOPPED
    assert gate.pulled and not gate.image_present_before, "the create did not pull the image"


@pytest.mark.fr("NFR-005")
@pytest.mark.fr("FR-016")
def test_e_start_stop_cycles(gate: Gate, host: Host) -> None:
    """NFR-005: 20 start cycles, p95 to running. FR-016: each services-off stop, timed."""
    starts: list[float] = []
    stops: list[float] = []
    exit_codes: list[int] = []
    for _ in range(20):
        t0 = time.perf_counter()
        _start_gate(host)
        starts.append(time.perf_counter() - t0)
        t1 = time.perf_counter()
        _stop_gate(host)
        stops.append(time.perf_counter() - t1)
        exit_codes.append(host.cli.inspect(_container_of(host, GATE)).exit_code)
    p95_start = p95(starts)
    host.results.record(
        "e-start-cycles",
        "MEASURED",
        n=len(starts),
        p95_s=round(p95_start, 3),
        p50_s=round(sorted(starts)[len(starts) // 2], 3),
        max_s=round(max(starts), 3),
        load=_loadavg(),
        io_pressure=Path("/proc/pressure/io").read_text(encoding="utf-8").splitlines()[0],
    )
    with gate_item(
        host.results,
        "c-stop-services-off",
        {"stop_s": [round(s, 3) for s in stops], "exit_codes": sorted(set(exit_codes))},
    ):
        assert max(stops) < 30, "a services-off stop reached the 30 s timeout"
    with gate_item(host.results, "e-start-p95-le-2s", {"p95_s": round(p95_start, 3), "n": 20}):
        assert p95_start <= 2.0, f"start p95 {p95_start:.3f} s exceeds 2 s"


@pytest.mark.fr("FR-082")
def test_f_no_systemd_process_and_minimal_init(gate: Gate, host: Host) -> None:
    cid = _start_gate(host)
    record = RealmStore(host.paths, time.time).load(RealmName(GATE))
    host.results.record(
        "f-share-skipped",
        "MEASURED",
        share_skipped=list(record.share_skipped),
        grant_hosts={g.realm: g.host for g in record.grants},
        hidden_mounts_after_start=_hidden_mounts(host, cid),
    )
    try:
        comms = sh(
            host,
            cid,
            "for p in /proc/[0-9]*; do cat $p/comm 2>/dev/null; echo; done",
        ).stdout.split()
        pid1 = sh(host, cid, "cat /proc/1/comm").stdout.strip()
        facts = {"pid1_comm": pid1, "process_count": len(comms), "comms": sorted(set(comms))}
        with gate_item(host.results, "f-no-systemd", facts):
            assert not any(comm.startswith("systemd") for comm in comms)
    finally:
        _stop_gate(host)


@pytest.mark.fr("FR-016")
def test_c_control_without_init_stop_waits_for_the_timeout(host: Host) -> None:
    """Control for FR-016 (c): the same services-off spec without --init. PID 1 ignores SIGTERM."""
    name = "noinit"
    home = host.realm_home(name)
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    spec = realm_spec(
        RealmName(name),
        DistroId(DISTRO),
        uid=host.uid,
        gid=host.gid,
        host_home=host.host_home,
        realm_home=home,
        owned_paths=host.paths.all_owned_roots(),
        env={},
    )
    cid = host.cli.create(dataclasses.replace(create_spec(spec), passwd=True, init=False))
    try:
        host.cli.start(cid)
        t0 = time.perf_counter()
        host.cli.stop(cid, 30)
        elapsed = time.perf_counter() - t0
        info = host.cli.inspect(cid)
        host.results.record(
            "c-control-no-init",
            "MEASURED",
            stop_s=round(elapsed, 3),
            exit_code=info.exit_code,
            timeout_s=30,
            note="no --init: PID 1 is sleep, which has no SIGTERM handler",
        )
    finally:
        with contextlib.suppress(GinnError):
            host.cli.rm(cid, force=True)


@pytest.mark.fr("NFR-004")
def test_g_ps_reports_the_pid(gate: Gate, host: Host) -> None:
    cid = _start_gate(host)
    try:
        ps_pid = next(info.pid for info in host.cli.ps() if info.id == cid)
        inspect_pid = host.cli.inspect(cid).pid
        with gate_item(host.results, "g-ps-pid", {"ps_pid": ps_pid, "inspect_pid": inspect_pid}):
            assert ps_pid == inspect_pid and ps_pid > 1
    finally:
        _stop_gate(host)


@pytest.mark.fr("FR-049")
def test_h_exec_workdir_and_missing_workdir(gate: Gate, host: Host) -> None:
    cid = _start_gate(host)
    try:
        ok = host.cli.exec(cid, ["/bin/sh", "-c", "pwd; id -u"], user=ROOT_USER, workdir="/root")
        missing = host.cli.exec(cid, ["/bin/true"], user=ROOT_USER, workdir="/no/such/dir")
        facts = {
            "root_workdir_exit": ok.exit_code,
            "root_workdir_stdout": ok.stdout.split(),
            "missing_workdir_exit": missing.exit_code,
            "missing_workdir_stderr": missing.stderr.strip()[:300],
        }
        with gate_item(host.results, "h-exec-workdir", facts):
            assert ok.exit_code == 0 and ok.stdout.split()[:1] == ["/root"]
            assert missing.exit_code != 0
    finally:
        _stop_gate(host)


@pytest.mark.fr("NFR-004")
@pytest.mark.fr("FR-049")
def test_e_exec_p95_over_100_launches(gate: Gate, host: Host) -> None:
    """NFR-004 on the enter path: the ADR-008 pre-check, one `podman ps`, then the exec."""
    _start_gate(host)
    session = HostUser(uid=host.uid, host_home=host.host_home, host_env=host.session_env())
    hooks = EnterHooks(
        lifecycle=LifecycleHooks(helper=host.helper, exists=_gate_exists), isatty=lambda: False
    )
    try:

        def launch() -> int:
            return enter_realm(
                host.env,
                host.cli,
                RealmName(GATE),
                session=session,
                gid=host.gid,
                argv=["/bin/true"],
                hooks=hooks,
            )

        for _ in range(5):
            launch()
        before = len(host.cli.history)
        times: list[float] = []
        codes: list[int] = []
        for _ in range(100):
            t0 = time.perf_counter()
            codes.append(launch())
            times.append(time.perf_counter() - t0)
        calls = host.cli.history[before:]
        ps_calls = sum(1 for argv in calls if "ps" in argv)
        p95_ms = round(p95(times) * 1000, 1)
        host.results.record(
            "e-exec-p95",
            "MEASURED",
            n=len(times),
            p95_ms=p95_ms,
            p50_ms=round(sorted(times)[len(times) // 2] * 1000, 1),
            max_ms=round(max(times) * 1000, 1),
            ps_calls=ps_calls,
            podman_calls=len(calls),
            exit_codes=sorted(set(codes)),
            load=_loadavg(),
        )
        host.results.record(
            "e-exec-hidden-mounts",
            "MEASURED",
            hidden_mounts=_hidden_mounts(host, _container_of(host, GATE)),
        )
        with gate_item(host.results, "e-exec-p95-le-300ms", {"p95_ms": p95_ms}):
            assert set(codes) == {0}
            assert p95(times) <= 0.300, f"exec p95 {p95_ms} ms exceeds 300 ms"
    finally:
        _stop_gate(host)


# ------------------------------------------------------------------ a: grant attachment


@pytest.mark.fr("FR-061")
def test_a_socket_directory_and_file_under_keep_id(host: Host, box: Box) -> None:
    src = host.base / "att" / "a1"
    (src / "d").mkdir(parents=True)
    (src / "d" / "a.txt").write_text("dir-v1\n", encoding="utf-8")
    (src / "f.txt").write_text("file-v1\n", encoding="utf-8")
    run = RUN_DIR.format(uid=host.uid)
    pong = _Pong(src / "s.sock")
    facts: dict[str, Any] = {}
    try:
        grants = [
            grant(src / "d", f"{run}/ginn-dir", is_dir=True),
            grant(src / "f.txt", f"{run}/ginn-file"),
            grant(src / "s.sock", f"{run}/ginn-sock"),
        ]
        with gate_item(host.results, "a-socket-dir-file-keep-id", facts):
            before = len(host.cli.history)
            result = attach(host, box, grants)
            helper_calls = [a for a in host.cli.history[before:] if "unshare" in a]
            facts["helper_calls_for_three_grants"] = len(helper_calls)
            types = sh(
                host,
                box.cid,
                "stat -c '%F' \"$1\" \"$2\" \"$3\"",
                f"{run}/ginn-dir",
                f"{run}/ginn-file",
                f"{run}/ginn-sock",
            ).stdout
            text = sh(
                host, box.cid, 'cat "$1/a.txt" "$2"', f"{run}/ginn-dir", f"{run}/ginn-file"
            ).stdout
            reply = sh(
                host,
                box.cid,
                "perl -MIO::Socket::UNIX -e '$s=IO::Socket::UNIX->new(Peer=>$ARGV[0]) or die $!; "
                "print <$s>' \"$1\"",
                f"{run}/ginn-sock",
                user="1000:1000",
            )
            facts.update(
                skipped=[s.reason for s in result.skipped],
                identities=[r.identity is not None for r in result.records],
                types=types.splitlines(),
                contents=text.split(),
                socket_reply=reply.stdout.strip(),
                socket_stderr=reply.stderr.strip()[:200],
            )
            assert not result.skipped
            assert "directory" in types and "regular" in types and "socket" in types
            assert text.split() == ["dir-v1", "file-v1"]
            assert reply.stdout.strip() == "pong"
    finally:
        pong.close()


@pytest.mark.fr("FR-061")
def test_a_read_only_file_and_directory(host: Host, box: Box) -> None:
    src = host.base / "att" / "a2"
    (src / "d").mkdir(parents=True)
    (src / "d" / "r.txt").write_text("ro-dir\n", encoding="utf-8")
    (src / "f.txt").write_text("ro-file\n", encoding="utf-8")
    run = RUN_DIR.format(uid=host.uid)
    facts: dict[str, Any] = {}
    with gate_item(host.results, "a-read-only-file-and-dir", facts):
        attach(
            host,
            box,
            [
                grant(src / "d", f"{run}/ginn-ro-dir", mode="ro", is_dir=True),
                grant(src / "f.txt", f"{run}/ginn-ro-file", mode="ro"),
            ],
        )
        write_dir = sh(host, box.cid, 'touch "$1/x" 2>&1; echo rc=$?', f"{run}/ginn-ro-dir").stdout
        write_file = sh(
            host, box.cid, 'echo x >> "$1" 2>&1; echo rc=$?', f"{run}/ginn-ro-file"
        ).stdout
        read = sh(
            host, box.cid, 'cat "$1/r.txt" "$2"', f"{run}/ginn-ro-dir", f"{run}/ginn-ro-file"
        ).stdout
        facts.update(
            dir_write=write_dir.strip(),
            file_write=write_file.strip(),
            dir_options=mount_options(host, box, f"{run}/ginn-ro-dir"),
            file_options=mount_options(host, box, f"{run}/ginn-ro-file"),
            read=read.split(),
        )
        assert "rc=0" not in write_dir and "rc=0" not in write_file
        assert "ro" in facts["dir_options"].split(",")
        assert "ro" in facts["file_options"].split(",")
        assert read.split() == ["ro-dir", "ro-file"]


@pytest.mark.fr("FR-061")
def test_a_read_only_reaches_a_child_mount_recursively(host: Host, box: Box) -> None:
    """AT_RECURSIVE clones a child mount, and mount_setattr(AT_RECURSIVE) makes it read-only."""
    src = host.base / "att" / "a3"
    child = src / "d" / "child"
    child.mkdir(parents=True)
    run = RUN_DIR.format(uid=host.uid)
    pause_mount(host, child)
    facts: dict[str, Any] = {}
    with gate_item(host.results, "a-recursive-ro-child-mount", facts):
        attach(host, box, [grant(src / "d", f"{run}/ginn-ro-tree", mode="ro", is_dir=True)])
        child_options = mount_options(host, box, f"{run}/ginn-ro-tree/child")
        write_child = sh(
            host, box.cid, 'touch "$1/child/x" 2>&1; echo rc=$?', f"{run}/ginn-ro-tree"
        )
        facts.update(child_mount_options=child_options, child_write=write_child.stdout.strip())
        assert "ro" in child_options.split(",")
        assert "rc=0" not in write_child.stdout


@pytest.mark.fr("FR-066")
def test_a_compare_and_skip_without_force(host: Host, box: Box) -> None:
    """ADR-008 step 3.1: an attached grant without --force is skipped by the compare."""
    src = host.base / "att" / "a9"
    src.mkdir(parents=True)
    (src / "c.txt").write_text("c-v1\n", encoding="utf-8")
    realm = f"{RUN_DIR.format(uid=host.uid)}/ginn-compare"
    attach(host, box, [grant(src / "c.txt", realm)])
    pid = str(host.cli.inspect(box.cid).pid)
    facts: dict[str, Any] = {}
    with gate_item(host.results, "a-compare-and-skip-no-force", facts):
        again = host.cli.unshare([host.helper, pid, str(src / "c.txt"), realm])
        facts.update(
            exit=again.exit_code,
            stdout=again.stdout.strip(),
            stderr=again.stderr.strip()[:200],
        )
        assert again.exit_code == 0 and again.stdout.strip() == "0 skipped"


@pytest.mark.fr("FR-066")
def test_a_lazy_reattach_after_a_host_change(host: Host, box: Box) -> None:
    src = host.base / "att" / "a4"
    src.mkdir(parents=True)
    (src / "f.txt").write_text("v1\n", encoding="utf-8")
    realm = f"{RUN_DIR.format(uid=host.uid)}/ginn-lazy"
    facts: dict[str, Any] = {}
    with gate_item(host.results, "a-lazy-reattach", facts):
        first = attach(host, box, [grant(src / "f.txt", realm)])
        pid_before = host.cli.inspect(box.cid).pid
        asked_before, _ = lazy(host, box, first.records)
        tmp = src / "f.tmp"
        tmp.write_text("v2\n", encoding="utf-8")
        os.replace(tmp, src / "f.txt")  # a new inode under the same host path
        asked_after, result = lazy(host, box, first.records)
        seen = sh(host, box.cid, 'cat "$1"', realm).stdout.strip()
        asked_again, _ = lazy(host, box, result.records if result else first.records)
        facts.update(
            asked_before_change=asked_before,
            asked_after_change=asked_after,
            realm_sees=seen,
            asked_after_reattach=asked_again,
            init_pid_unchanged=pid_before == host.cli.inspect(box.cid).pid,
        )
        assert not asked_before and asked_after and seen == "v2"
        assert not asked_again


@pytest.mark.fr("FR-061")
def test_a_busy_directory_grant_is_detached_with_mnt_detach(host: Host, box: Box) -> None:
    """An open file and a submount under a directory grant. The forced re-attach must detach it."""
    src = host.base / "att" / "a5"
    (src / "d" / "sub").mkdir(parents=True)
    (src / "d" / "busy.txt").write_text("busy-v1\n", encoding="utf-8")
    realm = f"{RUN_DIR.format(uid=host.uid)}/ginn-busy"
    pause_mount(host, src / "d" / "sub")
    facts: dict[str, Any] = {}
    pids: list[str] = []
    try:
        with gate_item(host.results, "a-busy-dir-mntdetach", facts):
            first = attach(host, box, [grant(src / "d", realm, is_dir=True)])
            pids.append(holder(host, box, 'cd "$1/sub" && exec 3<"$1/busy.txt"; sleep 600', realm))
            # Replace the host folder with a new inode under the same path, so the attach is forced.
            os.replace(src / "d", src / "d.old")
            (src / "d").mkdir()
            (src / "d" / "new.txt").write_text("new-v2\n", encoding="utf-8")
            asked, result = lazy(host, box, first.records)
            seen = sh(host, box.cid, 'ls "$1"', realm).stdout.split()
            facts.update(asked=asked, realm_sees=seen, holder_pids=pids)
            assert asked and result is not None and not result.skipped
            assert seen == ["new.txt"]
    finally:
        for pid in pids:
            unholder(host, box, pid)


@pytest.mark.fr("FR-061")
def test_a_busy_directory_under_plain_umount2_control(host: Host, box: Box) -> None:
    """Control for the MNT_DETACH test: the busy directory under a plain umount2 in the box.

    The first try used util-linux umount under `nsenter -m`, which returned ENOENT for a mount that
    /proc/self/mountinfo lists. Two retries follow: nsenter with -p as well, and a direct umount2
    (syscall 166 on x86_64) from the container's perl, which does not read mountinfo. EBUSY from
    either one is the control. Otherwise the control stays NOT RUN, with what was observed.
    """
    src = host.base / "att" / "a8"
    (src / "d" / "sub").mkdir(parents=True)
    (src / "d" / "busy.txt").write_text("busy-v1\n", encoding="utf-8")
    realm = f"{RUN_DIR.format(uid=host.uid)}/ginn-busy-ctl"
    pause_mount(host, src / "d" / "sub")
    attach(host, box, [grant(src / "d", realm, is_dir=True)])
    pid = holder(host, box, 'cd "$1/sub" && exec 3<"$1/busy.txt"; sleep 600', realm)
    init_pid = str(host.cli.inspect(box.cid).pid)
    attempts = {
        "nsenter_mount_ns_umount": [
            tool("nsenter"), "-t", init_pid, "-m", "--", "/usr/bin/umount", realm,
        ],
        "nsenter_mount_pid_ns_umount": [
            tool("nsenter"), "-t", init_pid, "-m", "-p", "--", "/usr/bin/umount", realm,
        ],
        "nsenter_mount_ns_perl_umount2": [
            tool("nsenter"), "-t", init_pid, "-m", "--", "/usr/bin/perl", "-e",
            'syscall(166, $ARGV[0], 0) == 0 or print "errno=", $!+0, "\\n"', realm,
        ],
    }
    observed: dict[str, str] = {}
    try:
        for name, argv in attempts.items():
            out = host.cli.unshare(argv)
            observed[name] = f"rc={out.exit_code} out={(out.stdout + out.stderr).strip()[:160]}"
    finally:
        unholder(host, box, pid)
        with contextlib.suppress(GinnError):
            host.cli.unshare(
                [tool("nsenter"), "-t", init_pid, "-m", "--", "/usr/bin/umount", "-l", realm]
            )
    busy = any("errno=16" in text or "busy" in text.lower() for text in observed.values())
    if busy:
        host.results.record("a-busy-dir-plain-umount-control", "MEASURED", observed=observed)
        return
    reason = "no plain umount2 variant returned EBUSY (see observed)"
    host.results.record(
        "a-busy-dir-plain-umount-control", "NOT RUN", reason=reason, observed=observed
    )
    pytest.skip(reason)


@pytest.mark.fr("FR-061")
def test_a_plain_umount2_file_with_an_open_descriptor(host: Host, box: Box) -> None:
    """ADR-008 open question: a file grant with an open descriptor, under a plain umount2."""
    src = host.base / "att" / "a6"
    src.mkdir(parents=True)
    (src / "h.txt").write_text("h-v1\n", encoding="utf-8")
    realm = f"{RUN_DIR.format(uid=host.uid)}/ginn-hold-file"
    first = attach(host, box, [grant(src / "h.txt", realm)])
    pid = holder(host, box, 'exec 3<"$1"; sleep 600', realm)
    facts: dict[str, Any] = {"holder_pid": pid}
    try:
        tmp = src / "h.tmp"
        tmp.write_text("h-v2\n", encoding="utf-8")
        os.replace(tmp, src / "h.txt")
        facts.update(reattach_outcome(host, box, first.records))
        with gate_item(host.results, "a-plain-umount2-file-open-fd", facts):
            assert facts["raised"] is None, f"the helper refused the file grant: {facts}"
    finally:
        unholder(host, box, pid)


@pytest.mark.fr("FR-061")
def test_a_plain_umount2_socket_with_a_connected_descriptor(host: Host, box: Box) -> None:
    src = host.base / "att" / "a7"
    src.mkdir(parents=True)
    realm = f"{RUN_DIR.format(uid=host.uid)}/ginn-hold-sock"
    pong = _Pong(src / "s.sock")
    first = attach(host, box, [grant(src / "s.sock", realm)])
    pid = holder(
        host,
        box,
        "perl -MIO::Socket::UNIX -e '$s=IO::Socket::UNIX->new(Peer=>$ARGV[0]) or die; "
        "sleep 600' \"$1\"",
        realm,
    )
    facts: dict[str, Any] = {"holder_pid": pid}
    try:
        pong.close()
        (src / "s.sock").unlink()  # replace the host socket under the same path
        pong = _Pong(src / "s.sock")
        facts.update(reattach_outcome(host, box, first.records))
        reply = sh(
            host,
            box.cid,
            "perl -MIO::Socket::UNIX -e '$s=IO::Socket::UNIX->new(Peer=>$ARGV[0]) or die $!; "
            "print <$s>' \"$1\"",
            realm,
            user="1000:1000",
        )
        facts["reply_after_reattach"] = reply.stdout.strip()
        with gate_item(host.results, "a-plain-umount2-socket-connected", facts):
            assert facts["raised"] is None, f"the helper refused the socket grant: {facts}"
            assert reply.stdout.strip() == "pong", "the realm does not reach the new socket"
    finally:
        pong.close()
        unholder(host, box, pid)


# ------------------------------------------------------------------------------- b: the drive


@pytest.mark.fr("FR-064")
def test_b_drive_mounted_on_the_host_after_start(host: Host, box: Box) -> None:
    """A FUSE drive under a mount root, mounted in the host namespace after the pause exists.

    ADR-008 (T4.9 notes): host mounts are private, so a negative result is expected and is a FAIL
    that escalates. The realm root is the host mount root, which the attach runs with admit=all.
    """
    root = host.mount_root
    drive = root / "drive"
    drive.mkdir(parents=True, exist_ok=True)
    realm_root = "/run/media/user"
    sh(host, box.cid, 'mkdir -p "$1"', realm_root)
    first = attach(host, box, [grant(root, realm_root, is_dir=True)])
    src = host.base / "drivesrc"
    src.mkdir(exist_ok=True)
    (src / "drive-marker.txt").write_text("drive\n", encoding="utf-8")
    run_tool([tool("fuse-overlayfs"), "-o", f"lowerdir={src}", str(drive)])
    host.fuse.append(drive)
    host_sees = str(drive) in Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    store_sees = host.cli.unshare(
        [tool("sh"), "-c", f"test -e {drive}/drive-marker.txt && echo SEEN || echo NOT_SEEN"]
    ).stdout.strip()
    asked = needs_reattach(first.records, SessionEnv(env={}, binds=[]), admit=_admit_all)
    outcome: dict[str, Any] = {}
    asked_after: bool | None = None
    try:
        again = reattach(
            host.cli,
            box.cid,
            pid=host.cli.inspect(box.cid).pid,
            grants=first.records,
            session=SessionEnv(env={}, binds=[]),
            realm_home=host.realm_home(box.name),
            host_home=host.host_home,
            admit=_admit_all,
            helper=host.helper,
        )
        outcome = {"raised": None, "records": len(again.records)}
        asked_after = needs_reattach(again.records, SessionEnv(env={}, binds=[]), admit=_admit_all)
    except GinnError as err:
        outcome = {"raised": err.code, "detail": str(err.details)[:200]}
    realm_sees = sh(
        host, box.cid, 'cat "$1/drive/drive-marker.txt" 2>&1; echo rc=$?', realm_root
    ).stdout
    facts: dict[str, Any] = {
        "host_mountinfo_has_drive": host_sees,
        "podman_unshare_sees_drive": store_sees,
        "needs_reattach_after_drive": asked,
        "reattach_outcome": outcome,
        "needs_reattach_after_reattach": asked_after,
        "realm_output": realm_sees.strip()[:200],
    }
    with gate_item(host.results, "b-drive-mounted-on-host-after-start", facts):
        assert host_sees and store_sees == "SEEN", "the host drive is not in the store namespace"
        assert realm_sees.splitlines()[:1] == ["drive"], "the realm does not see the drive"


@pytest.mark.fr("FR-066")
def test_b_drive_mounted_in_the_store_namespace_reaches_the_realm(host: Host, box: Box) -> None:
    """Positive control: a mount made in the store namespace, carried by the helper with --force."""
    root = host.mount_root
    ctl = root / "drive2"
    ctl.mkdir(parents=True, exist_ok=True)
    pause_mount(host, ctl)
    host.cli.unshare([tool("sh"), "-c", f"echo ctl > {ctl}/ctl-marker.txt"])
    pid = str(host.cli.inspect(box.cid).pid)
    helper_run = host.cli.unshare([host.helper, pid, "--force", str(root), "/run/media/user"])
    realm_sees = sh(host, box.cid, "cat /run/media/user/drive2/ctl-marker.txt 2>&1; echo rc=$?")
    facts: dict[str, Any] = {
        "helper_exit": helper_run.exit_code,
        "helper_stderr": helper_run.stderr[:200],
        "realm_output": realm_sees.stdout.strip(),
    }
    with gate_item(host.results, "b-drive-store-namespace-control", facts):
        assert helper_run.exit_code == 0
        assert realm_sees.stdout.splitlines()[0] == "ctl"


@pytest.mark.fr("FR-061")
def test_g_rebind_flags_pass_through_podman_unshare(host: Host, box: Box) -> None:
    run = RUN_DIR.format(uid=host.uid)
    src = host.base / "att" / "g"
    src.mkdir(parents=True)
    (src / "u.txt").write_text("u\n", encoding="utf-8")
    realm = f"{run}/ginn-unmount-me"
    first = attach(host, box, [grant(src / "u.txt", realm)])
    pid = str(host.cli.inspect(box.cid).pid)
    usage = host.cli.unshare([host.helper, pid, "--unmount", realm, "--ro"])
    unmount = host.cli.unshare([host.helper, pid, "--unmount", realm])
    facts: dict[str, Any] = {
        "unmount_with_ro_exit": usage.exit_code,
        "unmount_exit": unmount.exit_code,
        "options_after_unmount": mount_options(host, box, realm),
        "attached_records": len(first.records),
    }
    with gate_item(host.results, "g-rebind-flags", facts):
        assert usage.exit_code == 2, "--unmount takes no other flag (usage, exit 2)"
        assert unmount.exit_code == 0 and facts["options_after_unmount"] == ""


# ------------------------------------------------------------------------------- i: delete


@pytest.mark.fr("FR-019")
def test_i_delete_with_a_subordinate_uid_file_in_the_home(gate: Gate, host: Host) -> None:
    cid = _start_gate(host)
    # A directory made by container root is owned by a subordinate uid. The purge refuses it,
    # because our uid cannot remove the entries of a directory it does not own.
    sh(host, cid, 'mkdir "$1/subuid-dir" && echo x > "$1/subuid-dir/f"', str(host.host_home))
    _stop_gate(host)
    home = host.realm_home(GATE)
    facts: dict[str, Any] = {"home": str(home)}
    with gate_item(host.results, "i-delete-purge-subuid", facts):
        try:
            delete_realm(
                host.env, host.cli, RealmName(GATE), purge_home=True, confirm=lambda _p: True
            )
        except GinnError as err:
            facts.update(code=err.code, remedy=err.remedy, details=str(err.details)[:300])
            assert err.code == "GINN-E-HOME-UNSAFE"
            assert "podman unshare rm -rf --one-file-system" in err.remedy
        else:
            raise AssertionError("the purge should refuse a subordinate-uid file")
        findmnt = run_tool([tool("findmnt"), "-R", str(home)], check=False)
        facts.update(findmnt_exit=findmnt.returncode, findmnt_stdout=findmnt.stdout.strip()[:200])
        remedy = host.cli.unshare([tool("rm"), "-rf", "--one-file-system", str(home)])
        facts["remedy_exit"] = remedy.exit_code
        result = delete_realm(
            host.env, host.cli, RealmName(GATE), purge_home=True, confirm=lambda _p: True
        )
        facts["home_after_retry"] = str(result.home)
        facts["home_gone"] = not home.exists()
        facts["record_gone"] = not RealmStore(host.paths, time.time).exists(RealmName(GATE))
        assert remedy.exit_code == 0 and facts["home_gone"] and facts["record_gone"]


# ------------------------------------------------------------------------- musl smoke test


@pytest.mark.host_musl
@pytest.mark.fr("FR-010")
def test_musl_smoke_create_start_exec_delete(host: Host) -> None:
    """The host_musl smoke set: create, start, exec, delete. It runs only on a musl host."""
    name = RealmName("musl")
    request = CreateRequest(
        name=name,
        distro=DISTRO,
        uid=host.uid,
        gid=host.gid,
        host_home=host.host_home,
        host_env=host.session_env(),
    )
    hooks = CreateHooks(helper=host.helper, integrate=False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(create_mod._Create, "_make_user", _skip_user_step)
        create_realm(host.env, _PasswdTrue(host.cli), request, hooks=hooks)  # type: ignore[arg-type]
    try:
        start_realm(
            host.env,
            host.cli,
            name,
            session=HostUser(uid=host.uid, host_home=host.host_home, host_env=host.session_env()),
            hooks=LifecycleHooks(helper=host.helper),
        )
        cid = _container_of(host, "musl")
        assert host.cli.exec(cid, ["/bin/true"], user=ROOT_USER).exit_code == 0
        stop_realm(host.env, host.cli, name, hooks=LifecycleHooks(helper=host.helper))
    finally:
        if RealmStore(host.paths, time.time).exists(name):
            delete_realm(host.env, host.cli, name, purge_home=False, confirm=lambda _p: True)
