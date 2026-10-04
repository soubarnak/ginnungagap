from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import devices

PROC_DEVICES = """Character devices:
  1 mem
195 nvidia
195 nvidia-modeset
195 nvidiactl
236 nvidia-caps
506 nvidia-uvm

Block devices:
259 blkext
"""


class SysfslessMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        path = Path(self.temporary.name) / "devices"
        path.write_text(PROC_DEVICES, encoding="utf-8")
        patcher = mock.patch.object(devices, "PROC_DEVICES", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temporary.cleanup)

    def read(self, kind: str, major: int, minor: int) -> frozenset[str]:
        return devices._sysfsless_metadata(
            kind, os.makedev(major, minor)
        ).subsystems

    def test_render_and_compute_nodes_are_tagged(self) -> None:
        for major, minor in ((195, 0), (195, 254), (195, 255), (506, 0), (506, 1)):
            self.assertEqual(
                self.read("c", major, minor), frozenset({"nvidia"})
            )

    def test_caps_and_other_majors_are_not(self) -> None:
        for major, minor in ((236, 1), (1, 3), (999, 0)):
            self.assertEqual(self.read("c", major, minor), frozenset())

    def test_block_devices_and_missing_proc_file_are_ignored(self) -> None:
        self.assertEqual(self.read("b", 195, 0), frozenset())
        with mock.patch.object(
            devices, "PROC_DEVICES", Path("/nonexistent/devices")
        ):
            self.assertEqual(self.read("c", 195, 0), frozenset())

    def test_nvidia_subsystem_passes_the_basic_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("nvidia0", "nvidiactl"):
                (root / name).touch()
            fake = mock.Mock(side_effect=lambda kind, number: (
                devices._sysfsless_metadata(kind, number)
            ))
            real_lstat = Path.lstat

            def lstat(path: Path, **kwargs: object) -> object:
                result = real_lstat(path, **kwargs)
                if path.parent == root:
                    minor = 0 if path.name == "nvidia0" else 255
                    return mock.Mock(
                        st_mode=0o020666,
                        st_rdev=os.makedev(195, minor),
                        st_gid=0,
                    )
                return result

            with mock.patch.object(Path, "lstat", lstat):
                nodes = devices.discover(
                    "basic", device_root=root, metadata_reader=fake
                )
        self.assertEqual(
            [str(node.destination) for node in nodes],
            ["/dev/nvidia0", "/dev/nvidiactl"],
        )


if __name__ == "__main__":
    unittest.main()
