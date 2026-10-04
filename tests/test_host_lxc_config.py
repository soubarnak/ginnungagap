"""nspawn argv to LXC config translation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spaces import core
from spaces import launch as launch_module
from spaces.host import devices_lxc, lxc_config
from spaces.host.lxc_config import UnsupportedLaunchOption, translate

SECCOMP = "2\ndenylist\nreject_force_umount\n[all]\nkexec_load errno 1\n"


class TranslatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rootfs = self.root / "rootfs"
        (self.rootfs / "usr/lib/systemd").mkdir(parents=True)
        (self.rootfs / "usr/lib/systemd/systemd").touch()
        (self.rootfs / "etc/systemd/system").mkdir(parents=True)
        (self.rootfs / "etc/systemd/user").mkdir(parents=True)
        self.home = self.root / "home"
        (self.home / "root").mkdir(parents=True)
        self.cache = self.root / "cache"
        (self.cache / "work").mkdir(parents=True)
        self.runtime = self.root / "run" / "work"
        real_stat = lxc_config.os.stat
        packaged = self.root / "packaged-file"
        packaged.touch()

        def fake_stat(path, *args, **kwargs):
            # Packaged data files are not installed in a development tree.
            if str(path).startswith("/usr/share/spaces/"):
                path = packaged
            return real_stat(path, *args, **kwargs)

        for patch in (
            mock.patch.object(lxc_config.os, "stat", fake_stat),
            mock.patch.object(core, "CACHE_ROOT", self.cache),
            mock.patch.object(launch_module, "_selinux_arguments", lambda: ()),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def command(self, network="basic", kernel="basic", **kwargs) -> list[str]:
        return launch_module._command(
            "work", self.rootfs, self.home, network, kernel, **kwargs
        )

    def run_translate(self, argv, env=None, **kwargs):
        return translate(
            argv,
            env or {},
            runtime_dir=self.runtime,
            seccomp_base=SECCOMP,
            **kwargs,
        )

    def lines(self, spec) -> list[str]:
        return spec.config_text.splitlines()

    def keep(self, spec) -> set[str]:
        (line,) = [x for x in self.lines(spec) if x.startswith("lxc.cap.keep")]
        return set(line.partition("=")[2].split())

    def entries(self, spec) -> list[str]:
        return [
            line.partition("= ")[2]
            for line in self.lines(spec)
            if line.startswith("lxc.mount.entry")
        ]

    def test_basic_launch_translates(self) -> None:
        spec = self.run_translate(self.command())
        text = spec.config_text
        self.assertEqual(spec.name, "work")
        self.assertIn("lxc.rootfs.path = dir:" + str(self.rootfs), text)
        self.assertIn("lxc.init.cmd = /usr/lib/systemd/systemd", text)
        self.assertIn("lxc.net.0.type = none", text)
        self.assertIn("lxc.namespace.share.net = /proc/1/ns/net", text)
        self.assertIn("lxc.tty.max = 0", text)
        self.assertIn("lxc.environment = SYSTEMD_GETTY_AUTO=no", text)
        self.assertIn("lxc.mount.auto = proc:mixed sys:mixed cgroup:rw:force", text)
        self.assertIn("lxc.apparmor.profile = lxc-spaces-container", text)
        self.assertIn("lxc.cgroup.dir.container = spaces-work", text)
        self.assertNotIn("common.conf", text)
        self.assertEqual(spec.resolv_target, "etc/resolv.conf")
        self.assertEqual(spec.resolv_conf, self.runtime / "resolv.conf")
        self.assertIn(
            f"{self.runtime / 'resolv.conf'} etc/resolv.conf none "
            "bind,ro,create=file 0 0",
            self.entries(spec),
        )

    def test_tmpfs_entries_come_first_and_binds_sort_by_depth(self) -> None:
        spec = self.run_translate(self.command())
        entries = self.entries(spec)
        self.assertEqual(
            entries[:2],
            [
                "tmpfs run tmpfs rw,nosuid,nodev,mode=755 0 0",
                "tmpfs tmp tmpfs rw,nosuid,nodev 0 0",
            ],
        )
        depths = [e.split()[1].count("/") for e in entries[2:]]
        self.assertEqual(depths, sorted(depths))
        self.assertIn(f"{self.home} home none rbind 0 0", entries)
        self.assertIn(f"{self.home / 'root'} root none rbind 0 0", entries)
        self.assertIn(f"{self.cache / 'work'} var/cache none rbind 0 0", entries)

    def test_run_destinations_use_create_under_fresh_tmpfs(self) -> None:
        source = self.root / "sock"
        source.touch()
        spec = self.run_translate(
            self.command(authentication_binds=(f"--bind-ro={source}:/run/spaces/x.sock",))
        )
        self.assertIn(
            f"{source} run/spaces/x.sock none bind,ro,create=file 0 0",
            self.entries(spec),
        )

    def test_masked_units_resolve_symlinks_and_precreate(self) -> None:
        (self.rootfs / "usr/lib/systemd/user").mkdir(parents=True)
        (self.rootfs / "etc/systemd/user/pipewire.service").symlink_to(
            "/usr/lib/systemd/user/pipewire.service"
        )
        spec = self.run_translate(self.command())
        entries = self.entries(spec)
        self.assertIn(
            "/dev/null usr/lib/systemd/user/pipewire.service none bind,ro 0 0",
            entries,
        )
        self.assertIn(
            ("usr/lib/systemd/user/pipewire.service", "file"), spec.precreate
        )
        self.assertIn(
            ("etc/systemd/system/bluetooth.service", "file"), spec.precreate
        )

    def test_symlink_cannot_escape_rootfs(self) -> None:
        (self.rootfs / "etc/escape").symlink_to("../../../../../../x")
        self.assertEqual(
            lxc_config.resolve_in_root(self.rootfs, "/etc/escape/data"),
            "x/data",
        )
        with self.assertRaises(UnsupportedLaunchOption):
            lxc_config.resolve_in_root(self.rootfs, "/etc/../x")
        (self.rootfs / "loop").symlink_to("loop")
        with self.assertRaises(UnsupportedLaunchOption):
            lxc_config.resolve_in_root(self.rootfs, "/loop")

    def test_capabilities_per_network_and_kernel(self) -> None:
        for network, kernel in (
            ("basic", "basic"),
            ("advanced", "basic"),
            ("admin", "basic"),
            ("basic", "development"),
            ("admin", "admin"),
        ):
            with self.subTest(network=network, kernel=kernel):
                expected = {
                    c[4:].lower()
                    for c in (
                        *launch_module.KEPT_CAPS,
                        *launch_module.NETWORK_CAPS[network],
                        *launch_module.KERNEL_CAPS[kernel],
                    )
                }
                spec = self.run_translate(self.command(network, kernel))
                self.assertEqual(self.keep(spec), expected)
        basic = self.keep(self.run_translate(self.command()))
        self.assertNotIn("net_raw", basic)
        self.assertNotIn("sys_ptrace", basic)
        self.assertIn("sys_admin", basic)
        admin = self.keep(self.run_translate(self.command("admin", "development")))
        self.assertTrue({"net_admin", "perfmon", "bpf", "sys_ptrace"} <= admin)

    def test_perf_event_open_denied_unless_granted(self) -> None:
        basic = self.run_translate(self.command())
        self.assertIn("perf_event_open errno 1", basic.seccomp_text)
        granted = self.run_translate(self.command("basic", "development"))
        self.assertNotIn("perf_event_open", granted.seccomp_text)
        self.assertIn(
            f"lxc.seccomp.profile = {self.runtime / 'seccomp.profile'}",
            basic.config_text,
        )

    def test_admin_kernel_makes_api_filesystems_writable(self) -> None:
        spec = self.run_translate(
            self.command("basic", "admin"),
            {"SYSTEMD_NSPAWN_API_VFS_WRITABLE": "yes"},
        )
        self.assertIn(
            "lxc.mount.auto = proc:rw sys:rw cgroup:rw:force", spec.config_text
        )

    def test_network_sysctl_pair_collapses(self) -> None:
        spec = self.run_translate(self.command("admin", "basic"))
        entries = self.entries(spec)
        self.assertIn("/proc/sys/net proc/sys/net none rbind,create=dir 0 0", entries)
        self.assertFalse(any("proc-sys-net" in e for e in entries))

    def test_lone_sysctl_stage_fails_closed(self) -> None:
        argv = self.command()
        argv.insert(-3, launch_module.NETWORK_SYSCTL_BINDS[1])
        with self.assertRaises(UnsupportedLaunchOption):
            self.run_translate(argv)

    def test_escaping(self) -> None:
        source = self.root / "we:ird dir\\x"
        source.mkdir()
        argv = self.command(
            custom_binds=(
                launch_module._path_bind_argument(source, "/mnt/a b:c", read_only=True),
            )
        )
        spec = self.run_translate(argv)
        escaped = str(source).replace("\\", "\\134").replace(" ", "\\040")
        self.assertIn(f"{escaped} mnt/a\\040b:c none rbind,ro 0 0", self.entries(spec))
        newline = self.root / "nl\nx"
        newline.mkdir()
        with self.assertRaises(UnsupportedLaunchOption):
            self.run_translate(
                self.command(
                    custom_binds=(
                        launch_module._path_bind_argument(newline, "/mnt/n"),
                    )
                )
            )

    def test_missing_sources_fail_closed(self) -> None:
        with self.assertRaises(UnsupportedLaunchOption):
            self.run_translate(
                self.command(custom_binds=("--bind-ro=/nonexistent-x:/mnt/x",))
            )
        with self.assertRaisesRegex(UnsupportedLaunchOption, "runtime"):
            self.run_translate(
                self.command(
                    authentication_binds=("--bind-ro=/run/spaces-nope/s:/run/s",)
                )
            )

    def test_unknown_and_selinux_options_fail_closed(self) -> None:
        for extra in (
            "--bogus",
            "--tmpfs=/sys/fs/selinux:ro,mode=000",
            "--selinux-context=x",
            "--selinux-apifs-context=x",
            "--overlay=/a:/b:/c",
            "--bind=/a:/b:rootidmap",
            "--bind=/a\\x:/b",
            "positional",
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(UnsupportedLaunchOption):
                    self.run_translate([*self.command(), extra])

    def test_requires_boot_and_init(self) -> None:
        argv = [a for a in self.command() if a != "--boot"]
        with self.assertRaises(UnsupportedLaunchOption):
            self.run_translate(argv)
        (self.rootfs / "usr/lib/systemd/systemd").unlink()
        with self.assertRaisesRegex(UnsupportedLaunchOption, "init"):
            self.run_translate(self.command())
        (self.rootfs / "sbin").mkdir()
        (self.rootfs / "sbin/init").touch()
        self.assertIn(
            "lxc.init.cmd = /sbin/init", self.run_translate(self.command()).config_text
        )

    def test_cgroup_modes(self) -> None:
        relative = self.run_translate(self.command(), cgroup_base="spaces/work")
        self.assertIn("lxc.cgroup.relative = 1", relative.config_text)
        self.assertIn("lxc.cgroup.dir.container.inner = guest", relative.config_text)
        self.assertNotIn("spaces-work", relative.config_text)

    def test_device_rules(self) -> None:
        spec = self.run_translate(self.command(), device_rules=["c 226:0 rw"])
        self.assertIn("lxc.cgroup2.devices.deny = a", spec.devices_text)
        self.assertIn("lxc.cgroup2.devices.allow = c 226:0 rw", spec.devices_text)
        self.assertIn("lxc.cgroup2.devices.allow = c 1:3 rwm", spec.devices_text)
        full = self.run_translate(self.command(), device_rules=None)
        self.assertNotIn("deny = a", full.devices_text)
        self.assertIn(
            f"lxc.include = {self.runtime / 'devices.conf'}", spec.config_text
        )

    def test_full_level_keeps_watchdog_and_console_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Path(tmp) / "watchdog"
            (sysfs / "watchdog0").mkdir(parents=True)
            (sysfs / "watchdog0" / "dev").write_text("246:0\n")
            (sysfs / "watchdog1").mkdir()
            (sysfs / "watchdog1" / "dev").write_text("247:3\n")
            (sysfs / "broken").mkdir()
            proc = Path(tmp) / "devices"
            proc.write_text("Character devices:\n  4 tty\n246 watchdog\n\nBlock devices:\n")
            with (
                mock.patch.object(devices_lxc, "SYS_WATCHDOG", sysfs),
                mock.patch.object(devices_lxc, "PROC_DEVICES", proc),
            ):
                text = devices_lxc.config_text(None)
        lines = text.splitlines()
        self.assertEqual(lines[0], "lxc.cgroup2.devices.allow = a")
        for rule in (
            "c 10:130 rwm", "c 4:* rwm", "c 7:* rwm", "c 5:3 rwm",
            "c 246:0 rwm", "c 247:3 rwm", "c 246:* rwm",
        ):
            self.assertIn(f"lxc.cgroup2.devices.deny = {rule}", lines)
        self.assertNotIn("lxc.cgroup2.devices.deny = c 5:*", text)
        self.assertEqual(len(lines), len(set(lines)))
        # Without sysfs or /proc/devices only the fixed rules remain.
        with (
            mock.patch.object(devices_lxc, "SYS_WATCHDOG", Path("/nonexistent")),
            mock.patch.object(devices_lxc, "PROC_DEVICES", Path("/nonexistent")),
        ):
            self.assertEqual(
                devices_lxc.full_deny_rules(), list(devices_lxc.FULL_DENY_RULES)
            )

    def test_hostname_and_machine(self) -> None:
        argv = self.command()
        spec = self.run_translate(argv)
        import socket

        self.assertIn(f"lxc.uts.name = {socket.gethostname()}", spec.config_text)


class DeviceRuleTests(unittest.TestCase):
    def test_id_specs(self) -> None:
        self.assertEqual(
            devices_lxc.translate_spec("/dev/char/226:128", "rw"), ["c 226:128 rw"]
        )
        self.assertEqual(
            devices_lxc.translate_spec("/dev/block/8:0", "rwm"), ["b 8:0 rwm"]
        )

    def test_named_class_uses_proc_devices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices"
            path.write_text(
                "Character devices:\n  1 mem\n136 pts\n137 pts\n\n"
                "Block devices:\n  8 sd\n136 pts\n",
                encoding="utf-8",
            )
            with mock.patch.object(devices_lxc, "PROC_DEVICES", path):
                self.assertEqual(
                    devices_lxc.translate_spec("char-pts", "rw"),
                    ["c 136:* rw", "c 137:* rw"],
                )
                self.assertEqual(
                    devices_lxc.translate_spec("block-sd", "r"), ["b 8:* r"]
                )
                with self.assertRaises(devices_lxc.DeviceSpecError):
                    devices_lxc.translate_spec("char-none", "r")

    def test_path_specs_stat_the_node(self) -> None:
        rules = devices_lxc.translate_spec("/dev/null", "rwm")
        self.assertEqual(rules, ["c 1:3 rwm"])
        with self.assertRaises(devices_lxc.DeviceSpecError):
            devices_lxc.translate_spec("/etc/passwd", "rw")

    def test_permission_letters_are_validated(self) -> None:
        for bad in ("", "x", "rwx"):
            with self.assertRaises(devices_lxc.DeviceSpecError):
                devices_lxc.translate_spec("/dev/char/1:3", bad)

    def test_base_allow_translates(self) -> None:
        rules = devices_lxc.translate(launch_module.BASE_DEVICE_ALLOW)
        self.assertIn("c 136:* rw", rules)
        self.assertEqual(len(rules), len(set(rules)))


if __name__ == "__main__":
    unittest.main()
