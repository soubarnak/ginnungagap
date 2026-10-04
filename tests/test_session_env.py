"""Session environment publishing, trust checks, /proc scan and bus symlink."""

from __future__ import annotations

import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path

from spaces.host import session_env


class FakeLogin:
    def __init__(self, pids=None, active=("2",)):
        self.pids = pids or {}
        self.active = set(active)

    def pid_session(self, pid):
        return self.pids.get(pid)

    def graphical_session(self, uid, session_id):
        return session_id in self.active


class Base(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.uid = os.getuid()
        self.run_user = self.root / "run"
        self.runtime = self.run_user / str(self.uid)
        self.runtime.mkdir(parents=True)
        session_env._scan_cache.clear()


class FormatTests(unittest.TestCase):
    def test_parse_drops_malformed_lines(self):
        text = "A=1\nB=\n9X=2\nC=a=b\nD=x\0y\nnoequals\n=3\nE=ok\n"
        self.assertEqual(
            session_env.parse_environment(text), {"A": "1", "C": "a=b", "E": "ok"}
        )

    def test_format_round_trip(self):
        environment = {"WAYLAND_DISPLAY": "wayland-1", "A": "x y"}
        self.assertEqual(
            session_env.parse_environment(session_env.format_environment(environment)),
            environment,
        )

    def test_collect_filters_and_derives_session(self):
        login = FakeLogin({os.getpid(): "7"})
        result = session_env.collect_published_environment(
            {"WAYLAND_DISPLAY": "w", "SECRET": "no", "LANG": "C", "BAD": "a\nb"},
            {"WAYLAND_DISPLAY", "LANG", "BAD"},
            login,
        )
        self.assertEqual(
            result, {"WAYLAND_DISPLAY": "w", "LANG": "C", "XDG_SESSION_ID": "7"}
        )

    def test_publish_keys_include_bus_and_allowlist(self):
        keys = session_env.publish_keys()
        self.assertIn("DBUS_SESSION_BUS_ADDRESS", keys)
        self.assertIn("WAYLAND_DISPLAY", keys)
        self.assertIn("XDG_CURRENT_DESKTOP", keys)
        self.assertNotIn("HOME", keys)
        self.assertNotIn("PATH", keys)


class PublishedFileTests(Base):
    def test_write_is_private_atomic_and_readable(self):
        target = session_env.write_published(
            {"WAYLAND_DISPLAY": "w", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/b"},
            self.runtime,
        )
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertEqual(target.stat().st_uid, self.uid)
        self.assertEqual(
            session_env.read_published(self.uid, self.run_user)["WAYLAND_DISPLAY"], "w"
        )
        self.assertEqual(
            [p.name for p in target.parent.iterdir()], [session_env.ENVIRONMENT_FILE]
        )
        session_env.write_published({"A": "2"}, self.runtime)
        self.assertEqual(session_env.read_published(self.uid, self.run_user), {"A": "2"})

    def test_reader_rejects_unsafe_files(self):
        target = session_env.write_published({"A": "1"}, self.runtime)
        os.chmod(target, 0o666)
        self.assertIsNone(session_env.read_published(self.uid, self.run_user))
        os.chmod(target, 0o600)
        self.assertIsNotNone(session_env.read_published(self.uid, self.run_user))
        os.chmod(target.parent, 0o770)
        self.assertIsNone(session_env.read_published(self.uid, self.run_user))
        os.chmod(target.parent, 0o700)
        # Wrong expected owner.
        self.assertIsNone(session_env.read_published(self.uid + 1, self.run_user))
        target.unlink()
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("A=1\n")
        target.symlink_to(elsewhere)
        self.assertIsNone(session_env.read_published(self.uid, self.run_user))
        target.unlink()
        target.write_bytes(b"A=1\n" * (session_env.MAX_FILE_SIZE // 4 + 10))
        os.chmod(target, 0o600)
        self.assertIsNone(session_env.read_published(self.uid, self.run_user))

    def test_symlinked_directory_is_rejected(self):
        real = self.root / "real"
        real.mkdir()
        (real / "environment").write_text("A=1\n")
        (self.runtime / "spaces").symlink_to(real)
        self.assertIsNone(session_env.read_published(self.uid, self.run_user))

    def test_rejects_foreign_directory_on_write(self):
        (self.runtime / "spaces").symlink_to(self.root)
        with self.assertRaises(OSError):
            session_env.write_published({"A": "1"}, self.runtime)


class ResolveTests(Base):
    GRAPHICAL = (
        b"WAYLAND_DISPLAY=w\0DBUS_SESSION_BUS_ADDRESS=unix:path=/b\0"
        b"XDG_CURRENT_DESKTOP=niri\0"
    )

    def proc(self, pid, environ, owner=None):
        proc = self.root / "proc"
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / "environ").write_bytes(environ)
        return proc

    def test_published_wins_when_session_is_current(self):
        session_env.write_published(
            {
                "WAYLAND_DISPLAY": "wayland-9",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/b",
                "XDG_SESSION_ID": "2",
            },
            self.runtime,
        )
        proc = self.proc(10, self.GRAPHICAL)
        result = session_env.resolve_environment(
            self.uid, login=FakeLogin({10: "2"}), run_user=self.run_user, proc=proc
        )
        self.assertEqual(result["WAYLAND_DISPLAY"], "wayland-9")

    def test_stale_published_session_falls_back_to_scan(self):
        session_env.write_published(
            {
                "WAYLAND_DISPLAY": "wayland-old",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/b",
                "XDG_SESSION_ID": "1",
            },
            self.runtime,
        )
        proc = self.proc(10, self.GRAPHICAL)
        result = session_env.resolve_environment(
            self.uid, login=FakeLogin({10: "2"}), run_user=self.run_user, proc=proc
        )
        self.assertEqual(result["WAYLAND_DISPLAY"], "w")
        self.assertEqual(result["XDG_SESSION_ID"], "2")

    def test_scan_ignores_inactive_and_incomplete_processes(self):
        proc = self.proc(5, self.GRAPHICAL)
        self.proc(6, b"WAYLAND_DISPLAY=w\0")
        self.proc(7, self.GRAPHICAL)
        login = FakeLogin({5: "3", 6: "2", 7: "2"})
        result = session_env.scan_session_environment(self.uid, login, proc)
        self.assertEqual(result["XDG_SESSION_ID"], "2")
        self.assertIn("DBUS_SESSION_BUS_ADDRESS", result)
        self.assertIsNone(
            session_env.scan_session_environment(
                self.uid, FakeLogin({5: "3"}, active=()), proc
            )
        )

    def test_scan_cache_is_revalidated(self):
        proc = self.proc(5, self.GRAPHICAL)
        login = FakeLogin({5: "2"})
        self.assertIsNotNone(session_env.scan_session_environment(self.uid, login, proc))
        login.active = set()
        self.assertIsNone(session_env.scan_session_environment(self.uid, login, proc))


class BusLinkTests(Base):
    def listen(self, name="dbus-x"):
        path = self.root / name
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(path))
        return path

    def link(self):
        return self.runtime / "bus"

    def test_parse_address(self):
        parse = session_env.parse_bus_address
        self.assertEqual(parse("unix:path=/a/b,guid=1"), ("path", "/a/b"))
        self.assertEqual(parse("unix:guid=1,abstract=/tmp/x"), ("abstract", "/tmp/x"))
        self.assertIsNone(parse("tcp:host=x"))

    def test_creates_link_and_returns_standard_address(self):
        socket_path = self.listen()
        address = session_env.ensure_bus_link(
            self.uid, f"unix:path={socket_path},guid=7", self.run_user
        )
        self.assertEqual(address, f"unix:path={self.link()}")
        self.assertEqual(os.readlink(self.link()), str(socket_path))
        status = os.lstat(self.link())
        self.assertEqual(status.st_uid, self.uid)
        self.assertEqual(
            session_env.ensure_bus_link(
                self.uid, f"unix:path={socket_path}", self.run_user
            ),
            address,
        )
        self.assertEqual(os.listdir(self.runtime), ["bus"])

    def test_repoints_stale_link(self):
        old = self.root / "gone"
        self.link().symlink_to(old)
        new = self.listen("fresh")
        session_env.ensure_bus_link(self.uid, f"unix:path={new}", self.run_user)
        self.assertEqual(os.readlink(self.link()), str(new))

    def test_never_replaces_a_real_socket_or_file(self):
        real = socket.socket(socket.AF_UNIX)
        self.addCleanup(real.close)
        real.bind(str(self.link()))
        other = self.listen("other")
        address = session_env.ensure_bus_link(
            self.uid, f"unix:path={other}", self.run_user
        )
        self.assertEqual(address, f"unix:path={self.link()}")
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.link()).st_mode))
        self.link().unlink()
        self.link().write_text("file")
        self.assertEqual(
            session_env.ensure_bus_link(self.uid, f"unix:path={other}", self.run_user),
            f"unix:path={other}",
        )
        self.assertEqual(self.link().read_text(), "file")

    def test_abstract_and_missing_sockets_are_left_alone(self):
        self.assertEqual(
            session_env.ensure_bus_link(
                self.uid, "unix:abstract=/tmp/dbus-1", self.run_user
            ),
            "unix:abstract=/tmp/dbus-1",
        )
        self.assertEqual(
            session_env.ensure_bus_link(
                self.uid, f"unix:path={self.root}/missing", self.run_user
            ),
            f"unix:path={self.root}/missing",
        )
        self.assertFalse(os.path.lexists(self.link()))

    def test_remove_stale_only_removes_dangling_links(self):
        self.assertFalse(session_env.remove_stale_bus_link(self.uid, self.run_user))
        self.link().symlink_to(self.root / "gone")
        self.assertTrue(session_env.remove_stale_bus_link(self.uid, self.run_user))
        self.assertFalse(os.path.lexists(self.link()))
        live = self.listen("live")
        self.link().symlink_to(live)
        self.assertFalse(session_env.remove_stale_bus_link(self.uid, self.run_user))
        self.assertTrue(self.link().is_symlink())


if __name__ == "__main__":
    unittest.main()
