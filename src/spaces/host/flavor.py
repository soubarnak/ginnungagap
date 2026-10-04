"""Desktop flavour: extra guest packages that match the host desktop.

The guests ship KDE's polkit agent and portal backend (the guest polkit agent
must stay polkit-kde, the launcher parses its log). On a GTK based host
(niri, sway, GNOME, XFCE, ...) GTK and Qt applications in the guest then look
foreign, so the "gtk" flavour adds a GTK theme, icons, the GTK portal backend,
the GSettings schemas and the GTK platform theme for Qt. Nothing is removed.

The setting is `desktop_flavor` in /etc/spaces/void.json: "auto" (default),
"kde" (add nothing) or "gtk". The packages are added to
`distros.<id>.packages` of the generated /etc/spaces/config.json by
spaces.host.nvidia.generate and apply when a space is created;
`spaces-void install-flavor NAME` installs them into an existing space.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

FLAVORS = ("auto", "kde", "gtk")
DEFAULT = "auto"
FALLBACK = "gtk"
REMEMBERED = Path("/var/lib/spaces/.host/desktop-flavor")
RUN_USER = Path("/run/user")

_DEBIAN = [
    "adwaita-icon-theme",
    "dconf-gsettings-backend",
    "gnome-themes-extra",
    "gsettings-desktop-schemas",
    "qt5-gtk-platformtheme",
    "qt6-gtk-platformtheme",
    "xdg-desktop-portal-gtk",
]

# Package names were checked against the live repositories (see RESULTS.md,
# M7). Arch and Fedora ship the Qt GTK3 platform theme inside qt6-base /
# qt6-qtbase-gui; it needs gtk3. Fedora has no gnome-themes-extra, and
# Ubuntu and Kali have no adw-gtk3 package.
GTK_PACKAGES: dict[str, tuple[str, ...]] = {
    "ubuntu": tuple(_DEBIAN),
    "kali": tuple(_DEBIAN),
    "arch": (
        "adw-gtk-theme",
        "adwaita-icon-theme",
        "dconf",
        "gnome-themes-extra",
        "gsettings-desktop-schemas",
        "gtk3",
        "xdg-desktop-portal-gtk",
    ),
    "fedora": (
        "adw-gtk3-theme",
        "adwaita-icon-theme",
        "dconf",
        "gsettings-desktop-schemas",
        "gtk3",
        "xdg-desktop-portal-gtk",
    ),
}

_KDE = re.compile(r"(^|[:;])(kde|plasma)([:;]|$)", re.IGNORECASE)


def classify(desktop: str | None) -> str | None:
    """Map an XDG_CURRENT_DESKTOP value to "kde" or "gtk"; None when empty."""

    if not desktop or not desktop.strip():
        return None
    return "kde" if _KDE.search(desktop.strip()) else "gtk"


def packages_for(flavor: str, distro: str) -> tuple[str, ...]:
    """Packages the flavour adds for a distribution id."""

    if flavor == "gtk":
        return GTK_PACKAGES.get(distro, ())
    return ()


def install_command(distro: str, packages: Sequence[str]) -> list[str] | None:
    """Non-interactive install command to run as root in the guest."""

    packages = list(packages)
    if distro in ("ubuntu", "kali"):
        return [
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            *packages,
        ]
    if distro == "arch":
        # A partial upgrade is unsupported on Arch, so synchronise fully.
        return ["pacman", "-Syu", "--noconfirm", "--needed", *packages]
    if distro == "fedora":
        return [
            "dnf5",
            "install",
            "-y",
            "--setopt=install_weak_deps=False",
            *packages,
        ]
    return None


def _candidate_uids(preferred: int | None, run_user: Path) -> list[int]:
    uids: list[int] = []
    if preferred is not None and preferred > 0:
        uids.append(preferred)
    try:
        found = sorted(
            int(entry.name) for entry in run_user.iterdir() if entry.name.isdigit()
        )
    except OSError:
        found = []
    uids.extend(uid for uid in found if uid > 0 and uid not in uids)
    return uids


def session_desktop(
    uid: int | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    run_user: Path = RUN_USER,
) -> str | None:
    """XDG_CURRENT_DESKTOP of an active graphical session, or None.

    `uid` (the creating user) is tried first, then every user with a runtime
    directory. `environment` replaces the lookup (tests).
    """

    if environment is not None:
        return environment.get("XDG_CURRENT_DESKTOP")
    from . import session_env

    login = session_env.open_login()
    if login is None:
        return None
    for candidate in _candidate_uids(uid, run_user):
        found = session_env.resolve_environment(
            candidate, login=login, run_user=run_user
        )
        if found and found.get("XDG_CURRENT_DESKTOP"):
            return found["XDG_CURRENT_DESKTOP"]
    return None


def _caller_uid() -> int | None:
    for variable in ("PKEXEC_UID", "SUDO_UID"):
        try:
            return int(os.environ[variable])
        except (KeyError, ValueError):
            continue
    return None


def resolve_auto(
    uid: int | None = None,
    *,
    remembered: Path = REMEMBERED,
    desktop: str | None = None,
) -> str:
    """Resolve "auto": the desktop of the creating user's active session.

    A positive answer is remembered so that a regeneration without a session
    (boot time autostart) does not flip the result; with nothing known the
    answer is "gtk".
    """

    if uid is None:
        uid = _caller_uid()
    if desktop is None:
        desktop = session_desktop(uid)
    found = classify(desktop)
    if found:
        try:
            remembered.parent.mkdir(parents=True, exist_ok=True)
            if _read(remembered) != found:
                remembered.write_text(found + "\n", encoding="utf-8")
        except OSError:
            pass
        return found
    return _read(remembered) or FALLBACK


def _read(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if value in ("kde", "gtk") else None


def resolve(setting: str, uid: int | None = None) -> str:
    """Return "kde" or "gtk" for a `desktop_flavor` setting."""

    if setting not in FLAVORS:
        raise ValueError(f"desktop_flavor must be one of {', '.join(FLAVORS)}")
    return resolve_auto(uid) if setting == "auto" else setting


def configured_setting(
    extras: Path = Path("/etc/spaces/void.json"),
    base: Path = Path("/usr/share/spaces/config.base.json"),
) -> str:
    """The `desktop_flavor` setting: void.json overrides the shipped base."""

    import json

    for path in (extras, base):
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("desktop_flavor")
        except (OSError, ValueError, AttributeError):
            continue
        if value is not None:
            return str(value)
    return DEFAULT
