# SPDX-License-Identifier: Apache-2.0
"""System runit service that runs a user's runsvdir (FR-001, FR-081, FR-084, NFR-001).

Host Setup renders etc/sv/ginn-<user>/{run,finish,log/run} and links it into var/service, so the
system runit starts it at boot. `run` runs as root. It creates /run/ginn/<uid>, creates the
delegated cgroup leaves (the same writes as cgroups.delegate_user_tree, S-01), moves itself into
the supervisor leaf, waits for the user's state directory, then drops to the user and execs
runsvdir. runsvdir is the process that stays resident, and NFR-001 allows it 1 MiB RSS.

Every value is rendered at setup time, so the scripts run no account lookups (no getent).
Each value is quoted with shlex.quote because the scripts run as root.

Golden files for the rendered scripts are in tests/fixtures/hostsetup/ginn-alice/.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ginn.hostsetup._atomic import atomic_write
from ginn.hostsetup.cgroups import (
    CLI_LEAF,
    CONTROLLERS,
    DEFAULT_CGROUP_ROOT,
    GINN_DIR,
    SUPERVISOR_LEAF,
    chown_groups,
)

SCRIPT_MODE = 0o755
SV_PARENT = "etc/sv"
LINK_PARENT = "var/service"
DEFAULT_RUNTIME_PARENT = "/run/ginn"
WAIT_SECONDS = 60
XDG_KEYS: tuple[str, ...] = ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME")
SCRIPT_NAMES: tuple[str, ...] = ("run", "finish", "log/run")

_ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_PLACEHOLDER = re.compile(r"@([A-Z_]+)@")


@dataclass(frozen=True)
class _Groups:
    """Cgroup paths and chown groups for one user, derived from cgroups.py constants."""

    ginn: str
    user: str
    supervisor: str
    cli: str


def _groups(uid: int) -> _Groups:
    ginn = DEFAULT_CGROUP_ROOT / GINN_DIR
    user = ginn / str(uid)
    return _Groups(
        ginn=str(ginn),
        user=str(user),
        supervisor=str(user / SUPERVISOR_LEAF),
        cli=str(user / CLI_LEAF),
    )


def render(
    user: str,
    uid: int,
    gid: int,
    home: str,
    xdg: Mapping[str, str],
    *,
    group: str | None = None,
    runtime_dir: str | None = None,
) -> dict[str, str]:
    """Render the service scripts for `user`.

    Returns a mapping from root-relative path to file text, for example
    "etc/sv/ginn-alice/run". `install` takes that mapping directly.

    `group` defaults to the user name. `runtime_dir` defaults to /run/ginn/<uid> (ADR-004).
    uid 0 and gid 0 are refused, because a realm must never run as root (CR-003).
    """
    group_name = user if group is None else group
    _check_account("user", user)
    _check_account("group", group_name)
    if uid <= 0 or gid <= 0:
        raise ValueError(f"refusing a supervisor for uid {uid} gid {gid}")
    missing = [key for key in XDG_KEYS if key not in xdg]
    if missing:
        raise ValueError(f"missing XDG values: {', '.join(missing)}")
    runtime = runtime_dir if runtime_dir is not None else f"{DEFAULT_RUNTIME_PARENT}/{uid}"
    state = _abs("XDG_STATE_HOME", xdg["XDG_STATE_HOME"])
    data = _abs("XDG_DATA_HOME", xdg["XDG_DATA_HOME"])
    config = _abs("XDG_CONFIG_HOME", xdg["XDG_CONFIG_HOME"])
    home_dir = _abs("home", home)
    runtime = _abs("runtime_dir", runtime)

    values: dict[str, str] = {
        "USER_NAME": user,
        "GROUP_NAME": group_name,
        "UID_NUM": str(uid),
        "GID_NUM": str(gid),
        "HOME_DIR": home_dir,
        "RUNTIME_DIR": runtime,
        "STATE_HOME": state,
        "DATA_HOME": data,
        "CONFIG_HOME": config,
        "SV_DIR": str(PurePosixPath(state) / "ginn" / "sv"),
        "LOG_DIR": str(PurePosixPath(state) / "ginn" / "log" / "supervisor"),
    }
    service = f"ginn-{user}"
    base = f"{SV_PARENT}/{service}"
    return {
        f"{base}/run": _run_script(values, uid),
        f"{base}/finish": _FINISH,
        f"{base}/log/run": _log_script(values),
    }


def install(root_dir: Path, rendered: Mapping[str, str]) -> list[Path]:
    """Write the rendered service under `root_dir` and link it into var/service.

    Each script is written atomically with mode 0755. Files whose content and mode already match
    are not rewritten. The symlink is created last, so runsvdir never sees a half-built service.
    The link target is the absolute /etc/sv/ginn-<user>, not prefixed with `root_dir`.

    Returns every path this call created or changed (directories it made, files it wrote and
    the link), for the Host Setup Record (DR-009). Directories that already existed are not
    returned.
    """
    service = _service_name(rendered)
    created: list[Path] = []
    written: list[Path] = []
    for rel in sorted(rendered):
        target = root_dir / rel
        _ensure_dirs(target.parent, root_dir, created)
        data = rendered[rel].encode("utf-8")
        if _is_current(target, data):
            continue
        atomic_write(target, data, SCRIPT_MODE)
        written.append(target)
    link = root_dir / LINK_PARENT / service
    _ensure_dirs(link.parent, root_dir, created)
    if _ensure_link(link, f"/{SV_PARENT}/{service}"):
        written.append(link)
    return [*created, *written]


def _run_script(v: Mapping[str, str], uid: int) -> str:
    groups = _groups(uid)
    gid = v["GID_NUM"]
    chown_lines = "\n".join(
        f"chown {uid}:{gid} {' '.join(str(path) for path in group)}"
        for group in chown_groups(DEFAULT_CGROUP_ROOT, uid)
    )
    return _fill(
        _RUN,
        {
            **_assignments(v),
            "CONTROLLERS": " ".join(sorted(CONTROLLERS)),
            "GINN_CGROUP": groups.ginn,
            "USER_CGROUP": groups.user,
            "SUPERVISOR_CGROUP": groups.supervisor,
            "LEAF_PATHS": f"{groups.supervisor} {groups.cli}",
            "CHOWN_LINES": chown_lines,
            "WAIT_SECONDS": str(WAIT_SECONDS),
        },
    )


def _log_script(v: Mapping[str, str]) -> str:
    return _fill(_LOG_RUN, _assignments(v) | {"WAIT_SECONDS": str(WAIT_SECONDS)})


def _assignments(v: Mapping[str, str]) -> dict[str, str]:
    """Quote each value once, so every placeholder receives a shell-safe word."""
    return {key: shlex.quote(value) for key, value in v.items()}


def _fill(template: str, values: Mapping[str, str]) -> str:
    """Replace each @NAME@ in one pass, so text inserted by a value is never expanded again."""
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            missing.add(name)
            return match.group(0)
        return values[name]

    text = _PLACEHOLDER.sub(replace, template)
    if missing:
        raise ValueError(f"no value for placeholders in rendered script: {sorted(missing)}")
    return text


def _abs(label: str, value: str) -> str:
    """Require an absolute, normalised path with no line break or NUL."""
    if not value.startswith("/") or PurePosixPath(value).as_posix() != value:
        raise ValueError(f"{label} must be an absolute normalised path: {value!r}")
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError(f"{label} contains a control character: {value!r}")
    return value


def _check_account(label: str, name: str) -> None:
    if _ACCOUNT.fullmatch(name) is None:
        raise ValueError(f"invalid {label} name: {name!r}")


def _service_name(rendered: Mapping[str, str]) -> str:
    keys = set(rendered)
    if not keys:
        raise ValueError("nothing rendered")
    first = PurePosixPath(sorted(keys)[0])
    if len(first.parts) < 3 or first.parts[:2] != tuple(SV_PARENT.split("/")):
        raise ValueError(f"rendered path is not under {SV_PARENT}: {first}")
    service = first.parts[2]
    if not service.startswith("ginn-"):
        raise ValueError(f"service directory must start with ginn-: {service}")
    expected = {f"{SV_PARENT}/{service}/{name}" for name in SCRIPT_NAMES}
    if keys != expected:
        raise ValueError(f"expected exactly {sorted(expected)}, got {sorted(keys)}")
    return service


def _is_current(target: Path, data: bytes) -> bool:
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != SCRIPT_MODE:
        return False
    return target.read_bytes() == data


def _ensure_dirs(directory: Path, root_dir: Path, created: list[Path]) -> None:
    current = root_dir
    for part in directory.relative_to(root_dir).parts:
        current = current / part
        if not current.exists():
            os.mkdir(current, SCRIPT_MODE)
            created.append(current)


def _ensure_link(link: Path, target: str) -> bool:
    """Point `link` at `target`. Return True if it changed."""
    if link.is_symlink():
        if os.readlink(link) == target:
            return False
        tmp = link.with_name(f".{link.name}.tmp")
        tmp.unlink(missing_ok=True)
        os.symlink(target, tmp)
        os.replace(tmp, link)
        return True
    if link.exists():
        raise ValueError(f"{link} exists and is not a symlink")
    os.symlink(target, link)
    return True


_FINISH = """\
#!/bin/sh
# Runs after runsvdir exits. The cgroup leaves persist, and run recreates what is missing.
exit 0
"""

_WAIT_BLOCK = """\
waited=0
while [ ! -d "$SV_DIR" ]; do
    if [ "$waited" -ge @WAIT_SECONDS@ ]; then
        log "$SV_DIR missing after @WAIT_SECONDS@ s; exiting so runsv restarts this service"
        exit 1
    fi
    sleep 1
    waited=$((waited + 1))
done
"""

_RUN = (
    """\
#!/bin/sh
# ginn supervisor service for @USER_NAME@ (FR-001, FR-081, FR-084). Written by ginn host-setup.
# Runs as root under runsv. It creates the runtime directory and the delegated cgroup leaves
# (spike S-01), moves itself into the supervisor leaf, waits for the state directory, then drops
# to @USER_NAME@ and execs runsvdir. Every value below was rendered at setup time.
set -eu

PATH=/usr/bin:/bin
export PATH

USER_NAME=@USER_NAME@
GROUP_NAME=@GROUP_NAME@
UID_NUM=@UID_NUM@
GID_NUM=@GID_NUM@
HOME_DIR=@HOME_DIR@
RUNTIME_DIR=@RUNTIME_DIR@
STATE_HOME=@STATE_HOME@
DATA_HOME=@DATA_HOME@
CONFIG_HOME=@CONFIG_HOME@
SV_DIR=@SV_DIR@
CONTROLLERS='@CONTROLLERS@'

log() {
    printf '%s\\n' "ginn-supervisor: $*" >&2
}

# enable_controllers DIR writes +CTRL, one write per controller, for each controller that DIR
# offers and does not have yet. It mirrors enable_controllers() in hostsetup/cgroups.py.
enable_controllers() {
    offered=$(cat "$1/cgroup.controllers") || return 1
    enabled=$(cat "$1/cgroup.subtree_control") || return 1
    for ctrl in $CONTROLLERS; do
        case " $offered " in
            *" $ctrl "*) ;;
            *) continue ;;
        esac
        case " $enabled " in
            *" $ctrl "*) continue ;;
        esac
        if ! printf '+%s\\n' "$ctrl" > "$1/cgroup.subtree_control"; then
            return 1
        fi
    done
    return 0
}

# 1. Runtime directory (ADR-004): owned by the user, mode 0700.
mkdir -p "$RUNTIME_DIR"
chown "$UID_NUM:$GID_NUM" "$RUNTIME_DIR"
chmod 0700 "$RUNTIME_DIR"

# 2. Delegated cgroup subtree. cgroupfs does not survive a reboot, so this runs on every start.
if [ ! -f /sys/fs/cgroup/cgroup.controllers ]; then
    log "cgroup v2 is not mounted at /sys/fs/cgroup"
    exit 1
fi
if ! enable_controllers /sys/fs/cgroup; then
    log "could not enable controllers at /sys/fs/cgroup; continuing without them"
fi
mkdir -p @GINN_CGROUP@
if ! enable_controllers @GINN_CGROUP@; then
    log "could not enable controllers at @GINN_CGROUP@"
    exit 1
fi
mkdir -p @LEAF_PATHS@
if ! enable_controllers @USER_CGROUP@; then
    log "could not enable controllers at @USER_CGROUP@"
    exit 1
fi
@CHOWN_LINES@

# 3. Move this process into the supervisor leaf. The exec below keeps the PID and the cgroup.
printf '%s\\n' "$$" > @SUPERVISOR_CGROUP@/cgroup.procs

# 4. Wait for the state directory, for example an encrypted home that is not mounted yet.
"""
    + _WAIT_BLOCK
    + """
# 5. Drop to the user and run its runit supervisor. chpst -u clears the supplementary groups.
exec chpst -u "$USER_NAME:$GROUP_NAME" env HOME="$HOME_DIR" XDG_RUNTIME_DIR="$RUNTIME_DIR" \\
XDG_STATE_HOME="$STATE_HOME" XDG_DATA_HOME="$DATA_HOME" XDG_CONFIG_HOME="$CONFIG_HOME" \\
GINN_RUNTIME_DIR="$RUNTIME_DIR" runsvdir -P "$SV_DIR"
"""
)

_LOG_RUN = (
    """\
#!/bin/sh
# Log service for the ginn supervisor of @USER_NAME@ (FR-081). Runs as root. It creates the log
# directory as @USER_NAME@, then drops to @USER_NAME@ for svlogd.
set -eu

PATH=/usr/bin:/bin
export PATH

USER_NAME=@USER_NAME@
GROUP_NAME=@GROUP_NAME@
SV_DIR=@SV_DIR@
LOG_DIR=@LOG_DIR@

log() {
    printf '%s\\n' "ginn-supervisor-log: $*" >&2
}

# The same wait as run, so nothing is created under a state home that is not mounted yet.
"""
    + _WAIT_BLOCK
    + """
chpst -u "$USER_NAME:$GROUP_NAME" mkdir -p "$LOG_DIR"
exec chpst -u "$USER_NAME:$GROUP_NAME" svlogd -tt "$LOG_DIR"
"""
)
