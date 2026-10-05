"""The user namespace: id map, subuid/subgid, new spaces, /sys bind, idmapped binds, CLI, broker."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from spaces import core
from spaces import launch as launch_module
from spaces.host import cli, doctor, lxc, lxc_config, nvidia, userns

ROOT = Path(__file__).resolve().parents[1]
SECCOMP = "2\ndenylist\nreject_force_umount\n[all]\nkexec_load errno 1\n"
MOUNTINFO = "\n".join(
    [
        "26 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw",
        "30 26 0:6 / /sys rw,nosuid,nodev,noexec,relatime shared:2 - sysfs sysfs rw",
        "31 30 0:7 / /sys/kernel/security rw,relatime shared:3 - securityfs securityfs rw",
        "32 30 0:8 / /sys/firmware/efi/efivars rw,nosuid,nodev,noexec,relatime shared:4 - efivarfs efivarfs rw",
        "33 30 0:9 / /sys/fs/cgroup rw,relatime shared:5 - cgroup2 cgroup2 rw,nsdelegate",
        "34 33 0:10 / /sys/fs/cgroup/elogind rw,relatime shared:6 - cgroup cgroup rw,name=elogind",
        "35 26 0:11 / /proc rw,nosuid,nodev,noexec,relatime shared:7 - proc proc rw",
    ]
)


def write_space(root: Path, name: str, users: dict[int, int]) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    info = {"permissions": {"users": {str(uid): {"gid": gid} for uid, gid in users.items()}}}
    (root / name / "info.json").write_text(json.dumps(info))


class IdMapTests(unittest.TestCase):
    def test_identity_ids_are_carved_out_of_the_shifted_range(self) -> None:
        plan = userns.Plan((1000,), (12, 13, 1000))
        lines = plan.idmap_lines()
        self.assertEqual(
            [
                "lxc.idmap = u 0 1000000 1000",
                "lxc.idmap = u 1000 1000 1",
                "lxc.idmap = u 1001 1001001 64535",
                "lxc.idmap = g 0 1000000 12",
                "lxc.idmap = g 12 12 1",
                "lxc.idmap = g 13 13 1",
                "lxc.idmap = g 14 1000014 986",
                "lxc.idmap = g 1000 1000 1",
                "lxc.idmap = g 1001 1001001 64535",
            ],
            lines,
        )

    def test_the_map_is_a_partition_without_overlap(self) -> None:
        plan = userns.Plan((1000, 59990, 70000), (12, 13, 25, 100, 1000, 70001))
        for kind in ("u", "g"):
            spans = []
            for line in plan.idmap_lines():
                match = re.fullmatch(rf"lxc\.idmap = {kind} (\d+) (\d+) (\d+)", line)
                if match:
                    spans.append(tuple(map(int, match.groups())))
            inner = sorted((a, a + n) for a, _b, n in spans)
            outer = sorted((b, b + n) for _a, b, n in spans)
            for spans_of in (inner, outer):
                for (_s1, e1), (s2, _e2) in zip(spans_of, spans_of[1:]):
                    self.assertLessEqual(e1, s2)
            covered = sum(n for _a, _b, n in spans)
            # every id below 65536 is mapped once, plus the identity ids above it
            extra = len([i for i in ((70000,) if kind == "u" else (70001,))])
            self.assertEqual(65536 + extra, covered)

    def test_guest_root_is_not_host_root(self) -> None:
        plan = userns.Plan((1000,), (1000,))
        self.assertEqual(userns.SHIFT_BASE, plan.root_uid)
        self.assertNotEqual(0, plan.root_uid)
        # the shifted range is root's own allocation, not the user's (soubarna:100000:65536)
        self.assertGreaterEqual(plan.base, 1_000_000)


class NewSpaceTests(unittest.TestCase):
    """`spaces create` writes the marker: on by default, off with --no-userns or on a host that cannot."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.subuid = self.root / "subuid"
        self.subgid = self.root / "subgid"
        write_space(self.root, "dev", {1000: 1000})
        self.mountinfo = "26 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw"

    def choose(self, want: bool = True, subuid: Path | None = None, release: str = "7.2.9_1") -> tuple[bool, str | None]:
        return userns.choose_for_new_space(
            "dev", want, self.root, subuid or self.subuid, self.subgid, release, self.mountinfo
        )

    def test_default_is_on_and_seeds_subuid_and_subgid(self) -> None:
        self.assertEqual((True, None), self.choose())
        self.assertEqual("on", (self.root / "dev" / "userns").read_text().strip())
        self.assertTrue(userns.enabled("dev", self.root, self.root / "void.json"))
        self.assertIn(f"root:{userns.SHIFT_BASE}:{userns.SHIFT_SIZE}", self.subuid.read_text())
        self.assertIn("root:1000:1", self.subuid.read_text())
        self.assertIn(f"root:{userns.SHIFT_BASE}:{userns.SHIFT_SIZE}", self.subgid.read_text())

    def test_no_userns_writes_off_without_touching_subuid(self) -> None:
        self.assertEqual((False, None), self.choose(False))
        self.assertEqual("off", (self.root / "dev" / "userns").read_text().strip())
        self.assertFalse(self.subuid.exists())

    def test_an_old_kernel_falls_back_to_off_with_a_reason(self) -> None:
        enabled, reason = self.choose(release="5.10.0")
        self.assertFalse(enabled)
        self.assertIn("5.12", reason or "")
        self.assertEqual("off", (self.root / "dev" / "userns").read_text().strip())

    def test_a_filesystem_without_idmapped_mounts_falls_back_to_off(self) -> None:
        self.mountinfo += "\n40 26 0:30 / /var/lib/spaces rw - zfs tank/spaces rw"
        self.assertEqual("zfs", userns.filesystem_of(Path("/var/lib/spaces/dev"), self.mountinfo))
        self.assertIn("zfs", userns.unsupported_reason(Path("/var/lib/spaces"), "7.2.9_1", self.mountinfo) or "")
        self.assertIsNone(userns.unsupported_reason(Path("/home/x"), "7.2.9_1", self.mountinfo))

    def test_subuid_that_cannot_be_written_falls_back_to_off(self) -> None:
        enabled, reason = self.choose(subuid=self.root / "missing-dir" / "subuid")
        self.assertFalse(enabled)
        self.assertIn("subuid", reason or "")
        self.assertEqual("off", (self.root / "dev" / "userns").read_text().strip())

    def test_creating_again_writes_a_fresh_choice(self) -> None:
        userns.set_enabled("dev", False, self.root)
        self.assertEqual((True, None), self.choose())
        self.assertTrue(userns.enabled("dev", self.root, self.root / "void.json"))

    def test_existing_space_without_a_marker_stays_off(self) -> None:
        self.assertFalse(userns.enabled("dev", self.root, self.root / "void.json"))

    def test_kernel_version_parsing(self) -> None:
        self.assertEqual((7, 2), userns.kernel_version("7.2.9_1"))
        self.assertEqual((6, 1), userns.kernel_version("6.1.0-13-amd64"))
        self.assertEqual((0, 0), userns.kernel_version("weird"))


class ChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.extras = self.root / "void.json"
        write_space(self.root, "ubuntu", {1000: 1000})

    def test_default_is_off_and_the_space_choice_wins(self) -> None:
        self.assertFalse(userns.enabled("ubuntu", self.root, self.extras))
        self.extras.write_text('{"userns": true}')
        self.assertTrue(userns.enabled("ubuntu", self.root, self.extras))
        self.assertTrue(userns.set_enabled("ubuntu", False, self.root))
        self.assertFalse(userns.enabled("ubuntu", self.root, self.extras))
        self.assertTrue(userns.set_enabled("ubuntu", True, self.root))
        self.assertFalse(userns.set_enabled("ubuntu", True, self.root))
        self.assertTrue(userns.enabled("ubuntu", self.root, self.extras))
        self.assertTrue(userns.set_enabled("ubuntu", None, self.root))
        self.assertTrue(userns.enabled("ubuntu", self.root, self.extras))

    def test_garbage_means_the_default(self) -> None:
        (self.root / "ubuntu" / "userns").write_text("maybe\n")
        self.assertFalse(userns.enabled("ubuntu", self.root, self.extras))
        self.extras.write_text("not json")
        self.assertFalse(userns.enabled("ubuntu", self.root, self.extras))

    def test_unknown_space(self) -> None:
        with self.assertRaises(userns.UsernsError):
            userns.set_enabled("nothing", True, self.root)

    def test_void_json_accepts_a_boolean_only(self) -> None:
        base = ROOT / "data" / "config.base.json"
        if not base.exists():
            base = ROOT / "void" / "data" / "config.base.json"
        extras = self.root / "extras.json"
        extras.write_text('{"userns": "yes"}')
        with self.assertRaises(ValueError):
            nvidia.generate(base, extras, None)
        extras.write_text('{"userns": true}')
        nvidia.generate(base, extras, None)


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_plan_collects_users_and_device_groups(self) -> None:
        write_space(self.root, "ubuntu", {1000: 1000, 59990: 59990})
        node = self.root / "node"
        node.touch()
        with mock.patch.object(userns.os, "stat") as stat_mock:
            stat_mock.return_value = mock.Mock(st_gid=46)
            plan = userns.plan_for("ubuntu", [str(node)], self.root, group_ids=[12, 13])
        self.assertEqual((1000, 59990), plan.uids)
        self.assertEqual((12, 13, 46, 1000, 59990), plan.gids)

    def test_root_is_never_identity(self) -> None:
        write_space(self.root, "ubuntu", {0: 0, 1000: 1000})
        plan = userns.plan_for("ubuntu", [], self.root, group_ids=[0, 13])
        self.assertEqual((1000,), plan.uids)
        self.assertNotIn(0, plan.gids)

    def test_unreadable_info_is_an_error(self) -> None:
        with self.assertRaises(userns.UsernsError):
            userns.plan_for("missing", [], self.root, group_ids=[])


class SubidTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.subuid = self.root / "subuid"
        self.subgid = self.root / "subgid"
        self.subuid.write_text("soubarna:100000:65536\nroot:1000000:65536\n")
        self.subgid.write_text("soubarna:100000:65536\nroot:1000000:65536\n")

    def test_what_is_missing_is_exactly_the_identity_ids(self) -> None:
        plan = userns.Plan((1000,), (12, 13, 1000))
        lost_u, lost_g = userns.subid_status(plan, self.subuid, self.subgid)
        self.assertEqual([(1000, 1)], lost_u)
        self.assertEqual([(12, 1), (13, 1), (1000, 1)], lost_g)

    def test_the_users_range_does_not_count_for_root(self) -> None:
        plan = userns.Plan((1000,), (1000,))
        self.subuid.write_text("soubarna:100000:65536\n")
        lost_u, _lost_g = userns.subid_status(plan, self.subuid, self.subgid)
        self.assertIn((1000000, 65536), lost_u)

    def test_setup_adds_lines_once_and_keeps_the_rest(self) -> None:
        plan = userns.Plan((1000,), (12, 1000))
        added = userns.setup_subids(plan, self.subuid, self.subgid)
        self.assertEqual(([(1000, 1)], [(12, 1), (1000, 1)]), added)
        self.assertEqual(
            "soubarna:100000:65536\nroot:1000000:65536\nroot:1000:1\n", self.subuid.read_text()
        )
        self.assertEqual(([], []), userns.setup_subids(plan, self.subuid, self.subgid))
        self.assertEqual(([], []), userns.subid_status(plan, self.subuid, self.subgid))

    def test_numeric_owner_and_comments(self) -> None:
        text = "# comment\n0:1000:1\nroot:1000000:65536\nbob:5:5\nbroken\nroot:x:1\n"
        self.assertEqual([(1000, 1), (1000000, 65536)], userns.parse_subids(text))

    def test_fit_requires_the_base_range_and_the_users_and_drops_other_groups(self) -> None:
        plan = userns.Plan((1000,), (13, 1000))
        have_u = [(1000000, 65536), (1000, 1)]
        have_g = [(1000000, 65536), (1000, 1)]
        fitted, notes = userns.fit(plan, have_u, have_g)
        self.assertEqual((1000,), fitted.gids)
        self.assertEqual(1, len(notes))
        with self.assertRaises(userns.UsernsError):
            userns.fit(plan, [(1000000, 65536)], have_g)
        with self.assertRaises(userns.UsernsError):
            userns.fit(plan, have_u, [(1000, 1)])


class SysBindTests(unittest.TestCase):
    def test_bind_options_repeat_the_locked_flags_of_the_host(self) -> None:
        entries = userns.mountinfo_entries(MOUNTINFO)
        self.assertEqual("rbind,ro,nosuid,nodev,noexec,relatime", userns.sys_bind_options(entries))
        self.assertEqual("rbind,ro", userns.sys_bind_options([]))
        noatime = [("/sys", "rw,nosuid,noatime")]
        self.assertEqual("rbind,ro,nosuid,noatime", userns.sys_bind_options(noatime))

    def test_only_the_topmost_submounts_are_hidden(self) -> None:
        entries = userns.mountinfo_entries(MOUNTINFO)
        self.assertEqual(
            ["/sys/fs/cgroup", "/sys/kernel/security", "/sys/firmware/efi/efivars"],
            userns.sys_submounts(entries),
        )

    def test_escaped_mount_points(self) -> None:
        entries = userns.mountinfo_entries("1 2 0:1 / /sys/with\\040space rw - tmpfs x rw")
        self.assertEqual([("/sys/with space", "rw")], entries)


class TranslatorUsernsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rootfs = self.root / "rootfs"
        (self.rootfs / "usr/lib/systemd").mkdir(parents=True)
        (self.rootfs / "usr/lib/systemd/systemd").touch()
        self.home = self.root / "home"
        (self.home / "root").mkdir(parents=True)
        self.cache = self.root / "cache"
        (self.cache / "work").mkdir(parents=True)
        self.runtime = self.root / "run" / "work"
        real_stat = lxc_config.os.stat
        packaged = self.root / "packaged-file"
        packaged.touch()

        def fake_stat(path, *args, **kwargs):
            if str(path).startswith("/usr/share/spaces/"):
                path = packaged
            return real_stat(path, *args, **kwargs)

        for patch in (
            mock.patch.object(lxc_config.os, "stat", fake_stat),
            mock.patch.object(core, "CACHE_ROOT", self.cache),
            mock.patch.object(launch_module, "_selinux_arguments", lambda: ()),
            # the sources of the test are not under /var/lib/spaces: pretend the test root is
            mock.patch.object(userns, "IDMAP_SOURCES", (str(self.root) + "/",)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def translate(self, plan):
        argv = launch_module._command("work", self.rootfs, self.home, "basic", "basic")
        return lxc_config.translate(
            argv,
            {},
            runtime_dir=self.runtime,
            seccomp_base=SECCOMP,
            userns=plan,
            mountinfo=userns.mountinfo_entries(MOUNTINFO),
        )

    def test_off_changes_nothing(self) -> None:
        text = self.translate(None).config_text
        self.assertIn("lxc.mount.auto = proc:mixed sys:mixed cgroup:rw:force", text)
        self.assertNotIn("lxc.idmap", text)
        self.assertNotIn("idmap=container", text)
        self.assertIn("lxc.apparmor.profile = lxc-spaces-container\n", text + "\n")

    def test_on_uses_the_user_namespace_profile_and_off_the_default(self) -> None:
        on = self.translate(userns.Plan((1000,), (1000,))).config_text.splitlines()
        off = self.translate(None).config_text.splitlines()
        self.assertIn("lxc.apparmor.profile = lxc-spaces-container-userns", on)
        self.assertNotIn("lxc.apparmor.profile = lxc-spaces-container", on)
        self.assertIn("lxc.apparmor.profile = lxc-spaces-container", off)
        self.assertNotIn("lxc.apparmor.profile = lxc-spaces-container-userns", off)

    def test_the_launcher_loads_the_profile_the_space_runs_under(self) -> None:
        backend = lxc.LxcBackend()
        for enabled, expected in ((True, "lxc-spaces-container-userns"), (False, "lxc-spaces-container")):
            with (
                mock.patch.object(userns, "enabled", return_value=enabled),
                mock.patch.object(lxc, "APPARMOR_PROFILES", Path(tempfile.mkdtemp()) / "none"),
                mock.patch.object(lxc.subprocess, "run") as run,
                mock.patch.object(lxc, "_which", return_value="apparmor_parser"),
            ):
                backend._load_apparmor("work")
            self.assertEqual(expected, Path(run.call_args.args[0][-1]).name)

    def test_on_shifts_the_ids_and_idmaps_the_rootfs(self) -> None:
        text = self.translate(userns.Plan((1000,), (1000,))).config_text
        self.assertIn("lxc.rootfs.options = idmap=container", text)
        self.assertIn("lxc.idmap = u 0 1000000 1000", text)
        self.assertIn("lxc.idmap = u 1000 1000 1", text)
        self.assertIn("lxc.namespace.share.net = /proc/1/ns/net", text)
        self.assertLess(text.index("lxc.idmap"), text.index("lxc.net.0.type"))

    def test_on_masks_the_rpc_pipefs_units(self) -> None:
        on = self.translate(userns.Plan((1000,), (1000,))).config_text
        off = self.translate(None).config_text
        for unit in lxc_config.USERNS_MASKED_UNITS:
            self.assertIn(f"/dev/null etc/systemd/system/{unit} none bind,ro", on)
            self.assertNotIn(unit, off)

    def test_on_binds_sysfs_instead_of_mounting_it(self) -> None:
        text = self.translate(userns.Plan((1000,), (1000,))).config_text
        self.assertIn("lxc.mount.auto = proc:mixed cgroup:rw:force", text)
        self.assertNotIn("sys:mixed", text)
        lines = text.splitlines()
        bind = "lxc.mount.entry = /sys sys none rbind,ro,nosuid,nodev,noexec,relatime 0 0"
        self.assertIn(bind, lines)
        for point in ("sys/fs/cgroup", "sys/kernel/security", "sys/firmware/efi/efivars"):
            mask = f"lxc.mount.entry = tmpfs {point} tmpfs ro,nosuid,nodev,noexec,size=4k 0 0"
            self.assertIn(mask, lines)
            self.assertLess(lines.index(bind), lines.index(mask))
        self.assertFalse([x for x in lines if "elogind" in x], "a mount below a hidden one is not listed")

    def test_host_root_owned_spaces_binds_are_idmapped(self) -> None:
        real_stat = os.stat

        class Root:
            st_uid = 0
            st_mode = stat.S_IFDIR | 0o755

        def root_owned(path, *args, **kwargs):
            if str(path).startswith(str(self.root)):
                return Root()
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(userns.os, "stat", root_owned):
            text = self.translate(userns.Plan((1000,), (1000,))).config_text
        entries = [x for x in text.splitlines() if x.startswith("lxc.mount.entry = " + str(self.home))]
        self.assertTrue(entries)
        self.assertTrue(all("idmap=container" in e for e in entries), entries)

    def test_user_owned_and_device_sources_are_not_idmapped(self) -> None:
        self.assertFalse(userns.needs_idmap("/home/someone/Downloads"))
        self.assertFalse(userns.needs_idmap("/var/lib/spaces/.host/nvidia/x/lib/libcuda.so.1"))
        self.assertFalse(userns.needs_idmap("/dev/dri/card0"))
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(userns, "IDMAP_SOURCES", (directory + "/",)):
                owned = Path(directory) / "mine"
                owned.mkdir()
                expected = os.getuid() == 0
                self.assertEqual(expected, userns.needs_idmap(str(owned)))


class BackendTests(unittest.TestCase):
    def test_guest_root_uid_follows_the_choice(self) -> None:
        backend = lxc.LxcBackend()
        with mock.patch.object(userns, "enabled", return_value=False):
            self.assertIsNone(backend.guest_root_uid("ubuntu"))
        with mock.patch.object(userns, "enabled", return_value=True):
            self.assertEqual(userns.SHIFT_BASE, backend.guest_root_uid("ubuntu"))

    def test_plan_is_none_when_off_and_explains_what_is_missing_when_on(self) -> None:
        backend = lxc.LxcBackend()
        argv = ["--bind=/dev/dri/card0:/dev/dri/card0", "--machine=work"]
        with mock.patch.object(userns, "enabled", return_value=False):
            self.assertIsNone(backend._userns_plan("work", argv))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_space(root, "work", {1000: 1000})
            subuid = root / "subuid"
            subuid.write_text("root:1000000:65536\n")
            with (
                mock.patch.object(userns, "enabled", return_value=True),
                mock.patch.object(userns, "STATE_ROOT", root),
                mock.patch.object(userns, "SUBUID", subuid),
                mock.patch.object(userns, "SUBGID", subuid),
                    mock.patch.object(
                    userns, "plan_for", wraps=lambda n, s: userns.Plan((1000,), (1000,))
                ),
            ):
                with self.assertRaises(lxc_config.UnsupportedLaunchOption) as caught:
                    backend._userns_plan("work", argv)
        self.assertIn("spaces-void userns setup work", str(caught.exception))

    def test_system_bus_passes_the_mapped_root_to_the_broker(self) -> None:
        from spaces import host, system_bus

        host.set_backend(lxc.LxcBackend())
        self.addCleanup(host.set_backend, None)
        service = system_bus.SystemBusService("work", "basic")
        with (
            mock.patch.object(userns, "enabled", return_value=True),
            mock.patch.object(Path, "unlink"),
            mock.patch.object(system_bus.subprocess, "Popen") as popen,
        ):
            service._spawn()
        self.assertEqual(str(userns.SHIFT_BASE), popen.call_args.kwargs["env"]["SPACES_GUEST_ROOT_UID"])
        with (
            mock.patch.object(userns, "enabled", return_value=False),
            mock.patch.object(Path, "unlink"),
            mock.patch.object(system_bus.subprocess, "Popen") as popen,
            mock.patch.dict(os.environ, {"SPACES_GUEST_ROOT_UID": ""}),
        ):
            os.environ.pop("SPACES_GUEST_ROOT_UID")
            service._spawn()
        self.assertNotIn("SPACES_GUEST_ROOT_UID", popen.call_args.kwargs["env"])


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "spaces"
        write_space(self.root, "ubuntu", {1000: 1000})
        write_space(self.root, "fedora", {1000: 1000})
        self.subuid = Path(self.temporary.name) / "subuid"
        self.subgid = Path(self.temporary.name) / "subgid"
        for path in (self.subuid, self.subgid):
            path.write_text("root:1000000:65536\n")
        for patch in (
            mock.patch.object(userns, "SUBUID", self.subuid),
            mock.patch.object(userns, "SUBGID", self.subgid),
            mock.patch.object(userns, "EXTRAS_PATH", Path(self.temporary.name) / "void.json"),
            mock.patch.object(userns, "_group_ids", return_value=[13]),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, function, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = function(*args)
        return code, out.getvalue(), err.getvalue()

    def test_enable_runs_setup_and_sets_the_marker(self) -> None:
        code, out, _err = self.call(cli.cmd_userns_enable, ["ubuntu"], False, True, self.root)
        self.assertEqual(0, code)
        self.assertIn("user namespace of ubuntu: on", out)
        self.assertTrue(userns.enabled("ubuntu", self.root))
        self.assertFalse(userns.enabled("fedora", self.root))
        self.assertIn("root:1000:1", self.subuid.read_text())
        self.assertIn("root:13:1", self.subgid.read_text())
        code, out, _err = self.call(cli.cmd_userns_status, self.root)
        self.assertIn("ready", out)
        self.assertRegex(out, r"ubuntu\s+on\s+ready")
        self.assertRegex(out, r"fedora\s+off\s+ready")

    def test_disable_all_and_a_missing_name(self) -> None:
        self.call(cli.cmd_userns_enable, [], True, True, self.root)
        code, out, _err = self.call(cli.cmd_userns_enable, [], True, False, self.root)
        self.assertEqual(0, code)
        self.assertFalse(userns.enabled("ubuntu", self.root))
        self.assertFalse(userns.enabled("fedora", self.root))
        code, _out, err = self.call(cli.cmd_userns_enable, [], False, True, self.root)
        self.assertEqual(2, code)
        self.assertIn("name a space", err)

    def test_enable_an_unknown_space_fails(self) -> None:
        code, _out, err = self.call(cli.cmd_userns_enable, ["ghost"], False, True, self.root)
        self.assertEqual(1, code)
        self.assertIn("ghost", err)

    def test_parser_and_privilege(self) -> None:
        arguments = cli.build_parser().parse_args(["userns", "enable", "--all"])
        self.assertTrue(arguments.all)
        with (
            mock.patch.object(cli.os, "geteuid", return_value=1000),
            mock.patch.object(cli, "_reexec_with_sudo", return_value=7) as reexec,
        ):
            self.assertEqual(7, cli.main(["userns", "setup", "ubuntu"]))
            reexec.assert_called_once()
            reexec.reset_mock()
            with mock.patch.object(cli, "cmd_userns_status", return_value=0):
                self.assertEqual(0, cli.main(["userns", "status"]))
            reexec.assert_not_called()

    def test_doctor_reports_missing_ranges_and_off(self) -> None:
        with mock.patch.object(doctor.autostart, "space_names", return_value=["ubuntu"]):
            with mock.patch.object(userns, "enabled", return_value=False):
                (result,) = list(doctor.check_userns())
            self.assertEqual("PASS", result[0])
            with (
                mock.patch.object(userns, "enabled", return_value=True),
                mock.patch.object(userns, "STATE_ROOT", self.root),
            ):
                results = list(doctor.check_userns())
                self.assertEqual("FAIL", results[0][0])
                self.assertIn("spaces-void userns setup ubuntu", results[0][2])
                userns.setup_subids(userns.plan_for("ubuntu", root=self.root))
                results = list(doctor.check_userns())
                self.assertEqual("PASS", results[0][0])


class ProfileAndBrokerTests(unittest.TestCase):
    def test_profile_allows_the_nosymfollow_remount_of_a_user_namespace(self) -> None:
        text = (ROOT / "void/apparmor/lxc-spaces-container").read_text()
        self.assertIn(
            "mount options=(remount, bind, nosuid, nodev, noexec, ro, nosymfollow) -> /**,", text
        )

    def test_broker_source_reads_the_mapped_root(self) -> None:
        source = (ROOT / "native/spaces_system_broker.c").read_text()
        self.assertIn("SPACES_GUEST_ROOT_UID", source)
        self.assertIn("uid == 0 || (guest_root_uid != 0 && uid == guest_root_uid)", source)


if __name__ == "__main__":
    unittest.main()
