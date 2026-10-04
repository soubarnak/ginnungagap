"""The spaces-lxc wrapper: private network namespace for lxc-start, nsenter for the rest."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "void" / "bin" / "spaces-lxc"

# Stand-ins for unshare, nsenter, stat and umount: they print what they were asked to
# do, and unshare/nsenter run the rest of the command line like the real tools do.
FAKES = {
    "unshare": '''#!/bin/sh
echo "unshare $*" >>"$FAKE_LOG"
while [ $# -gt 0 ]; do
    case $1 in
        --) shift; break ;;
        *) shift ;;
    esac
done
exec "$@"
''',
    "nsenter": '''#!/bin/sh
echo "nsenter $*" >>"$FAKE_LOG"
while [ $# -gt 0 ]; do
    case $1 in
        --) shift; break ;;
        *) shift ;;
    esac
done
exec "$@"
''',
    "umount": '#!/bin/sh\necho "umount $*" >>"$FAKE_LOG"\n',
    # `stat -f -c %T FILE`: nsfs once the pin exists and has content, else ext2/ext3.
    "stat": '#!/bin/sh\n[ -s "$4" ] && echo nsfs || echo ext2/ext3\n',
}


class WrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, text in FAKES.items():
            path = self.bin / name
            path.write_text(text)
            path.chmod(0o755)
        self.log = self.root / "log"
        self.lxc = self.root / "lxc"
        (self.lxc / "work").mkdir(parents=True)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE_LOG": str(self.log),
        }

    def run_wrapper(self, *args: str) -> list[str]:
        subprocess.run(
            [str(WRAPPER), *args], env=self.env, check=True, stdout=subprocess.DEVNULL
        )
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_wrapper_is_posix_sh(self) -> None:
        self.assertEqual(WRAPPER.read_text().splitlines()[0], "#!/bin/sh")
        self.assertTrue(os.stat(WRAPPER).st_mode & stat.S_IXUSR)
        subprocess.run(["sh", "-n", str(WRAPPER)], check=True)

    def test_lxc_start_pins_a_private_network_namespace(self) -> None:
        lines = self.run_wrapper_as("lxc-start")
        pin = self.lxc / "work" / "netns"
        self.assertTrue(pin.exists())
        unshares = [line for line in lines if line.startswith("unshare")]
        self.assertEqual(len(unshares), 2)
        self.assertTrue(unshares[0].startswith(f"unshare --net={pin} -- unshare --mount "))
        self.assertFalse(any(line.startswith("nsenter") for line in lines))

    def run_wrapper_as(self, tool: str, *extra: str) -> list[str]:
        """Run the wrapper with a stub tool called `tool` that does nothing."""

        stub = self.bin / tool
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        return self.run_wrapper(str(stub), "-P", str(self.lxc), "-n", "work", *extra)

    def test_lxc_start_behind_a_shell_is_still_recognised(self) -> None:
        stub = self.bin / "lxc-start"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        lines = self.run_wrapper(
            "sh", "-c", 'echo $$ >"$1" && shift && exec "$@"', "sh",
            str(self.root / "procs"), str(stub), "-P", str(self.lxc), "-n", "work",
        )
        self.assertTrue((self.lxc / "work" / "netns").exists())
        self.assertEqual(sum(line.startswith("unshare") for line in lines), 2)

    def test_other_tools_enter_the_pinned_namespace(self) -> None:
        (self.lxc / "work" / "netns").write_text("pinned\n")
        lines = self.run_wrapper_as("lxc-info", "-s")
        pin = self.lxc / "work" / "netns"
        self.assertEqual(sum(line.startswith("unshare --mount") for line in lines), 1)
        self.assertTrue(any(line.startswith(f"nsenter --net={pin} --") for line in lines))

    def test_without_a_pin_the_tool_runs_in_the_host_namespace(self) -> None:
        lines = self.run_wrapper_as("lxc-info", "-s")
        self.assertFalse(any(line.startswith("nsenter") for line in lines))
        self.assertEqual(sum(line.startswith("unshare") for line in lines), 1)

    def test_a_stale_pin_is_unmounted_before_lxc_start(self) -> None:
        pin = self.lxc / "work" / "netns"
        pin.write_text("stale\n")
        lines = self.run_wrapper_as("lxc-start")
        self.assertEqual(lines[0], f"umount {pin}")

    def test_arguments_after_double_dash_are_not_options_of_the_wrapper(self) -> None:
        (self.lxc / "work" / "netns").write_text("pinned\n")
        lines = self.run_wrapper_as("lxc-attach", "--", "sh", "-n", "other", "-P", "/x")
        pin = self.lxc / "work" / "netns"
        self.assertTrue(any(line.startswith(f"nsenter --net={pin} --") for line in lines))

    def test_unsafe_names_never_reach_a_path(self) -> None:
        stub = self.bin / "lxc-info"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        lines = self.run_wrapper(str(stub), "-P", str(self.lxc), "-n", "../work")
        self.assertFalse(any("nsenter" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
