"""Is the host's login session locked? The GUI checks cannot pass on a locked one.

A locked niri session (elogind `LockedHint` true) delivers no input and no `close-window` action to any window, and
`niri msg focused-window` is `null`, so "the window closes" fails for a reason that is not the product's. The checks call
`locked()` and SKIP with `SKIP_REASON` when it is true; on an unlocked or unknown session they run unchanged.
"""

from __future__ import annotations

import os
import subprocess

SKIP_REASON = "the host session is locked (LockedHint=yes): niri delivers no close-window; unlock and rerun"


def _loginctl(*args: str) -> str | None:
    try:
        done = subprocess.run(["loginctl", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def session_id() -> str | None:
    """This process's session, else the user's graphical one (a check run over ssh or sudo has no XDG_SESSION_ID)."""
    if os.environ.get("XDG_SESSION_ID"):
        return os.environ["XDG_SESSION_ID"]
    out = _loginctl("show-user", str(os.getuid()), "-p", "Display", "--value")
    return out or None


def locked(loginctl=_loginctl, session=session_id) -> bool:
    """True only when the session reports LockedHint=yes; unknown (no loginctl, no session) counts as unlocked."""
    found = session()
    if not found:
        return False
    out = loginctl("show-session", found, "-p", "LockedHint", "--value")
    return (out or "").strip().lower() in ("yes", "true", "1")
