# SPDX-License-Identifier: Apache-2.0
"""NVIDIA injection (FR-069, ADR-012, R-005): the CDI spec, the drop-in, the argv, the stale refusal
and the musl and absent cases. Every case runs on a fake host, so no driver is read from the
machine running the tests. `OsProbe` gets one check on /dev/null, which is a character device on
every Linux host.

The first test is the one ADR-012 asks for: the generated spec carries the LD_LIBRARY_PATH env
field, as containerEdits.env, before any other NVIDIA test relies on it.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from ginn.errors import GinnError
from ginn.security import nvidia

DATA = "/home/alice/.local/share/ginn"
DRIVER = "595.104.02"
PROC_PATH = nvidia.PROC_VERSION
PROC = f"NVRM version: NVIDIA UNIX x86_64 Kernel Module  {DRIVER}  Mon Sep  1 00:00:00 UTC 2026\n"
LIB_ML = "/usr/lib/libnvidia-ml.so.1"
LIB_ML_REAL = f"/usr/lib/libnvidia-ml.so.{DRIVER}"
LIB_CUDA = "/usr/lib/libcuda.so.1"
LIB_CUDA_REAL = f"/usr/lib/libcuda.so.{DRIVER}"
ICD = "/usr/share/vulkan/icd.d/nvidia_icd.json"
SMI_HOST = "/usr/bin/nvidia-smi"
LD_ENV = "LD_LIBRARY_PATH=/usr/lib/ginn-nvidia"

pytestmark = [pytest.mark.fr("FR-069")]


def _match(pattern: str, path: str) -> bool:
    """glob semantics: `*` does not cross a `/`, so the segment counts must agree."""
    return fnmatch.fnmatchcase(path, pattern) and path.count("/") == pattern.count("/")


@dataclass
class FakeHost:
    """A host described by its files, its links, its character devices and its tools."""

    files: dict[str, str] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)
    chars: dict[str, tuple[int, int]] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    tools: dict[str, str] = field(default_factory=dict)

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)

    def glob(self, pattern: str) -> list[str]:
        names = {*self.files, *self.links, *self.chars, *self.dirs}
        return sorted(name for name in names if _match(pattern, name))

    def realpath(self, path: str) -> str:
        return self.links.get(path, path)

    def char_device(self, path: str) -> tuple[int, int] | None:
        return self.chars.get(path)

    def which(self, name: str) -> str | None:
        return self.tools.get(name)


def _host_for(version: str, **fields: Any) -> FakeHost:
    """A glibc host with the kernel driver `version`: its module line, libraries, nodes and tools.

    Real versions have two parts (550.67, 390.157) or three (595.104.02), so both are built here.
    """
    proc = f"NVRM version: NVIDIA UNIX x86_64 Kernel Module  {version}  Thu Mar 19 00:00:00 2026\n"
    libml = f"/usr/lib/libnvidia-ml.so.{version}"
    libcuda = f"/usr/lib/libcuda.so.{version}"
    glcore = f"/usr/lib/libnvidia-glcore.so.{version}"
    values: dict[str, Any] = {
        "files": {PROC_PATH: proc, ICD: "{}\n", libml: "", libcuda: "", glcore: ""},
        "links": {LIB_ML: libml, LIB_CUDA: libcuda},
        "chars": {
            "/dev/nvidiactl": (195, 255),
            "/dev/nvidia0": (195, 0),
            "/dev/nvidia-uvm": (510, 0),
            "/dev/nvidia-caps/nvidia-cap1": (240, 1),
        },
        "dirs": {"/dev/nvidia-caps"},
        "tools": {"nvidia-smi": SMI_HOST},
    }
    values.update(fields)
    return FakeHost(**values)


def _driver_host(**fields: Any) -> FakeHost:
    """A glibc host with the driver 595.104.02."""
    return _host_for(DRIVER, **fields)


def _spec(result: nvidia.NvidiaPlan) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(result.spec_text)
    return spec


def _edits(result: nvidia.NvidiaPlan) -> dict[str, Any]:
    edits: dict[str, Any] = _spec(result)["devices"][0]["containerEdits"]
    return edits


def test_the_spec_sets_ld_library_path_in_container_edits_env() -> None:
    """ADR-012: the CDI env field carries the loader path, so no step runs inside the realm."""
    result = nvidia.plan(_driver_host(), DATA)
    assert LD_ENV in _edits(result)["env"]
    assert result.env == {"LD_LIBRARY_PATH": "/usr/lib/ginn-nvidia"}


def test_the_spec_declares_cdi_version_0_6_0_and_the_nvidia_gpu_kind() -> None:
    spec = _spec(nvidia.plan(_driver_host(), DATA))
    assert spec["cdiVersion"] == "0.6.0"
    assert spec["kind"] == "nvidia.com/gpu"
    assert [device["name"] for device in spec["devices"]] == ["all"]


def test_the_spec_is_one_file_in_the_cdi_directory() -> None:
    result = nvidia.plan(_driver_host(), DATA)
    assert result.spec_path == f"{DATA}/cdi/nvidia.json"
    assert result.global_argv == ("--cdi-spec-dir", f"{DATA}/cdi")


def test_the_argv_is_the_global_cdi_flag_then_the_create_device_flag() -> None:
    result = nvidia.plan(_driver_host(), DATA)
    assert nvidia.podman_argv(result) == (
        "--cdi-spec-dir",
        f"{DATA}/cdi",
        "--device",
        "nvidia.com/gpu=all",
    )
    assert result.create_argv == ("--device", "nvidia.com/gpu=all")


def test_libraries_are_bound_read_only_under_the_lib_dir_by_their_names() -> None:
    edits = _edits(nvidia.plan(_driver_host(), DATA))
    mounts = {m["containerPath"]: m for m in edits["mounts"]}
    ml = mounts["/usr/lib/ginn-nvidia/libnvidia-ml.so.1"]
    assert ml["hostPath"] == LIB_ML_REAL
    assert ml["options"] == ["ro", "nosuid", "nodev", "bind"]
    assert mounts["/usr/lib/ginn-nvidia/libcuda.so.1"]["hostPath"] == LIB_CUDA_REAL


def test_vendor_json_and_nvidia_smi_keep_their_host_paths_and_the_drop_in_is_bound() -> None:
    result = nvidia.plan(_driver_host(), DATA)
    mounts = {m["containerPath"]: m["hostPath"] for m in _edits(result)["mounts"]}
    assert mounts[ICD] == ICD
    assert mounts["/usr/bin/nvidia-smi"] == SMI_HOST
    assert mounts[nvidia.LD_DROPIN_REALM] == f"{DATA}/ld/ginn-nvidia.conf"
    assert result.ld_text == "/usr/lib/ginn-nvidia\n"
    assert result.ld_path == f"{DATA}/ld/ginn-nvidia.conf"


def test_device_nodes_are_the_driver_character_devices_and_not_directories() -> None:
    edits = _edits(nvidia.plan(_driver_host(), DATA))
    paths = [node["path"] for node in edits["deviceNodes"]]
    assert "/dev/nvidia-caps" not in paths
    assert "/dev/nvidia0" in paths
    uvm = next(node for node in edits["deviceNodes"] if node["path"] == "/dev/nvidia-uvm")
    assert (uvm["type"], uvm["major"], uvm["minor"], uvm["permissions"]) == ("c", 510, 0, "rwm")


def test_the_spec_is_the_same_for_the_same_host_and_changes_with_the_driver() -> None:
    """A driver update changes the digest, so the caller rewrites the spec (R-005)."""
    first = nvidia.plan(_driver_host(), DATA)
    assert first.digest == nvidia.plan(_driver_host(), DATA).digest
    newer = "595.104.03"
    upgraded = _driver_host(
        files={
            PROC_PATH: PROC.replace(DRIVER, newer),
            ICD: "{}\n",
            f"/usr/lib/libnvidia-ml.so.{newer}": "",
            f"/usr/lib/libcuda.so.{newer}": "",
        },
        links={
            LIB_ML: f"/usr/lib/libnvidia-ml.so.{newer}",
            LIB_CUDA: f"/usr/lib/libcuda.so.{newer}",
        },
    )
    result = nvidia.plan(upgraded, DATA)
    assert result.status is nvidia.Status.READY
    assert result.driver == newer
    assert result.digest != first.digest


def test_a_library_from_another_driver_version_is_stale_and_refuses_the_start() -> None:
    """R-005: the library names carry the version. A mismatch refuses the start, and names it."""
    stale_real = "/usr/lib/libcuda.so.595.104.01"
    host = _driver_host(links={LIB_ML: LIB_ML_REAL, LIB_CUDA: stale_real})
    with pytest.raises(GinnError) as caught:
        nvidia.plan(host, DATA)
    assert caught.value.code == "GINN-E-NVIDIA-STALE"
    assert "libcuda.so.595.104.01" in str(caught.value.details["detail"])
    assert caught.value.exit_code == 1


def test_musl_is_informational_with_no_spec_and_no_argv() -> None:
    """FR-069 AC-2: NVIDIA is unsupported on musl. This is information, not a failure."""
    host = _driver_host()
    host.files["/lib/ld-musl-x86_64.so.1"] = ""
    result = nvidia.plan(host, DATA)
    assert result.status is nvidia.Status.MUSL
    assert result.reason == "NVIDIA unsupported on musl"
    assert result.spec_text == ""
    assert result.binds == ()
    assert nvidia.podman_argv(result) == ()


def test_no_kernel_driver_is_absent_and_offers_nothing() -> None:
    host = _driver_host(chars={})
    result = nvidia.plan(host, DATA)
    assert result.status is nvidia.Status.ABSENT
    assert result.spec_text == ""
    assert nvidia.podman_argv(result) == ()


def test_an_unreadable_driver_version_is_absent() -> None:
    result = nvidia.plan(_driver_host(files={ICD: "{}\n"}), DATA)
    assert result.status is nvidia.Status.ABSENT


def test_the_fallback_is_ldconfig_as_container_root_and_is_not_the_selected_form() -> None:
    """ADR-012: ldconfig as root writes the realm's ld cache. It is data here, never selected."""
    result = nvidia.plan(_driver_host(), DATA)
    assert result.fallback == nvidia.ExecStep(user="0:0", argv=("ldconfig",))
    assert result.env == {"LD_LIBRARY_PATH": "/usr/lib/ginn-nvidia"}


def test_a_relative_data_directory_raises() -> None:
    with pytest.raises(ValueError, match="absolute"):
        nvidia.plan(_driver_host(), "relative/data")


def test_the_real_probe_reads_a_character_device_as_a_device() -> None:
    assert nvidia.OsProbe().char_device("/dev/null") is not None
    assert nvidia.OsProbe().char_device("/") is None


@pytest.mark.parametrize("version", [DRIVER, "550.67", "390.157"])
def test_the_driver_version_is_read_in_its_two_or_three_part_form(version: str) -> None:
    """Real drivers have two-part versions (550.67) as well as three-part ones (595.104.02)."""
    result = nvidia.plan(_host_for(version), DATA)
    assert result.status is nvidia.Status.READY
    assert result.driver == version
    mounts = {m["containerPath"] for m in _edits(result)["mounts"]}
    assert f"/usr/lib/ginn-nvidia/libnvidia-glcore.so.{version}" in mounts


def test_a_two_part_library_from_another_driver_is_stale_and_refuses_the_start() -> None:
    host = _host_for("550.67")
    host.files["/usr/lib/libnvidia-glcore.so.390.157"] = ""
    with pytest.raises(GinnError) as caught:
        nvidia.plan(host, DATA)
    assert caught.value.code == "GINN-E-NVIDIA-STALE"
    assert "libnvidia-glcore.so.390.157" in str(caught.value.details["detail"])


def test_a_two_part_driver_with_matching_libraries_is_not_stale() -> None:
    result = nvidia.plan(_host_for("390.157"), DATA)
    assert result.status is nvidia.Status.READY
    assert result.driver == "390.157"
    assert result.env == {"LD_LIBRARY_PATH": "/usr/lib/ginn-nvidia"}
