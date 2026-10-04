"""The menu entries and icons that the Void package installs for the four distributions."""

from __future__ import annotations

import configparser
import re
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRIES = ROOT / "void" / "data" / "applications"
ICONS = ROOT / "data" / "icons" / "hicolor" / "256x256" / "apps"
TEMPLATE = ROOT / "void" / "srcpkgs" / "spaces" / "template"
# desktop file id -> the entry command it runs
COMMANDS = {"ubuntu": "ubuntu", "arch": "arch-linux", "fedora": "fedora", "kali": "kali"}


def read(path: Path) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # keys are case sensitive
    parser.read(path, encoding="utf-8")
    return parser["Desktop Entry"]


class DesktopEntryTests(unittest.TestCase):
    def test_exactly_the_four_distributions_have_an_entry(self) -> None:
        names = sorted(path.name for path in ENTRIES.glob("*.desktop"))
        self.assertEqual(names, sorted(f"spaces-{key}.desktop" for key in COMMANDS))

    def test_each_entry_runs_its_entry_command_in_a_terminal(self) -> None:
        for key, command in COMMANDS.items():
            with self.subTest(entry=key):
                entry = read(ENTRIES / f"spaces-{key}.desktop")
                self.assertEqual(entry["Type"], "Application")
                self.assertEqual(entry["Terminal"], "true")
                self.assertEqual(entry["Exec"], f"/usr/bin/{command}")
                self.assertEqual(entry["TryExec"], f"/usr/bin/{command}")
                self.assertEqual(entry["Icon"], f"spaces-{key}")
                self.assertTrue(entry["Name"].startswith("Space ("))

    def test_the_template_installs_the_entry_commands_they_run(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")
        loop = re.search(r"for d in (ubuntu fedora kali arch-linux); do\n\t\tvbin void/entry/enter-space \$d", template)
        self.assertIsNotNone(loop, "the entry command loop moved")
        self.assertEqual(set(loop.group(1).split()), set(COMMANDS.values()))

    def test_the_template_installs_every_entry_and_icon(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")
        loop = re.search(
            r"for d in (ubuntu arch fedora kali); do\n"
            r"\t\tvinstall void/data/applications/spaces-\$d\.desktop 644 usr/share/applications\n"
            r"\t\tvinstall data/icons/hicolor/256x256/apps/spaces-\$d\.png 644 usr/share/icons/hicolor/256x256/apps\n"
            r"\tdone",
            template,
        )
        self.assertIsNotNone(loop, "the menu entry loop is missing or changed")
        self.assertEqual(set(loop.group(1).split()), set(COMMANDS))

    def test_every_icon_is_a_256_pixel_png(self) -> None:
        for key in COMMANDS:
            with self.subTest(icon=key):
                data = (ICONS / f"spaces-{key}.png").read_bytes()
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", data[16:24]), (256, 256))

    def test_the_icon_script_makes_the_kali_icon_too(self) -> None:
        script = (ROOT / "art" / "distros" / "generate.sh").read_text(encoding="utf-8")
        self.assertIn("for distribution in arch fedora kali ubuntu; do", script)
        self.assertTrue((ROOT / "art" / "distros" / "kali.png").is_file())


if __name__ == "__main__":
    unittest.main()
