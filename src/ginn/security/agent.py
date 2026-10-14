# SPDX-License-Identifier: Apache-2.0
"""The Agent Proxy of a Development realm: SSH agent access through a filter, and the GPG agent in
restricted mode (FR-062 AC-1, FR-067, NFR-SEC-007).

The SSH side has two parts. `ginn-agent-filter` (native/ginn-agent-filter.c) listens on a host
socket that the realm sees. It passes only the requests that list keys and sign (types 11 and 13),
and it refuses the rest with a failure, so `ssh-add -D` and key import do not reach the host agent
(FR-067 AC-1). The realm sees the filter's socket at a fixed name, `/run/user/<uid>/ginn-ssh-agent`,
and `SSH_AUTH_SOCK` points there at exec. The GPG side has no filter. The host gpg-agent's extra
socket is already the restricted socket of the agent (sign and decrypt, no key export or import),
and its protocol is Assuan text lines, not the framing the filter reads. ginn binds that socket
at `/run/user/<uid>/gnupg/S.gpg-agent`, the path gpg looks at in a realm (FR-067 AC-2).

Both sockets are variable grants. They are attached after `podman start` (ADR-008), so this module
adds no create-time option, and TierExtras stays empty for the agent.

The filter runs while at least one Development realm runs, and only when the host has an SSH agent
to filter (FR-067, last sentence). `decide` is the rule. Each ginn command is a new process, so the
filter's state is an `AgentRecord` that the caller stores with the realm records. `sync` takes the
stored record, and returns the record to store again.

Lock contract: the caller holds the host-wide lock (`store.locks.global_lock`) from reading the
record until it has written the record that `sync` returned. Two commands that run `sync` without
that lock can each start a filter. The live-socket check in `sync` stops a second start on the same
socket, but it does not replace the lock.

Pure planning: `plan_realm` reads the host environment and calls `file_type`, which tests replace.
It starts nothing. Only `sync` starts or stops a process, through the injected launcher.
"""

from __future__ import annotations

import enum
import os
import stat
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from ginn.errors import GinnError, make
from ginn.runtime.env import FileType
from ginn.runtime.mounts import SessionBind, Skipped, runtime_dir
from ginn.security._proc import (
    Child,
    Launcher,
    Proc,
    host_file_type,
    listening,
    peer_pid,
    start_ready,
    stop_group,
)

SSH_NAME: Final = "ginn-ssh-agent"
"""The filter's socket name in the realm, under the runtime directory (fixed, ADR-008)."""
GPG_PARENT: Final = "gnupg"
GPG_NAME: Final = "S.gpg-agent"
"""The gpg socket's directory and name in the realm. gpg reads it from XDG_RUNTIME_DIR."""


@dataclass(frozen=True)
class FilterSpec:
    """The filter helper for one host agent. `argv` starts it, and `listen` is the host socket."""

    upstream: str
    listen: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class AgentRecord:
    """The filter helper that a ginn command started, as the realm records keep it.

    `proc` is None when no helper runs. `upstream` and `listen` are the spec the helper was started
    with. When a later spec differs (SSH_AUTH_SOCK changes after a re-login), `sync` restarts the
    helper, so a stale upstream is never kept. `members` names the realms that need the filter, and
    the helper stops when none is left (FR-067).
    """

    proc: Proc | None = None
    upstream: str = ""
    listen: str = ""
    members: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "proc": None if self.proc is None else self.proc.to_dict(),
            "upstream": self.upstream,
            "listen": self.listen,
            "members": list(self.members),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AgentRecord:
        """Read the record back. A malformed value raises, so a bad record starts no helper."""
        proc, upstream, listen = data.get("proc"), data.get("upstream"), data.get("listen")
        members = data.get("members", [])
        if not isinstance(upstream, str) or not isinstance(listen, str):
            raise ValueError("an agent record needs the upstream and the listen path as text")
        if not isinstance(members, list) or not all(isinstance(name, str) for name in members):
            raise ValueError("an agent record needs its members as a list of realm names")
        return cls(
            None if proc is None else Proc.from_dict(proc),
            upstream,
            listen,
            tuple(members),
        )


@dataclass(frozen=True)
class AgentPlan:
    """What a Development realm gets from the Agent Proxy. Pure data.

    `binds` are the attach entries, at fixed realm names. `parents` are realm directories that the
    attach must make first. `env` is the exec environment. `filter` is the helper to run, or None
    when the host has no SSH agent. `skipped` says why a grant is absent (FR-064 style).
    """

    binds: list[SessionBind] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    filter: FilterSpec | None = None
    skipped: list[Skipped] = field(default_factory=list)


class Action(enum.Enum):
    """What `sync` did, or what `decide` asks for: start, stop, restart, or leave it as it is.

    LEFT is a listener that this user's filter does not own: it is left alone, and the caller warns.
    """

    START = "start"
    STOP = "stop"
    RESTART = "restart"
    KEEP = "keep"
    IDLE = "idle"
    LEFT = "left"


GPGCONF_ARGV: Final[tuple[str, ...]] = ("gpgconf", "--list-dirs", "agent-extra-socket")
"""The argv for the host gpg-agent extra socket. The caller resolves `gpgconf` on PATH with
shutil.which, then runs the argv as a list, never through a shell."""


def extra_socket_from(output: str) -> str | None:
    """The extra socket path from `gpgconf --list-dirs agent-extra-socket`, or None.

    gpgconf prints one absolute path per line. A blank or relative answer gives None, so the
    caller records a skip and never guesses the directory (GnuPG hashes non-default homedirs).
    """
    first = output.strip().splitlines()[:1]
    if not first or not first[0].startswith("/"):
        return None
    return first[0]


def realm_paths(uid: int) -> tuple[str, str]:
    """The realm-side paths of the SSH filter socket and the gpg socket, for user `uid`."""
    runtime = runtime_dir(uid)
    return f"{runtime}/{SSH_NAME}", f"{runtime}/{GPG_PARENT}/{GPG_NAME}"


def plan_realm(
    host_env: Mapping[str, str],
    *,
    uid: int,
    helper: str,
    listen: str,
    gpg_extra: str | None,
    file_type: FileType = host_file_type,
) -> AgentPlan:
    """The Agent Proxy grants for one realm that has the agent toggle (FR-067).

    The caller decides who gets the grant (`agent_enabled` in runtime/realm_security.py).

    `listen` is the host path of the filter socket, which the filter helper creates. `helper` is
    the absolute path of ginn-agent-filter. `gpg_extra` is the host gpg-agent extra socket, as the
    caller resolved it with `gpgconf --list-dirs agent-extra-socket`, or None when there is none.
    A missing host agent is a skip and never a failure (ADR-008). Malformed input raises.
    """
    if not os.path.isabs(helper) or not os.path.isabs(listen):
        raise ValueError("the filter helper and its socket are absolute paths")
    ssh_realm, gpg_realm = realm_paths(uid)
    upstream = host_env.get("SSH_AUTH_SOCK", "")
    ssh = _ssh(upstream, file_type, helper, listen, ssh_realm)
    gpg = _gpg(gpg_extra, file_type, gpg_realm)
    return AgentPlan(
        binds=[*ssh.binds, *gpg.binds],
        parents=[*ssh.parents, *gpg.parents],
        env={**ssh.env, **gpg.env},
        filter=ssh.filter,
        skipped=[*ssh.skipped, *gpg.skipped],
    )


def _ssh(upstream: str, file_type: FileType, helper: str, listen: str, realm: str) -> AgentPlan:
    """The SSH part of the plan. A missing host agent is a skip that a later start may retry.

    A missing filter helper is a skip too (ADR-008): the realm starts with no SSH grant, and the
    start warns (GINN-W-AGENT-FILTER). The helper is looked up the same way as the sockets.
    """
    if not upstream:
        return AgentPlan(skipped=[_skip("SSH agent", "SSH_AUTH_SOCK is not set", "", "", False)])
    if file_type(helper) is None:
        return AgentPlan(skipped=[_skip("SSH agent", HELPER_MISSING, "", "", False)])
    kind = file_type(upstream)
    if kind is None:
        skip = _skip("SSH agent", "the host agent socket is missing", listen, realm, True)
        return AgentPlan(skipped=[skip])
    if kind != stat.S_IFSOCK:
        skip = _skip("SSH agent", "SSH_AUTH_SOCK is not a socket", "", "", False)
        return AgentPlan(skipped=[skip])
    # The filter socket is under ginn's runtime root, so it is admitted as ginn's own (CL-14).
    return AgentPlan(
        binds=[SessionBind(host=listen, realm=realm, internal=True)],
        env={"SSH_AUTH_SOCK": realm},
        filter=FilterSpec(upstream, listen, (helper, upstream, listen)),
    )


def _gpg(extra: str | None, file_type: FileType, realm: str) -> AgentPlan:
    """The gpg part of the plan. gpg-agent restricts its extra socket itself (FR-067 AC-2)."""
    if extra is None:
        return AgentPlan(
            skipped=[_skip("gpg agent", "no host gpg-agent extra socket", "", "", False)]
        )
    kind = file_type(extra)
    if kind is None:
        skip = _skip("gpg agent", "the gpg extra socket is missing", extra, realm, True)
        return AgentPlan(skipped=[skip])
    if kind != stat.S_IFSOCK:
        skip = _skip("gpg agent", "the gpg extra socket is not a socket", "", "", False)
        return AgentPlan(skipped=[skip])
    return AgentPlan(binds=[SessionBind(host=extra, realm=realm)], parents=[os.path.dirname(realm)])


HELPER_MISSING: Final = "the filter helper is not installed"
"""The skip reason of an SSH grant whose filter helper is missing (ADR-008, FR-067)."""


def _skip(name: str, reason: str, host: str, realm: str, missing: bool) -> Skipped:
    """A skip for the Agent Proxy. Only a missing source keeps the paths a retry needs."""
    return Skipped(kind="agent", name=name, reason=reason, host=host, realm=realm, missing=missing)


def decide(*, development_running: Collection[str], filter_running: bool, agent: bool) -> Action:
    """Start the filter while a Development realm runs and the host has an agent, else stop it.

    `development_running` is the set of running Development realms. `agent` says the host has an
    SSH agent socket to filter. The rule is the whole lifecycle. `sync` adds the restart, which
    needs the stored spec.
    """
    wanted = bool(development_running) and agent
    if wanted and not filter_running:
        return Action.START
    if not wanted and filter_running:
        return Action.STOP
    return Action.KEEP if filter_running else Action.IDLE


def _remove_stale_socket(path: str) -> None:
    """Remove a socket that a dead filter left behind. A socket that still listens is kept, and so
    is any other file at `path`."""
    if not path or listening(path):
        return
    try:
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            os.unlink(path)
    except FileNotFoundError:
        return


def _adopt(spec: FilterSpec) -> Proc | None:
    """The listener at `spec.listen` when it is this user's filter, started with this spec.

    The pid comes from SO_PEERCRED on the probe connection. The process must belong to this user,
    run the helper, and have exactly the spec's argv, so a listener from another agent is never
    adopted. Any other listener is left alone, and the caller warns (FR-067).
    """
    pid = peer_pid(spec.listen)
    if pid is None:
        return None
    proc = Proc.of(pid)
    if proc is None or os.stat(f"/proc/{pid}").st_uid != os.getuid():
        return None
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = [part.decode("utf-8", "surrogateescape") for part in handle.read().split(b"\0")]
    except OSError:
        return None
    if exe != os.path.realpath(spec.argv[0]) or argv[:-1] != list(spec.argv):
        return None
    return proc


def sync(
    record: AgentRecord,
    spec: FilterSpec | None,
    *,
    development_running: Collection[str],
    launch: Launcher,
) -> tuple[AgentRecord, Action]:
    """Bring the filter in line with its members and the host agent (FR-067). Return the new record.

    `development_running` is the membership: the names of the realms that need the filter. It is
    stored as the record's `members`, and the filter stops when it is empty. `record` is the stored
    state, and the caller stores the returned record in its place (see the lock contract in the
    module docstring). The cases:
    - a helper that the record names is dead: its pid is dropped, and the rule decides again;
    - `spec` is None (the caller has no SSH agent in its session): a running filter is kept while
      members remain, and no helper is started;
    - the helper runs with an upstream or socket other than the spec's: it is stopped and started
      again (RESTART). Other members re-attach at their next enter or launch (ADR-008);
    - a listener already serves the spec's socket: this filter is adopted when its pid, executable
      and argv match, and otherwise it is left alone (LEFT), never restarted or deleted;
    - the rule says STOP: the helper and its clients are stopped, and its socket is removed.
    """
    members = tuple(sorted(set(development_running)))
    running = record.proc is not None and record.proc.alive()
    if not running:
        record = replace(record, proc=None)
    wanted_key = None if spec is None else (spec.upstream, spec.listen)
    changed = running and wanted_key is not None and (record.upstream, record.listen) != wanted_key
    if spec is None and members and running:
        action = Action.KEEP
    elif changed and members:
        action = Action.RESTART
    else:
        action = decide(development_running=members, filter_running=running, agent=spec is not None)
    if action in (Action.STOP, Action.RESTART):
        if record.proc is not None:
            stop_group(record.proc)
        _remove_stale_socket(record.listen)
        record = AgentRecord(members=members)
    if action in (Action.START, Action.RESTART) and spec is not None:
        if listening(spec.listen):
            adopted = _adopt(spec)
            if adopted is None:
                return AgentRecord(members=members), Action.LEFT
            return AgentRecord(adopted, spec.upstream, spec.listen, members), Action.KEEP
        _remove_stale_socket(spec.listen)
        child = launch(spec.argv)
        return AgentRecord(child.proc, spec.upstream, spec.listen, members), action
    return replace(record, members=members), action


def filter_launcher(listen: str, log: str) -> Launcher:
    """The real launcher for the filter. `log` is the helper's stderr file (Owned state)."""

    def failure(detail: str) -> GinnError:
        return make("GINN-E-AGENT-FILTER", detail=detail)

    def launch(argv: Sequence[str]) -> Child:
        return start_ready(argv, listen, log, failure)

    return launch
