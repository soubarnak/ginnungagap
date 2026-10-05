import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "void" / "spike"))

import session_lock  # noqa: E402


def fake(output):
    calls = []

    def loginctl(*args):
        calls.append(args)
        return output

    return loginctl, calls


class SessionLockTests(unittest.TestCase):
    def test_locked_hint_yes_is_locked(self) -> None:
        loginctl, calls = fake("yes")
        self.assertTrue(session_lock.locked(loginctl, lambda: "2"))
        self.assertEqual(calls, [("show-session", "2", "-p", "LockedHint", "--value")])

    def test_locked_hint_no_is_unlocked(self) -> None:
        self.assertFalse(session_lock.locked(fake("no")[0], lambda: "2"))

    def test_unknown_session_or_loginctl_failure_runs_the_check(self) -> None:
        loginctl, calls = fake("yes")
        self.assertFalse(session_lock.locked(loginctl, lambda: None))
        self.assertEqual(calls, [])
        self.assertFalse(session_lock.locked(fake(None)[0], lambda: "2"))

    def test_session_id_prefers_the_environment(self) -> None:
        from unittest import mock

        with mock.patch.dict("os.environ", {"XDG_SESSION_ID": "7"}):
            self.assertEqual(session_lock.session_id(), "7")


if __name__ == "__main__":
    unittest.main()
