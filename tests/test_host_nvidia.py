from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from spaces import host_config
from spaces.host import nvidia

FILES = {
    "usr/lib/libGLX_nvidia.so.595.1": "libs",
    "usr/lib/libEGL_nvidia.so.595.1": "libs",
    "usr/lib/libnvidia-gtk3.so.595.1": "gtk",
    "usr/lib/libnvidia-ml.so.595.1": "ml",
    "usr/lib32/libGLX_nvidia.so.595.1": "libs32",
    "usr/lib/gbm/nvidia-drm_gbm.so": "gbm",
    "usr/lib/nvidia/xorg/libglxserver_nvidia.so.595.1": "xorg",
    "usr/bin/nvidia-smi": "smi",
    "usr/bin/nvidia-settings": "settings",
    "usr/share/vulkan/icd.d/nvidia_icd.json": "{}",
    "usr/share/nvidia/nvidia-application-profiles-595.1-key-documentation": "doc",
    "usr/share/applications/nvidia-settings.desktop": "x",
}
SYMLINKS = {
    "usr/lib/libGLX_nvidia.so.0": "libGLX_nvidia.so.595.1",
    "usr/lib/libnvidia-ml.so.1": "libnvidia-ml.so.595.1",
    "usr/lib/libnvidia-ml.so": "libnvidia-ml.so.1",
    "usr/lib/libdangling.so.1": "missing",
    "usr/lib32/libGLX_nvidia.so.0": "libGLX_nvidia.so.595.1",
}


class FarmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.host = self.root / "host"
        for relative, content in FILES.items():
            path = self.host / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        for relative, target in SYMLINKS.items():
            os.symlink(target, self.host / relative)

    def lister(self, package: str) -> list[str]:
        if package != "nvidia-libs":
            return []
        return ["/" + name for name in (*FILES, *SYMLINKS)]

    def collect(self) -> dict[str, str]:
        return nvidia.collect(lister=self.lister, host_root=self.host)

    def test_collect_selects_vendor_files_only(self) -> None:
        entries = self.collect()
        self.assertEqual(
            sorted(entries),
            [
                "bin/nvidia-smi",
                "lib/gbm/nvidia-drm_gbm.so",
                "lib/libEGL_nvidia.so.595.1",
                "lib/libGLX_nvidia.so.0",
                "lib/libGLX_nvidia.so.595.1",
                "lib/libnvidia-ml.so.1",
                "lib/libnvidia-ml.so.595.1",
                "lib32/libGLX_nvidia.so.0",
                "lib32/libGLX_nvidia.so.595.1",
                "share/vulkan/icd.d/nvidia_icd.json",
            ],
        )

    def test_aliases_point_at_the_final_file(self) -> None:
        entries = self.collect()
        self.assertEqual(
            entries["lib/libGLX_nvidia.so.0"],
            str(self.host / "usr/lib/libGLX_nvidia.so.595.1"),
        )

    def test_build_farm_layout_current_and_stale_cleanup(self) -> None:
        farm_root = self.root / "farm"
        entries = self.collect()
        nvidia.build_farm(farm_root, "595.1_1", entries)
        stale = farm_root / "590.0_1"
        stale.mkdir()
        nvidia.build_farm(farm_root, "595.1_1", entries)
        self.assertFalse(stale.exists())
        current = farm_root / "current"
        self.assertEqual(os.readlink(current), "595.1_1")
        link = current / "lib/libGLX_nvidia.so.0"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_text(), "libs")
        for directory in ("lib", "lib32", "share", "bin", "opencl"):
            self.assertTrue((current / directory).is_dir())

    def test_generated_config_is_accepted_by_host_config(self) -> None:
        farm_root = self.root / "farm"
        nvidia.build_farm(farm_root, "595.1_1", self.collect())
        base = self.root / "base.json"
        base.write_text(
            json.dumps(
                {
                    "version": 1,
                    "distros": {
                        "ubuntu": {"packages": ["tmux"]},
                        "arch": {"packages": ["tmux"]},
                        "fedora": {"packages": ["tmux"]},
                    },
                }
            )
        )
        extras = self.root / "void.json"
        extras.write_text(
            json.dumps({"distros": {"ubuntu": {"packages": ["tmux", "htop"]}}})
        )
        text = nvidia.generate(base, extras, farm_root / "current")
        self.assertEqual(nvidia.validate(text), [])
        data = json.loads(text)
        ubuntu = data["distros"]["ubuntu"]
        self.assertEqual(ubuntu["packages"], ["tmux", "htop"])
        destinations = {o["destination"] for o in ubuntu["overlays"]}
        self.assertEqual(
            destinations,
            {"/usr/share", "/usr/lib/x86_64-linux-gnu", "/usr/lib/i386-linux-gnu"},
        )
        self.assertEqual(
            {o["destination"] for o in data["distros"]["fedora"]["overlays"]},
            {"/usr/share", "/usr/lib64", "/usr/lib"},
        )
        self.assertEqual(
            {o["destination"] for o in data["distros"]["arch"]["overlays"]},
            {"/usr/share", "/usr/lib", "/usr/lib32"},
        )
        self.assertIn(
            "/usr/bin/nvidia-smi", {m["destination"] for m in ubuntu["mounts"]}
        )
        loaded = host_config.load(self._write(text))
        self.assertEqual(len(loaded.overlays_for("ubuntu")), 3)

    def _write(self, text: str) -> Path:
        path = self.root / "check.json"
        path.write_text(text)
        return path

    def test_without_driver_no_nvidia_entries(self) -> None:
        base = self.root / "base.json"
        base.write_text('{"version": 1, "distros": {"ubuntu": {"packages": []}}}')
        text = nvidia.generate(base, self.root / "none.json", None)
        ubuntu = json.loads(text)["distros"]["ubuntu"]
        self.assertEqual(ubuntu["mounts"], [])
        self.assertEqual(ubuntu["overlays"], [])

    def test_invalid_extras_are_rejected(self) -> None:
        base = self.root / "base.json"
        base.write_text('{"version": 1, "distros": {}}')
        extras = self.root / "void.json"
        extras.write_text('{"distros": {"ubuntu": {"bogus": 1}}}')
        with self.assertRaises(ValueError):
            nvidia.generate(base, extras, None)


class DriverVersionTests(unittest.TestCase):
    def test_reads_the_kernel_module_version(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version"
            path.write_text(
                "NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  "
                "595.104.02  Release Build  (dvs-builder@host)  Thu Sep 17\n"
                "GCC version:  gcc version 14.2.1 20250405\n"
            )
            with mock.patch.object(nvidia, "DRIVER_VERSION_PATH", path):
                self.assertEqual(nvidia.driver_version(), "595.104.02")
            with mock.patch.object(nvidia, "DRIVER_VERSION_PATH", path / "x"):
                self.assertIsNone(nvidia.driver_version())


class InstallConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "config.json"

    def test_first_write_then_regenerate(self) -> None:
        self.assertEqual(nvidia.install_config(self.path, "a\n"), "written")
        self.assertEqual(nvidia.install_config(self.path, "a\n"), "unchanged")
        self.assertEqual(nvidia.install_config(self.path, "b\n"), "written")
        self.assertEqual(self.path.read_text(), "b\n")

    def test_hand_edited_file_is_kept(self) -> None:
        nvidia.install_config(self.path, "a\n")
        self.path.write_text("edited\n")
        self.assertEqual(nvidia.install_config(self.path, "b\n"), "kept")
        self.assertEqual(self.path.read_text(), "edited\n")
        self.assertEqual(
            self.path.with_name("config.json.new").read_text(), "b\n"
        )

    def test_unmarked_foreign_file_is_kept_but_legacy_is_adopted(self) -> None:
        self.path.write_text('{"version": 1}\n')
        self.assertEqual(nvidia.install_config(self.path, "b\n"), "kept")
        packages = ["fastfetch", "screen", "tmux", "zsh"]
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "distros": {
                        d: {"packages": packages}
                        for d in ("arch", "fedora", "kali", "ubuntu")
                    },
                }
            )
        )
        self.assertEqual(nvidia.install_config(self.path, "b\n"), "written")


if __name__ == "__main__":
    unittest.main()
