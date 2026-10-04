"""`void/tools/release.sh repo`: a signed local repository, with a stand-in for xbps-rindex."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "void" / "tools" / "release.sh"

STUB = """#!/bin/sh
# records the calls; the signature files are made like xbps-rindex does
echo "$@" >>"$STUB_LOG"
case "$1" in
    --privkey) shift 2; [ "$1" = --signedby ] && shift 2 ;;
esac
case "$1" in
    -S) shift; for file in "$@"; do : >"$file.sig2"; done ;;
esac
"""


@unittest.skipUnless(shutil.which("bash") and shutil.which("realpath"), "bash and realpath are required")
class ReleaseRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "xbps-rindex").write_text(STUB)
        (self.bin / "xbps-rindex").chmod(0o755)
        self.log = self.root / "calls.log"
        self.packages = self.root / "binpkgs"
        self.packages.mkdir()
        for name in (
            "spaces-0.0.1_1.x86_64.xbps",
            "spaces-0.0.1_2.x86_64.xbps",
            "spaces-0.0.1_1.aarch64.xbps",
            "spaces-arch-install-scripts-31_1.x86_64.xbps",
            "ubuntu-keyring-2026.08.18_1.x86_64.xbps",
            "unrelated-1_1.x86_64.xbps",
        ):
            (self.packages / name).write_bytes(b"package")
        self.key = self.root / "key.pem"
        self.key.write_text("not a real key")
        self.key.chmod(0o600)
        self.out = self.root / "repo"

    def run_repo(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", STUB_LOG=str(self.log))
        return subprocess.run(
            [str(SCRIPT), "repo", *arguments], cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )

    def test_it_needs_a_key_and_a_signer(self) -> None:
        for arguments, message in (
            ((), "--key"),
            (("--key", str(self.key)), "--signedby"),
            (("--key", str(self.root / "missing"), "--signedby", "A <a@b>"), "cannot read the key"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_repo(*arguments, "--from", str(self.packages), "--out", str(self.out))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout)
        self.assertFalse(self.log.exists())

    def test_it_indexes_then_signs_the_repository_and_every_package(self) -> None:
        result = self.run_repo(
            "--key", str(self.key), "--signedby", "Maintainer <m@example.org>",
            "--from", str(self.packages), "--out", str(self.out),
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        calls = self.log.read_text().splitlines()
        self.assertEqual(len(calls), 3, calls)
        self.assertTrue(calls[0].startswith("-a "), calls[0])
        self.assertEqual(
            calls[1], f"--privkey {self.key} --signedby Maintainer <m@example.org> -s {self.out}",
        )
        self.assertTrue(calls[2].startswith(f"--privkey {self.key} -S "), calls[2])
        # the newest version per package and architecture, our packages only
        expected = {
            "spaces-0.0.1_2.x86_64.xbps",
            "spaces-0.0.1_1.aarch64.xbps",
            "spaces-arch-install-scripts-31_1.x86_64.xbps",
            "ubuntu-keyring-2026.08.18_1.x86_64.xbps",
        }
        self.assertEqual({path.name for path in self.out.glob("*.xbps")}, expected)
        self.assertEqual({path.name for path in self.out.glob("*.sig2")}, {f"{name}.sig2" for name in expected})
        self.assertIn("nothing was published", result.stdout)

    def test_the_key_is_never_copied_or_printed(self) -> None:
        self.key.write_text("SECRET-KEY-MATERIAL")
        result = self.run_repo(
            "--key", str(self.key), "--signedby", "M <m@example.org>",
            "--from", str(self.packages), "--out", str(self.out),
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("SECRET-KEY-MATERIAL", result.stdout)
        for path in self.out.iterdir():
            self.assertNotIn(b"SECRET-KEY-MATERIAL", path.read_bytes())

    def test_an_existing_repository_is_replaced_only_on_request(self) -> None:
        arguments = ("--key", str(self.key), "--signedby", "M <m@e>", "--from", str(self.packages), "--out", str(self.out))
        self.assertEqual(self.run_repo(*arguments).returncode, 0)
        (self.out / "keep.txt").write_text("mine")
        again = self.run_repo(*arguments)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("--replace", again.stdout)
        self.assertEqual(self.run_repo(*arguments, "--replace").returncode, 0)
        self.assertEqual((self.out / "keep.txt").read_text(), "mine")

    def test_an_empty_package_directory_is_an_error(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        result = self.run_repo(
            "--key", str(self.key), "--signedby", "M <m@e>", "--from", str(empty), "--out", str(self.out),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no package of void/srcpkgs", result.stdout)


if __name__ == "__main__":
    unittest.main()
