# SPDX-License-Identifier: Apache-2.0
"""Read-only host checks behind `ginn doctor` (FR-007, FR-069 AC-2, NFR-041, ADR-013 Decision 14).

Each check is a function from a Probe to one CheckResult. It reads host state under `Probe.root`,
which is `/` on a host and a temporary directory in tests. It never reads the Host Setup Record
(ADR-013 Decision 14). Every FAIL names at least one fix command (NFR-041). Doctor prints those
commands and runs none of them (FR-004). Each path in a fix command is quoted with shlex, so a
space or a metacharacter in a path stays one word when the user copies the line.

Doctor writes nothing. Its podman check runs `podman info` through the adapter with ginn's own
flags, and it does not call `OwnedPaths.ensure()`, so ginn creates no file. Podman itself may
create storage files under the Owned Paths.

Host paths in messages and fix commands are shown as the host sees them (`/run/ginn/1000`), with
the test root dropped (`Probe.show`).
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

from ginn.errors import GinnError
from ginn.hostsetup import subids
from ginn.hostsetup._common import SUBGID, SUBUID, supervise_links
from ginn.hostsetup.cgroups import (
    CONTROLLERS,
    DEFAULT_CGROUP_ROOT,
    GINN_DIR,
    ROOT_OWNED_LEAF_FILES,
    SUPERVISOR_LEAF,
    chown_paths,
    read_controllers,
    read_subtree_control,
)
from ginn.podman.cli import PodmanCli
from ginn.store.paths import OwnedPaths

GIB: Final = 1024**3
DISK_FAIL_BYTES: Final = 2 * GIB
"""Less free space than this fails the disk check. Ginn-defined, not an SRS number (CL-13)."""
DISK_WARN_BYTES: Final = 10 * GIB
"""Less free space than this warns. Ginn-defined, not an SRS number (CL-13)."""
USERNS_WANTED: Final = 15000
RENDER_MODE: Final = 0o666
"""Mode of the render nodes on the reference host, where basic GPU access works (S-06 section 2)."""
SHARED_DIRS: Final = ("etc/ginn", "etc/sv", "run/ginn", "run/runit", "sys/fs/cgroup/ginn")
"""Shared directories whose owner and mode doctor checks (ADR-013 Decisions 12 and 18)."""
CGROUP_REL: Final = str(DEFAULT_CGROUP_ROOT).lstrip("/")
SERVICE_CHAIN: Final = "var/service"
SERVICE_PARENT: Final = "etc/sv"
MAX_HOPS: Final = 40
SYSCTL_DROPIN: Final = "/etc/sysctl.d/99-ginn-userns.conf"
ELOGIND_TREES: Final = ("etc/elogind", "run/elogind", "usr/lib/elogind")
"""Elogind config trees, highest precedence first. A file in an earlier tree hides the same name
in a later one, as for systemd-style config (ADR-004)."""
ELOGIND_FIX_FILE: Final = "/etc/elogind/logind.conf.d/99-ginn.conf"
ELOGIND_DROPIN_DIR_HOST: Final = "/etc/elogind/logind.conf.d"
_TRUE_VALUES: Final = frozenset({"y", "t", "yes", "true", "on", "1"})
"""Values that systemd-style booleans read as yes."""
PODMAN_TOOLS: Final = {
    "newuidmap": "shadow",
    "newgidmap": "shadow",
    "crun": "crun",
    "conmon": "conmon",
}
"""Podman helpers that must be on PATH, each with the Void package that provides it (DEP-1)."""
NETWORK_TOOLS: Final = ("pasta", "slirp4netns")
"""Podman needs one of these for rootless networking (DEP-1). Void's `passt` package has pasta."""
RESTART_NOTE: Final = "restarting the supervisor stops services-on realms"
STOP_NOTE: Final = "stopping the supervisor stops services-on realms"
ELOGIND_NOTE: Final = "restarting elogind may end the graphical session"


class Status(StrEnum):
    """The outcome of one check. Only FAIL makes `ginn doctor` exit non-zero (FR-007 AC-1)."""

    PASS = "PASS"  # noqa: S105 - a status label, not a password
    WARN = "WARN"
    FAIL = "FAIL"
    INFO = "INFO"


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome. A FAIL must carry at least one fix command (NFR-041)."""

    name: str
    status: Status
    message: str
    fixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is Status.FAIL and not self.fixes:
            raise ValueError(f"check {self.name} fails without a fix command (NFR-041)")


@dataclass(frozen=True)
class Identity:
    """The account that runs doctor. Its uid owns the runtime directory and its subordinate IDs."""

    user: str
    uid: int
    gid: int
    home: str


def _lstat_owner(path: Path) -> int:
    return os.lstat(path).st_uid


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


@dataclass(frozen=True)
class Probe:
    """What a check may read: the root that stands for `/`, the environment and the account.

    `owner_of` and `free_bytes` can be injected. Tests need them, because a test runs as a user who
    cannot own a root-owned directory and cannot choose the free space of a disk. Both default to
    the host. The defaults are looked up at call time, so a test may also patch the module.
    """

    root: Path
    env: Mapping[str, str]
    identity: Identity
    owner_of: Callable[[Path], int] | None = None
    free_bytes: Callable[[Path], int] | None = None
    # Receives each podman argv that the podman check runs, for `ginn --verbose doctor` (NFR-042).
    verbose: Callable[[str], None] | None = None

    def at(self, rel: str) -> Path:
        """The path under the root for a host path such as `etc/subuid`."""
        return self.root / rel

    def show(self, path: Path) -> str:
        """The host path of `path`, with the root dropped, for messages and fix commands."""
        try:
            return "/" + str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def owner(self, path: Path) -> int:
        return (self.owner_of or _lstat_owner)(path)

    def free(self, path: Path) -> int:
        return (self.free_bytes or _disk_free)(path)

    def which(self, tool: str) -> str | None:
        """Find `tool` on the PATH of the environment. An empty PATH finds nothing."""
        path = self.env.get("PATH", "")
        return shutil.which(tool, path=path) if path else None


def _pass(name: str, message: str) -> CheckResult:
    return CheckResult(name, Status.PASS, message)


def _info(name: str, message: str) -> CheckResult:
    return CheckResult(name, Status.INFO, message)


def _warn(name: str, message: str, *fixes: str) -> CheckResult:
    return CheckResult(name, Status.WARN, message, fixes)


def _fail(name: str, message: str, *fixes: str) -> CheckResult:
    return CheckResult(name, Status.FAIL, message, fixes)


def _quote(value: str) -> str:
    """Quote one path or word for a fix line, so that it stays one word when copied."""
    return shlex.quote(value)


def _install(*packages: str) -> str:
    """A printed install line for the Void package manager. Doctor prints it and never runs it."""
    unique = list(dict.fromkeys(packages))
    return "sudo " + shlex.join(["xbps-install", "-S", *unique])


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _kind(path: Path) -> str | None:
    """The kind of `path` as lstat sees it: link, dir, file or other. None when absent."""
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return None
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _is_executable(path: Path) -> bool:
    """True when `path` is a regular file with an execute bit set (runit runs it as a script)."""
    return _kind(path) == "file" and bool(os.lstat(path).st_mode & 0o111)


def _service_name(p: Probe) -> str:
    return f"ginn-{p.identity.user}"


def _service_path(p: Probe) -> str:
    return f"/var/service/{_service_name(p)}"


def _restart_fix(p: Probe) -> str:
    return f"sudo sv restart {_quote(_service_path(p))}"


def _up_fix(p: Probe) -> str:
    return f"sudo sv up {_quote(_service_path(p))}"


def _host_setup_fix(p: Probe) -> str:
    return f"sudo ginn host-setup --user {_quote(p.identity.user)}"


def runtime_path(p: Probe) -> Path:
    """The runtime root, /run/ginn/<uid> (ADR-004). GINN_RUNTIME_DIR overrides it, as from_env."""
    override = p.env.get("GINN_RUNTIME_DIR", "")
    if override and os.path.isabs(override):
        return Path(override)
    return p.at(f"run/ginn/{p.identity.uid}")


def _gaps(node: Path) -> list[str]:
    """Controllers that `node` offers and does not enable for its children. Raises OSError."""
    offered = read_controllers(node) & CONTROLLERS
    return sorted(offered - read_subtree_control(node))


def _service_dir(p: Probe) -> str | None:
    """The root-relative directory that holds the service link, following `var/service`.

    A read-only copy of `setup._resolve_service_dir` (ADR-013 Decision 10). `var` itself must be a
    real directory, so a symbolic link there is refused. Each symbolic link after it is read with
    readlink and re-rooted, never followed by the kernel. A `..` that would leave the root, a chain
    longer than MAX_HOPS, a missing or unsafe part, and a final directory that root does not own
    all give None.
    """
    pending = list(PurePosixPath(SERVICE_CHAIN).parts)
    done: list[str] = []
    consumed = 0
    hops = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            if not done:
                return None
            done.pop()
            continue
        here = p.at("/".join([*done, part]))
        kind = _kind(here)
        if kind == "link" and consumed > 0:
            hops += 1
            if hops > MAX_HOPS:
                return None
            target = os.readlink(here)
            parts = list(PurePosixPath(target).parts)
            if target.startswith("/"):
                done = []
                parts = parts[1:]
            pending = parts + pending
            continue
        if kind != "dir":
            return None
        done.append(part)
        consumed += 1
    if not done:
        return None
    rel = "/".join(done)
    if p.owner(p.at(rel)) != 0:
        return None
    return rel


def _is_ginn_runsvdir(argv: list[str]) -> bool:
    """True for `runsvdir -P <state>/ginn/sv`, the ginn supervisor, and not another runsvdir."""
    if not argv or PurePosixPath(argv[0]).name != "runsvdir":
        return False
    return any(PurePosixPath(arg).parts[-2:] == ("ginn", "sv") for arg in argv[1:])


def _is_runsv_of(argv: list[str], service: str) -> bool:
    """True for `runsv <service>`, the process that runit starts to supervise one service."""
    return len(argv) >= 2 and PurePosixPath(argv[0]).name == "runsv" and argv[1] == service


def _proc_entries(p: Probe) -> list[Path]:
    """The numeric entries of the probe's /proc, in pid order. Empty when /proc cannot be listed."""
    proc = p.at("proc")
    try:
        names = sorted(os.listdir(proc))
    except OSError:
        return []
    return [proc / name for name in names if name.isdigit()]


def _argv_of(entry: Path) -> list[str] | None:
    """The argv of one process, from its cmdline, or None when it cannot be read."""
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _ppid_of(entry: Path) -> int | None:
    """The parent pid of one process, from /proc/<pid>/stat, or None.

    The command name may hold spaces and parentheses, so the fields are read after the last ')'.
    """
    stat = _read(entry / "stat")
    if stat is None or ")" not in stat:
        return None
    fields = stat.rsplit(")", 1)[1].split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    return int(fields[1])


def _find_process(
    p: Probe, wanted: Callable[[list[str]], bool], uid: int | None = None
) -> int | None:
    """The pid of the first process in /proc whose argv `wanted` accepts, or None.

    With `uid`, only processes that run as that user are considered. Reads are made under the
    probe root, so a test can fake /proc.
    """
    for entry in _proc_entries(p):
        if uid is not None:
            status = _read(entry / "status") or ""
            uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
            fields = uid_line.split()
            if len(fields) < 2 or fields[1] != str(uid):
                continue
        argv = _argv_of(entry)
        if argv is not None and wanted(argv):
            return int(entry.name)
    return None


def _child_argvs(p: Probe, parent: int) -> list[list[str]]:
    """The argv of each child of `parent`, in pid order. Unreadable children are skipped."""
    children: list[list[str]] = []
    for entry in _proc_entries(p):
        if _ppid_of(entry) != parent:
            continue
        argv = _argv_of(entry)
        if argv is not None:
            children.append(argv)
    return children


def _user_runsvdir(p: Probe) -> int | None:
    """The pid of the ginn supervisor's runsvdir that runs as the user, found in /proc, or None."""
    return _find_process(p, _is_ginn_runsvdir, uid=p.identity.uid)


def _is_run_script(argv: list[str], service: str) -> bool:
    """True for the run script of `service` as runsv starts it: `./run`, from the service directory.

    runsv starts the script by its relative name, and the kernel passes that name to the shebang
    interpreter, so it is an argument of the child's argv. The log script has the same argv until it
    execs svlogd, so a log child can match for the moment before that exec.
    """
    return any(arg == "./run" or arg.endswith(f"/{service}/run") for arg in argv)


def _service_up(p: Probe) -> bool:
    """True when runit runs the service's run script, which is the state the supervisor waits in.

    runit starts runsv for ginn-<user> as root, so only a root runsv counts. A runsv with no run
    script as its child is not up. `sv down` leaves runsv running with no child. runsv also restarts
    a run script that exited after a pause of about one second, so a check made in that second
    sees no child and reports the service down. A rerun shows the state that holds.
    """
    service = _service_name(p)
    pid = _find_process(p, lambda argv: _is_runsv_of(argv, service), uid=0)
    if pid is None:
        return False
    return any(_is_run_script(argv, service) for argv in _child_argvs(p, pid))


def _state_sv_dir(p: Probe) -> Path:
    """The realm services directory, <home>/.local/state/ginn/sv, that the run script waits for.

    setup's _render sets XDG_STATE_HOME to <home>/.local/state for the run script, whatever the
    environment says, so doctor reads the same path (ADR-013 Decision 14).
    """
    return Path(p.identity.home) / ".local" / "state" / "ginn" / "sv"


def _state_note(p: Probe) -> str:
    """A note for a message when doctor's own XDG_STATE_HOME names another state directory.

    OwnedPaths follows XDG_STATE_HOME, so ginn's own state may be there, and it is not the
    directory that this check reads. Empty when XDG_STATE_HOME is unset or is the default.
    """
    base = p.env.get("XDG_STATE_HOME", "")
    if not base or not os.path.isabs(base):
        return ""
    if Path(base) == Path(p.identity.home) / ".local" / "state":
        return ""
    return f" (doctor's XDG_STATE_HOME is {base}, so ginn's own state directory may be there)"


def _settings(text: str, key: str) -> list[str]:
    """The values of `key` in the [Login] section of one systemd-style file, in file order."""
    section = ""
    values: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        name, sep, value = line.partition("=")
        if sep and section == "Login" and name.strip() == key:
            values.append(value.strip())
    return values


def _elogind_files(p: Probe) -> list[Path]:
    """The files elogind reads, in order (ADR-004). systemd rules, which elogind follows.

    The main file is the first of ELOGIND_TREES that has one. Drop-ins are gathered from every tree,
    and a name found in an earlier tree hides the same name later. The survivors are sorted by name,
    so the last value read wins.
    """
    main = next(
        (
            path
            for path in (p.at(f"{tree}/logind.conf") for tree in ELOGIND_TREES)
            if _kind(path) == "file"
        ),
        None,
    )
    chosen: dict[str, Path] = {}
    for tree in ELOGIND_TREES:
        directory = p.at(f"{tree}/logind.conf.d")
        if _kind(directory) != "dir":
            continue
        for path in directory.glob("*.conf"):
            if _kind(path) == "file":
                chosen.setdefault(path.name, path)
    files = [] if main is None else [main]
    return [*files, *(chosen[name] for name in sorted(chosen))]


def _kill_user_processes(p: Probe) -> tuple[bool, str]:
    """The effective KillUserProcesses and the file that set it last (ADR-004).

    An unset key, or an empty value, counts as no. The booleans are those of _TRUE_VALUES.
    """
    effective = ""
    source = "no file sets it"
    for path in _elogind_files(p):
        for value in _settings(_read(path) or "", "KillUserProcesses"):
            effective = value
            source = p.show(path)
    return effective.lower() in _TRUE_VALUES, source


def _is_musl(p: Probe) -> bool:
    """True when the root holds a musl dynamic loader (ld-musl-*.so.1), in lib or usr/lib."""
    for rel in ("lib", "usr/lib"):
        directory = p.at(rel)
        if _kind(directory) == "dir" and any(directory.glob("ld-musl-*.so.1")):
            return True
    return False


def _owned_data_root(p: Probe) -> Path:
    """The Owned Paths data root, $XDG_DATA_HOME/ginn or ~/.local/share/ginn (DR-010)."""
    base = p.env.get("XDG_DATA_HOME", "")
    if base and os.path.isabs(base):
        return Path(base) / "ginn"
    home = Path(p.env.get("HOME") or p.identity.home)
    return home / ".local" / "share" / "ginn"


def _existing_ancestor(path: Path) -> Path:
    current = path
    while not os.path.lexists(current) and current != current.parent:
        current = current.parent
    return current


def check_podman(p: Probe) -> CheckResult:
    """Podman and its helpers are on PATH, and `podman info` runs with ginn's flags (DEP-1)."""
    name = "podman"
    if p.which("podman") is None:
        return _fail(name, "podman is not on PATH", _install("podman", "crun"))
    missing = [tool for tool in PODMAN_TOOLS if p.which(tool) is None]
    packages = [PODMAN_TOOLS[tool] for tool in missing]
    if not any(p.which(tool) is not None for tool in NETWORK_TOOLS):
        missing.append("pasta or slirp4netns")
        packages.append("passt")
    if missing:
        return _fail(
            name,
            f"podman helper not on PATH: {', '.join(missing)}",
            _install(*packages),
        )
    try:
        paths = OwnedPaths.from_env(p.env, uid=p.identity.uid, system_run=p.at("run"))
    except GinnError as err:
        return _info(name, f"not checked: {err.code}; see the runtime-dir check")
    configs = (paths.podman_containers_conf, paths.podman_storage_conf)
    if any(_kind(path) != "file" for path in configs) or _kind(paths.image_tmp) != "dir":
        return _warn(
            name,
            "ginn's podman config is not written yet; ginn writes it on its first podman call, "
            "so rootless podman is not verified yet",
        )
    cli = PodmanCli(
        paths.podman_root,
        paths.podman_runroot,
        tmpdir=paths.podman_tmpdir,
        image_tmp=paths.image_tmp,
        containers_conf=paths.podman_containers_conf,
        storage_conf=paths.podman_storage_conf,
        runtime_root=paths.runtime,
        verbose=p.verbose,
    )
    try:
        info = cli.info()
    except GinnError as err:
        detail = err.details.get("stderr") or err.details.get("detail") or err.message
        return _fail(
            name,
            f"podman info failed: {detail}",
            _install("podman", *PODMAN_TOOLS.values(), "passt"),
        )
    if not info.rootless:
        return _warn(
            name,
            "podman reports rootless as false; this line is a diagnostic from podman, not a "
            "fault ginn can repair",
        )
    return _pass(name, f"podman {info.version} runs rootless")


def check_subids(p: Probe) -> CheckResult:
    """Each of /etc/subuid and /etc/subgid holds one line of at least MIN_SIZE IDs (FR-007 AC-1)."""
    name = "subids"
    names = {p.identity.user, str(p.identity.uid)}
    problems: list[str] = []
    for rel in (SUBUID, SUBGID):
        entries = subids.parse_entries(_read(p.at(rel)) or "")
        if not any(e.name in names and e.count >= subids.MIN_SIZE for e in entries):
            problems.append(
                f"/{rel} has no single line of at least {subids.MIN_SIZE} IDs for {p.identity.user}"
            )
    if problems:
        return _fail(name, "; ".join(problems), _host_setup_fix(p))
    return _pass(
        name,
        f"/etc/subuid and /etc/subgid each hold one range of at least {subids.MIN_SIZE} IDs "
        f"for {p.identity.user}",
    )


def check_runtime_dir(p: Probe) -> CheckResult:
    """/run/ginn/<uid> exists, is a real directory owned by the user, with mode 0700 (ADR-004)."""
    name = "runtime-dir"
    path = runtime_path(p)
    shown = p.show(path)
    try:
        info = os.lstat(path)
    except OSError:
        return _fail(name, f"supervisor not running: {shown} is missing", _up_fix(p))
    mode = stat.S_IMODE(info.st_mode)
    owner = p.owner(path)
    if stat.S_ISLNK(info.st_mode):
        found = "a symbolic link"
    elif not stat.S_ISDIR(info.st_mode):
        found = "not a directory"
    elif owner != p.identity.uid or mode != 0o700:
        found = f"owner uid {owner}, mode {mode:04o}"
    else:
        return _pass(name, f"{shown} is owned by uid {p.identity.uid} with mode 0700")
    message = (
        f"{shown} must be a real directory owned by uid {p.identity.uid} with mode 0700; "
        f"found {found}; {RESTART_NOTE}"
    )
    if not stat.S_ISDIR(info.st_mode):
        # A chown or chmod as root would follow a symbolic link, so the path goes first.
        return _fail(name, message, f"sudo rm {_quote(shown)}", _restart_fix(p))
    return _fail(name, message, _restart_fix(p))


def check_runsvdir(p: Probe) -> CheckResult:
    """The ginn supervisor, a runsvdir on the user's sv directory, is running (FR-081, DEP-6)."""
    name = "runsvdir"
    pid = _user_runsvdir(p)
    if pid is None:
        return _runsvdir_down(p)
    return _pass(name, f"ginn supervisor runsvdir (pid {pid}) runs for {p.identity.user}")


def _runsvdir_down(p: Probe) -> CheckResult:
    """The result for a supervisor runsvdir that is not running, by the state of the service.

    A service that is not up (see `_service_up`) needs `sv up`, a FAIL. A service that is up, with
    its realm services directory missing, is an idle supervisor, a WARN. Host Setup does not create
    that directory, because it lies in the user's home, and the fix creates it as the user at 0700.
    A path that is there but is not a directory, a file or a link, is a FAIL, because the run script
    never finds the directory it waits for. Its fix removes the path and then creates the
    directory. A real directory with the service up means runsvdir stopped, and `sv up` does
    nothing to a running service, so the fix restarts the service (FAIL).

    The first ginn command that runs podman creates the directory at 0700 (OwnedPaths.ensure), but
    only when XDG_STATE_HOME is the default, so the message names a different XDG_STATE_HOME.
    """
    name = "runsvdir"
    user = p.identity.user
    down = f"the ginn supervisor (runsvdir on .../ginn/sv) is not running for {user}"
    if not _service_up(p):
        return _fail(name, down, _up_fix(p))
    sv_dir = _state_sv_dir(p)
    shown = p.show(sv_dir)
    kind = _kind(sv_dir)
    if kind is None:
        return _warn(
            name,
            f"the ginn supervisor is idle for {user}: its service is up, and the supervisor is "
            f"idle until the realm services directory {shown} exists; the first ginn command "
            f"that runs podman creates it{_state_note(p)}",
            f"mkdir -m 0700 -p {_quote(shown)}",
        )
    if kind != "dir":
        return _fail(
            name,
            f"{down}: its service is up, but {shown} is a {kind}, not a directory, so the run "
            f"script does not find the directory it waits for{_state_note(p)}",
            f"rm {_quote(shown)}",
            f"mkdir -m 0700 -p {_quote(shown)}",
        )
    return _fail(name, f"{down}, although its service is up; {RESTART_NOTE}", _restart_fix(p))


def check_runit_dir(p: Probe) -> CheckResult:
    """/run/runit exists, where runit keeps the supervise state (ADR-013 Decision 21)."""
    name = "runit-dir"
    if _kind(p.at("run/runit")) == "dir":
        return _pass(name, "/run/runit exists")
    return _fail(name, "/run/runit is missing", "sudo install -d -m 0755 /run/runit")


def check_sv_dir(p: Probe) -> CheckResult:
    """The service directory /etc/sv/ginn-<user> holds an executable run and log/run."""
    name = "sv-dir"
    base = p.at(f"etc/sv/{_service_name(p)}")
    missing = [part for part in ("run", "log/run") if not _is_executable(base / part)]
    if missing:
        return _fail(
            name,
            f"{p.show(base)} lacks an executable {' or '.join(missing)}",
            _host_setup_fix(p),
        )
    return _pass(name, f"{p.show(base)} holds an executable run and log/run")


def check_service_link(p: Probe) -> CheckResult:
    """The service link in the runsvdir default directory points at /etc/sv/ginn-<user>."""
    name = "service-link"
    want = f"/{SERVICE_PARENT}/{_service_name(p)}"
    service = _service_dir(p)
    if service is None:
        return _fail(
            name,
            "the /var/service chain does not resolve to a root-owned directory inside the root",
            _host_setup_fix(p),
        )
    link = p.at(f"{service}/{_service_name(p)}")
    kind = _kind(link)
    if kind is None:
        return _fail(name, f"{p.show(link)} is missing", _host_setup_fix(p))
    if kind != "link":
        return _fail(name, f"{p.show(link)} is a {kind}, not a symbolic link", _host_setup_fix(p))
    found = os.readlink(link)
    if found != want:
        return _fail(name, f"{p.show(link)} points at {found}, not {want}", _host_setup_fix(p))
    return _pass(name, f"{p.show(link)} points at {want}")


def check_supervise_links(p: Probe) -> CheckResult:
    """Both supervise links point into /run/runit, at the exact targets Host Setup writes.

    A real directory in place of a link is a leftover from a runsv that started before the link
    existed. `ln -s` would put the link inside that directory, so the fix stops the service, removes
    the directory and then makes the link.
    """
    name = "supervise-links"
    problems: list[str] = []
    fixes: list[str] = []
    stops_service = False
    for rel, want in supervise_links(p.identity.user).items():
        link = p.at(rel)
        shown = p.show(link)
        kind = _kind(link)
        if kind == "link" and os.readlink(link) == want:
            continue
        if kind == "dir":
            stops_service = True
            problems.append(f"{shown} is a real directory, not a link to {want}")
            fixes.append(f"sudo sv down {_quote(_service_path(p))}")
            fixes.append(f"sudo rm -r {_quote(shown)}")
            fixes.append(f"sudo ln -s {_quote(want)} {_quote(shown)}")
        else:
            problems.append(f"{shown} is not a link to {want}")
            fixes.append(f"sudo ln -sfn {_quote(want)} {_quote(shown)}")
    if problems:
        message = "; ".join(problems)
        if stops_service:
            message = f"{message}; {STOP_NOTE}"
        return _fail(name, message, *dict.fromkeys(fixes))
    return _pass(name, "both supervise links point into /run/runit")


def check_cgroup(p: Probe) -> CheckResult:
    """cgroup v2 is mounted, and ginn and ginn/<uid> enable the controllers (FR-080, ADR-003).

    Controllers that the root does not enable are tolerated and reported as WARN, because a refused
    root write is accepted at delegation (ADR-013 Decision 14, FR-001). Their tolerance comes from
    the current root subtree_control, not from the record.
    """
    name = "cgroup"
    cgroup = p.at(CGROUP_REL)
    if _kind(cgroup / "cgroup.controllers") != "file":
        return _fail(
            name,
            "cgroup v2 is not mounted at /sys/fs/cgroup",
            "sudo mount -t cgroup2 none /sys/fs/cgroup",
        )
    user_dir = cgroup / GINN_DIR / str(p.identity.uid)
    supervisor = user_dir / SUPERVISOR_LEAF
    problems: list[str] = []
    try:
        root_gaps = _gaps(cgroup)
        for node in (cgroup / GINN_DIR, user_dir):
            if _kind(node) != "dir":
                problems.append(f"{p.show(node)} is missing")
                continue
            gaps = _gaps(node)
            if gaps:
                problems.append(f"{p.show(node)} does not enable {', '.join(gaps)}")
        if _kind(supervisor) != "dir":
            problems.append(f"{p.show(supervisor)} is missing; the supervisor creates it at start")
    except OSError as exc:
        problems.append(f"cannot read the cgroup controllers: {exc.strerror or exc}")
        root_gaps = []
    if problems:
        return _fail(name, f"{'; '.join(problems)}; {RESTART_NOTE}", _restart_fix(p))
    if root_gaps:
        return _warn(
            name,
            f"the root does not enable {', '.join(root_gaps)}; those controllers are not "
            "delegated (tolerated, FR-001)",
        )
    return _pass(name, f"controllers are delegated under {p.show(user_dir)}")


def check_cgroup_owner(p: Probe) -> CheckResult:
    """ginn/<uid> and its files belong to the user, and the supervisor leaf stays root-owned.

    The leaf is root-owned by design (ADR-003). A user-owned leaf is fixed by a root chown. Paths
    that are missing are reported by the cgroup check, not here.
    """
    name = "cgroup-owner"
    cgroup = p.at(CGROUP_REL)
    uid = p.identity.uid
    user_paths = [path for path in chown_paths(cgroup, uid) if os.path.lexists(path)]
    supervisor = cgroup / GINN_DIR / str(uid) / SUPERVISOR_LEAF
    leaf_all = [supervisor, *(supervisor / leaf for leaf in ROOT_OWNED_LEAF_FILES)]
    leaf_paths = [path for path in leaf_all if os.path.lexists(path)]
    fixes: list[str] = []
    user_bad = [path for path in user_paths if p.owner(path) != uid]
    if user_bad:
        shown = " ".join(_quote(p.show(path)) for path in user_bad)
        fixes.append(f"sudo chown {uid}:{p.identity.gid} {shown}")
    leaf_bad = [path for path in leaf_paths if p.owner(path) != 0]
    if leaf_bad:
        shown = " ".join(_quote(p.show(path)) for path in leaf_bad)
        fixes.append(f"sudo chown root:root {shown}")
    if fixes:
        wrong = ", ".join(p.show(path) for path in (*user_bad, *leaf_bad))
        return _fail(name, f"delegated cgroup paths have the wrong owner: {wrong}", *fixes)
    return _pass(name, f"{p.show(cgroup / GINN_DIR / str(uid))} is owned by uid {uid}")


def check_shared_dirs(p: Probe) -> CheckResult:
    """Each shared directory that exists is a real root-owned directory, not writable by others.

    Host Setup's rerun does not check these modes (ADR-013 Decision 18), so doctor does. A missing
    directory is not a fault here: the supervisor creates some of them at its first start. A path
    that is not a directory is removed by hand, and then Host Setup runs again, because ADR-013
    Decision 19 refuses to repair it.
    """
    name = "shared-dirs"
    problems: list[str] = []
    fixes: list[str] = []
    present = 0
    for rel in SHARED_DIRS:
        path = p.at(rel)
        if not os.path.lexists(path):
            continue
        shown = p.show(path)
        if _kind(path) != "dir":
            problems.append(f"{shown} is not a real directory")
            fixes.append(f"sudo rm {_quote(shown)}")
            fixes.append(_host_setup_fix(p))
            continue
        present += 1
        owner = p.owner(path)
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        if owner != 0:
            problems.append(f"{shown} is owned by uid {owner}, not 0")
            fixes.append(f"sudo chown root:root {_quote(shown)}")
        if mode & 0o022:
            problems.append(f"{shown} is mode {mode:04o}, writable by group or others")
            fixes.append(f"sudo chmod 0755 {_quote(shown)}")
    if problems:
        return _fail(name, "; ".join(problems), *dict.fromkeys(fixes))
    if present == 0:
        return _info(name, "no shared directory exists yet")
    return _pass(
        name,
        f"all {present} existing shared directories are owned by root and not writable by others",
    )


def check_userns(p: Probe) -> CheckResult:
    """User namespaces are enabled: user.max_user_namespaces > 0, and the Debian switch if set."""
    name = "userns"
    fixes = (
        f"sudo sysctl -w user.max_user_namespaces={USERNS_WANTED}",
        f"echo 'user.max_user_namespaces = {USERNS_WANTED}' | sudo tee {_quote(SYSCTL_DROPIN)}",
    )
    raw = (_read(p.at("proc/sys/user/max_user_namespaces")) or "").strip()
    if not raw.isdigit() or int(raw) == 0:
        return _fail(
            name,
            "user namespaces are off (user.max_user_namespaces is 0 or missing)",
            *fixes,
        )
    clone = _read(p.at("proc/sys/kernel/unprivileged_userns_clone"))
    if clone is not None and clone.strip() != "1":
        return _fail(
            name,
            "kernel.unprivileged_userns_clone is not 1",
            "sudo sysctl -w kernel.unprivileged_userns_clone=1",
        )
    return _pass(name, f"user namespaces enabled (user.max_user_namespaces {int(raw)})")


def check_overlay_fuse(p: Probe) -> CheckResult:
    """The kernel offers overlay, or FUSE with fuse-overlayfs on PATH (DEP-2)."""
    name = "overlay-fuse"
    text = _read(p.at("proc/filesystems")) or ""
    names = {line.split()[-1] for line in text.splitlines() if line.strip()}
    if "overlay" in names:
        return _pass(name, "overlay filesystem is available")
    if "fuse" in names and p.which("fuse-overlayfs") is not None:
        return _pass(name, "FUSE overlay is available through fuse-overlayfs")
    return _fail(
        name,
        "neither overlay nor FUSE overlay is available",
        "sudo modprobe overlay",
        "sudo modprobe fuse",
    )


def check_disk(p: Probe) -> CheckResult:
    """Free space where the Owned Paths live is at least DISK_WARN_BYTES (FR-007, CL-13)."""
    name = "disk"
    data = _owned_data_root(p)
    probe = _existing_ancestor(data)
    free = p.free(probe)
    gib = free / GIB
    if free < DISK_FAIL_BYTES:
        return _fail(
            name,
            f"only {gib:.1f} GiB free where Owned Paths live ({data}); at least "
            f"{DISK_FAIL_BYTES // GIB} GiB is needed (ginn-defined, CL-13)",
            f"space must be freed: free at least {DISK_FAIL_BYTES // GIB} GiB on the filesystem "
            f"that holds {_quote(str(probe))}; df -h {_quote(str(probe))} shows the free space",
        )
    if free < DISK_WARN_BYTES:
        return _warn(name, f"{gib:.1f} GiB free under {data}; {DISK_WARN_BYTES // GIB} GiB advised")
    return _pass(name, f"{gib:.1f} GiB free under {data}")


def check_nvidia(p: Probe) -> CheckResult:
    """On musl, NVIDIA is informational and never a failure (FR-069 AC-2). On glibc, it passes."""
    name = "nvidia"
    if _is_musl(p):
        return _info(name, "NVIDIA unsupported on musl: no musl driver exists (FR-069 AC-2)")
    if _kind(p.at("proc/driver/nvidia/version")) == "file":
        return _pass(name, "NVIDIA driver is loaded (library injection for FR-069 lands in M6)")
    return _pass(name, "no NVIDIA driver is loaded; nothing to inject")


def check_elogind(p: Probe) -> CheckResult:
    """elogind does not end user processes at logout: KillUserProcesses is no (ADR-004)."""
    name = "elogind"
    yes, source = _kill_user_processes(p)
    if yes:
        return _fail(
            name,
            f"KillUserProcesses is yes ({source}); user processes end at logout and the runtime "
            f"directory with them (ADR-004); {ELOGIND_NOTE}",
            f"sudo mkdir -p {_quote(ELOGIND_DROPIN_DIR_HOST)}",
            f"printf '[Login]\\nKillUserProcesses=no\\n' | sudo tee {_quote(ELOGIND_FIX_FILE)} "
            ">/dev/null",
            "sudo sv restart elogind",
        )
    return _pass(name, f"KillUserProcesses is no ({source})")


def check_render_nodes(p: Probe) -> CheckResult:
    """Report the mode of each /dev/dri/renderD* node (S-06 section 2, M6 decides keep-groups)."""
    name = "render-nodes"
    directory = p.at("dev/dri")
    nodes = sorted(directory.glob("renderD*")) if _kind(directory) == "dir" else []
    if not nodes:
        return _info(name, "no render node under /dev/dri")
    modes = {p.show(node): stat.S_IMODE(os.lstat(node).st_mode) for node in nodes}
    listing = ", ".join(f"{shown} {mode:04o}" for shown, mode in modes.items())
    if any(mode != RENDER_MODE for mode in modes.values()):
        return _warn(
            name,
            f"render node modes: {listing}; a node not at 0666 needs keep-groups, which M6 decides",
        )
    return _pass(name, f"render node modes: {listing}")


ABSTRACT_BUS_WARNING: Final = (
    "session bus uses an abstract socket; realms on the host network can reach it unfiltered; "
    "FR-068 filtering is not in effect"
)
"""The doctor message for an abstract session bus (FR-068, AS-012, docs/clarifications.md CL-15)."""


def _has_abstract_member(address: str) -> bool:
    """True when a unix member of a session address names an abstract socket (AS-012)."""
    for member in address.split(";"):
        if not member.startswith("unix:"):
            continue
        if any(part.partition("=")[0] == "abstract" for part in member[len("unix:") :].split(",")):
            return True
    return False


def check_session_bus(p: Probe) -> CheckResult:
    """An abstract socket in the session bus address is reachable from a host-network realm.

    Every non-Kali realm shares the host network namespace (FR-026), so such a socket bypasses the
    FR-068 proxy. The check reads the environment only, and it is a WARN, never a FAIL.
    """
    name = "session-bus"
    address = p.env.get("DBUS_SESSION_BUS_ADDRESS", "")
    if _has_abstract_member(address):
        return _warn(name, ABSTRACT_BUS_WARNING)
    if not address:
        return _pass(name, "no session bus address; a realm gets no bus")
    return _pass(name, "the session bus is a path socket, which the FR-068 proxy filters")


CHECKS: Final[tuple[Callable[[Probe], CheckResult], ...]] = (
    check_podman,
    check_subids,
    check_runtime_dir,
    check_runsvdir,
    check_runit_dir,
    check_sv_dir,
    check_service_link,
    check_supervise_links,
    check_cgroup,
    check_cgroup_owner,
    check_shared_dirs,
    check_userns,
    check_overlay_fuse,
    check_disk,
    check_nvidia,
    check_elogind,
    check_render_nodes,
    check_session_bus,
)
"""Every check, in report order. Each NFR-041 item of Host Setup and of SRS section 2.5 has one."""


def run_checks(probe: Probe) -> list[CheckResult]:
    """Run every check against `probe` and return the results in report order."""
    return [check(probe) for check in CHECKS]
