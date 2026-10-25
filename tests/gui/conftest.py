# SPDX-License-Identifier: Apache-2.0
"""Session fixtures of the view smoke tests (FR-100..107, NFR-051).

The tests need one Xvfb display. The session fixture starts Xvfb with `-displayfd`, so Xvfb picks a
free display and writes its number back. It waits 10 s for that number and skips the tests when
Xvfb does not come up, and it sets the environment that GTK needs for a headless run. The Xvfb
process is always stopped in a `finally`, and the environment is restored there too. The GTK
fixture skips when gi, GTK 4 or libadwaita is missing, so the rest of the suite runs without them.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

XVFB_CANDIDATES: tuple[str, ...] = ("/usr/sbin/Xvfb", "/usr/bin/Xvfb")
XVFB_START_TIMEOUT_S = 10.0
XVFB_STOP_TIMEOUT_S = 10.0
HEADLESS_ENV: dict[str, str] = {
    "GDK_BACKEND": "x11",
    "DBUS_SESSION_BUS_ADDRESS": "disabled:",
    "GTK_A11Y": "none",
    "NO_AT_BRIDGE": "1",
    "GSETTINGS_BACKEND": "memory",
}


def _xvfb_binary() -> str | None:
    found = shutil.which("Xvfb")
    if found is not None:
        return found
    return next((path for path in XVFB_CANDIDATES if os.path.exists(path)), None)


def _read_display_number(read_fd: int, process: subprocess.Popen[bytes]) -> int | None:
    """The display number that Xvfb writes to `read_fd`, or None when it is not up in time."""
    ready, _, _ = select.select([read_fd], [], [], XVFB_START_TIMEOUT_S)
    if not ready:
        return None
    text = os.read(read_fd, 32).decode("ascii", errors="replace").strip()
    if not text.isdigit() or process.poll() is not None:
        return None
    return int(text)


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Terminate Xvfb, and kill it when it does not stop in time. Never waits without a bound."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(XVFB_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(XVFB_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass


@pytest.fixture(scope="session")
def display() -> Iterator[str]:
    """One Xvfb display for the session. It yields the DISPLAY value, for example `:99`."""
    binary = _xvfb_binary()
    if binary is None:
        pytest.skip("Xvfb is not installed")
    saved = {key: os.environ.get(key) for key in (*HEADLESS_ENV, "DISPLAY")}
    read_fd, write_fd = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            process = subprocess.Popen(  # noqa: S603 - argv list, no shell
                [
                    binary,
                    "-displayfd",
                    str(write_fd),
                    "-nolisten",
                    "tcp",
                    "-screen",
                    "0",
                    "1280x800x24",
                ],
                pass_fds=(write_fd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            pytest.skip(f"Xvfb cannot start: {exc}")
        finally:
            os.close(write_fd)
        number = _read_display_number(read_fd, process)
        if number is None:
            pytest.skip(f"Xvfb did not come up within {XVFB_START_TIMEOUT_S:g} seconds")
        os.environ.update(HEADLESS_ENV)
        os.environ["DISPLAY"] = f":{number}"
        yield f":{number}"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if process is not None:
            _stop(process)
        os.close(read_fd)


@pytest.fixture(scope="session")
def gtk(display: str) -> Any:
    """The GTK and libadwaita modules, initialised on the session display. Skips without them."""
    try:
        from ginn.gui.views import _gtk
    except (ImportError, ValueError) as exc:
        pytest.skip(f"PyGObject with GTK 4 and libadwaita is needed: {exc}")
    if not _gtk.Gtk.init_check():
        pytest.skip("GTK cannot open the display")
    _gtk.Adw.init()
    return _gtk
