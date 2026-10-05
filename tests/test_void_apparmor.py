"""The AppArmor profile of the Void port has to stay loadable by Void's lxc-start profile.

Void loads /etc/apparmor.d at every boot (runit core service 09-apparmor.sh), which confines
/usr/bin/lxc-start with usr.bin.lxc-start. That profile only allows `change_profile -> lxc-*`,
so a container profile with another name makes every launch fail after a reboot. Nothing
else catches it: tests that run without a boot never have lxc-start confined.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spaces.host import doctor, lxc  # noqa: E402

PROFILE_FILE = ROOT / "void/apparmor/lxc-spaces-container"
USERNS_PROFILE_FILE = ROOT / "void/apparmor/lxc-spaces-container-userns"
START_CONTAINER = """\
  change_profile -> lxc-*,
  change_profile -> lxc-**,
  change_profile -> unconfined,
  change_profile -> :lxc-*:unconfined,
"""


class ProfileNameTests(unittest.TestCase):
    def test_every_user_of_the_profile_agrees_on_the_name(self) -> None:
        declared = re.search(r"^profile (\S+) ", PROFILE_FILE.read_text(), re.M)
        self.assertIsNotNone(declared)
        name = declared[1]
        self.assertEqual(PROFILE_FILE.name, name)
        self.assertEqual(lxc.APPARMOR_PROFILE, name)
        self.assertEqual(doctor.PROFILE, name)
        spec_source = (ROOT / "src/spaces/host/lxc_config.py").read_text()
        self.assertIn(f'APPARMOR_PROFILE = "{name}"', spec_source)

    def test_the_user_namespace_profile_has_a_matching_name(self) -> None:
        declared = re.search(r"^profile (\S+) ", USERNS_PROFILE_FILE.read_text(), re.M)
        self.assertIsNotNone(declared)
        name = declared[1]
        self.assertEqual(USERNS_PROFILE_FILE.name, name)
        self.assertEqual(lxc.APPARMOR_PROFILE_USERNS, name)
        self.assertEqual(doctor.PROFILE_USERNS, name)
        self.assertTrue(name.startswith("lxc-"))
        self.assertIn(f'APPARMOR_PROFILE_USERNS = "{name}"', (ROOT / "src/spaces/host/lxc_config.py").read_text())

    def test_name_matches_what_lxc_start_may_change_to(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "abstractions/lxc").mkdir(parents=True)
            (root / "abstractions/lxc/start-container").write_text(START_CONTAINER)
            (root / "usr.bin.lxc-start").write_text(
                "/usr/bin/lxc-start flags=(attach_disconnected) {\n"
                "  #include <abstractions/lxc/start-container>\n}\n"
            )
            self.assertTrue(doctor.lxc_start_allows(lxc.APPARMOR_PROFILE, root))
            self.assertFalse(doctor.lxc_start_allows("spaces-container", root))

    def test_installed_distro_profile_allows_the_name(self) -> None:
        if not Path("/etc/apparmor.d/usr.bin.lxc-start").exists():
            self.skipTest("no LXC AppArmor profile on this host")
        self.assertTrue(doctor.lxc_start_allows(lxc.APPARMOR_PROFILE))


def rules(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#") and not line.startswith("profile ")
    }


class UsernsProfileTests(unittest.TestCase):
    def test_it_is_the_default_profile_plus_a_fresh_proc_mount_and_nothing_else(self) -> None:
        base, userns = rules(PROFILE_FILE), rules(USERNS_PROFILE_FILE)
        self.assertEqual(set(), base - userns, "a rule of the default profile is missing")
        self.assertEqual({"mount fstype=proc,"}, userns - base)

    def test_the_default_profile_still_refuses_proc_sysfs_and_cgroup_v1(self) -> None:
        for path in (PROFILE_FILE, USERNS_PROFILE_FILE):
            for rule in rules(path):
                self.assertFalse(rule.startswith("mount fstype=sysfs"), (path.name, rule))
                self.assertFalse(rule.startswith("mount fstype=cgroup "), (path.name, rule))
        self.assertNotIn("mount fstype=proc,", rules(PROFILE_FILE))

    def test_the_default_profile_stays_the_one_without_proc_for_other_spaces(self) -> None:
        self.assertFalse(any("fstype=proc" in rule for rule in rules(PROFILE_FILE)))


class PackageTests(unittest.TestCase):
    def test_template_installs_the_profile_under_its_name(self) -> None:
        template = (ROOT / "void/srcpkgs/spaces/template").read_text()
        self.assertIn(
            "vinstall void/apparmor/lxc-spaces-container 644 etc/apparmor.d", template
        )
        self.assertIn(
            "vinstall void/apparmor/lxc-spaces-container-userns 644 etc/apparmor.d", template
        )

    def test_hooks_load_and_unload_the_profile_under_its_name(self) -> None:
        install = (ROOT / "void/srcpkgs/spaces/INSTALL").read_text()
        remove = (ROOT / "void/srcpkgs/spaces/REMOVE").read_text()
        for text, flag in ((install, "-r"), (remove, "-R")):
            self.assertIn(f"apparmor_parser {flag} /etc/apparmor.d/${{profile}}", text)
            self.assertIn("lxc-spaces-container lxc-spaces-container-userns", text)


if __name__ == "__main__":
    unittest.main()
