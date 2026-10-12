# SPDX-License-Identifier: Apache-2.0
"""Void packaging checks: template metadata, declared dependencies and setuid-free tools.

Covers AS-003 (ginn ships as a Void package that declares Podman and its helpers, with the
x86_64 glibc and musl archs, and no setuid or setgid tool source), DEP-1 (Podman and its runtime
helpers) and CR-005 (Apache-2.0). The template is parsed with regular expressions; xbps-src is
never run. NFR-062 (the CI matrix) and NFR-SEC-003 (the built .xbps) are not proven here; see
docs/traceability-deferred.toml.
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

TOP_LEVEL_FIELD = re.compile(r"^([a-z_]+)=(.*)$")
GUI_DEFERRED_COMMENT = (
    "# ginn-gui subpackage is added in M9 (FR-100..107) once src/ginn/gui exists."
)
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
    assert "crun" in depends, "ginn passes --runtime crun on every podman call (ADR-004)"
    assert "runc" not in depends, "runc is not the OCI runtime ginn uses (ADR-004)"
    assert "conmon" in depends
    assert "netavark" in depends
    assert depends & {"passt", "slirp4netns"}, "no rootless network helper declared"
    assert "shadow" in depends, "shadow provides newuidmap and newgidmap"
    assert "catatonit" in depends
    assert "xdg-dbus-proxy" in depends


@pytest.mark.fr("CR-005")
def test_template_license_is_apache_2_0() -> None:
    assert _template_fields()["license"] == "Apache-2.0"


@pytest.mark.fr("AS-003")
def test_template_builds_for_glibc_and_musl_x86_64() -> None:
    archs = set(_template_fields()["archs"].split())
    assert {"x86_64", "x86_64-musl"} <= archs


@pytest.mark.fr("AS-003")
def test_template_has_no_gui_subpackage_before_m9() -> None:
    # xbps-src pkglint rejects an empty subpackage, so ginn-gui waits for src/ginn/gui (M9).
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "ginn-gui_package" not in text
    assert GUI_DEFERRED_COMMENT in text.splitlines()
    assert not os.path.lexists(SRCPKGS / "ginn-gui"), "ginn-gui srcpkg returns with M9"


@pytest.mark.fr("AS-003")
def test_checksum_is_placeholder_or_sha256() -> None:
    assert CHECKSUM_VALUE.match(_template_fields()["checksum"])


@pytest.mark.fr("AS-003")
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
