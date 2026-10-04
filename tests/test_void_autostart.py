from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from spaces.host import autostart


class FakeLogins:
    def __init__(self) -> None:
        self.states: dict[int, str] = {}
        self.session_ids: dict[int, list[str]] = {}

    def login(self, uid: int, state: str, *sessions: str) -> None:
        self.states[uid] = state
        self.session_ids[uid] = list(sessions)

    def logout(self, uid: int) -> None:
        self.states[uid] = "offline"
        self.session_ids[uid] = []

    def state(self, uid: int) -> str | None:
        return self.states.get(uid)

    def sessions(self, uid: int) -> list[str]:
        return self.session_ids.get(uid, [])

    def wait(self, timeout: float) -> None:
        pass


class FakeRunner:
    def __init__(self) -> None:
        self.up: set[str] = set()
        self.started: list[str] = []

    def running(self, name: str) -> bool:
        return name in self.up

    def start(self, name: str) -> bool:
        self.started.append(name)
        self.up.add(name)
        return True


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "spaces"
        self.run_dir = Path(self.temporary.name) / "run"
        for name in ("ubuntu", "arch"):
            (self.root / name).mkdir(parents=True)
            (self.root / name / "info.json").write_text("{}")

    def daemon(self, logins: FakeLogins, runner: FakeRunner) -> autostart.Autostarter:
        return autostart.Autostarter(
            logins,
            runner,
            root=self.root,
            run_dir=self.run_dir,
            uid_of=lambda user: {"alice": 1000, "bob": 1001}.get(user),
        )


class StateFileTests(Base):
    def test_user_file_keeps_the_existing_format(self) -> None:
        # LxcBackend.enable_user_autostart wrote one name per line.
        (self.root / "ubuntu" / "autostart-users").write_text("alice\nbob\n")
        self.assertEqual(autostart.read_users("ubuntu", self.root), ["alice", "bob"])
        self.assertFalse(autostart.set_user("ubuntu", "alice", True, self.root))
        self.assertTrue(autostart.set_user("ubuntu", "carol", True, self.root))
        self.assertEqual(
            (self.root / "ubuntu" / "autostart-users").read_text(), "alice\nbob\ncarol\n"
        )

    def test_disable_removes_user_and_empty_file(self) -> None:
        autostart.set_user("ubuntu", "alice", True, self.root)
        self.assertTrue(autostart.set_user("ubuntu", "alice", False, self.root))
        self.assertFalse((self.root / "ubuntu" / "autostart-users").exists())
        self.assertFalse(autostart.set_user("ubuntu", "alice", False, self.root))

    def test_boot_flag(self) -> None:
        self.assertFalse(autostart.boot_enabled("ubuntu", self.root))
        self.assertTrue(autostart.set_boot("ubuntu", True, self.root))
        self.assertTrue(autostart.boot_enabled("ubuntu", self.root))
        self.assertFalse(autostart.set_boot("ubuntu", True, self.root))
        self.assertTrue(autostart.set_boot("ubuntu", False, self.root))
        self.assertFalse(autostart.boot_enabled("ubuntu", self.root))

    def test_unknown_space_and_bad_user_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            autostart.set_boot("nothing", True, self.root)
        with self.assertRaises(ValueError):
            autostart.set_user("ubuntu", "a b", True, self.root)
        with self.assertRaises(ValueError):
            autostart.set_user("../etc", "alice", True, self.root)

    def test_space_names_need_info_json(self) -> None:
        (self.root / "half").mkdir()
        (self.root / ".host").mkdir()
        (self.root / ".host" / "info.json").write_text("{}")
        self.assertEqual(autostart.space_names(self.root), ["arch", "ubuntu"])


class DaemonTests(Base):
    def test_nothing_enabled_does_nothing(self) -> None:
        logins, runner = FakeLogins(), FakeRunner()
        logins.login(1000, "active", "1")
        self.daemon(logins, runner).evaluate()
        self.assertEqual(runner.started, [])

    def test_boot_flag_starts_once_per_boot(self) -> None:
        autostart.set_boot("ubuntu", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        daemon = self.daemon(logins, runner)
        daemon.evaluate()
        self.assertEqual(runner.started, ["ubuntu"])
        runner.up.clear()  # deliberate stop
        daemon.evaluate()
        self.assertEqual(runner.started, ["ubuntu"])
        # A restarted daemon reads the tmpfs state and does not repeat it.
        self.daemon(logins, runner).evaluate()
        self.assertEqual(runner.started, ["ubuntu"])

    def test_active_user_starts_the_enabled_space_only(self) -> None:
        autostart.set_user("ubuntu", "alice", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        logins.login(1000, "active", "1")
        self.daemon(logins, runner).evaluate()
        self.assertEqual(runner.started, ["ubuntu"])

    def test_offline_user_does_not_start(self) -> None:
        autostart.set_user("ubuntu", "alice", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        logins.login(1000, "closing", "1")
        self.daemon(logins, runner).evaluate()
        self.assertEqual(runner.started, [])

    def test_deliberate_stop_is_kept_until_the_next_login(self) -> None:
        autostart.set_user("ubuntu", "alice", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        logins.login(1000, "active", "1")
        daemon = self.daemon(logins, runner)
        daemon.evaluate()
        runner.up.clear()  # sv down
        daemon.evaluate()
        daemon.evaluate()
        self.assertEqual(runner.started, ["ubuntu"])
        logins.logout(1000)
        daemon.evaluate()
        logins.login(1000, "active", "2")
        daemon.evaluate()
        self.assertEqual(runner.started, ["ubuntu", "ubuntu"])

    def test_second_session_counts_as_a_new_login(self) -> None:
        autostart.set_user("ubuntu", "alice", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        logins.login(1000, "active", "1")
        daemon = self.daemon(logins, runner)
        daemon.evaluate()
        runner.up.clear()
        logins.login(1000, "active", "1", "2")  # ssh login next to the desktop
        daemon.evaluate()
        self.assertEqual(runner.started, ["ubuntu", "ubuntu"])

    def test_running_space_is_left_alone(self) -> None:
        autostart.set_user("ubuntu", "alice", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        runner.up.add("ubuntu")
        logins.login(1000, "active", "1")
        self.daemon(logins, runner).evaluate()
        self.assertEqual(runner.started, [])

    def test_lingering_user_without_session_starts_once(self) -> None:
        autostart.set_user("arch", "bob", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        logins.login(1001, "lingering")
        daemon = self.daemon(logins, runner)
        daemon.evaluate()
        runner.up.clear()
        daemon.evaluate()
        self.assertEqual(runner.started, ["arch"])

    def test_empty_session_list_while_active_does_not_look_like_a_new_login(self) -> None:
        autostart.set_user("ubuntu", "alice", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        logins.login(1000, "active", "1")
        daemon = self.daemon(logins, runner)
        daemon.evaluate()
        runner.up.clear()  # sv down
        logins.login(1000, "active")  # transient: sessions not listed
        daemon.evaluate()
        logins.login(1000, "active", "1")
        daemon.evaluate()
        self.assertEqual(runner.started, ["ubuntu"])

    def test_logout_of_a_lingering_user_does_not_start_spaces(self) -> None:
        autostart.set_user("ubuntu", "alice", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        logins.login(1000, "active", "1")
        daemon = self.daemon(logins, runner)
        daemon.evaluate()
        runner.up.clear()
        logins.login(1000, "lingering")  # logged out, linger keeps the manager
        daemon.evaluate()
        self.assertEqual(runner.started, ["ubuntu"])
        logins.login(1000, "active", "2")
        daemon.evaluate()
        self.assertEqual(runner.started, ["ubuntu", "ubuntu"])

    def test_unknown_user_is_ignored_and_failures_do_not_stop_the_loop(self) -> None:
        autostart.set_user("ubuntu", "nobody-here", True, self.root)
        autostart.set_user("arch", "alice", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        logins.login(1000, "online", "1")

        def failing(name: str) -> bool:
            if name == "arch":
                raise OSError("boom")
            return True

        runner.start = failing  # type: ignore[method-assign]
        self.daemon(logins, runner).evaluate()  # must not raise

    def test_disabling_forgets_the_login(self) -> None:
        autostart.set_user("ubuntu", "alice", True, self.root)
        logins, runner = FakeLogins(), FakeRunner()
        logins.login(1000, "active", "1")
        daemon = self.daemon(logins, runner)
        daemon.evaluate()
        autostart.set_user("ubuntu", "alice", False, self.root)
        daemon.evaluate()
        runner.up.clear()
        autostart.set_user("ubuntu", "alice", True, self.root)
        daemon.evaluate()
        self.assertEqual(runner.started, ["ubuntu", "ubuntu"])


class GcTests(Base):
    def test_gc_removes_only_services_of_deleted_spaces(self) -> None:
        svdir = Path(self.temporary.name) / "sv"
        for name in ("spaces-ubuntu", "spaces-gone", "spaces-autostart", "other"):
            (svdir / name).mkdir(parents=True)
        forgotten: list[str] = []
        self.assertEqual(
            autostart.gc_services(root=self.root, svdir=svdir, forget=forgotten.append, dry_run=True),
            ["gone"],
        )
        self.assertEqual(forgotten, [])
        removed = autostart.gc_services(root=self.root, svdir=svdir, forget=forgotten.append)
        self.assertEqual(removed, ["gone"])
        self.assertEqual(forgotten, ["gone"])


class ImportTests(unittest.TestCase):
    def test_daemon_does_not_import_the_ui_stack(self) -> None:
        import subprocess
        import sys

        code = (
            "import sys; import spaces.host.autostart, spaces.host.cli, spaces.host.doctor;"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('textual', 'PIL', 'rich')];"
            "print(bad)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        self.assertEqual(result.stdout.strip(), "[]", result.stderr)


if __name__ == "__main__":
    unittest.main()
