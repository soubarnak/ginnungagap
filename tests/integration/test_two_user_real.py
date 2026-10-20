# SPDX-License-Identifier: Apache-2.0
"""T10.5 host gate: per-user realm isolation (FR-025) and the Audit Log's reach (NFR-SEC-008).

Run on a Void host with rootless Podman and a SECOND host user, as the first user (user A):

    GINN_INTEGRATION=1 GINN_SECOND_USER=<name> \
        pytest tests/integration/test_two_user_real.py -q -rs

Skipped, with the reason, unless all of these hold:
- GINN_INTEGRATION=1 (conftest.py);
- GINN_SECOND_USER names an existing host user other than the current one (user B);
- `sudo -n -u <B> true` succeeds, so no password prompt is ever needed;
- user B can read this checkout's src/ and run the Python of the test session.
Nothing here asks for a password or writes to B's real home. User B runs in a throwaway directory
that B owns (`mktemp -d` run as B): its HOME, XDG_*_HOME and GINN_RUNTIME_DIR point there, and the
directory is removed at the end.

User A's realm lives in the gate's isolated storage (conftest.py: own --root, --runroot, --tmpdir,
own XDG_RUNTIME_DIR). That storage base is /tmp/ginn-it-*, mode 0700. To test the modes that ginn
itself sets, the test opens the base and the XDG parent directories to traverse-only (0711) for the
module, so that B reaches the Owned Path roots and only their own modes stop it. The base is put
back to 0700 at the end.

FR-025: B creates its own realm (NOT RUN if B's Podman cannot). B's `ginn list` shows it and
not A's; B's enter, exec, start, stop, status and delete of
A's realm name all fail with GINN-E-NO-SUCH-REALM (exit 3); A's realm record is unchanged after.
NFR-SEC-008: the Audit Log and the Owned Path roots of A are 0600 / 0700 and A-owned; B's attempts
to read, append, create and delete there fail with "Permission denied"; B cannot connect to a unix
socket in A's runtime directory. Inside a Custom realm that has every grant that needs no package
or hardware, run as realm root, the log path is absent, appending to it fails, a share that would
expose it is refused (DR-004), and the host log only grew (its old bytes are a prefix of its new).

Not covered here: the musl legs (no musl host), and B using its real /run/ginn/<uid> layout.
"""

from __future__ import annotations

import contextlib
import os
import pwd
import shutil
import socket
import stat
import subprocess  # noqa: S404 - fixed argv lists only, never a shell (NFR-SEC-005)
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from ginn.domain import RealmName, Tier
from ginn.domain.paths import SharedPath
from ginn.domain.permissions import Toggle
from ginn.errors import GinnError
from ginn.ops.lifecycle import HostUser
from ginn.ops.permissions import set_permissions, set_tier
from ginn.podman.adapter import ExecTarget
from ginn.store.locks import audit_lock_path
from ginn.store.paths import OWNED_DIR_MODE, OWNED_FILE_MODE
from tests.integration.conftest import REPO, Host, gate_item
from tests.integration.test_lifecycle_real import (  # noqa: F401 - the mount-root hide is autouse
    _hide_real_mount_roots,
)
from tests.integration.test_tiers_real import _create, _drop, _load, _start, _stop

SECOND_USER_ENV = "GINN_SECOND_USER"
REALM = "fr025a"
B_REALM = "fr025b"
DENIED = "Permission denied"
NO_SUCH = "GINN-E-NO-SUCH-REALM"
EXIT_NOT_FOUND = 3  # IR-011: not found
RUN_AS_B_TIMEOUT = 120.0


class SecondUser:
    """User B: how to run a fixed argv as that user, and B's own throwaway ginn directories."""

    def __init__(self, name: str, uid: int, scratch: Path) -> None:
        self.name = name
        self.uid = uid
        self.scratch = scratch
        self.home = scratch / "home"
        self.runtime = scratch / "run"

    def run(
        self, argv: Sequence[str], *, env: dict[str, str] | None = None, timeout: float = 60.0
    ) -> subprocess.CompletedProcess[str]:
        """Run `argv` as B through `sudo -n -u B env ...`. No shell, no password prompt."""
        prefix = [_sudo(), "-n", "-u", self.name, "env", f"HOME={self.home}", "LC_ALL=C"]
        prefix += [f"{k}={v}" for k, v in (env or {}).items()]
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*prefix, *argv],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )

    def cleanup(self) -> list[str]:
        """Remove B's scratch directory. Returns the problems, so the fixture can report them.

        B's own `ginn create` leaves layers owned by B's sub-IDs, which only B's user namespace can
        remove, and a pause process. As the gate does for user A (conftest.py): run
        `podman unshare rm -rf` with B's own --root, --runroot and --tmpdir (ginn's layout under
        the scratch directory) and XDG_RUNTIME_DIR = the scratch session, then stop the pause
        process after checking that it is catatonit, then remove the rest.
        """
        problems: list[str] = []
        podman = shutil.which("podman")
        data, state, config = (self.scratch / d for d in ("data", "state", "config"))
        env = self.ginn_env()
        # The files ginn writes for Podman (OwnedPaths.podman_containers_conf, podman_storage_conf).
        env["CONTAINERS_CONF"] = str(config / "ginn" / "containers.conf")
        env["CONTAINERS_STORAGE_CONF"] = str(config / "ginn" / "storage.conf")
        if podman is not None and self.run(["test", "-d", str(data)]).returncode == 0:
            flags = [
                "--root",
                str(data / "ginn" / "podman"),
                "--runroot",
                str(self.runtime / "podman"),
                "--tmpdir",
                str(self.runtime / "podman-tmp"),
            ]
            gone = self.run(
                [
                    podman,
                    *flags,
                    "unshare",
                    "rm",
                    "-rf",
                    "--one-file-system",
                    str(data),
                    str(state),
                    str(config),
                ],
                env=env,
                timeout=600,
            )
            if gone.returncode != 0:
                problems.append(f"podman unshare rm failed: {gone.stderr.strip()[:200]}")
        pid_file = self.scratch / "session" / "libpod" / "tmp" / "pause.pid"
        pid_text = self.run(["cat", str(pid_file)]).stdout.strip()
        if pid_text.isdigit():
            comm = self.run(["cat", f"/proc/{pid_text}/comm"]).stdout.strip()
            if comm == "catatonit":  # checked right before the signal; a recycled pid is left alone
                self.run(["kill", "-TERM", pid_text])
                time.sleep(1)
                if self.run(["cat", f"/proc/{pid_text}/comm"]).stdout.strip() == "catatonit":
                    self.run(["kill", "-KILL", pid_text])
        left = self.run(["rm", "-rf", "--one-file-system", str(self.scratch)])
        if left.returncode != 0 or Path(self.scratch).exists():
            problems.append(f"scratch directory remains: {left.stderr.strip()[:200]}")
        return problems

    def ginn_env(self) -> dict[str, str]:
        return {
            "XDG_DATA_HOME": str(self.scratch / "data"),
            "XDG_CONFIG_HOME": str(self.scratch / "config"),
            "XDG_STATE_HOME": str(self.scratch / "state"),
            "GINN_RUNTIME_DIR": str(self.runtime),
            "XDG_RUNTIME_DIR": str(self.scratch / "session"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }

    def ginn(
        self, *args: str, timeout: float = RUN_AS_B_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        """Run the product CLI of this checkout as B, in B's own directories."""
        boot = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from ginn.__main__ import main; sys.exit(main(sys.argv[2:]))"
        )
        return self.run(
            [sys.executable, "-c", boot, str(REPO / "src"), *args],
            env=self.ginn_env(),
            timeout=timeout,
        )


def _sudo() -> str:
    found = shutil.which("sudo")
    if found is None:
        pytest.skip("sudo is not installed; the two-user gate needs `sudo -n -u <user>`")
    return found


def _check(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(argv),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )


@pytest.fixture(scope="module")
def user_b() -> Iterator[SecondUser]:
    """User B, or a skip that says why the gate cannot run."""
    name = os.environ.get(SECOND_USER_ENV, "")
    if not name:
        pytest.skip(f"set {SECOND_USER_ENV}=<second host user> for the two-user gate (T10.5)")
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        pytest.skip(f"{SECOND_USER_ENV}={name!r} is not a user of this host")
    if entry.pw_uid == os.getuid():
        pytest.skip(f"{SECOND_USER_ENV} must name a different user than the current one")
    sudo = _sudo()
    probe = _check([sudo, "-n", "-u", name, "true"])
    if probe.returncode != 0:
        pytest.skip(
            f"`sudo -n -u {name} true` failed ({probe.stderr.strip()}); the gate needs "
            "passwordless sudo to the second user"
        )
    for need, why in (
        (["test", "-r", str(REPO / "src" / "ginn" / "__main__.py")], "read this checkout"),
        (["test", "-x", sys.executable], "run the Python of the test session"),
    ):
        if _check([sudo, "-n", "-u", name, *need]).returncode != 0:
            pytest.skip(f"user {name} cannot {why}; grant it read/execute access first")
    importable = "import sys; sys.path.insert(0, sys.argv[1]); import ginn.__main__"
    if (
        _check(
            [
                sudo,
                "-n",
                "-u",
                name,
                "env",
                "PYTHONDONTWRITEBYTECODE=1",
                sys.executable,
                "-c",
                importable,
                str(REPO / "src"),
            ]
        ).returncode
        != 0
    ):
        pytest.skip(f"user {name} cannot import ginn from this checkout with {sys.executable}")
    made = _check([sudo, "-n", "-u", name, "mktemp", "-d", "/tmp/ginn-it-b-XXXXXX"])  # noqa: S108
    if made.returncode != 0:
        pytest.fail(f"mktemp as {name} failed: {made.stderr}")
    scratch = Path(made.stdout.strip())
    other = SecondUser(name, entry.pw_uid, scratch)
    try:
        session = other.scratch / "session"
        prep = other.run(["mkdir", "-m", "0700", str(other.home), str(other.runtime), str(session)])
        assert prep.returncode == 0, prep.stderr
        yield other
    finally:
        problems = other.cleanup()
        if problems:
            host_note = "; ".join(problems)
            print(f"two-user gate: cleanup of {scratch} left problems: {host_note}")


@pytest.fixture(scope="module")
def a_realm(host: Host) -> Iterator[str]:
    """A Custom-tier realm of user A, made by the product create. Removed at the end."""
    _create(host, REALM, Tier.CUSTOM)
    try:
        yield REALM
    finally:
        _drop(host, REALM)


@pytest.fixture(scope="module")
def traversable(host: Host) -> Iterator[None]:
    """Open the path down to A's Owned Path roots to traverse-only, so only their modes matter."""
    paths = host.paths
    opened = {host.base, *(root.parent for root in (paths.data, paths.config, paths.state))}
    saved = {p: stat.S_IMODE(p.stat().st_mode) for p in opened if p.exists()}
    try:
        for path in saved:
            path.chmod(0o711)
        yield
    finally:
        for path, mode in saved.items():
            with contextlib.suppress(OSError):
                path.chmod(mode)
        with contextlib.suppress(OSError):
            host.base.chmod(0o700)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _roots(host: Host) -> list[Path]:
    return [host.paths.data, host.paths.config, host.paths.state, host.paths.runtime]


# FR-025 -------------------------------------------------------------------------------------


@pytest.mark.fr("FR-025")
def test_fr025_ac1_b_does_not_see_or_enter_a_realm_of_a(
    host: Host, user_b: SecondUser, a_realm: str, traversable: None
) -> None:
    """B's list shows nothing of A, and every verb on A's realm name is GINN-E-NO-SUCH-REALM."""
    record = host.paths.realms / f"{a_realm}.json"
    before = record.read_bytes() if record.is_file() else None
    # B gets a realm of its own, so the list proves filtering and not just emptiness. The create
    # needs B's rootless Podman (sub-IDs) and the network; when it fails the positive half is
    # recorded as NOT RUN with the error, and the other checks still run.
    made = user_b.ginn("create", "debian", "--name", B_REALM, timeout=1800)
    b_realm = made.returncode == 0
    if not b_realm:
        host.results.record(
            "FR-025-B-own-realm",
            "NOT RUN",
            rc=made.returncode,
            error=(made.stdout + made.stderr)[-400:],
        )
    try:
        listing = user_b.ginn("--json", "list")
        with gate_item(
            host.results, "FR-025-AC1-list", {"rc": listing.returncode, "b_realm": b_realm}
        ):
            assert listing.returncode == 0, listing.stdout + listing.stderr
            said = listing.stdout + listing.stderr
            assert a_realm not in said
            if b_realm:
                assert B_REALM in said
        _b_verbs_and_unchanged(host, user_b, a_realm, record, before)
    finally:
        if b_realm:
            user_b.ginn("delete", B_REALM, "--yes", timeout=600)


def _b_verbs_and_unchanged(
    host: Host, user_b: SecondUser, a_realm: str, record: Path, before: bytes | None
) -> None:
    verbs = [
        ["enter", a_realm],
        ["exec", a_realm, "--", "true"],
        ["start", a_realm],
        ["stop", a_realm],
        ["status", a_realm],
        ["delete", a_realm, "--yes"],
    ]
    failures: list[str] = []
    for argv in verbs:
        ran = user_b.ginn(*argv)
        said = ran.stdout + ran.stderr
        if ran.returncode != EXIT_NOT_FOUND or NO_SUCH not in said:
            failures.append(f"{' '.join(argv)}: rc={ran.returncode} out={said.strip()[:300]!r}")
    with gate_item(host.results, "FR-025-AC1-verbs", {"failures": failures}):
        assert not failures, "\n".join(failures)
    # A's side is untouched: the record is the same bytes and the realm is still there.
    with gate_item(host.results, "FR-025-A-unchanged"):
        assert _load(host, a_realm).name == RealmName(a_realm)
        if before is not None:
            assert record.read_bytes() == before


@pytest.mark.fr("FR-025")
def test_fr025_a_owned_path_roots_and_records_are_private_to_a(
    host: Host, user_b: SecondUser, a_realm: str, traversable: None
) -> None:
    """The modes ginn sets: roots and owned directories 0700, owned files 0600, all A-owned."""
    paths = host.paths
    bad: list[str] = []
    for directory in {*paths.owned_directories(), paths.runtime}:
        if directory.is_dir() and (
            _mode(directory) != OWNED_DIR_MODE or _owner(directory) != host.uid
        ):
            bad.append(f"{directory}: mode {_mode(directory):03o} uid {_owner(directory)}")
    files = [paths.audit_log, *sorted(paths.realms.glob("*.json"))]
    for file in files:
        if _mode(file) != OWNED_FILE_MODE or _owner(file) != host.uid:
            bad.append(f"{file}: mode {_mode(file):03o} uid {_owner(file)}")
    with gate_item(host.results, "FR-025-modes", {"bad": bad, "files": len(files)}):
        assert paths.audit_log.is_file(), "the product create left no audit log"
        assert files[1:], "no realm record was found"
        assert not bad, "\n".join(bad)


@pytest.mark.fr("FR-025")
def test_fr025_b_cannot_list_read_or_change_a_owned_paths_or_sockets(
    host: Host, user_b: SecondUser, a_realm: str, traversable: None
) -> None:
    """Real attempts as B. Each fails with a permission error, not because a path is absent."""
    paths = host.paths
    record = next(iter(sorted(paths.realms.glob("*.json"))))
    home = paths.homes / a_realm
    probe = paths.runtime / "fr025-probe.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sockets = [probe, *_product_sockets(paths.runtime)]
    try:
        listener.bind(str(probe))
        listener.listen(1)
        attempts: list[tuple[str, list[str]]] = []
        for root in _roots(host):
            attempts.append((f"list {root}", ["ls", "--", str(root)]))
        attempts += [
            ("list realm home", ["ls", "--", str(home)]),
            ("read realm record", ["cat", "--", str(record)]),
            ("overwrite realm record", ["sh", "-c", 'echo x > "$1"', "sh", str(record)]),
            ("create in realms", ["touch", "--", str(paths.realms / "intruder.json")]),
            ("delete realm record", ["rm", "-f", "--", str(record)]),
            ("create in data", ["mkdir", "--", str(paths.data / "intruder")]),
            ("create in runtime", ["touch", "--", str(paths.runtime / "intruder")]),
        ]
        for sock in sockets:
            connect = "import socket,sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])"
            attempts.append((f"connect {sock.name}", [sys.executable, "-c", connect, str(sock)]))
        failures: list[str] = []
        for label, argv in attempts:
            ran = user_b.run(argv)
            if ran.returncode == 0 or DENIED not in ran.stderr:
                failures.append(f"{label}: rc={ran.returncode} err={ran.stderr.strip()[:200]!r}")
    finally:
        listener.close()
        with contextlib.suppress(OSError):
            probe.unlink()
    with gate_item(
        host.results, "FR-025-access", {"attempts": len(attempts), "failures": failures}
    ):
        assert not failures, "\n".join(failures)
    assert not (paths.realms / "intruder.json").exists()
    assert not (paths.data / "intruder").exists()


def _owner(path: Path) -> int:
    return path.lstat().st_uid


def _product_sockets(root: Path) -> list[Path]:
    found: list[Path] = []
    for base, _dirs, names in os.walk(root, onerror=lambda _e: None):
        for name in names:
            candidate = Path(base) / name
            with contextlib.suppress(OSError):
                if stat.S_ISSOCK(candidate.lstat().st_mode):
                    found.append(candidate)
    return found


# NFR-SEC-008 --------------------------------------------------------------------------------


@pytest.mark.fr("NFR-SEC-008")
def test_nfr_sec_008_b_cannot_read_or_write_the_audit_log_of_a(
    host: Host, user_b: SecondUser, a_realm: str, traversable: None
) -> None:
    """Modes, then real attempts as B on the log, its rotations and its lock; A's log is intact."""
    paths = host.paths
    log = paths.audit_log
    before = log.read_bytes()
    targets = [log, log.with_name(log.name + ".1"), audit_lock_path(paths)]
    existing = [p for p in targets if p.exists()]
    failures: list[str] = []
    for target in existing:
        if _mode(target) & 0o077:
            failures.append(f"{target}: mode {_mode(target):03o} grants group or other access")
    for target in targets:
        for label, argv in (
            ("read", ["cat", "--", str(target)]),
            ("append", ["sh", "-c", 'echo x >> "$1"', "sh", str(target)]),
            ("truncate", ["sh", "-c", ': > "$1"', "sh", str(target)]),
            ("delete", ["rm", "-f", "--", str(target)]),
        ):
            ran = user_b.run(argv)
            if ran.returncode == 0 or DENIED not in ran.stderr:
                failures.append(
                    f"{label} {target.name}: rc={ran.returncode} err={ran.stderr.strip()[:200]!r}"
                )
    with gate_item(
        host.results, "NFR-SEC-008-other-user", {"checked": len(existing), "bad": failures}
    ):
        assert log in existing
        assert not failures, "\n".join(failures)
        assert log.read_bytes() == before, "the audit log changed during B's attempts"
        assert _mode(log) == OWNED_FILE_MODE
        assert _owner(log) == host.uid


@pytest.mark.fr("NFR-SEC-008")
def test_nfr_sec_008_a_custom_realm_with_every_grant_cannot_reach_or_change_the_log(
    host: Host, a_realm: str
) -> None:
    """From inside, as realm root, with all grants that need no package or hardware (T6.8 set)."""
    paths = host.paths
    log = paths.audit_log
    session = HostUser(uid=host.uid, host_home=host.host_home, host_env=host.session_env())
    documents = host.host_home / "Documents"
    documents.mkdir(parents=True, exist_ok=True)
    set_tier(host.env, RealmName(a_realm), Tier.CUSTOM)
    # Every toggle except the ones that need a host desktop session (display, audio, gpu: the gate's
    # session directory is empty) or an agent socket (agent), and kvm when the host has no /dev/kvm.
    # None of the left-out grants opens a path to the host's state.
    left_out = {Toggle.DISPLAY, Toggle.AUDIO, Toggle.GPU, Toggle.AGENT}
    if not os.path.exists("/dev/kvm"):
        left_out.add(Toggle.KVM)
    wanted = set(Toggle) - left_out
    current = set(_load(host, a_realm).permissions.toggles)
    set_permissions(
        host.env,
        RealmName(a_realm),
        session,
        enable=sorted(t.value for t in wanted - current),
        share=[(str(documents), "rw")],
    )
    # DR-004: a share that would expose the log is refused by the Owned-Path rule (not merely by
    # the allowed-roots rule), and the filesystem root by its own rule.
    exposing = [log.parent, paths.state, paths.data, paths.runtime, paths.state.parent, host.base]
    refused: list[str] = []
    cases = [(path, "DR-004.owned") for path in exposing] + [(Path("/"), "DR-004.root")]
    for path, rule in cases:
        try:
            set_permissions(host.env, RealmName(a_realm), session, share=[(str(path), "rw")])
        except GinnError as err:
            said = f"{err} {err.details}"
            if err.code != "GINN-E-PATH-FORBIDDEN" or rule not in said:
                refused.append(f"{path}: refused as {err.code} without {rule}: {said[:200]}")
        else:
            refused.append(f"{path}: the share was accepted")
    # The default layout puts the Owned Paths under the home, which is an allowed root, so there the
    # Owned-Path rule is the only thing that refuses `~/.local`.
    home_owned = [
        host.host_home / ".local" / "share" / "ginn",
        host.host_home / ".local" / "state" / "ginn",
        host.host_home / ".config" / "ginn",
    ]
    for path in (host.host_home / ".local", host.host_home / ".local" / "state"):
        try:
            SharedPath.parse(
                str(path), home=host.host_home, owned_paths=home_owned, mode="rw", env={}
            )
        except GinnError as err:
            if "DR-004.owned" not in f"{err} {err.details}":
                refused.append(f"{path}: default layout refused without DR-004.owned: {err}")
        else:
            refused.append(f"{path}: accepted in the default layout")
    shared = [share.path for share in _load(host, a_realm).permissions.shares]
    leaked = [s for s in shared if str(log).startswith(s.rstrip("/") + "/") or s == str(log)]
    with gate_item(host.results, "NFR-SEC-008-shares", {"bad": refused, "shared": shared}):
        assert not refused, "\n".join(refused)
        assert not leaked, f"a recorded share covers the audit log: {leaked}"

    before = log.read_bytes()
    record = _start(host, a_realm)
    container = record.container_id or ""
    target = ExecTarget(realm=a_realm, services=False, mutating=False)

    def as_root(argv: list[str]) -> tuple[int, str]:
        done = host.cli.exec(container, argv, target=target, user="0:0")
        return done.exit_code, done.stdout + done.stderr

    problems: list[str] = []
    try:
        for check in (log.parent, log, paths.state, paths.data, paths.runtime):
            code, said = as_root(["/usr/bin/test", "-e", str(check)])
            if code == 0:
                problems.append(f"{check} exists inside the realm")
        for label, script in (
            ("append", 'echo tampered >> "$1"'),
            ("truncate", ': > "$1"'),
        ):
            code, said = as_root(["/bin/sh", "-c", script, "sh", str(log)])
            if code == 0:
                problems.append(f"{label} of the log succeeded inside the realm: {said!r}")
        code, listing = as_root(
            [
                "/bin/sh",
                "-c",
                "find / -name 'audit.log*' -not -path '/proc/*' -not -path '/sys/*' 2>/dev/null",
            ]
        )
        if listing.strip():
            problems.append(
                f"an audit.log* file exists inside the realm: {listing.strip()[:300]!r}"
            )
    finally:
        _stop(host, a_realm)
    after = log.read_bytes()
    with gate_item(
        host.results, "NFR-SEC-008-realm", {"bad": problems, "grew": len(after) - len(before)}
    ):
        assert not problems, "\n".join(problems)
        assert after.startswith(before), "old audit log bytes changed (append-only broken)"
        assert b"tampered" not in after
        assert _mode(log) == OWNED_FILE_MODE
