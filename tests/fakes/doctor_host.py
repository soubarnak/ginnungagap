# SPDX-License-Identifier: Apache-2.0
"""A fake Void host for `ginn doctor` tests (FR-007, NFR-041). Test use only.

`build_host` lays out a fully configured host under a temporary root: subordinate IDs, the shared
directories, the runtime directory, the service and supervise links, the cgroup tree, /proc,
/dev/dri and ginn's podman config. Every check of a fully configured host passes, so a test breaks
one piece of the tree and then runs one check. Tests run as a user, so they cannot create
root-owned paths. `owner_fake` returns uid 0 for the paths that Host Setup makes root-owned, and
`GIB` is the unit for the disk injection.

`install_podman` puts a podman wrapper on a PATH directory. The wrapper runs the replay shim in
tests/fakes/podman_bin, which never runs a real podman. No code here runs podman or writes outside
the directories it is given.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

from ginn.doctor.checks import Identity, Probe

REPO = Path(__file__).resolve().parents[2]
SHIM = REPO / "tests" / "fakes" / "podman_bin" / "podman"
FIXTURES = REPO / "tests" / "fixtures" / "podman"
USER = "alice"
UID = os.getuid()
GID = os.getgid()
GIB = 1024**3
CONTROLLERS = "cpuset cpu io memory pids\n"
SHARED = ("etc/ginn", "etc/sv", "run/ginn", "run/runit", "sys/fs/cgroup/ginn")
DEFAULT_DIR = "etc/runit/runsvdir/default"
"""The runsvdir default directory, which holds the service link (ADR-013 Decision 10)."""
SUPERVISOR = f"sys/fs/cgroup/ginn/{UID}/supervisor"


def _write(path: Path, data: str | bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)


def _dir(path: Path, mode: int = 0o755) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def _link(path: Path, target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, path)


def _controllers(node: Path, text: str = CONTROLLERS) -> None:
    _dir(node)
    _write(node / "cgroup.controllers", text)
    _write(node / "cgroup.subtree_control", text)


def build_host(root: Path, home: Path, user: str = USER, uid: int = UID) -> None:
    """Lay out a fully configured host under `root`, and the user's directories under `home`."""
    sv = f"etc/sv/ginn-{user}"
    _write(root / "etc/subuid", f"{user}:524288:524288\n")
    _write(root / "etc/subgid", f"{user}:524288:524288\n")
    for rel in SHARED:
        _dir(root / rel)
    _dir(root / f"run/ginn/{uid}", 0o700)
    _write(root / f"{sv}/run", "#!/bin/sh\nexec true\n", 0o755)
    _write(root / f"{sv}/log/run", "#!/bin/sh\nexec true\n", 0o755)
    _link(root / f"{sv}/supervise", f"/run/runit/supervise.ginn-{user}")
    _link(root / f"{sv}/log/supervise", f"/run/runit/supervise.ginn-{user}-log")
    # The Void chain: var/service -> runsvdir/current -> default (ADR-013 Decision 10).
    _link(root / "var/service", "../run/runit/runsvdir/current")
    _dir(root / "run/runit/runsvdir")
    _link(root / "run/runit/runsvdir/current", "/etc/runit/runsvdir/current")
    _dir(root / "etc/runit/runsvdir/default")
    _link(root / "etc/runit/runsvdir/current", "default")
    _link(root / f"etc/runit/runsvdir/default/ginn-{user}", f"/etc/sv/ginn-{user}")
    _controllers(root / "sys/fs/cgroup")
    _controllers(root / "sys/fs/cgroup/ginn")
    _controllers(root / f"sys/fs/cgroup/ginn/{uid}")
    _write(root / f"sys/fs/cgroup/ginn/{uid}/cgroup.procs", "")
    _write(root / f"sys/fs/cgroup/ginn/{uid}/cgroup.threads", "")
    _dir(root / SUPERVISOR)
    _write(root / f"{SUPERVISOR}/cgroup.procs", "")
    _write(root / f"{SUPERVISOR}/cgroup.threads", "")
    _write(root / "proc/sys/user/max_user_namespaces", "15000\n")
    _write(root / "proc/sys/kernel/unprivileged_userns_clone", "1\n")
    _write(root / "proc/filesystems", "nodev\tproc\n\toverlay\n")
    _write(
        root / "proc/4242/status",
        f"Name:\trunsvdir\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
    )
    _write(root / "proc/4242/cmdline", b"runsvdir\x00-P\x00/state/ginn/sv\x00")
    _write(root / "etc/elogind/logind.conf", "[Login]\n#KillUserProcesses=no\n")
    _write(root / "dev/dri/renderD128", "", 0o666)
    _dir(home / ".config/ginn")
    _write(home / ".config/ginn/containers.conf", "[engine]\n")
    _write(home / ".config/ginn/storage.conf", "[storage]\n")
    _dir(home / ".local/share/ginn/image-tmp")


def owned_by_root(root: Path, uid: int = UID) -> frozenset[Path]:
    """The host paths that Host Setup and the run script make root-owned (ADR-003, ADR-013)."""
    rels = [
        *SHARED,
        DEFAULT_DIR,
        SUPERVISOR,
        f"{SUPERVISOR}/cgroup.procs",
        f"{SUPERVISOR}/cgroup.threads",
    ]
    return frozenset(root / rel for rel in rels)


def owner_fake(root: Path, uid: int = UID) -> Callable[[Path], int]:
    """An owner function for the fake tree: root for root-owned paths, the test user otherwise."""
    owned = owned_by_root(root, uid)

    def owner(path: Path) -> int:
        return 0 if path in owned else os.lstat(path).st_uid

    return owner


def doctor_env(home: Path, bin_dir: Path) -> dict[str, str]:
    """The environment doctor sees: its own HOME and XDG directories, and PATH on bin_dir only."""
    return {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "PATH": str(bin_dir),
    }


def make_probe(root: Path, home: Path, bin_dir: Path, *, free: int = 50 * GIB) -> Probe:
    """A Probe over the fake tree, with the root owner and the free space injected."""
    identity = Identity(user=USER, uid=UID, gid=GID, home=str(home))
    return Probe(
        root=root,
        env=doctor_env(home, bin_dir),
        identity=identity,
        owner_of=owner_fake(root),
        free_bytes=lambda _path: free,
    )


HELPERS = ("newuidmap", "newgidmap", "crun", "conmon", "pasta")
"""The podman helpers that doctor looks for on PATH (DEP-1)."""


def install_podman(bin_dir: Path, fixtures: Path = FIXTURES, env_log: Path | None = None) -> None:
    """Put a podman wrapper for the replay shim, and the helpers, on `bin_dir`.

    The wrapper sets the fixture directory and runs the shim with this interpreter. When `env_log`
    is given, the wrapper first writes its environment there, so a test can read what podman was
    given. The helpers are stubs: doctor only looks for them on PATH.
    """
    # printf is a shell builtin: the test PATH holds only the bin directory, so no `env` runs.
    dump = (
        "printf 'XDG_RUNTIME_DIR=%s\\nCONTAINERS_CONF=%s\\nCONTAINERS_STORAGE_CONF=%s\\n"
        'TMPDIR=%s\\n\' "$XDG_RUNTIME_DIR" "$CONTAINERS_CONF" "$CONTAINERS_STORAGE_CONF" '
        f'"$TMPDIR" > {shlex.quote(str(env_log))}\n'
        if env_log is not None
        else ""
    )
    wrapper = bin_dir / "podman"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"GINN_FAKE_PODMAN_FIXTURES={shlex.quote(str(fixtures))}\n"
        "export GINN_FAKE_PODMAN_FIXTURES\n"
        f"{dump}"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(SHIM))} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    for helper in HELPERS:
        stub = bin_dir / helper
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)


def snapshot(root: Path) -> dict[str, tuple[str, int, str]]:
    """Every path under `root` with its kind, mode and content or link target, lstat-based."""
    found: dict[str, tuple[str, int, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            info = os.lstat(path)
            mode = info.st_mode & 0o7777
            if os.path.islink(path):
                found[str(path)] = ("link", mode, os.readlink(path))
            elif path.is_dir():
                found[str(path)] = ("dir", mode, "")
            else:
                found[str(path)] = ("file", mode, path.read_bytes().hex())
    return found
