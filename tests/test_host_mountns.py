from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spaces.host import mountns


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        self.dev = Path(self.root) / "dev"
        (self.dev / "input").mkdir(parents=True)

    def prune(self, destination: str) -> None:
        mountns._prune_device_placeholder(destination, root=self.root)

    def test_empty_placeholder_and_empty_parent_are_removed(self) -> None:
        (self.dev / "input" / "event7").touch()
        self.prune("/dev/input/event7")
        self.assertFalse((self.dev / "input").exists())

    def test_parent_with_other_entries_stays(self) -> None:
        (self.dev / "input" / "event7").touch()
        (self.dev / "input" / "event8").touch()
        self.prune("/dev/input/event7")
        self.assertTrue((self.dev / "input" / "event8").exists())
        self.assertFalse((self.dev / "input" / "event7").exists())

    def test_non_empty_file_and_paths_outside_dev_are_kept(self) -> None:
        (self.dev / "input" / "data").write_text("x")
        self.prune("/dev/input/data")
        self.assertTrue((self.dev / "input" / "data").exists())
        mountns._prune_device_placeholder("/home/x/y")
        mountns._prune_device_placeholder("/dev/null")
        self.assertTrue(self.dev.exists())


if __name__ == "__main__":
    unittest.main()
