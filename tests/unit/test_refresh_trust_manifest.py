# SPDX-License-Identifier: Apache-2.0
"""tools/refresh-trust-manifest.py with skopeo stubbed: no network and no real skopeo (DR-040)."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ginn.trust import load, load_file

REPO = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO / "tools" / "refresh-trust-manifest.py"
SRC_GINN = REPO / "src" / "ginn"


def _load_tool() -> Any:
    """Load the hyphenated tool by path, as conftest.py loads tools/traceability.py."""
    spec = importlib.util.spec_from_file_location("refresh_trust_manifest", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()

ARCH_REF = "quay.io/archlinux/archlinux:base"
DEBIAN_REF = "docker.io/library/debian:stable-slim"
ARCH_RAW = json.dumps(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": "sha256:"},
        "layers": [],
    }
).encode()
ARCH_DIGEST = "sha256:" + hashlib.sha256(ARCH_RAW).hexdigest()
DEBIAN_OLD = "sha256:" + "e" * 64
DEBIAN_NEW = "sha256:" + "f" * 64
INDEX_RAW = json.dumps(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "1" * 64,
                "platform": {"architecture": "arm64", "os": "linux"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": DEBIAN_NEW,
                "platform": {"architecture": "amd64", "os": "linux"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "2" * 64,
                "platform": {"architecture": "unknown", "os": "unknown"},
            },
        ],
    }
).encode()


def _manifest_text(arch: str, debian: str) -> str:
    return f"""\
# Comment lines stay exactly as they are.
schema = 1

[distro.arch]
image = "quay.io/archlinux/archlinux:base"
digest_kind = "platform"
digest = "{arch}"
compressed_size_bytes = 139248529
estimated_installed_bytes = 410379776
measured_installed_bytes = 410385305
signature = "sigstore-unverified"
verified_on = "2026-10-12"

[distro.debian]
image = "docker.io/library/debian:stable-slim"
digest_kind = "platform"
digest = "{debian}"  # trailing comment stays too
compressed_size_bytes = 29843347
estimated_installed_bytes = 81254400
measured_installed_bytes = 81256893
signature = "none"
verified_on = "2026-10-12"
"""


def _write_manifest(directory: Path, *, arch: str, debian: str) -> Path:
    path = directory / "trust.toml"
    path.write_text(_manifest_text(arch, debian), encoding="utf-8")
    return path


def _answers(
    *,
    arch_raw: bytes = ARCH_RAW,
    arch_info: dict[str, object] | None = None,
    debian_raw: bytes = INDEX_RAW,
) -> dict[tuple[str, ...], bytes]:
    if arch_info is None:
        arch_info = {"Digest": ARCH_DIGEST, "Architecture": "amd64", "Os": "linux"}
    return {
        ("inspect", "--raw", f"docker://{ARCH_REF}"): arch_raw,
        ("inspect", f"docker://{ARCH_REF}"): json.dumps(arch_info).encode(),
        ("inspect", "--raw", f"docker://{DEBIAN_REF}"): debian_raw,
    }


class FakeSkopeo:
    """Answers each argv list from a table and records the calls. Nothing is executed."""

    def __init__(self, answers: dict[tuple[str, ...], bytes]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> bytes:
        call = tuple(argv)
        self.calls.append(call)
        return self.answers[call]


@pytest.mark.fr("DR-040")
def test_unchanged_digests_pass_check_and_write_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_NEW)
    before = path.read_bytes()
    code = tool.main(["--check", "--manifest", str(path)], run=FakeSkopeo(_answers()))
    assert code == tool.EXIT_OK
    assert path.read_bytes() == before
    assert capsys.readouterr().out.count("unchanged") == 2


@pytest.mark.fr("DR-040")
def test_moved_digest_fails_check_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_OLD)
    before = path.read_bytes()
    code = tool.main(["--check", "--manifest", str(path)], run=FakeSkopeo(_answers()))
    assert code == tool.EXIT_MOVED
    assert path.read_bytes() == before
    out = capsys.readouterr()
    assert "debian" in out.out and "MOVED" in out.out
    assert f"current {DEBIAN_NEW}" in out.out
    assert "digest moved for: debian" in out.err


@pytest.mark.fr("DR-040")
def test_default_mode_reports_a_moved_digest_without_writing(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_OLD)
    before = path.read_bytes()
    code = tool.main(["--manifest", str(path)], run=FakeSkopeo(_answers()))
    assert code == tool.EXIT_OK
    assert path.read_bytes() == before


@pytest.mark.fr("DR-040")
def test_write_replaces_only_the_moved_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_OLD)
    before = path.read_text(encoding="utf-8")
    code = tool.main(["--write", "--manifest", str(path)], run=FakeSkopeo(_answers()))
    assert code == tool.EXIT_OK
    after = path.read_text(encoding="utf-8")
    assert after == before.replace(f'"{DEBIAN_OLD}"', f'"{DEBIAN_NEW}"')
    assert f'digest = "{DEBIAN_NEW}"  # trailing comment stays too' in after
    reloaded = load_file(path)
    assert reloaded.entry("debian").digest == DEBIAN_NEW
    assert reloaded.entry("arch").digest == ARCH_DIGEST
    out = capsys.readouterr().out
    assert "wrote new digests for: debian" in out
    assert "not refreshed:" in out and "S-10 section 6" in out


@pytest.mark.fr("DR-040")
def test_write_with_nothing_moved_leaves_the_file_alone(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_NEW)
    before = path.read_bytes()
    code = tool.main(["--write", "--manifest", str(path)], run=FakeSkopeo(_answers()))
    assert code == tool.EXIT_OK
    assert path.read_bytes() == before


@pytest.mark.fr("DR-040")
def test_index_without_an_amd64_manifest_is_refused(tmp_path: Path) -> None:
    arm_only = json.dumps(
        {
            "manifests": [
                {
                    "digest": "sha256:" + "1" * 64,
                    "platform": {"os": "linux", "architecture": "arm64"},
                }
            ]
        }
    ).encode()
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_OLD)
    before = path.read_bytes()
    code = tool.main(
        ["--write", "--manifest", str(path)], run=FakeSkopeo(_answers(debian_raw=arm_only))
    )
    assert code == tool.EXIT_ERROR
    assert path.read_bytes() == before


@pytest.mark.fr("DR-040")
def test_single_manifest_for_another_architecture_is_refused(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_NEW)
    arm = {"Digest": ARCH_DIGEST, "Architecture": "arm64", "Os": "linux"}
    code = tool.main(["--check", "--manifest", str(path)], run=FakeSkopeo(_answers(arch_info=arm)))
    assert code == tool.EXIT_ERROR


@pytest.mark.fr("DR-040")
def test_single_manifest_digest_that_disagrees_with_skopeo_is_refused(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_NEW)
    other = {"Digest": "sha256:" + "9" * 64, "Architecture": "amd64", "Os": "linux"}
    code = tool.main(
        ["--check", "--manifest", str(path)], run=FakeSkopeo(_answers(arch_info=other))
    )
    assert code == tool.EXIT_ERROR


@pytest.mark.fr("DR-040")
def test_invalid_manifest_is_refused_before_skopeo_runs(tmp_path: Path) -> None:
    path = tmp_path / "trust.toml"
    path.write_text("schema = 2\n", encoding="utf-8")
    fake = FakeSkopeo(_answers())
    code = tool.main(["--check", "--manifest", str(path)], run=fake)
    assert code == tool.EXIT_ERROR
    assert fake.calls == []


@pytest.mark.fr("DR-040")
def test_missing_manifest_file_exits_2(tmp_path: Path) -> None:
    code = tool.main(["--check", "--manifest", str(tmp_path / "absent.toml")], run=FakeSkopeo({}))
    assert code == tool.EXIT_ERROR


@pytest.mark.fr("DR-040")
def test_real_runner_builds_argv_lists_and_never_uses_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_NEW)
    answers = _answers()
    seen: list[tuple[object, dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append((args, dict(kwargs)))
        return subprocess.CompletedProcess(args, 0, stdout=answers[tuple(args[1:])], stderr=b"")

    monkeypatch.setattr(tool.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    code = tool.main(["--check", "--manifest", str(path)])
    assert code == tool.EXIT_OK
    assert seen, "the real runner must call skopeo"
    for args, kwargs in seen:
        assert isinstance(args, list)
        assert args[0] == "/usr/bin/skopeo"
        assert all(isinstance(part, str) for part in args)
        assert kwargs.get("shell", False) is False


@pytest.mark.fr("DR-040")
def test_real_runner_turns_a_skopeo_failure_into_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_OLD)
    before = path.read_bytes()

    def failing_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"unauthorized")

    monkeypatch.setattr(tool.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(tool.subprocess, "run", failing_run)
    code = tool.main(["--write", "--manifest", str(path)])
    assert code == tool.EXIT_ERROR
    assert path.read_bytes() == before
    assert "skopeo inspect exited 1: unauthorized" in capsys.readouterr().err


@pytest.mark.fr("DR-040")
def test_missing_skopeo_exits_2_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_manifest(tmp_path, arch=ARCH_DIGEST, debian=DEBIAN_NEW)
    monkeypatch.setattr(tool.shutil, "which", lambda name: None)
    assert tool.main(["--check", "--manifest", str(path)]) == tool.EXIT_ERROR
    assert "skopeo is not on PATH" in capsys.readouterr().err


@pytest.mark.fr("DR-040")
def test_rewrite_refuses_a_table_without_exactly_one_digest_line() -> None:
    text = '[distro.arch]\nimage = "quay.io/archlinux/archlinux:base"\n'
    with pytest.raises(tool.RefreshError, match="exactly one digest line"):
        tool.rewrite(text, {"arch": DEBIAN_NEW})


@pytest.mark.fr("DR-040")
def test_rewrite_refuses_a_distro_that_is_not_in_the_file() -> None:
    with pytest.raises(tool.RefreshError, match=r"\[distro.nosuch\] must appear exactly once"):
        tool.rewrite(_manifest_text(ARCH_DIGEST, DEBIAN_NEW), {"nosuch": DEBIAN_NEW})


@pytest.mark.fr("DR-040")
def test_default_manifest_is_the_packaged_trust_file() -> None:
    assert tool.MANIFEST == SRC_GINN / "trust" / "data" / "trust.toml"
    assert load_file(tool.MANIFEST).entries == load().entries


@pytest.mark.fr("DR-040")
def test_package_code_never_imports_the_refresh_tool() -> None:
    # The tool is dev-time only (DR-040). Docstrings may name it. No import line may load it.
    for path in SRC_GINN.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.strip()
            if code.startswith(("import ", "from ")):
                assert "refresh" not in code, f"{path}: {line}"
