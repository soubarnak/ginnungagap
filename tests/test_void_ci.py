"""The CI workflow of the Void port and what it relies on (no network, nothing is run)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "void.yaml").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "void" / "srcpkgs" / "spaces" / "template").read_text(encoding="utf-8")


class WorkflowTests(unittest.TestCase):
    def test_the_build_job_uses_void_packages_own_container_setup(self) -> None:
        for fragment in (
            "ghcr.io/void-linux/void-glibc-full",
            "options: --platform linux/amd64 --privileged",
            "- /dev:/dev",
            "XBPS_CHROOT_CMD=uchroot",
            "XBPS_BUILD_ENVIRONMENT=void-packages-ci",
            "useradd -m -G xbuilder builder",
            "./xbps-src binary-bootstrap",
        ):
            self.assertIn(fragment, WORKFLOW)

    def test_aarch64_is_cross_built_without_tests(self) -> None:
        self.assertIn("{ arch: aarch64, test: 0 }", WORKFLOW)
        self.assertIn('--arch "$ARCH"', WORKFLOW)
        self.assertIn("--check", WORKFLOW)

    def test_musl_is_not_built_and_the_template_excludes_it(self) -> None:
        self.assertNotIn("musl", WORKFLOW.replace("void-packages' own", ""))
        self.assertIn('archs="x86_64 aarch64"', TEMPLATE)
        self.assertIn("musl is not supported", TEMPLATE)

    def test_the_guest_abi_check_follows_the_target_architecture(self) -> None:
        self.assertIn("TARGET_ARCH=${XBPS_TARGET_MACHINE}", TEMPLATE)

    def test_xbps_build_refuses_tests_for_a_foreign_architecture(self) -> None:
        result = subprocess.run(
            [str(ROOT / "void" / "tools" / "xbps-build.sh"), "--arch", "aarch64", "--check"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be combined", result.stderr)


if __name__ == "__main__":
    unittest.main()
