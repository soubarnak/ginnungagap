"""LxcBackend behaviour with fake subprocesses and temporary directories."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from spaces import core
from spaces.host import lxc


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                "SPACES_LXC_PATH": str(self.root / "lxc"),
                "SPACES_LXC_WRAPPER": "/wrap/spaces-lxc",
                "SPACES_RUNIT_SVDIR": str(self.root / "sv"),
                "SPACES_RUNIT_SERVICE_DIR": str(self.root / "service"),
                "SPACES_PRIV": "/opt/spaces.priv",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.root / "service").mkdir()
        (self.root / "sv").mkdir()
        self.backend = lxc.LxcBackend()


class ServiceTests(Base):
    def adopt(self, name: str) -> None:
        # Stand in for runsv creating the supervise directory.
        patcher = mock.patch.object(lxc, "RUNIT_SUPERVISE", str(self.root / "run"))
        patcher.start()
        self.addCleanup(patcher.stop)
        ok = self.root / "run" / f"supervise.spaces-{name}" / "ok"
        ok.parent.mkdir(parents=True)
        ok.touch()

    def test_service_files(self) -> None:
        self.adopt("work")
        link = lxc.ensure_service("work")
        directory = self.root / "sv" / "spaces-work"
        self.assertEqual(link, (self.root / "service").resolve() / "spaces-work")
        self.assertEqual(os.readlink(link), str(directory))
        run = (directory / "run").read_text()
        self.assertEqual(
            run.splitlines()[:2], ["#!/bin/sh", "exec 2>&1"]
        )
        self.assertIn("exec /opt/spaces.priv launch work", run)
        self.assertIn(
            f"rm -f {self.root}/lxc/work/ready", (directory / "finish").read_text()
        )
        log_run = (directory / "log" / "run").read_text()
        self.assertIn("mkdir -p /var/log/spaces/work", log_run)
        self.assertIn("exec svlogd -tt /var/log/spaces/work", log_run)
        for script in ("run", "finish", "log/run"):
            self.assertEqual(stat.S_IMODE((directory / script).stat().st_mode), 0o755)
        self.assertEqual((directory / "down").read_text(), "")
        self.assertEqual(
            os.readlink(directory / "supervise"),
            f"{self.root}/run/supervise.spaces-work",
        )
        self.assertEqual(
            os.readlink(directory / "log" / "supervise"),
            f"{self.root}/run/supervise.spaces-work.log",
        )

    def test_ensure_is_idempotent_and_keeps_down_file(self) -> None:
        self.adopt("work")
        lxc.ensure_service("work")
        (self.root / "sv/spaces-work/run").write_text("tampered")
        lxc.ensure_service("work")
        self.assertIn("launch work", (self.root / "sv/spaces-work/run").read_text())
        self.assertTrue((self.root / "sv/spaces-work/down").exists())

    def test_service_dir_symlink_is_resolved(self) -> None:
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(real)
        os.environ["SPACES_RUNIT_SERVICE_DIR"] = str(alias)
        self.adopt("work")
        lxc.ensure_service("work")
        self.assertTrue((real / "spaces-work").is_symlink())

    def test_times_out_without_runsv(self) -> None:
        with (
            mock.patch.object(lxc, "SERVICE_TIMEOUT", 0.3),
            mock.patch.object(lxc, "RUNIT_SUPERVISE", str(self.root / "none")),
        ):
            with self.assertRaises(TimeoutError):
                lxc.ensure_service("work")

    def test_unsafe_names_are_rejected(self) -> None:
        for name in ("../x", "a/b", "", "-x ", ".hidden"):
            with self.assertRaises(ValueError):
                lxc.ensure_service(name)

    def test_forget_removes_service(self) -> None:
        self.adopt("work")
        link = lxc.ensure_service("work")
        with (
            mock.patch.object(lxc, "_sv"),
            mock.patch.object(lxc.LxcBackend, "_state", return_value=None),
        ):
            self.backend.forget_unit("work")
        self.assertFalse(link.is_symlink())
        self.assertFalse((self.root / "sv/spaces-work").exists())

    def test_start_unit_waits_for_marker(self) -> None:
        marker = self.root / "lxc" / "work" / "ready"
        marker.parent.mkdir(parents=True)
        states = iter(["down", "run", "run"])

        def fake_sv(*args: str):
            return subprocess.CompletedProcess(args, 0, "", "")

        def state(name: str) -> str:
            value = next(states, "run")
            if value == "run":
                marker.touch()
            return value

        with (
            mock.patch.object(lxc, "ensure_service"),
            mock.patch.object(lxc, "_sv", fake_sv),
            mock.patch.object(lxc, "_service_state", state),
            mock.patch.object(lxc.time, "sleep"),
        ):
            self.assertEqual(self.backend.start_unit("work"), 0)

    def test_start_unit_fails_when_service_dies(self) -> None:
        with (
            mock.patch.object(lxc, "ensure_service"),
            mock.patch.object(
                lxc, "_sv", lambda *a: subprocess.CompletedProcess(a, 0, "", "")
            ),
            mock.patch.object(lxc, "_service_state", return_value="down"),
            mock.patch.object(lxc.time, "sleep"),
        ):
            self.assertEqual(self.backend.start_unit("work"), 1)

    def test_autostart_users_are_deduplicated(self) -> None:
        state = self.root / "state"
        (state / "work").mkdir(parents=True)
        with mock.patch.object(core, "STATE_ROOT", state):
            self.backend.enable_user_autostart("alice", "work")
            self.backend.enable_user_autostart("bob", "work")
            self.backend.enable_user_autostart("alice", "work")
            path = state / "work" / "autostart-users"
            self.assertEqual(path.read_text(), "alice\nbob\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            with self.assertRaises(subprocess.CalledProcessError):
                self.backend.enable_user_autostart("bad user", "work")
            with self.assertRaises(subprocess.CalledProcessError):
                self.backend.enable_user_autostart("alice", "missing")


class CommandTests(Base):
    def run_capture(self, call):
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(lxc.subprocess, "run", fake_run),
            mock.patch.object(lxc.shutil, "which", lambda tool: f"/bin/{tool}"),
        ):
            call()
        return calls

    def test_root_exec_uses_lxc_attach_with_variables(self) -> None:
        (command,) = self.run_capture(
            lambda: self.backend.exec_in_guest(
                "root", "work", ["/bin/id"], env={"B": "2", "A": "1"}
            )
        )
        self.assertEqual(
            command,
            [
                "/wrap/spaces-lxc",
                "/bin/lxc-attach",
                "-P",
                str(self.root / "lxc"),
                "-n",
                "work",
                "--clear-env",
                "-v",
                "A=1",
                "-v",
                "B=2",
                "--",
                "/bin/id",
            ],
        )

    def test_user_exec_uses_systemd_run_pipe_or_pty(self) -> None:
        for tty, flag in ((False, "--pipe"), (True, "--pty")):
            with self.subTest(tty=tty), mock.patch.object(
                lxc.sys.stdin, "isatty", return_value=tty, create=True
            ):
                (command,) = self.run_capture(
                    lambda: self.backend.exec_in_guest(
                        "alice", "work", ["ls", "-l"], env={"K": "v"}, check=True
                    )
                )
                tail = command[command.index("--") + 1 :]
                self.assertEqual(tail[:2], ["/usr/bin/systemd-run", "--quiet"])
                self.assertIn(flag, tail)
                self.assertIn("--uid=alice", tail)
                self.assertIn("PAMName=login", tail)
                self.assertIn("--setenv=K=v", tail)
                self.assertNotIn("-v", command)
                self.assertEqual(tail[-2:], ["ls", "-l"])
                self.assertNotIn("--set-var", command)

    def test_probes(self) -> None:
        def fake_run(command, **kwargs):
            if "lxc-info" in command[1]:
                return subprocess.CompletedProcess(command, 0, "RUNNING\n", "")
            return subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.object(lxc.subprocess, "run", fake_run),
            mock.patch.object(lxc, "_quiet", lambda c, **k: fake_run(c)),
        ):
            self.assertTrue(self.backend.is_running("work"))
            self.assertTrue(self.backend.probe_registered("work"))
            self.assertTrue(self.backend.probe_guest_shell("work"))

    def test_bind_into_runs_mountns_subprocess(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, "4242\n", "")

        with mock.patch.object(lxc.subprocess, "run", fake_run):
            self.backend.bind_into("work", "/src", "/dst", read_only=True)
            self.backend.unmount_in("work", "/dst")
        bind, unbind = calls[1], calls[3]
        self.assertEqual(bind[1:4], ["-m", "spaces.host.mountns", "bind"])
        self.assertEqual(bind[4:], ["4242", "--read-only", "--", "/src", "/dst"])
        self.assertEqual(unbind[3:], ["unbind", "4242", "--", "/dst"])

    def test_spawn_user_scope_drops_privileges(self) -> None:
        with mock.patch.object(lxc.subprocess, "Popen") as popen:
            self.backend.spawn_user_scope(
                "unit", ["/bin/true"], {"A": "1"}, description="d", uid=7, gid=8,
                pass_fds=(5,),
            )
        kwargs = popen.call_args.kwargs
        self.assertEqual(popen.call_args.args[0], ["/bin/true"])
        self.assertEqual(
            (kwargs["user"], kwargs["group"], kwargs["extra_groups"]), (7, 8, [])
        )
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["pass_fds"], (5,))

    def test_login_library(self) -> None:
        self.assertEqual(
            self.backend.login_library_names(), ("elogind", "libelogind.so.0")
        )


class DevicePolicyTests(Base):
    def test_policy_files_without_a_running_container(self) -> None:
        with mock.patch.object(lxc.LxcBackend, "is_running", return_value=False):
            self.backend.set_device_policy(
                "work", "disabled", [("/dev/char/226:0", "rw")]
            )
            runtime = self.root / "lxc" / "work"
            text = (runtime / "devices.conf").read_text()
            self.assertIn("devices.deny = a", text)
            self.assertIn("devices.allow = c 226:0 rw", text)
            self.backend.set_device_policy("work", "full", ())
            self.assertNotIn("deny", (runtime / "devices.conf").read_text())
            self.assertEqual(json.loads((runtime / "policy.json").read_text())["level"], "full")

    def test_bad_permissions_raise_called_process_error(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            self.backend.set_device_policy("work", "disabled", [("/dev/char/1:3", "x")])

    def test_live_update_allows_before_denying(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_run(command, **kwargs):
            calls.append((command[command.index("-n") + 2], command[-1]))
            return subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.object(lxc.LxcBackend, "is_running", return_value=True),
            mock.patch.object(lxc.subprocess, "run", fake_run),
        ):
            self.backend.set_device_policy("work", "disabled", [("/dev/char/9:9", "rw")])
            calls.clear()
            self.backend.set_device_policy("work", "disabled", [("/dev/char/8:8", "rw")])
        self.assertEqual(
            calls,
            [("devices.allow", "c 8:8 rw"), ("devices.deny", "c 9:9 rw")],
        )


class HostIntegrationTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.proc = self.root / "proc"
        self.run_user = self.root / "runuser"
        for name, value in (("PROC", self.proc), ("RUN_USER", self.run_user)):
            patcher = mock.patch.object(lxc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.proc.mkdir()
        self.run_user.mkdir()

    def fake_process(self, pid: int, environ: bytes, cgroup: str = "") -> None:
        directory = self.proc / str(pid)
        directory.mkdir()
        (directory / "environ").write_bytes(environ)
        (directory / "cgroup").write_text(cgroup)

    def test_peer_in_space(self) -> None:
        self.fake_process(10, b"", "0::/spaces/work/payload/guest/init.scope\n")
        self.fake_process(11, b"", "0::/spaces/work/monitor\n")
        self.fake_process(12, b"", "0::/spaces/workshop/payload/x\n")
        self.fake_process(13, b"", "0::/spaces-work/x\n1:name=elogind:/\n")
        self.fake_process(1, b"", "0::/init.scope\n")
        self.assertTrue(self.backend.peer_in_space(10, "work"))
        self.assertFalse(self.backend.peer_in_space(11, "work"))
        self.assertFalse(self.backend.peer_in_space(12, "work"))
        self.assertTrue(self.backend.peer_in_space(13, "work"))
        self.assertFalse(self.backend.peer_in_space(1, "work"))
        self.assertFalse(self.backend.peer_in_space(999, "work"))

    def test_environment_hook_file(self) -> None:
        uid = os.getuid()
        hook = self.run_user / str(uid) / "spaces" / "environment"
        hook.parent.mkdir(parents=True)
        hook.write_text("WAYLAND_DISPLAY=wayland-1\n")
        self.assertEqual(
            self.backend.host_user_environment(uid, uid), "WAYLAND_DISPLAY=wayland-1\n"
        )

    def test_environment_hook_must_be_owned_regular_file(self) -> None:
        uid = os.getuid()
        hook = self.run_user / str(uid) / "spaces" / "environment"
        hook.parent.mkdir(parents=True)
        target = self.root / "elsewhere"
        target.write_text("A=1\n")
        hook.symlink_to(target)
        self.assertIsNone(self.backend.host_user_environment(uid, uid))
        hook.unlink()
        hook.write_text("A=1\n")
        # A file claimed for another uid is not trusted.
        self.assertIsNone(self.backend.host_user_environment(uid + 1, uid))

    def test_environment_scan_picks_lowest_matching_pid(self) -> None:
        uid = os.getuid()
        self.fake_process(30, b"WAYLAND_DISPLAY=w\0XDG_RUNTIME_DIR=/run/user/1\0A=late\0")
        self.fake_process(20, b"WAYLAND_DISPLAY=w\0XDG_RUNTIME_DIR=/run/user/1\0A=early\0")
        self.fake_process(10, b"XDG_RUNTIME_DIR=/run/user/1\0")
        block = self.backend.host_user_environment(uid, uid)
        self.assertEqual(
            block.splitlines(),
            ["WAYLAND_DISPLAY=w", "XDG_RUNTIME_DIR=/run/user/1", "A=early"],
        )
        self.assertIsNone(self.backend.host_user_environment(uid + 5, uid))

    def test_session_bus_address(self) -> None:
        uid = os.getuid()
        default = f"unix:path={self.run_user}/{uid}/bus"
        self.assertEqual(self.backend.session_bus_address(uid), default)
        self.fake_process(
            5,
            b"WAYLAND_DISPLAY=w\0XDG_RUNTIME_DIR=/x\0DBUS_SESSION_BUS_ADDRESS=unix:path=/custom\0",
        )
        self.assertEqual(self.backend.session_bus_address(uid), "unix:path=/custom")
        import socket

        (self.run_user / str(uid)).mkdir()
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(self.run_user / str(uid) / "bus"))
        self.assertEqual(self.backend.session_bus_address(uid), default)


class LauncherTests(Base):
    def test_signals_trigger_one_graceful_stop(self) -> None:
        process = mock.Mock()
        process.pid = 5
        process.poll.return_value = None
        stops: list[list[str]] = []
        release = threading.Event()

        def fake_quiet(command, **kwargs):
            stops.append(list(command))
            release.wait(2)
            process.poll.return_value = 0
            return subprocess.CompletedProcess(command, 0)

        launcher = lxc._Launcher(
            self.backend, "work", process, [], threading.Event(), []
        )
        with mock.patch.object(lxc, "_quiet", fake_quiet):
            launcher.send_signal(signal.SIGTERM)
            launcher.send_signal(signal.SIGINT)
            launcher.terminate()
            release.set()
            launcher._stopper.join(2)
        self.assertEqual(len(stops), 1)
        self.assertIn("lxc-stop", stops[0][1])
        self.assertEqual(stops[0][-2:], ["-t", "30"])
        process.send_signal.assert_not_called()

    def test_other_signals_reach_lxc_start(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        launcher = lxc._Launcher(
            self.backend, "work", process, [], threading.Event(), []
        )
        launcher.send_signal(signal.SIGUSR1)
        process.send_signal.assert_called_once_with(signal.SIGUSR1)

    def test_exit_runs_cleanup_once(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        cleaned: list[int] = []
        stop = threading.Event()
        launcher = lxc._Launcher(
            self.backend, "work", process, [lambda: cleaned.append(1)], stop, []
        )
        self.assertEqual(launcher.poll(), 0)
        self.assertEqual(launcher.wait(), 0)
        self.assertEqual(cleaned, [1])
        self.assertTrue(stop.is_set())


class PrecreateTests(Base):
    def test_precreate_and_symlink_refusal(self) -> None:
        rootfs = self.root / "rootfs"
        rootfs.mkdir()
        lxc._precreate(rootfs, "a/b/file", "file")
        lxc._precreate(rootfs, "a/dir", "dir")
        self.assertTrue((rootfs / "a/b/file").is_file())
        self.assertTrue((rootfs / "a/dir").is_dir())
        (rootfs / "link").symlink_to("/tmp")
        with self.assertRaises(OSError):
            lxc._precreate(rootfs, "link/x", "file")
        with self.assertRaises(OSError):
            lxc._precreate(rootfs, "a/b/file", "dir")

    def test_resolv_symlink_is_replaced_by_empty_file(self) -> None:
        rootfs = self.root / "rootfs"
        (rootfs / "etc").mkdir(parents=True)
        (rootfs / "etc/resolv.conf").symlink_to("../run/systemd/resolve/stub")
        lxc._preexisting_resolv(rootfs, "etc/resolv.conf")
        path = rootfs / "etc/resolv.conf"
        self.assertTrue(path.is_file() and not path.is_symlink())
        self.assertEqual(path.read_text(), "")

    def test_resolv_copy_keeps_inode(self) -> None:
        copy = self.root / "resolv.conf"
        lxc._sync_resolv(copy)
        inode = copy.stat().st_ino
        host = Path("/etc/resolv.conf")
        lxc._sync_resolv(copy)
        self.assertEqual(copy.stat().st_ino, inode)
        self.assertEqual(copy.read_bytes(), host.read_bytes() if host.exists() else b"")


if __name__ == "__main__":
    unittest.main()
