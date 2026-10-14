# SPDX-License-Identifier: Apache-2.0
"""Bounded reads and listings of a realm's files, under the realm lock (FR-044, DR-033).

The realm is untrusted, so every read of it is one bounded exec in the realm: a timeout, and a cap
on the output. A file that is swapped after it was listed therefore cannot fill ginn's disk or
memory, because the copy is cut at the cap. `podman cp` is not used, since it has no cap.

The reads run as container root (user 0:0). A listing as the realm user fails on an unreadable
first-level directory of /usr/share (Fedora's /usr/share/empty.sshd is 0711, and `find` then exits
1). Under keep-id, container uid 0 is a subordinate uid on the host: neither host root nor the host
user. It reads what the image gives the realm, and these reads never write.

A podman failure, a hang or an overflow raises GINN-E-EXPORT-LIST for a listing. For a file read,
an overflow is RealmFileTooLarge and any other failure is RealmFileUnreadable. The caller decides
what each one does.
"""

from __future__ import annotations

from typing import Final

from ginn.errors import GinnError, make
from ginn.podman.adapter import ExecTarget, Podman
from ginn.podman.models import ExecResult

LISTING_USER: Final = "0:0"
"""The user the realm reads run as: container root (see the module docstring)."""
LISTING_TIMEOUT_S: Final = 30.0
"""Seconds one realm read may run before the podman client is killed (FR-044)."""
LISTING_MAX_BYTES: Final = 8 * 1024 * 1024
"""The most stdout a realm listing may print before it is cut (FR-044)."""
_OVERFLOW: Final = "printed more than"


class RealmFileError(Exception):
    """A realm file cannot be used. The message is the reason."""


class RealmFileTooLarge(RealmFileError):
    """The realm file is over the cap. Nothing past the cap was kept."""


class RealmFileUnreadable(RealmFileError):
    """The realm file cannot be read: a failed exec, a hang, or a non-zero status of `cat`."""


def _reason(exc: GinnError) -> str:
    return str(exc.details.get("reason", exc.code))


def run_listing(
    podman: Podman,
    container_id: str,
    argv: tuple[str, ...],
    *,
    realm: str,
    target: ExecTarget,
) -> ExecResult:
    """Run one listing argv in the realm as container root, bounded. Raise GINN-E-EXPORT-LIST
    when it cannot run: a podman error, a hang, or an overflow."""
    try:
        return podman.exec(
            container_id,
            argv,
            target=target,
            capture=True,
            user=LISTING_USER,
            timeout_s=LISTING_TIMEOUT_S,
            max_output=LISTING_MAX_BYTES,
        )
    except GinnError as exc:
        raise make("GINN-E-EXPORT-LIST", realm=realm, reason=_reason(exc)) from exc


def read_realm_file(
    podman: Podman,
    container_id: str,
    path: str,
    *,
    cap: int,
    target: ExecTarget,
) -> bytes:
    """The bytes of the realm file `path`, at most `cap` bytes, read by one bounded `cat`.

    The output is cut at `cap + 1`, so the copy never holds more than that. The bytes come back
    exactly: the adapter decodes stdout with surrogateescape, and encoding it that way restores
    them. Raise RealmFileTooLarge over the cap, and RealmFileUnreadable for any other failure.
    `path` must be a listed regular file, or a link target that is one (the caller checks).
    """
    try:
        result = podman.exec(
            container_id,
            ("cat", "--", path),
            target=target,
            capture=True,
            user=LISTING_USER,
            timeout_s=LISTING_TIMEOUT_S,
            max_output=cap + 1,
        )
    except GinnError as exc:
        reason = _reason(exc)
        if reason.startswith(_OVERFLOW):
            raise RealmFileTooLarge(f"larger than {cap} bytes") from exc
        raise RealmFileUnreadable(f"cannot read it out of the realm ({reason})") from exc
    if result.exit_code != 0:
        raise RealmFileUnreadable(f"cat exited with status {result.exit_code}")
    data = result.stdout.encode("utf-8", "surrogateescape")
    if len(data) > cap:
        raise RealmFileTooLarge(f"larger than {cap} bytes")
    return data
