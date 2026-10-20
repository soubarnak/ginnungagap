# SPDX-License-Identifier: Apache-2.0
"""Release pipeline checks: tag-driven workflow, version agreement and signed-repository docs.

Covers AS-003 (ginn ships as signed Void packages). The workflow is parsed, never run. The
tag check of tools/release.sh runs for real: it reads two files and calls no xbps command, so it
is safe here (FR-004 is a runtime rule; the xbps parts of release.sh are not run).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
RELEASE_YML = REPO / ".github" / "workflows" / "release.yml"
CI_YAML = REPO / ".github" / "workflows" / "ci.yaml"
RELEASE_SH = REPO / "tools" / "release.sh"
TEMPLATE = REPO / "packaging" / "void" / "srcpkgs" / "ginn" / "template"
FINGERPRINT = "fa:d1:00:32:ce:b1:b9:fd:73:05:05:76:75:54:37:25"
REPO_URL = "https://github.com/soubarnak/ginnungagap/releases/latest/download"
NOARCH_ARCHS = "x86_64 x86_64-musl i686 aarch64 aarch64-musl armv7l armv7l-musl armv6l armv6l-musl"


def _workflow(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the bare key "on" as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _project_version() -> str:
    with (REPO / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _template_version() -> str:
    match = re.search(r"^version=(.+)$", TEMPLATE.read_text(encoding="utf-8"), re.MULTILINE)
    assert match
    return match.group(1).strip('"')


def _verify_tag(tag: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    return subprocess.run(  # noqa: S603 - argv list, no shell
        [bash, str(RELEASE_SH), "verify-tag", tag],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.fr("AS-003")
def test_pyproject_and_template_versions_agree() -> None:
    assert _project_version() == _template_version()


@pytest.mark.fr("AS-003")
def test_release_workflow_runs_only_on_ginn_version_tags() -> None:
    triggers = _triggers(_workflow(RELEASE_YML))
    assert triggers == {"push": {"tags": ["ginn-v*"]}}


@pytest.mark.fr("AS-003")
def test_release_workflow_jobs_are_ordered_verify_test_release_bump() -> None:
    jobs = _workflow(RELEASE_YML)["jobs"]
    assert list(jobs) == ["verify", "test", "release", "bump"]
    assert jobs["test"]["needs"] == "verify"
    assert set(jobs["release"]["needs"]) == {"verify", "test"}
    assert jobs["bump"]["needs"] == "release"
    verify_steps = "\n".join(str(step.get("run", "")) for step in jobs["verify"]["steps"])
    assert 'release.sh verify-tag "$GITHUB_REF_NAME"' in verify_steps


@pytest.mark.fr("AS-003")
def test_release_workflow_builds_with_xbps_build_and_signs_with_the_secret() -> None:
    text = RELEASE_YML.read_text(encoding="utf-8")
    assert "tools/xbps-build.sh" in text
    assert "ghcr.io/void-linux/void-glibc" in text
    assert "secrets.XBPS_PRIVKEY" in text
    assert "GINN_SIGNING_KEY=/key.pem bash tools/release.sh repo" in text
    assert "umask 077" in text, "the key file must be created with mode 600"
    release = _workflow(RELEASE_YML)["jobs"]["release"]
    steps = release["steps"]
    cleanup = next(step for step in steps if step.get("name", "") == "Remove the signing key")
    assert cleanup["if"] == "always()" and "rm -f" in cleanup["run"]
    assert "env" not in release, "the runner context is not allowed in job-level env"
    # The build container never sees the key; only the second, short container mounts it.
    build = next(step for step in steps if step.get("name", "").startswith("Build the package"))
    assert "key.pem" not in build["run"] and "--privileged" in build["run"]
    sign = next(step for step in steps if step.get("name", "").startswith("Index and sign"))
    assert "--privileged" not in sign["run"] and "/key.pem:ro" in sign["run"]
    assert release["permissions"] == {"contents": "write"}


@pytest.mark.fr("AS-003")
def test_release_workflow_bump_commits_straight_to_void_without_a_pull_request() -> None:
    bump = _workflow(RELEASE_YML)["jobs"]["bump"]
    assert bump["steps"][0]["with"]["ref"] == "void"
    runs = "\n".join(str(step.get("run", "")) for step in bump["steps"])
    assert 'release.sh bump "$GITHUB_REF_NAME"' in runs
    assert "git push origin HEAD:void" in runs
    assert "git pull --rebase origin void" in runs
    assert "gh release upload" in runs and "--clobber" in runs
    assert "pull-requests" not in str(bump.get("permissions", {}))
    assert "gh pr" not in runs


@pytest.mark.fr("AS-003")
def test_release_workflow_pins_actions_to_commit_shas() -> None:
    text = RELEASE_YML.read_text(encoding="utf-8")
    uses = re.findall(r"uses: (\S+)", text)
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref
    ci_refs = set(re.findall(r"uses: (\S+)", CI_YAML.read_text(encoding="utf-8")))
    assert set(uses) <= ci_refs, "release.yml reuses the SHAs that ci.yaml pins"


@pytest.mark.fr("AS-003")
def test_ci_has_no_tag_trigger_and_no_release_upload() -> None:
    ci = _workflow(CI_YAML)
    assert "tags" not in _triggers(ci)["push"]
    text = CI_YAML.read_text(encoding="utf-8")
    assert "refs/tags" not in text
    assert "release.sh repo" not in text
    assert "gh release" not in text


@pytest.mark.fr("AS-003")
def test_verify_tag_accepts_the_ginn_tag_of_the_source_version() -> None:
    result = _verify_tag(f"ginn-v{_project_version()}")
    assert result.returncode == 0, result.stderr


@pytest.mark.fr("AS-003")
@pytest.mark.parametrize("tag", ["v1.0.0", "ginn-v9.9.9", "ginn-1.0.0", "1.0.0", "ginn-v1.0.0.1"])
def test_verify_tag_rejects_a_tag_that_is_not_ginn_v_plus_the_source_version(tag: str) -> None:
    result = _verify_tag(tag)
    assert result.returncode == 1
    assert "differs" in result.stderr


@pytest.mark.fr("AS-003")
def test_release_script_uses_ginn_tags_and_the_xbps_arch_variable() -> None:
    text = RELEASE_SH.read_text(encoding="utf-8")
    assert "refs/tags/ginn-v$tv" in text
    assert "refs/tags/v$tv" not in text
    assert "XBPS_TARGET_ARCH" not in text
    assert "XBPS_ARCH=" in text
    assert f"noarch_archs=({NOARCH_ARCHS})" in text
    assert '--signedby "$signer" -s "$out"' in text
    assert '--signedby "$signer" -S "$pkg"' in text
    assert "signer='Bongbetic <soubarna@live.in>'" in text


@pytest.mark.fr("AS-003")
def test_readme_and_packaging_doc_describe_the_signed_repository() -> None:
    for name in ("README.md", "docs/packaging.md"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert f"echo 'repository={REPO_URL}' | sudo tee /etc/xbps.d/20-ginn.conf" in text, name
        assert "sudo xbps-install -S ginn" in text, name
        assert "sudo xbps-install -Su" in text, name
        assert FINGERPRINT in text, name
        assert '-R "$HOME/ginn' not in text, (
            f"{name} still tells users to install from a local directory"
        )
    packaging = (REPO / "docs" / "packaging.md").read_text(encoding="utf-8")
    assert "XBPS_PRIVKEY" in packaging
    assert "gh secret set XBPS_PRIVKEY" in packaging
    assert "Never build or upload release assets by hand" in packaging


@pytest.mark.fr("AS-003")
def test_release_notes_install_section_names_the_signed_repository() -> None:
    text = (REPO / "docs" / "release-notes" / "v1.0.0.md").read_text(encoding="utf-8")
    install = text.split("## Install\n", 1)[1].split("\n## ", 1)[0]
    assert REPO_URL in install
    assert FINGERPRINT in install
    assert "unsigned" not in install.lower()
