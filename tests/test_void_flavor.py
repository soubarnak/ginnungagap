from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spaces import host_config
from spaces.host import flavor, nvidia


class ClassifyTests(unittest.TestCase):
    def test_kde_family(self) -> None:
        for value in ("KDE", "plasma", "Spaces:KDE", "KDE:Plasma", "Plasma"):
            self.assertEqual(flavor.classify(value), "kde", value)

    def test_everything_else_is_gtk(self) -> None:
        for value in ("niri", "sway", "Hyprland", "GNOME", "XFCE", "ubuntu:GNOME", "KDEnlive"):
            self.assertEqual(flavor.classify(value), "gtk", value)

    def test_empty_is_unknown(self) -> None:
        self.assertIsNone(flavor.classify(None))
        self.assertIsNone(flavor.classify("  "))


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.remembered = Path(self.temporary.name) / "state" / "desktop-flavor"

    def test_auto_follows_the_session_and_remembers_it(self) -> None:
        self.assertEqual(
            flavor.resolve_auto(remembered=self.remembered, desktop="KDE"), "kde"
        )
        # No session later (boot time regeneration): keep the last answer.
        self.assertEqual(
            flavor.resolve_auto(remembered=self.remembered, desktop=""), "kde"
        )

    def test_auto_without_any_information_is_gtk(self) -> None:
        self.assertEqual(
            flavor.resolve_auto(remembered=self.remembered, desktop=""), "gtk"
        )

    def test_session_desktop_uses_given_environment(self) -> None:
        self.assertEqual(
            flavor.session_desktop(environment={"XDG_CURRENT_DESKTOP": "niri"}), "niri"
        )

    def test_explicit_values_and_rejection(self) -> None:
        self.assertEqual(flavor.resolve("kde"), "kde")
        self.assertEqual(flavor.resolve("gtk"), "gtk")
        with self.assertRaises(ValueError):
            flavor.resolve("qt")


class PackageTests(unittest.TestCase):
    def test_gtk_sets_exist_for_every_distro_and_never_remove_kde(self) -> None:
        for distro in ("ubuntu", "kali", "arch", "fedora"):
            packages = flavor.packages_for("gtk", distro)
            self.assertIn("xdg-desktop-portal-gtk", packages, distro)
            self.assertFalse([p for p in packages if "kde" in p or "plasma" in p], distro)
            self.assertEqual(flavor.packages_for("kde", distro), ())
        self.assertIn("qt6-gtk-platformtheme", flavor.packages_for("gtk", "ubuntu"))
        self.assertEqual(flavor.packages_for("gtk", "custom"), ())

    def test_install_commands_are_non_interactive(self) -> None:
        deb = flavor.install_command("ubuntu", ["a", "b"])
        self.assertEqual(deb[:2], ["env", "DEBIAN_FRONTEND=noninteractive"])
        self.assertIn("-y", deb)
        self.assertEqual(deb[-2:], ["a", "b"])
        self.assertIn("--noconfirm", flavor.install_command("arch", ["a"]))
        self.assertIn("-y", flavor.install_command("fedora", ["a"]))
        self.assertIsNone(flavor.install_command("custom", ["a"]))

    def test_configured_setting_prefers_void_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base.json"
            extras = Path(directory) / "void.json"
            self.assertEqual(flavor.configured_setting(extras, base), "auto")
            base.write_text(json.dumps({"desktop_flavor": "kde"}))
            self.assertEqual(flavor.configured_setting(extras, base), "kde")
            extras.write_text(json.dumps({"desktop_flavor": "gtk"}))
            self.assertEqual(flavor.configured_setting(extras, base), "gtk")


class GenerateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "base.json"
        self.base.write_text(
            json.dumps(
                {
                    "version": 1,
                    "desktop_flavor": "auto",
                    "distros": {
                        "ubuntu": {"packages": ["tmux"]},
                        "arch": {"packages": ["tmux"]},
                    },
                }
            )
        )
        self.extras = self.root / "void.json"

    def packages(self, resolver, distro="ubuntu"):
        text = nvidia.generate(self.base, self.extras, None, resolver)
        self.assertEqual(nvidia.validate(text), [])
        return json.loads(text)["distros"][distro]["packages"]

    def test_gtk_adds_to_the_base_packages(self) -> None:
        packages = self.packages(lambda setting: "gtk")
        self.assertEqual(packages[0], "tmux")
        self.assertIn("gnome-themes-extra", packages)
        self.assertIn("adw-gtk-theme", self.packages(lambda s: "gtk", "arch"))

    def test_kde_and_missing_resolver_add_nothing(self) -> None:
        self.assertEqual(self.packages(lambda setting: "kde"), ["tmux"])
        self.assertEqual(self.packages(None), ["tmux"])

    def test_void_json_setting_is_passed_to_the_resolver(self) -> None:
        seen = []
        self.extras.write_text(json.dumps({"desktop_flavor": "kde"}))
        self.packages(lambda setting: seen.append(setting) or setting)
        self.assertEqual(seen, ["kde"])
        seen.clear()
        self.extras.unlink()
        self.packages(lambda setting: seen.append(setting) or "kde")
        self.assertEqual(seen, ["auto"])

    def test_invalid_setting_is_rejected(self) -> None:
        self.extras.write_text(json.dumps({"desktop_flavor": "qt"}))
        with self.assertRaises(ValueError):
            nvidia.generate(self.base, self.extras, None, lambda s: "gtk")

    def test_extra_packages_stay_after_the_flavour(self) -> None:
        self.extras.write_text(
            json.dumps({"distros": {"ubuntu": {"packages": ["htop", "tmux"]}}})
        )
        packages = self.packages(lambda s: "gtk")
        self.assertEqual(packages.count("tmux"), 1)
        self.assertEqual(packages[-1], "htop")

    def test_shipped_base_config_is_accepted(self) -> None:
        shipped = Path(__file__).resolve().parents[1] / "void/data/config.base.json"
        text = nvidia.generate(shipped, self.extras, None, lambda s: "gtk")
        self.assertEqual(nvidia.validate(text), [])
        config = json.loads(text)
        self.assertIn("xdg-desktop-portal-gtk", config["distros"]["fedora"]["packages"])
        self.assertTrue(host_config.load is not None)


class MultilibTests(unittest.TestCase):
    def test_arch_multilib_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(nvidia.arch_multilib(root))
            (root / "etc").mkdir()
            (root / "etc/pacman.conf").write_text("#[multilib]\n#Include = x\n")
            self.assertFalse(nvidia.arch_multilib(root))
            (root / "etc/pacman.conf").write_text("[core]\n[multilib]\nInclude = x\n")
            self.assertTrue(nvidia.arch_multilib(root))

    def test_lib32_overlay_is_dropped_without_multilib(self) -> None:
        farm = Path("/farm")
        full = nvidia.nvidia_distro_config(farm, "arch")
        self.assertIn("/usr/lib32", {o["destination"] for o in full["overlays"]})
        reduced = nvidia.nvidia_distro_config(farm, "arch", lib32_overlay=False)
        self.assertEqual(
            {o["destination"] for o in reduced["overlays"]}, {"/usr/share", "/usr/lib"}
        )


class CreateHookTests(unittest.TestCase):
    def test_refresh_is_skipped_for_normal_users_and_never_raises(self) -> None:
        from unittest import mock

        from spaces import priv

        with mock.patch("os.geteuid", return_value=1000), mock.patch.object(nvidia, "sync") as sync:
            priv._refresh_host_config()
            sync.assert_not_called()
        with mock.patch("os.geteuid", return_value=0), mock.patch.object(
            nvidia, "sync", side_effect=OSError("denied")
        ), mock.patch.object(nvidia.BASE_PATH.__class__, "exists", return_value=True):
            priv._refresh_host_config()  # prints a warning, does not raise


if __name__ == "__main__":
    unittest.main()
