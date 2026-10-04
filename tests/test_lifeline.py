"""The lifeline that ties the open broker to the launcher (spaces.lifeline, native/lifeline.h)."""

from __future__ import annotations

import gc
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from spaces import lifeline, session

ROOT = Path(__file__).resolve().parents[1]

# A stand-in for the broker: waits for POLLHUP on the descriptor named by --death-fd.
HELPER = (
    "import select, sys\n"
    "fd = int(sys.argv[sys.argv.index('--death-fd') + 1])\n"
    "p = select.poll(); p.register(fd, select.POLLHUP)\n"
    "print('ready', flush=True)\n"
    "p.poll()\n"
)


def wait_exit(process: subprocess.Popen, timeout: float = 5.0) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def spawn_helper(read_end: int) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-c", HELPER, "--death-fd", str(read_end)],
        pass_fds=(read_end,), stdout=subprocess.PIPE,
    )
    assert process.stdout.readline() == b"ready\n"
    return process


class LifelineTests(unittest.TestCase):
    def test_both_ends_are_close_on_exec(self) -> None:
        read_end, write_end = lifeline.open_lifeline()
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, write_end)
        for descriptor in (read_end, write_end):
            self.assertTrue(os.get_inheritable(descriptor) is False)

    def test_release_stops_the_helper(self) -> None:
        read_end, write_end = lifeline.open_lifeline()
        helper = spawn_helper(read_end)
        os.close(read_end)
        self.addCleanup(helper.kill)
        lifeline.hold(helper, write_end)
        time.sleep(0.2)
        self.assertIsNone(helper.poll())
        lifeline.release(helper)
        self.assertTrue(wait_exit(helper))
        lifeline.release(helper)  # a second release is harmless

    def test_the_write_end_closes_when_a_finished_process_object_is_collected(self) -> None:
        read_end, write_end = lifeline.open_lifeline()
        helper = spawn_helper(read_end)
        os.close(read_end)
        lifeline.hold(helper, write_end)
        helper.kill()
        helper.wait()
        helper.stdout.close()
        del helper
        gc.collect()
        with self.assertRaises(OSError):
            os.fstat(write_end)

    def test_other_children_do_not_inherit_the_write_end(self) -> None:
        read_end, write_end = lifeline.open_lifeline()
        helper = spawn_helper(read_end)
        os.close(read_end)
        self.addCleanup(helper.kill)
        lifeline.hold(helper, write_end)
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(other.kill)
        time.sleep(0.1)
        self.assertNotIn(str(write_end), os.listdir(f"/proc/{other.pid}/fd"))
        lifeline.release(helper)
        self.assertTrue(wait_exit(helper))

    def test_killing_the_launcher_with_sigkill_stops_the_helper(self) -> None:
        launcher_code = textwrap.dedent(
            """
            import subprocess, sys, threading
            from spaces import lifeline

            def start():
                read_end, write_end = lifeline.open_lifeline()
                helper = subprocess.Popen(
                    [sys.executable, "-c", sys.argv[1], "--death-fd", str(read_end)],
                    pass_fds=(read_end,), stdout=subprocess.PIPE)
                helper.stdout.readline()
                lifeline.hold(helper, write_end)
                print(helper.pid, flush=True)
                holder.append(helper)

            holder = []
            # Started from a thread that exits, like the launcher's session workers.
            thread = threading.Thread(target=start)
            thread.start(); thread.join()
            sys.stdin.read()
            """
        )
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        launcher = subprocess.Popen(
            [sys.executable, "-c", launcher_code, HELPER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
        )
        self.addCleanup(launcher.kill)
        helper_pid = int(launcher.stdout.readline())
        time.sleep(0.3)
        os.kill(helper_pid, 0)  # alive although the thread that forked it is gone
        launcher.kill()
        launcher.wait()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(helper_pid, 0)
            except ProcessLookupError:
                return
            # a zombie of a reparented child is reaped by init; treat state Z as gone
            try:
                state = Path(f"/proc/{helper_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except OSError:
                return
            if state == "Z":
                return
            time.sleep(0.05)
        os.kill(helper_pid, signal.SIGKILL)
        self.fail("the helper survived the launcher")


class StartBrokerTests(unittest.TestCase):
    def test_the_broker_gets_a_lifeline_and_the_launcher_keeps_the_write_end(self) -> None:
        seen = {}

        def fake_popen(command, **kwargs):
            seen["command"] = command
            seen["pass_fds"] = kwargs["pass_fds"]
            os.write(int(command[command.index("--ready-fd") + 1]), b"1")
            process = mock.MagicMock()
            process.poll.return_value = None
            return process

        backend = mock.MagicMock()
        backend.session_bus_address.return_value = "unix:path=/run/user/1000/bus"
        user = SimpleNamespace(uid=1000, gid=1000)
        with mock.patch.object(session, "_open_mapping_descriptors", return_value=([], ["--map", "/a", "/b"])), \
                mock.patch.object(session.host, "get_backend", return_value=backend), \
                mock.patch.object(session.subprocess, "Popen", side_effect=fake_popen), \
                mock.patch.object(session.lifeline, "hold") as hold:
            process, _name = session._start_open_broker(
                "work", user, "1", mock.MagicMock(), Path("/rootfs"), Path("/home"),
            )
        command = seen["command"]
        read_end = int(command[command.index("--death-fd") + 1])
        self.assertIn(read_end, seen["pass_fds"])
        hold.assert_called_once()
        self.assertIs(hold.call_args.args[0], process)
        write_end = hold.call_args.args[1]
        self.assertNotEqual(read_end, write_end)
        # The launcher closed its copy of the read end and still has the write end.
        with self.assertRaises(OSError):
            os.fstat(read_end)
        os.fstat(write_end)
        os.close(write_end)

    def test_a_failing_spawn_leaks_no_lifeline(self) -> None:
        before = set(os.listdir("/proc/self/fd"))
        backend = mock.MagicMock()
        with mock.patch.object(session, "_open_mapping_descriptors", return_value=([], ["--map", "/a", "/b"])), \
                mock.patch.object(session.host, "get_backend", return_value=backend), \
                mock.patch.object(session.subprocess, "Popen", side_effect=OSError("no")):
            with self.assertRaises(OSError):
                session._start_open_broker(
                    "work", SimpleNamespace(uid=1000, gid=1000), "1", mock.MagicMock(), Path("/r"), Path("/h"),
                )
        self.assertEqual(set(os.listdir("/proc/self/fd")) - before, set())


@unittest.skipUnless(shutil.which("cc") and shutil.which("pkg-config"), "C compiler and pkg-config are required")
class NativeLifelineTests(unittest.TestCase):
    def test_the_event_loop_quits_when_the_write_end_closes(self) -> None:
        flags = subprocess.run(
            ["pkg-config", "--cflags", "--libs", "gio-unix-2.0"],
            capture_output=True, text=True, check=False,
        )
        if flags.returncode != 0:
            self.skipTest("gio-unix-2.0 is required")
        program = textwrap.dedent(
            f"""
            #include "{ROOT / 'native' / 'lifeline.h'}"
            #include <stdio.h>
            #include <unistd.h>

            static gboolean fail_after_timeout(gpointer data)
            {{
                (void)data;
                puts("TIMEOUT");
                _exit(3);
            }}

            int main(void)
            {{
                int fds[2];
                GMainLoop *loop = g_main_loop_new(NULL, FALSE);
                if (pipe(fds) != 0 || lifeline_valid(-1) || lifeline_watch(-1, loop) != 0)
                    return 2;
                if (lifeline_watch(fds[0], loop) == 0)
                    return 4;
                if (!(fcntl(fds[0], F_GETFD) & FD_CLOEXEC))
                    return 5;
                g_timeout_add(300, (GSourceFunc)close_writer, GINT_TO_POINTER(fds[1]));
                g_timeout_add_seconds(5, fail_after_timeout, NULL);
                g_main_loop_run(loop);
                puts("QUIT");
                return 0;
            }}
            """
        ).replace(
            "int main(void)",
            "static gboolean close_writer(gpointer fd) { close(GPOINTER_TO_INT(fd)); return G_SOURCE_REMOVE; }\n"
            "int main(void)",
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "probe.c"
            source.write_text(program)
            binary = Path(temporary) / "probe"
            build = subprocess.run(
                ["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(binary), *flags.stdout.split()],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "QUIT"), run.stderr)


if __name__ == "__main__":
    unittest.main()
