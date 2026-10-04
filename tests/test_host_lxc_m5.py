"""M5 additions to the LXC backend: guest locale handling and stale containers."""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces.host import guest_locale, lxc


def locale_archive(names: list[str], slots: int = 7) -> bytes:
    """A minimal glibc locale-archive: header plus a name hash table."""

    header_size = 56
    table_offset = header_size
    strings_offset = table_offset + slots * 12
    strings = b""
    entries = []
    for index, name in enumerate(names):
        entries.append((index + 1, strings_offset + len(strings), 0))
        strings += name.encode() + b"\0"
    entries += [(0, 0, 0)] * (slots - len(entries))
    header = struct.pack(
        "<14I",
        guest_locale.ARCHIVE_MAGIC, 0, table_offset, len(names), slots,
        strings_offset, len(strings), len(strings), 0, 0, 0, 0, 0, 0,
    )
    table = b"".join(struct.pack("<III", *entry) for entry in entries)
    return header + table + strings


class GuestLocaleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.rootfs = Path(self.temporary.name)
        directory = self.rootfs / "usr/lib/locale"
        (directory / "C.utf8").mkdir(parents=True)
        (directory / "locale-archive").write_bytes(
            locale_archive(["en_US.utf8"])
        )

    def test_normalize(self) -> None:
        self.assertEqual(guest_locale.normalize("en_US.UTF-8"), "en_US.utf8")
        self.assertEqual(
            guest_locale.normalize("de_DE.ISO-8859-15@euro"),
            "de_DE.iso885915@euro",
        )
        self.assertEqual(guest_locale.normalize("fr_FR"), "fr_FR")

    def test_available_reads_archive_and_directories(self) -> None:
        self.assertEqual(
            guest_locale.available(self.rootfs),
            frozenset({"en_US.utf8", "C.utf8"}),
        )

    def test_present_locale_is_kept(self) -> None:
        env = {"LANG": "en_US.UTF-8", "LC_TIME": "en_US.utf8", "A": "1"}
        self.assertEqual(guest_locale.adjust(self.rootfs, env), env)

    def test_missing_lang_becomes_c_utf8_and_language_is_dropped(self) -> None:
        env = {
            "LANG": "de_DE.UTF-8",
            "LANGUAGE": "de:en",
            "LC_ALL": "de_DE.UTF-8",
            "LC_TIME": "en_US.UTF-8",
            "PATH": "/bin",
        }
        self.assertEqual(
            guest_locale.adjust(self.rootfs, env),
            {"LANG": "C.UTF-8", "LC_TIME": "en_US.UTF-8", "PATH": "/bin"},
        )

    def test_c_locales_and_unreadable_archive_are_left_alone(self) -> None:
        env = {"LANG": "C.UTF-8", "LC_ALL": "POSIX"}
        self.assertEqual(guest_locale.adjust(self.rootfs, env), env)
        (self.rootfs / "usr/lib/locale/locale-archive").write_bytes(b"garbage" * 20)
        env = {"LANG": "de_DE.UTF-8"}
        self.assertEqual(guest_locale.adjust(self.rootfs, env), env)
        self.assertEqual(guest_locale.adjust(self.rootfs / "nope", env), env)

    def test_no_archive_means_only_c(self) -> None:
        (self.rootfs / "usr/lib/locale/locale-archive").unlink()
        self.assertEqual(
            guest_locale.adjust(self.rootfs, {"LANG": "en_US.UTF-8"}),
            {"LANG": "C.UTF-8"},
        )

    def test_attach_applies_the_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            rootfs = Path(state) / "work" / "rootfs"
            (rootfs / "usr/lib/locale").mkdir(parents=True)
            backend = lxc.LxcBackend()
            with (
                mock.patch("spaces.core.STATE_ROOT", Path(state)),
                mock.patch.dict(os.environ, {"SPACES_LXC_WRAPPER": "/w"}),
            ):
                command = backend._attach(
                    "alice", "work", ["true"], {"LANG": "de_DE.UTF-8", "X": "1"}
                )
        self.assertIn("--setenv=LANG=C.UTF-8", command)
        self.assertIn("--setenv=X=1", command)
        self.assertNotIn("--setenv=LANG=de_DE.UTF-8", command)


class RunScriptTests(unittest.TestCase):
    def test_run_script_syncs_nvidia_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service").mkdir()
            (root / "sv").mkdir()
            ok = root / "run" / "supervise.spaces-work" / "ok"
            ok.parent.mkdir(parents=True)
            ok.touch()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "SPACES_LXC_PATH": str(root / "lxc"),
                        "SPACES_RUNIT_SVDIR": str(root / "sv"),
                        "SPACES_RUNIT_SERVICE_DIR": str(root / "service"),
                        "SPACES_PRIV": "/opt/spaces.priv",
                    },
                ),
                mock.patch.object(lxc, "RUNIT_SUPERVISE", str(root / "run")),
            ):
                lxc.ensure_service("work")
            lines = (root / "sv/spaces-work/run").read_text().splitlines()
        sync = next(i for i, l in enumerate(lines) if "spaces-nvidia-sync" in l)
        launch = next(i for i, l in enumerate(lines) if "launch work" in l)
        self.assertLess(sync, launch)
        self.assertTrue(lines[sync].endswith("|| true"))


class StaleContainerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        env = mock.patch.dict(
            os.environ,
            {
                "SPACES_CGROUP_ROOT": str(self.root / "cgroup"),
                "SPACES_LXC_WRAPPER": "/w",
                "SPACES_LXC_PATH": str(self.root / "lxc"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.backend = lxc.LxcBackend()
        self.base = self.root / "cgroup" / "spaces" / "work"
        (self.base / "monitor" / "pivot").mkdir(parents=True)

    def test_running_container_is_killed_and_cgroup_removed(self) -> None:
        states = iter(["RUNNING", "RUNNING", "STOPPED"])
        calls: list[list[str]] = []

        def fake_quiet(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.object(self.backend, "_state", lambda name: next(states)),
            mock.patch.object(lxc, "_quiet", fake_quiet),
            mock.patch.object(lxc.time, "sleep", lambda _s: None),
        ):
            self.backend._reap_stale_container("work")
        self.assertEqual(len(calls), 1)
        self.assertIn("lxc-stop", calls[0][1])
        self.assertEqual(calls[0][-1], "-k")
        self.assertFalse(self.base.exists())

    def test_stopped_container_only_cleans_the_cgroup(self) -> None:
        with (
            mock.patch.object(self.backend, "_state", lambda name: None),
            mock.patch.object(lxc, "_quiet") as quiet,
        ):
            self.backend._reap_stale_container("work")
        quiet.assert_not_called()
        self.assertFalse(self.base.exists())

    def test_stale_netns_pin_is_unmounted_and_removed(self) -> None:
        runtime = self.root / "lxc" / "work"
        runtime.mkdir(parents=True)
        pin = runtime / "netns"
        pin.touch()
        with (
            mock.patch.object(self.backend, "_state", lambda name: None),
            mock.patch.object(lxc, "_quiet") as quiet,
        ):
            self.backend._reap_stale_container("work")
        quiet.assert_called_once_with(["umount", str(pin)], timeout=10)
        self.assertFalse(pin.exists())

    def test_unstoppable_container_raises(self) -> None:
        ticks = iter(range(0, 1000, 5))
        with (
            mock.patch.object(self.backend, "_state", lambda name: "RUNNING"),
            mock.patch.object(lxc, "_quiet", lambda *a, **k: None),
            mock.patch.object(lxc.time, "sleep", lambda _s: None),
            mock.patch.object(lxc.time, "monotonic", lambda: next(ticks)),
        ):
            with self.assertRaises(RuntimeError):
                self.backend._reap_stale_container("work")

    def test_orphaned_container_is_not_running(self) -> None:
        runtime = self.root / "lxc" / "work"
        runtime.mkdir(parents=True)
        with mock.patch.object(self.backend, "_state", lambda name: "RUNNING"):
            # no lock file: assume a live launcher of an older version
            self.assertTrue(self.backend.is_running("work"))
            lock = lxc._lock_launcher(runtime)
            self.assertTrue(self.backend.is_running("work"))
            lock.close()
            self.assertFalse(self.backend.is_running("work"))
        with mock.patch.object(self.backend, "_state", lambda name: "STOPPED"):
            self.assertFalse(self.backend.is_running("work"))

    def test_second_launcher_cannot_take_the_lock(self) -> None:
        runtime = self.root / "run" / "work"
        runtime.mkdir(parents=True)
        first = lxc._lock_launcher(runtime)
        try:
            with self.assertRaises(RuntimeError):
                lxc._lock_launcher(runtime)
        finally:
            first.close()
        lxc._lock_launcher(runtime).close()


if __name__ == "__main__":
    unittest.main()
