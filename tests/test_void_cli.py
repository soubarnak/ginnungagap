from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from spaces.host import autostart, cli, doctor

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "void" / "shell" / "spaces.sh"


class CliBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "spaces"
        for name, distro in (("ubuntu", "ubuntu"), ("fedora", "fedora")):
            (self.root / name).mkdir(parents=True)
            (self.root / name / "info.json").write_text(
                json.dumps({"distribution": {"id": distro}})
            )

    def call(self, function, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = function(*args, **kwargs)
        return code, out.getvalue(), err.getvalue()


class AutostartCommandTests(CliBase):
    def test_enable_disable_user_and_boot(self) -> None:
        with mock.patch.object(cli, "caller_name", return_value="alice"), mock.patch.object(
            cli, "caller_uid", return_value=1000
        ):
            code, out, _ = self.call(
                cli.cmd_autostart_change, "ubuntu", enable=True, user=None, boot=False, root=self.root
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(autostart.read_users("ubuntu", self.root), ["alice"])
            self.assertFalse(autostart.boot_enabled("ubuntu", self.root))
            code, out, _ = self.call(
                cli.cmd_autostart_change, "ubuntu", enable=True, user=None, boot=True, root=self.root
            )
            self.assertTrue(autostart.boot_enabled("ubuntu", self.root))
            self.assertEqual(autostart.read_users("ubuntu", self.root), ["alice"])
            self.call(cli.cmd_autostart_change, "ubuntu", enable=False, user="alice", boot=True, root=self.root)
            self.assertFalse(autostart.boot_enabled("ubuntu", self.root))
            self.assertEqual(autostart.read_users("ubuntu", self.root), [])

    def test_root_without_user_must_name_one(self) -> None:
        with mock.patch.object(cli, "caller_name", return_value="root"), mock.patch.object(
            cli, "caller_uid", return_value=0
        ):
            code, _, err = self.call(
                cli.cmd_autostart_change, "ubuntu", enable=True, user=None, boot=False, root=self.root
            )
        self.assertEqual(code, 2)
        self.assertIn("--user", err)

    def test_unknown_space_fails(self) -> None:
        code, _, err = self.call(
            cli.cmd_autostart_change, "nope", enable=True, user="alice", boot=False, root=self.root
        )
        self.assertEqual(code, 1)
        self.assertIn("no such space", err)

    def test_list_shows_users_and_boot(self) -> None:
        autostart.set_user("ubuntu", "alice", True, self.root)
        autostart.set_boot("fedora", True, self.root)
        code, out, _ = self.call(cli.cmd_autostart_list, self.root)
        self.assertEqual(code, 0)
        self.assertRegex(out, r"ubuntu\s+no\s+alice")
        self.assertRegex(out, r"fedora\s+yes\s+-")

    def test_changing_commands_reexec_through_sudo(self) -> None:
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch.object(
            subprocess, "call", return_value=0
        ) as call:
            self.assertEqual(cli.main(["autostart", "enable", "ubuntu"]), 0)
        self.assertEqual(call.call_args.args[0][0], "sudo")
        self.assertEqual(call.call_args.args[0][-3:], ["autostart", "enable", "ubuntu"])


class InstallFlavorTests(CliBase):
    def run_flavor(self, name: str, choice: str | None, **patches):
        with mock.patch.object(cli.flavor, "resolve", return_value=choice or "gtk"), mock.patch.object(
            subprocess, "call", return_value=patches.get("status", 0)
        ) as call:
            result = self.call(cli.cmd_install_flavor, name, choice, self.root)
        return result, call

    def test_ubuntu_runs_apt_as_root_through_spaces_enter(self) -> None:
        (code, out, _), call = self.run_flavor("ubuntu", "gtk")
        self.assertEqual(code, 0)
        command = call.call_args.args[0]
        self.assertEqual(command[:5], ["spaces", "enter", "ubuntu", "--root", "--"])
        self.assertIn("apt-get", command)
        self.assertIn("xdg-desktop-portal-gtk", command)
        self.assertIn("installed in ubuntu", out)

    def test_fedora_uses_dnf5(self) -> None:
        (code, _, _), call = self.run_flavor("fedora", "gtk")
        self.assertIn("dnf5", call.call_args.args[0])

    def test_kde_installs_nothing(self) -> None:
        (code, out, _), call = self.run_flavor("ubuntu", "kde")
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertIn("adds nothing", out)

    def test_failure_status_is_returned(self) -> None:
        (code, _, err), _ = self.run_flavor("ubuntu", "gtk", status=100)
        self.assertEqual(code, 100)
        self.assertIn("failed", err)

    def test_unknown_space(self) -> None:
        (code, _, err), _ = self.run_flavor("ghost", "gtk")
        self.assertEqual(code, 1)


class DoctorTests(unittest.TestCase):
    def test_run_counts_failures_and_survives_a_crash(self) -> None:
        def good():
            yield "PASS", "a", "fine"
            yield "WARN", "b", "hmm"

        def bad():
            yield "FAIL", "c", "broken"

        def crash():
            raise RuntimeError("x")
            yield  # pragma: no cover

        results, failures = doctor.run((good, bad, crash))
        self.assertEqual(failures, 2)
        self.assertEqual([r[0] for r in results], ["PASS", "WARN", "FAIL", "FAIL"])

    def test_service_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state, svdir, service_dir = base / "spaces", base / "sv", base / "service"
            (state / "ubuntu").mkdir(parents=True)
            (state / "ubuntu" / "info.json").write_text("{}")
            autostart.set_user("ubuntu", "alice", True, state)
            service = svdir / "spaces-ubuntu"
            (service / "log").mkdir(parents=True)
            for relative in ("run", "finish", "log/run"):
                (service / relative).write_text("#!/bin/sh\n")
                (service / relative).chmod(0o755)
            (service / "down").touch()
            (svdir / "spaces-gone").mkdir()
            service_dir.mkdir()
            (service_dir / "spaces-ubuntu").symlink_to(service)
            with mock.patch.object(doctor, "_service_status", return_value="down"):
                results = list(doctor.check_services(state, svdir, service_dir))
        by_name = {name: (status, detail) for status, name, detail in results}
        self.assertEqual(by_name["service ubuntu"][0], "PASS")
        self.assertEqual(by_name["service gone"][0], "WARN")
        self.assertEqual(by_name["autostart service"][0], "WARN")
        self.assertIn("ln -s /etc/sv/spaces-autostart", by_name["autostart service"][1])


class EntryWrapperTests(unittest.TestCase):
    def test_wrapper_maps_the_command_name_to_the_space(self) -> None:
        wrapper = ROOT / "void" / "entry" / "enter-space"
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory)
            fake = bin_dir / "spaces"
            fake.write_text('#!/bin/sh\necho "$@"\n')
            fake.chmod(0o755)
            for command, space in (("ubuntu", "ubuntu"), ("arch-linux", "arch"), ("kali", "kali")):
                link = bin_dir / command
                link.symlink_to(wrapper)
                for arguments in (["id"], ["--", "id"]):
                    result = subprocess.run(
                        [str(link), *arguments],
                        capture_output=True,
                        text=True,
                        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
                    )
                    self.assertEqual(result.stdout.strip(), f"enter {space} -- id", command)

    def test_wrapper_is_posix_sh(self) -> None:
        text = (ROOT / "void" / "entry" / "enter-space").read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertNotIn("bash", text)


@unittest.skipUnless(shutil.which("bash") and shutil.which("sh"), "needs bash")
class ShellSnippetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bin = Path(self.temporary.name)
        spaces = self.bin / "spaces"
        spaces.write_text('#!/bin/sh\necho "spaces $*"\n')
        spaces.chmod(0o755)

    def bash(self, script: str, extra_commands: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        for name in extra_commands:
            command = self.bin / name
            command.write_text("#!/bin/sh\necho real\n")
            command.chmod(0o755)
        env = {"PATH": str(self.bin), "HOME": self.temporary.name}
        return subprocess.run(
            [shutil.which("bash"), "--norc", "-c", f". {SHELL}\n{script}"],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_entry_functions_enter_the_space(self) -> None:
        for name in ("arch", "ubuntu", "fedora", "kali"):
            result = self.bash(f"{name} id -u")
            self.assertEqual(result.stdout.strip(), f"spaces enter {name} -- id -u", result.stderr)

    def test_leading_double_dash_is_dropped(self) -> None:
        result = self.bash("ubuntu -- id; ubuntu id")
        self.assertEqual(result.stdout.split("\n")[:2], ["spaces enter ubuntu -- id"] * 2)

    def test_hints_when_the_host_lacks_the_command(self) -> None:
        for name, space in (("apt", "ubuntu"), ("apt-get", "ubuntu"), ("dnf", "fedora")):
            result = self.bash(f"{name} install x; echo status=$?")
            self.assertIn("status=127", result.stdout)
            self.assertIn(f"`{space} {name} ...`", result.stderr)

    def test_a_real_command_is_not_shadowed(self) -> None:
        result = self.bash("apt", extra_commands=("apt",))
        self.assertEqual(result.stdout.strip(), "real")

    def test_host_package_managers_are_never_touched(self) -> None:
        result = self.bash("type -t pacman xbps-install _unused; true")
        self.assertNotIn("function", result.stdout)

    def test_no_hints_switch_and_missing_spaces(self) -> None:
        # Only the fake directory on PATH: the host may have a real spaces.
        env = {"PATH": str(self.bin), "SPACES_NO_HINTS": "1"}
        run = lambda script: subprocess.run(  # noqa: E731
            [shutil.which("bash"), "--norc", "-c", f". {SHELL}\n{script}"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(run("type -t apt || echo none").stdout.strip(), "none")
        self.assertEqual(run("type -t ubuntu").stdout.strip(), "function")
        (self.bin / "spaces").unlink()
        self.assertEqual(run("type -t ubuntu || echo none").stdout.strip(), "none")


if __name__ == "__main__":
    unittest.main()
