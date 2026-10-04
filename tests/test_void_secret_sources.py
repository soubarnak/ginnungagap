"""Private key directories never become home mounts (the code-level gate on Void).

Upstream's SELinux policy keeps a space away from ~/.ssh and ~/.gnupg even when a home
entry names them. AppArmor cannot do that for binds the host makes, so the gate is the
validation of home entries; these tests pin it (see void/docs/apparmor-review.md).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from spaces import core
from spaces import launch as launch_module


class SecretSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def user(self, *permitted: str) -> launch_module.SpaceUser:
        home = self.root / "host"
        (home / ".ssh").mkdir(parents=True, exist_ok=True)
        (home / ".gnupg").mkdir(exist_ok=True)
        (home / ".ssh" / "config").write_text("Host x\n")
        (home / ".ssh" / "id_ed25519").write_text("secret\n")
        (home / ".gnupg" / "private-keys-v1.d").mkdir(exist_ok=True)
        space_home = self.root / "space"
        space_home.mkdir(exist_ok=True)
        return launch_module.SpaceUser(
            uid=os.getuid(),
            gid=os.getgid(),
            name="alice",
            host_home=home,
            space_home=space_home,
            guest_home=launch_module.PurePosixPath("/home/alice"),
            permitted_home=tuple(permitted),
            administrator=True,
            desktop=False,
            mounted_drives=False,
        )

    def test_whole_ssh_and_gnupg_directories_are_not_mounted(self) -> None:
        user = self.user(".ssh", ".gnupg")
        with self.assertLogs(launch_module.logger, level="WARNING"):
            self.assertEqual(launch_module._prepare_mounts((user,)), ())
        self.assertFalse((user.space_home / ".ssh").exists())
        self.assertFalse((user.space_home / ".gnupg").exists())

    def test_ssh_config_is_the_only_nested_entry(self) -> None:
        self.assertEqual(core.validate_home_name(".ssh/config"), ".ssh/config")
        for name in (".ssh/id_ed25519", ".gnupg/pubring.kbx", ".ssh/../.ssh/config", "../.ssh/config"):
            with self.assertRaises(core.SpacesError, msg=name):
                core.validate_home_name(name)

    def test_ssh_config_alone_is_mounted(self) -> None:
        user = self.user(".ssh/config")
        mounts = launch_module._prepare_mounts((user,))
        self.assertEqual([m.destination for m in mounts], ["/home/alice/.ssh/config"])
        self.assertEqual(mounts[0].source, user.host_home / ".ssh" / "config")


if __name__ == "__main__":
    unittest.main()
