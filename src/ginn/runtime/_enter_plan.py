# SPDX-License-Identifier: Apache-2.0
"""The pure parts of enter: skip names, the share list after a re-attach, and the exec argv.

Nothing here runs podman, reads the host or writes a file. `runtime/enter.py` calls these, and the
tests check them without a container. The split keeps enter.py short (FR-048, FR-049, FR-064).
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from ginn.domain.grants import SESSION_KIND, GrantRecord
from ginn.runtime.attach import AttachResult
from ginn.runtime.env import SessionEnv
from ginn.runtime.mounts import DOWNLOADS, Skipped

SESSION_SKIP_PREFIX: Final = f"{SESSION_KIND} skipped: "
"""The start of every skip message of a session grant or session variable (runtime/mounts.py)."""
LOGIN_FLAG: Final = "-l"
"""The shell flag that makes an interactive shell a login shell, for `ginn enter` (FR-048)."""
ROOT_HOME: Final = "/root"
"""HOME and the workdir of a `--root` exec. Under keep-id, container root is a subordinate uid on
the host, so a root write into the bound Realm Home would leave files that the user cannot delete
(FR-042, ADR-006). Container root therefore starts in the image's own root home."""


def display_name(kind: str, host: str, realm: str, host_home: Path) -> str:
    """The name a skip message shows, as the start shows it (FR-064 AC-1).

    A session grant is named by its host path, or by its realm path when it has none. A tier folder
    under the host home is `~/Downloads`. Any other grant, such as a mount root, is named by its
    realm path, which is the same path on the host (FR-065).
    """
    if kind == SESSION_KIND:
        return host or realm
    if realm == str(host_home / DOWNLOADS):
        return f"~/{DOWNLOADS}"
    return realm


def _skip_prefix(grant: GrantRecord, host_home: Path) -> str:
    name = display_name(grant.kind, grant.host, grant.realm, host_home)
    return f"{grant.kind} skipped: {name} ("


def share_after(
    stored: Sequence[str],
    before: Sequence[GrantRecord],
    after: Sequence[GrantRecord],
    session: SessionEnv,
    result: AttachResult,
    host_home: Path,
) -> list[str]:
    """The share_skipped list after a re-attach that changed something (FR-064 AC-1, FR-066).

    The start wrote the list from its plan, so enter has no plan. Its rule is this. A stored
    message that names a grant of either list is dropped, because that grant has been checked
    again, and the message is written again from the attach result if the grant is still skipped.
    A stored session message is dropped, because the session is read again. The stored messages
    that name no grant stay as they are: a refused path is never in the grant list, so it is never
    tried again, and it stays until the next start. Then the session messages of this enter come
    first, and the attach-time skips follow. A skip whose realm path is already named by the session
    list, or that is silent (an audio socket that is not there), is left out, as the start leaves it
    out. Duplicates are dropped, keeping the first.
    """
    named = tuple({_skip_prefix(grant, host_home) for grant in [*before, *after]})
    kept = [
        message
        for message in stored
        if not message.startswith(SESSION_SKIP_PREFIX) and not message.startswith(named)
    ]
    listed = {skip.realm for skip in session.skipped if skip.realm}
    silent = {bind.realm for bind in session.absent}
    later = [
        Skipped(
            skip.kind,
            display_name(skip.kind, skip.host, skip.realm, host_home),
            skip.reason,
        ).message()
        for skip in result.skipped
        if skip.realm not in listed and skip.realm not in silent
    ]
    fresh = [skip.message() for skip in session.skipped]
    return list(dict.fromkeys([*fresh, *kept, *later]))


def login_argv(shell: str) -> list[str]:
    """The argv of the interactive login shell of a realm, from the distribution data (FR-048)."""
    return [shell, LOGIN_FLAG]


def exec_home(host_home: Path, *, root: bool) -> str:
    """The HOME and the workdir of an exec: the host home path (FR-048), or `ROOT_HOME` for root."""
    return ROOT_HOME if root else str(host_home)


def exec_user(uid: int, gid: int, *, root: bool) -> str:
    """The `--user` value. The realm user has the host ids under keep-id (AS-007, ADR-006).

    `--root` gives container root, which is uid 0 inside the container and nothing on the host
    (FR-042, CR-003). The host user never runs as host root.
    """
    return "0:0" if root else f"{uid}:{gid}"
