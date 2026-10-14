# SPDX-License-Identifier: Apache-2.0
"""Void packaging checks: template metadata, subpackages, dependencies and setuid-free tools.

Covers AS-003 (ginn ships as Void packages, ginn and ginn-gui, with the x86_64 glibc and musl
archs, the two console scripts, and no setuid or setgid tool source), DEP-1 (Podman and its
runtime helpers), IR-029 (the GUI package installs the desktop entry and icon) and CR-005
(Apache-2.0). The template is parsed with regular expressions; xbps-src is never run. NFR-062
(the CI matrix) and NFR-SEC-003 (the built .xbps) are not proven here; see
docs/traceability-deferred.toml.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SRCPKGS = REPO / "packaging" / "void" / "srcpkgs"
TEMPLATE = SRCPKGS / "ginn" / "template"
NATIVE_MAKEFILE = REPO / "native" / "Makefile"

TOP_LEVEL_FIELD = re.compile(r"^([a-z_]+)=(.*)$")
CHECKSUM_VALUE = re.compile(r"^(@CHECKSUM@|[0-9a-f]{64})$")
MAKEFILE_HELPERS = re.compile(r"^HELPERS := (.+)$", re.MULTILINE)
HELPER_LOOP = re.compile(r"^for helper in (.+); do$")

# The five helpers of native/Makefile (AS-003 installs them to LIBEXECDIR, the runtime path).
NATIVE_HELPERS = frozenset(
    {"ginn-wait", "ginn-rebind", "ginn-watch", "ginn-agent-filter", "ginn-idle"}
)
# The runtime depends of T0.6 (docs/roadmap.md). The template adds python3 on top, because the
# CLI is Python.
T0_6_DEPENDS = frozenset(
    {
        "podman",
        "crun",
        "conmon",
        "netavark",
        "aardvark-dns",
        "passt",
        "catatonit",
        "fuse-overlayfs",
        "shadow",
        "xdg-dbus-proxy",
        "runit",
        "util-linux",
        "containers-common",
    }
)
GUI_DEPENDS = 'depends="ginn>=${version}_${revision} python3-gobject gtk4 libadwaita"'
GUI_MODULE_MOVE = 'vmove "${py3_sitelib}/ginn/gui"'
DESKTOP_STAGE = "vinstall src/ginn/gui/data/org.bongbetic.ginn.desktop 644 usr/share/applications"
ICON_STAGE = (
    "vinstall src/ginn/gui/data/org.bongbetic.ginn.svg 644 usr/share/icons/hicolor/scalable/apps"
)
DESKTOP_MOVE = "vmove usr/share/applications/org.bongbetic.ginn.desktop"
ICON_MOVE = "vmove usr/share/icons/hicolor/scalable/apps/org.bongbetic.ginn.svg"
NATIVE_INSTALL = 'make -C native install DESTDIR="${DESTDIR}" LIBEXECDIR=/usr/libexec/ginn'

# Octal modes with the setuid (4), setgid (2) or both bits, as 4 digits, optionally with
# a leading 0 or a Python 0o prefix. Leading zeros on plain modes such as 0755 do not match.
OCTAL_SETID = re.compile(r"(?<![0-9])(?:0o)?[2-7][0-7]{3}(?![0-9])")
# Symbolic forms such as u+s, g=s or a+rxs.
SYMBOLIC_SETID = re.compile(r"\b[ugoa]*[+=][rwxX]*s\b")
# Only lines that set a file mode are inspected, so dates and version numbers pass.
MODE_CONTEXT = re.compile(r"\b(chmod|vinstall|install\s+-m)\b")


def _template_lines() -> list[str]:
    return TEMPLATE.read_text(encoding="utf-8").splitlines()


def _template_fields() -> dict[str, str]:
    """Return the top-level NAME=value fields of the template, with quotes removed."""
    fields: dict[str, str] = {}
    for line in _template_lines():
        match = TOP_LEVEL_FIELD.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip('"')
    return fields


def _function_body(header: str) -> list[str]:
    """Return the stripped lines of the function whose header line is exactly `header`.

    The body ends at the first bare closing brace. A nested function is indented, so its
    closing brace does not end the outer function.
    """
    lines = _template_lines()
    start = lines.index(header)
    end = lines.index("}", start)
    return [line.strip() for line in lines[start + 1 : end]]


def _pyproject() -> dict[str, Any]:
    with (REPO / "pyproject.toml").open("rb") as handle:
        return dict(tomllib.load(handle))


def _makefile_helpers() -> set[str]:
    match = MAKEFILE_HELPERS.search(NATIVE_MAKEFILE.read_text(encoding="utf-8"))
    assert match, "native/Makefile has no HELPERS line"
    return set(match.group(1).split())


@pytest.mark.fr("AS-003")
def test_template_version_is_1_0_0() -> None:
    assert _template_fields()["version"] == "1.0.0"


@pytest.mark.fr("AS-003")
@pytest.mark.xfail(
    strict=True,
    reason="pyproject.toml and src/ginn/__init__.py still say 0.1.0.dev0. Closing this needs "
    "the release bump outside packaging/ (docs/packaging.md, release step 1).",
)
def test_template_version_matches_pyproject_without_dev_marker() -> None:
    expected = str(_pyproject()["project"]["version"]).removesuffix(".dev0")
    assert _template_fields()["version"] == expected


@pytest.mark.fr("AS-003")
def test_template_keeps_the_default_wrksrc_and_no_wrksrc_paths() -> None:
    # The tag archive has one top-level directory, which xbps-src unpacks into the default wrksrc
    # (void-packages Manual, create_wrksrc). wrksrc is a bare name under XBPS_BUILDDIR, so a path
    # built from it would be wrong from inside wrksrc. Paths in the template are wrksrc-relative.
    assert "wrksrc" not in _template_fields()
    assert "${wrksrc}" not in "\n".join(_template_lines())
    assert _template_fields()["distfiles"] == "${homepage}/archive/refs/tags/v${version}.tar.gz"


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


@pytest.mark.fr("AS-003")
@pytest.mark.fr("DEP-1")
def test_template_depends_are_the_t0_6_list_and_python3() -> None:
    depends = set(_template_fields()["depends"].split())
    assert T0_6_DEPENDS <= depends
    assert depends - T0_6_DEPENDS == {"python3"}


@pytest.mark.fr("AS-003")
def test_template_compiles_the_native_helpers_with_gcc_and_make() -> None:
    hostmake = set(_template_fields()["hostmakedepends"].split())
    assert {"gcc", "make"} <= hostmake


@pytest.mark.fr("CR-005")
def test_template_license_is_apache_2_0() -> None:
    assert _template_fields()["license"] == "Apache-2.0"


@pytest.mark.fr("CR-005")
def test_template_installs_notice_with_vlicense() -> None:
    # Apache-2.0 4(d): the NOTICE text is project-specific, so ginn ships its own copy.
    assert "vlicense NOTICE" in _function_body("post_install() {")


@pytest.mark.fr("CR-005")
def test_template_does_not_install_license_with_vlicense() -> None:
    # Void ships the Apache-2.0 text (/usr/share/licenses/Apache-2.0), which covers 4(a).
    # xlint reports "license Apache-2.0 should not be installed" for a vlicense of the license.
    assert "vlicense LICENSE" not in _function_body("post_install() {")


@pytest.mark.fr("AS-003")
def test_template_builds_for_glibc_and_musl_x86_64() -> None:
    archs = set(_template_fields()["archs"].split())
    assert {"x86_64", "x86_64-musl"} <= archs


@pytest.mark.fr("AS-003")
def test_template_installs_the_native_helpers_to_libexec_with_make() -> None:
    assert NATIVE_INSTALL in _function_body("post_install() {")


@pytest.mark.fr("AS-003")
def test_template_lists_the_five_native_helpers_it_checks() -> None:
    body = _function_body("post_install() {")
    loops = [m.group(1) for line in body if (m := HELPER_LOOP.match(line))]
    assert len(loops) == 1, "post_install must check the helpers in one loop"
    listed = loops[0].split()
    assert len(listed) == 5
    assert set(listed) == NATIVE_HELPERS
    assert _makefile_helpers() == NATIVE_HELPERS, "native/Makefile HELPERS drifted"
    assert 'test -x "${DESTDIR}/usr/libexec/ginn/${helper}" || return 1' in body


@pytest.mark.fr("AS-003")
def test_template_defines_the_ginn_gui_subpackage() -> None:
    assert "ginn-gui_package() {" in _template_lines()


@pytest.mark.fr("AS-003")
def test_ginn_gui_srcpkg_is_a_symlink_to_the_ginn_template() -> None:
    link = SRCPKGS / "ginn-gui"
    assert link.is_symlink(), "xbps-src needs srcpkgs/ginn-gui to name the subpackage"
    assert os.readlink(link) == "ginn"
    assert (link / "template").is_file()


@pytest.mark.fr("AS-003")
def test_ginn_gui_depends_on_ginn_and_the_gui_stack() -> None:
    assert GUI_DEPENDS in _function_body("ginn-gui_package() {")


@pytest.mark.fr("AS-003")
def test_ginn_gui_package_moves_the_module_and_the_console_script() -> None:
    body = _function_body("ginn-gui_package() {")
    assert GUI_MODULE_MOVE in body
    assert "vmove usr/bin/ginn-gui" in body


@pytest.mark.fr("IR-029")
def test_main_package_stages_the_desktop_entry_and_icon_from_the_source_tree() -> None:
    body = _function_body("post_install() {")
    assert DESKTOP_STAGE in body
    assert ICON_STAGE in body


@pytest.mark.fr("IR-029")
def test_ginn_gui_moves_the_desktop_entry_and_icon_out_of_the_main_package() -> None:
    body = _function_body("ginn-gui_package() {")
    assert DESKTOP_MOVE in body
    assert ICON_MOVE in body


@pytest.mark.fr("AS-003")
def test_checksum_is_placeholder_or_sha256() -> None:
    assert CHECKSUM_VALUE.match(_template_fields()["checksum"])


@pytest.mark.fr("AS-003")
def test_checksum_placeholder_names_the_release_script_that_fills_it() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "tools/release.sh checksum" in text


@pytest.mark.fr("AS-003")
def test_pyproject_declares_the_ginn_and_ginn_gui_console_scripts() -> None:
    scripts = _pyproject()["project"]["scripts"]
    assert scripts == {"ginn": "ginn.__main__:main", "ginn-gui": "ginn.gui.app:main"}


@pytest.mark.fr("AS-003")
def test_pyproject_package_data_ships_the_distro_and_trust_toml_files() -> None:
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]["ginn"]
    assert "**/*.toml" in package_data


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
