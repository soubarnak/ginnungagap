"""Host backend selection and SystemdBackend command construction."""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from spaces import host
from spaces.host.base import HostBackend
from spaces.host.lxc import LxcBackend
from spaces.host.systemd import SystemdBackend


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(host.set_backend, None)
        host.set_backend(None)

    def select(self, env: dict[str, str], systemd_dir: bool) -> HostBackend:
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(host.os.path, "isdir", return_value=systemd_dir),
        ):
            host.set_backend(None)
            return host.get_backend()

    def test_env_override_wins(self) -> None:
        self.assertIsInstance(
            self.select({"SPACES_HOST_BACKEND": "lxc"}, True), LxcBackend
        )
        self.assertIsInstance(
            self.select({"SPACES_HOST_BACKEND": "systemd"}, False),
            SystemdBackend,
        )

    def test_autodetects_systemd_runtime_directory(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("SPACES_HOST_BACKEND", None)
            host.set_backend(None)
            with mock.patch.object(
                host.os.path, "isdir", return_value=True
            ) as isdir:
                self.assertIsInstance(host.get_backend(), SystemdBackend)
            isdir.assert_called_once_with("/run/systemd/system")
            host.set_backend(None)
            with mock.patch.object(host.os.path, "isdir", return_value=False):
                self.assertIsInstance(host.get_backend(), LxcBackend)

    def test_unknown_override_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.select({"SPACES_HOST_BACKEND": "bogus"}, True)

    def test_backend_is_cached_and_settable(self) -> None:
        first = self.select({"SPACES_HOST_BACKEND": "systemd"}, False)
        self.assertIs(host.get_backend(), first)
        replacement = LxcBackend()
        host.set_backend(replacement)
        self.assertIs(host.get_backend(), replacement)


class SystemdBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = SystemdBackend()
        patcher = mock.patch.object(subprocess, "run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)
        self.run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="A=1\n"
        )

    def test_unit_commands(self) -> None:
        self.assertTrue(self.backend.is_running("work"))
        self.run.assert_called_once_with(
            ["/usr/bin/machinectl", "--quiet", "show", "work"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.run.reset_mock()
        self.assertEqual(self.backend.start_unit("work"), 0)
        self.run.assert_called_once_with(
            ["/usr/bin/systemctl", "start", "spaces@work.service"],
            check=False,
        )
        self.run.reset_mock()
        self.backend.stop_unit("work")
        self.run.assert_called_once_with(
            ["/usr/bin/systemctl", "stop", "spaces@work.service"],
            check=True,
        )
        self.run.reset_mock()
        self.backend.try_restart_unit("work")
        self.run.assert_called_once_with(
            ["/usr/bin/systemctl", "try-restart", "spaces@work.service"],
            check=True,
        )
        self.run.reset_mock()
        self.backend.enable_user_autostart("alice", "work")
        self.run.assert_called_once_with(
            [
                "/usr/bin/systemctl",
                "--machine=alice@.host",
                "--user",
                "--no-reload",
                "reenable",
                "spaces@work.service",
            ],
            check=True,
        )

    def test_run_launcher_uses_popen(self) -> None:
        with mock.patch.object(subprocess, "Popen") as popen:
            self.backend.run_launcher(["nspawn", "-M", "work"], {"A": "1"})
        popen.assert_called_once_with(["nspawn", "-M", "work"], env={"A": "1"})

    def test_probes(self) -> None:
        self.run.return_value = subprocess.CompletedProcess([], 1)
        self.assertFalse(self.backend.probe_registered("work"))
        self.assertEqual(
            self.run.call_args.args[0],
            [
                "/usr/bin/machinectl",
                "--quiet",
                "--no-ask-password",
                "show",
                "--property=Leader",
                "--value",
                "work",
            ],
        )
        self.assertFalse(self.backend.probe_guest_shell("work"))
        self.assertEqual(
            self.run.call_args.args[0],
            [
                "/usr/bin/machinectl",
                "--quiet",
                "--no-ask-password",
                "--uid=root",
                "--",
                "shell",
                "work",
                "/usr/bin/true",
            ],
        )

    def test_exec_in_guest(self) -> None:
        self.backend.exec_in_guest(
            "alice",
            "work",
            ["id", "-u"],
            env={"B": "2", "A": "1"},
            check=True,
            stdout=subprocess.DEVNULL,
        )
        self.run.assert_called_once_with(
            [
                "/usr/bin/machinectl",
                "--quiet",
                "--uid=alice",
                "--setenv=A=1",
                "--setenv=B=2",
                "--",
                "shell",
                "work",
                "id",
                "-u",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with mock.patch.object(subprocess, "Popen") as popen:
            self.backend.spawn_in_guest("alice", "work", ["id"])
        popen.assert_called_once_with(
            [
                "/usr/bin/machinectl",
                "--quiet",
                "--uid=alice",
                "--",
                "shell",
                "work",
                "id",
            ]
        )

    def test_bind_and_unmount(self) -> None:
        self.backend.bind_into("work", "/src", "/dst", read_only=True)
        self.run.assert_called_once_with(
            [
                "/usr/bin/machinectl",
                "--quiet",
                "--no-ask-password",
                "--mkdir",
                "--read-only",
                "bind",
                "work",
                "/src",
                "/dst",
            ],
            check=True,
        )
        self.run.reset_mock()
        self.backend.bind_into("work", "/src", "/dst", mkdir=False)
        self.assertNotIn("--mkdir", self.run.call_args.args[0])
        self.assertNotIn("--read-only", self.run.call_args.args[0])
        self.run.reset_mock()
        self.backend.unmount_in("work", "/dst")
        self.run.assert_called_once_with(
            [
                "/usr/bin/systemd-run",
                "--machine=work",
                "--no-ask-password",
                "--quiet",
                "--wait",
                "--pipe",
                "--collect",
                "--service-type=exec",
                "--",
                "/usr/bin/umount",
                "--lazy",
                "--",
                "/dst",
            ],
            check=True,
        )

    def test_device_policy(self) -> None:
        self.backend.set_device_policy("work", "basic", [("/dev/fuse", "rwm")])
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/busctl")
        self.assertEqual(
            argv[7:],
            [
                "spaces@work.service",
                "true",
                "2",
                "DevicePolicy",
                "s",
                "closed",
                "DeviceAllow",
                "a(ss)",
                "1",
                "/dev/fuse",
                "rwm",
            ],
        )
        self.backend.set_device_policy("work", "full", [("/dev/fuse", "rwm")])
        argv = self.run.call_args.args[0]
        self.assertIn("auto", argv)
        self.assertEqual(argv[-1], "0")

    def test_user_environment(self) -> None:
        self.assertEqual(self.backend.host_user_environment(1000, 100), "A=1\n")
        self.assertEqual(
            self.run.call_args.args[0],
            [
                "/usr/bin/systemctl",
                "--user",
                "--no-ask-password",
                "show-environment",
            ],
        )
        self.assertEqual(
            self.run.call_args.kwargs["env"],
            {
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "XDG_RUNTIME_DIR": "/run/user/1000",
            },
        )
        self.run.return_value = subprocess.CompletedProcess([], 1, stdout="")
        self.assertIsNone(self.backend.host_user_environment(1000, 100))

    def test_static_values(self) -> None:
        self.assertEqual(
            self.backend.login_library_names(),
            ("systemd", "libsystemd.so.0"),
        )
        self.assertEqual(
            self.backend.session_bus_address(7), "unix:path=/run/user/7/bus"
        )

    def test_spawn_user_scope(self) -> None:
        with mock.patch.object(subprocess, "Popen") as popen:
            self.backend.spawn_user_scope(
                "app.scope",
                ["proxy", "--x"],
                {"A": "1"},
                description="desc",
                uid=1000,
                gid=100,
                pass_fds=(5,),
            )
        popen.assert_called_once_with(
            [
                "/usr/bin/systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--unit=app.scope",
                "--description=desc",
                "--",
                "proxy",
                "--x",
            ],
            env={"A": "1"},
            user=1000,
            group=100,
            pass_fds=(5,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_peer_in_space(self) -> None:
        with mock.patch(
            "spaces.host.systemd._cgroup_components",
            return_value={"machine-work.scope"},
        ):
            self.assertTrue(self.backend.peer_in_space(1, "work"))
            self.assertFalse(self.backend.peer_in_space(1, "other"))
        with mock.patch(
            "spaces.host.systemd._cgroup_components", side_effect=OSError
        ):
            self.assertFalse(self.backend.peer_in_space(1, "work"))


class LxcStubTests(unittest.TestCase):
    def test_every_method_raises(self) -> None:
        backend = LxcBackend()
        with self.assertRaisesRegex(NotImplementedError, "M2"):
            backend.is_running("work")
        with self.assertRaises(NotImplementedError):
            backend.exec_in_guest("root", "work", ["true"])
        with self.assertRaises(NotImplementedError):
            backend.peer_in_space(1, "work")
        with self.assertRaises(NotImplementedError):
            backend.login_library_names()


if __name__ == "__main__":
    unittest.main()
