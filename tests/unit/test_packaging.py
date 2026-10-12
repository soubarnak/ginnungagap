# SPDX-License-Identifier: Apache-2.0
"""Void packaging checks: template metadata, declared dependencies and setuid-free tools.

Covers AS-003 (ginn ships as a Void package that declares Podman and its helpers),
DEP-1 (Podman and its runtime helpers), CR-005 (Apache-2.0), NFR-062 (x86_64 glibc and
musl targets) and NFR-SEC-003 (no setuid or setgid files). The template is parsed with
regular expressions; xbps-src is never run.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRCPKGS = REPO / "packaging" / "void" / "srcpkgs"
TEMPLATE = SRCPKGS / "ginn" / "template"

TOP_LEVEL_FIELD = re.compile(r'^([a-z_]+)=(.*)$')
GUI_BLOCK = re.compile(r"^ginn-gui_package\(\) \{\n(.*?)^\}", re.MULTILINE | re.DOTALL)
FIELD_IN_BLOCK = re.compile(r'^\s*depends="([^"]*)"', re.MULTILINE)
CHECKSUM_VALUE = re.compile(r"^(@CHECKSUM@|[0-9a-f]{64})$")

# Octal modes with the setuid (4), setgid (2) or both bits, as 4 digits, optionally with
# a leading 0 or a Python 0o prefix. Leading zeros on plain modes such as 0755 do not match.
OCTAL_SETID = re.compile(r"(?<![0-9])(?:0o)?[2-7][0-7]{3}(?![0-9])")
# Symbolic forms such as u+s, g=s or a+rxs.
SYMBOLIC_SETID = re.compile(r"\b[ugoa]*[+=][rwxX]*s\b")
# Only lines that set a file mode are inspected, so dates and version numbers pass.
MODE_CONTEXT = re.compile(r"\b(chmod|vinstall|install\s+-m)\b")


def _template_fields() -> dict[str, str]:
    """Return the top-level NAME=value fields of the template, with quotes removed."""
    fields: dict[str, str] = {}
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        match = TOP_LEVEL_FIELD.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip('"')
    return fields


def _gui_block() -> str:
    """Return the body of the ginn-gui_package() subpackage function."""
    match = GUI_BLOCK.search(TEMPLATE.read_text(encoding="utf-8"))
    assert match, "ginn-gui_package() not found in the template"
    return match.group(1)


def _project_version() -> str:
    with (REPO / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


@pytest.mark.fr("AS-003")
def test_template_version_matches_pyproject_without_dev_marker() -> None:
    expected = _project_version().removesuffix(".dev0")
    assert _template_fields()["version"] == expected


@pytest.mark.fr("AS-003")
@pytest.mark.fr("DEP-1")
def test_template_depends_on_podman_and_its_helpers() -> None:
    depends = set(_template_fields()["depends"].split())
    assert "podman" in depends
    assert depends & {"crun", "runc"}, "no OCI runtime (crun or runc) declared"
    assert "conmon" in depends
    assert "netavark" in depends
    assert depends & {"passt", "slirp4netns"}, "no rootless network helper declared"
    assert "shadow" in depends, "shadow provides newuidmap and newgidmap"
    assert "catatonit" in depends
    assert "xdg-dbus-proxy" in depends


@pytest.mark.fr("CR-005")
def test_template_license_is_apache_2_0() -> None:
    assert _template_fields()["license"] == "Apache-2.0"


@pytest.mark.fr("NFR-062")
def test_template_builds_for_glibc_and_musl_x86_64() -> None:
    archs = set(_template_fields()["archs"].split())
    assert {"x86_64", "x86_64-musl"} <= archs


@pytest.mark.fr("AS-003")
def test_gui_subpackage_pins_matching_ginn_version_and_gui_deps() -> None:
    depends = FIELD_IN_BLOCK.search(_gui_block())
    assert depends, "ginn-gui_package() sets no depends"
    names = set(depends.group(1).split())
    assert "ginn>=${version}_${revision}" in names
    assert {"python3-gobject", "gtk4", "libadwaita"} <= names


@pytest.mark.fr("AS-003")
def test_ginn_gui_srcpkg_is_symlink_to_ginn() -> None:
    link = SRCPKGS / "ginn-gui"
    assert link.is_symlink(), "packaging/void/srcpkgs/ginn-gui must be a symlink"
    assert os.readlink(link) == "ginn"


@pytest.mark.fr("AS-003")
def test_checksum_is_placeholder_or_sha256() -> None:
    assert CHECKSUM_VALUE.match(_template_fields()["checksum"])


@pytest.mark.fr("NFR-SEC-003")
def test_no_setuid_or_setgid_modes_in_packaging_tools() -> None:
    offenders: list[str] = []
    for root in (REPO / "tools", REPO / "packaging"):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            data = path.read_bytes()
            if b"\0" in data:
                continue  # compiled or binary data
            text = data.decode("utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if not MODE_CONTEXT.search(line):
                    continue
                if OCTAL_SETID.search(line) or SYMBOLIC_SETID.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not offenders, "setuid or setgid mode set in packaging files:\n" + "\n".join(offenders)
