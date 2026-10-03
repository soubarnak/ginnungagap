"""Translate Spaces device specs into LXC cgroup2 device rules."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

PROC_DEVICES = Path("/proc/devices")

# Same defaults as LXC's common.conf: pseudo devices, pty slaves and fuse.
BASE_RULES = (
    "c *:* m",
    "b *:* m",
    "c 1:3 rwm",
    "c 1:5 rwm",
    "c 1:7 rwm",
    "c 5:0 rwm",
    "c 5:1 rwm",
    "c 5:2 rwm",
    "c 1:8 rwm",
    "c 1:9 rwm",
    "c 136:* rwm",
    "c 10:229 rwm",
)

_ID_SPEC = re.compile(r"^/dev/(char|block)/(\d+):(\d+)$")
_NAMED_SPEC = re.compile(r"^(char|block)-([A-Za-z0-9_.-]+)$")


class DeviceSpecError(ValueError):
    """A device specification could not be translated."""


def _permissions(value: str) -> str:
    if not value or any(letter not in "rwm" for letter in value):
        raise DeviceSpecError(f"unsupported device permissions: {value!r}")
    return "".join(letter for letter in "rwm" if letter in value)


def _majors(kind: str, name: str) -> list[int]:
    section = None
    found: list[int] = []
    for line in PROC_DEVICES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line == "Character devices:":
            section = "char"
        elif line == "Block devices:":
            section = "block"
        elif section == kind:
            major, _, device = line.partition(" ")
            if device.strip() == name and major.isdigit():
                found.append(int(major))
    return found


def translate_spec(spec: str, permissions: str) -> list[str]:
    """Return the cgroup2 rules for one (spec, permissions) pair."""

    perms = _permissions(permissions)
    match = _ID_SPEC.match(spec)
    if match:
        kind = "c" if match[1] == "char" else "b"
        return [f"{kind} {int(match[2])}:{int(match[3])} {perms}"]
    match = _NAMED_SPEC.match(spec)
    if match:
        majors = _majors(match[1], match[2])
        if not majors:
            raise DeviceSpecError(f"unknown device class: {spec}")
        kind = "c" if match[1] == "char" else "b"
        return [f"{kind} {major}:* {perms}" for major in majors]
    if spec.startswith("/"):
        try:
            result = os.stat(spec)
        except OSError as error:
            raise DeviceSpecError(f"cannot stat {spec}: {error}") from error
        import stat

        if stat.S_ISCHR(result.st_mode):
            kind = "c"
        elif stat.S_ISBLK(result.st_mode):
            kind = "b"
        else:
            raise DeviceSpecError(f"not a device node: {spec}")
        return [
            f"{kind} {os.major(result.st_rdev)}:{os.minor(result.st_rdev)} "
            f"{perms}"
        ]
    raise DeviceSpecError(f"unsupported device specification: {spec!r}")


def translate(allow: Sequence[tuple[str, str]]) -> list[str]:
    """Translate (spec, permissions) pairs, dropping duplicates in order."""

    rules: list[str] = []
    for spec, permissions in allow:
        for rule in translate_spec(spec, permissions):
            if rule not in rules:
                rules.append(rule)
    return rules


def closed_rules(rules: Sequence[str]) -> list[str]:
    """Return the full allow list of a closed policy: base plus extras."""

    merged = list(BASE_RULES)
    merged.extend(rule for rule in rules if rule not in merged)
    return merged


def config_text(rules: Sequence[str] | None) -> str:
    """Return the devices include file; None means unrestricted."""

    if rules is None:
        return "# devices: full\n"
    lines = ["lxc.cgroup2.devices.deny = a"]
    lines.extend(
        f"lxc.cgroup2.devices.allow = {rule}" for rule in closed_rules(rules)
    )
    return "\n".join(lines) + "\n"
