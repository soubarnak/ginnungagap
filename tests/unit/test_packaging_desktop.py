# SPDX-License-Identifier: Apache-2.0
"""Desktop entry and icon checks for the Control Plane (ginn-gui).

Covers IR-029 (freedesktop Desktop Entry and icon theme specifications). The desktop file is
parsed with configparser under the Desktop Entry rules: one [Desktop Entry] group, case-sensitive
keys, no duplicate keys, and semicolon-terminated lists. desktop-file-validate runs when it is on
PATH and is skipped otherwise. The icon must be a byte-for-byte copy of art/ginnungagap-mark.svg.
The Control Plane's usability (NFR-050, NFR-051) is not proven here.
"""

from __future__ import annotations

import configparser
import hashlib
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "src" / "ginn" / "gui" / "data"
DESKTOP = DATA / "org.bongbetic.ginn.desktop"
ICON = DATA / "org.bongbetic.ginn.svg"
MARK = REPO / "art" / "ginnungagap-mark.svg"

GROUP = "Desktop Entry"
# Key names of the spec: A-Za-z0-9-, with an optional locale suffix such as Name[de].
KEY_NAME = re.compile(r"^[A-Za-z0-9-]+(\[[A-Za-z_@]+\])?$")
LIST_KEYS = ("Categories", "Keywords")
BOOLEAN_KEYS = ("Terminal", "StartupNotify")
REQUIRED_VALUES = {
    "Type": "Application",
    "Name": "Ginnungagap",
    "Exec": "ginn-gui",
    "Icon": "org.bongbetic.ginn",
    "Categories": "System;Utility;",
    "Terminal": "false",
    "StartupNotify": "true",
}


class _SpecParser(configparser.ConfigParser):
    """Keeps key case, as the Desktop Entry spec does."""

    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _parse() -> _SpecParser:
    parser = _SpecParser(
        interpolation=None,
        strict=True,
        delimiters=("=",),
        comment_prefixes=("#",),
    )
    parser.read_string(DESKTOP.read_text(encoding="utf-8"), source=str(DESKTOP))
    return parser


def _entry() -> dict[str, str]:
    return dict(_parse()[GROUP])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.fr("IR-029")
def test_desktop_file_has_exactly_one_desktop_entry_group() -> None:
    parser = _parse()
    assert parser.sections() == [GROUP]
    text = DESKTOP.read_text(encoding="utf-8")
    assert text.splitlines()[0] == f"[{GROUP}]"
    assert "\r" not in text, "the spec requires LF line endings"
    assert text.endswith("\n")


@pytest.mark.fr("IR-029")
def test_desktop_keys_are_spec_key_names() -> None:
    bad = [key for key in _entry() if not KEY_NAME.match(key)]
    assert not bad, f"keys outside the spec's key-name grammar: {bad}"


@pytest.mark.fr("IR-029")
def test_desktop_entry_has_the_required_values() -> None:
    entry = _entry()
    assert entry.get("Comment", "").strip(), "Comment must not be empty"
    for key, value in REQUIRED_VALUES.items():
        assert entry.get(key) == value, f"{key} is {entry.get(key)!r}, expected {value!r}"


@pytest.mark.fr("IR-029")
def test_list_values_end_with_a_semicolon() -> None:
    entry = _entry()
    for key in LIST_KEYS:
        if key not in entry:
            continue
        value = entry[key]
        assert value.endswith(";"), f"{key} must end with ';': {value!r}"
        items = value.split(";")[:-1]
        assert all(items), f"{key} has an empty item: {value!r}"


@pytest.mark.fr("IR-029")
def test_boolean_values_are_true_or_false() -> None:
    entry = _entry()
    for key in BOOLEAN_KEYS:
        assert entry.get(key) in {"true", "false"}, f"{key} is not a boolean: {entry.get(key)!r}"


@pytest.mark.fr("IR-029")
def test_exec_is_one_bare_argv_with_no_field_codes() -> None:
    exec_value = _entry()["Exec"]
    assert "%" not in exec_value, "the desktop file passes no files or URLs to ginn-gui"
    assert shlex.split(exec_value) == ["ginn-gui"]


@pytest.mark.fr("IR-029")
def test_exec_names_the_console_script_declared_in_pyproject() -> None:
    with (REPO / "pyproject.toml").open("rb") as handle:
        scripts = tomllib.load(handle)["project"]["scripts"]
    assert shlex.split(_entry()["Exec"])[0] in scripts


@pytest.mark.fr("IR-029")
def test_desktop_file_name_icon_name_and_svg_name_agree() -> None:
    # The GUI's application id is the desktop file id, so the three names must match.
    assert DESKTOP.stem == _entry()["Icon"] == ICON.stem == "org.bongbetic.ginn"


@pytest.mark.fr("IR-029")
def test_icon_is_the_project_mark_copied_byte_for_byte() -> None:
    assert _sha256(ICON) == _sha256(MARK), "src/ginn/gui/data copy drifted from art/"
    assert ICON.read_bytes().lstrip().startswith((b"<svg", b"<?xml"))


@pytest.mark.fr("IR-029")
def test_desktop_file_passes_desktop_file_validate_when_present() -> None:
    validator = shutil.which("desktop-file-validate")
    if validator is None:
        pytest.skip("desktop-file-validate is not on PATH")
    # Fixed argv, no shell. The validator path comes from PATH lookup above.
    result = subprocess.run(  # noqa: S603
        [validator, str(DESKTOP)], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "error" not in output.lower(), output
