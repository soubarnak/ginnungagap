"""Tests for void/bin/dnf5, the dnf5 shim that runs a bootstrap Fedora."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SHIM = Path(__file__).resolve().parents[1] / "void" / "bin" / "dnf5"


def load():
    loader = importlib.machinery.SourceFileLoader("void_dnf5_shim", str(SHIM))
    spec = importlib.util.spec_from_loader("void_dnf5_shim", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


shim = load()


class ArgumentTests(unittest.TestCase):
    def test_release(self) -> None:
        self.assertEqual(shim.release_of(["install", "--releasever=44"]), "44")
        self.assertEqual(shim.release_of(["--releasever", "44", "x"]), "44")
        self.assertEqual(shim.release_of(["--version"]), "44")
        with self.assertRaises(shim.ShimError):
            shim.release_of(["--releasever=7"])

    def test_installroot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(shim.installroot_of([f"--installroot={root}"]), root)
            self.assertEqual(shim.installroot_of(["--installroot", str(root)]), root)
        self.assertIsNone(shim.installroot_of(["install", "x"]))

    def test_mount_script(self) -> None:
        script = shim.mount_script(Path("/b/root"), Path("/var/lib/spaces/fedora/rootfs"))
        self.assertIn("mount --rbind '/var/lib/spaces/fedora/rootfs' "
                      "'/b/root/var/lib/spaces/fedora/rootfs'", script)
        self.assertIn("mount -t proc proc '/b/root/proc'", script)
        self.assertIn("'/b/root/usr/share/spaces'", script)
        self.assertTrue(script.rstrip().endswith('exec chroot "$@"'))
        self.assertNotIn("/b/root/var/lib/spaces/fedora/rootfs", shim.mount_script(Path("/b/root"), None))


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        if not (shutil.which("gpg") and shutil.which("gpgv")):
            self.skipTest("gpg/gpgv missing")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "gh"
        self.home.mkdir(mode=0o700)
        self.env = {**os.environ, "GNUPGHOME": str(self.home)}
        self.keys = self.tmp / "keys"
        self.keys.mkdir()
        patcher = mock.patch.object(shim, "KEY_DIR", self.keys)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_key(self, uid: str) -> str:
        subprocess.run(["gpg", "--batch", "--passphrase", "", "--quick-gen-key", uid, "ed25519", "sign", "never"],
                       check=True, env=self.env, capture_output=True)
        out = subprocess.run(["gpg", "--batch", "--list-keys", "--with-colons", uid], check=True,
                             env=self.env, capture_output=True, text=True).stdout
        return next(l.split(":")[9] for l in out.splitlines() if l.startswith("fpr:"))

    def export(self, uid: str, name: str) -> None:
        data = subprocess.run(["gpg", "--batch", "--armor", "--export", uid], check=True, env=self.env,
                              capture_output=True).stdout
        (self.keys / name).write_bytes(data)

    def sign(self, uid: str, text: str) -> Path:
        path = self.tmp / "CHECKSUM"
        path.write_text(text)
        subprocess.run(["gpg", "--batch", "--yes", "--local-user", uid, "--clearsign", str(path)],
                       check=True, env=self.env, capture_output=True)
        return Path(str(path) + ".asc")

    def test_valid_signature_and_parse(self) -> None:
        self.make_key("release <r@example.test>")
        self.export("release <r@example.test>", "RPM-GPG-KEY-fedora-44-primary")
        digest = "ab" * 32
        signed = self.sign("release <r@example.test>", f"# x\nSHA256 (image.tar.xz) = {digest}\n")
        self.assertEqual(shim.verify_checksum_file(signed, "44"), {"image.tar.xz": digest})

    def test_other_signer_or_tampering_is_refused(self) -> None:
        self.make_key("release <r@example.test>")
        self.make_key("other <o@example.test>")
        self.export("release <r@example.test>", "RPM-GPG-KEY-fedora-44-primary")
        digest = "cd" * 32
        text = f"SHA256 (image.tar.xz) = {digest}\n"
        with self.assertRaises(shim.ShimError):
            shim.verify_checksum_file(self.sign("other <o@example.test>", text), "44")
        good = self.sign("release <r@example.test>", text)
        good.write_text(good.read_text().replace(digest, "ef" * 32))
        with self.assertRaises(shim.ShimError):
            shim.verify_checksum_file(good, "44")


class OciTests(unittest.TestCase):
    def layer(self, path: Path, files: dict[str, bytes | None]) -> None:
        with tarfile.open(path, "w:gz") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                data = data or b""
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))

    def test_blob_digest_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blobs = Path(tmp)
            data = b"hello"
            digest = hashlib.sha256(data).hexdigest()
            (blobs / digest).write_bytes(data)
            self.assertEqual(shim.blob_path(blobs, f"sha256:{digest}"), blobs / digest)
            (blobs / digest).write_bytes(b"tampered")
            with self.assertRaises(shim.ShimError):
                shim.blob_path(blobs, f"sha256:{digest}")
            with self.assertRaises(shim.ShimError):
                shim.blob_path(blobs, "md5:00")

    def test_layers_and_whiteouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root = tmp / "root"
            root.mkdir()
            first = tmp / "l1.tar.gz"
            second = tmp / "l2.tar.gz"
            self.layer(first, {"etc/a": b"1", "etc/b": b"2", "opt/x": b"3", "opt/y": b"4"})
            self.layer(second, {"etc/.wh.a": None, "opt/.wh..wh..opq": None, "opt/z": b"5"})
            shim.apply_layer(first, root)
            shim.apply_layer(second, root)
            self.assertFalse((root / "etc" / "a").exists())
            self.assertEqual((root / "etc" / "b").read_bytes(), b"2")
            self.assertEqual(sorted(p.name for p in (root / "opt").iterdir()), ["z"])
            self.assertFalse(list(root.rglob(".wh.*")))

    def test_manifest_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blobs = Path(tmp)

            def put(data: bytes) -> str:
                digest = hashlib.sha256(data).hexdigest()
                (blobs / digest).write_bytes(data)
                return "sha256:" + digest

            layer = put(b"layer")
            manifest = put(json.dumps({"layers": [{"digest": layer}]}).encode())
            index = {"manifests": [{"digest": manifest}]}
            self.assertEqual([p.name for p in shim.oci_layers(blobs, index)], [layer.split(":")[1]])
            with self.assertRaises(shim.ShimError):
                shim.oci_layers(blobs, {"manifests": []})


class TuningTests(unittest.TestCase):
    def test_defaults_are_added(self) -> None:
        added = shim.tuning_of(["--installroot=/x", "install", "tmux"])
        self.assertEqual(
            sorted(a.partition("=")[2].partition("=")[0] for a in added),
            ["fastestmirror", "max_parallel_downloads", "retries", "timeout"],
        )

    def test_caller_options_win(self) -> None:
        added = shim.tuning_of(["--setopt=timeout=5", "--setopt", "retries=1", "install"])
        self.assertFalse([a for a in added if "timeout" in a or "retries" in a])
        self.assertTrue([a for a in added if "max_parallel_downloads" in a])


if __name__ == "__main__":
    unittest.main()
