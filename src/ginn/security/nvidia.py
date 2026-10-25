# SPDX-License-Identifier: Apache-2.0
"""NVIDIA driver injection for GPU realms (FR-069, ADR-012, R-005).

On a glibc host with the NVIDIA proprietary driver, a realm with GPU permission gets the host's
matching userspace libraries, its device nodes and `nvidia-smi`, all read-only. The grant is a
CDI spec that ginn writes under its data directory. The realm receives it through
`--cdi-spec-dir <dir> --device nvidia.com/gpu=all`. The spec sets `LD_LIBRARY_PATH` in
`containerEdits.env`, so the loader finds the libraries with no step inside the realm (ADR-012).
The ld.so drop-in is bind-mounted too, so `ldconfig` can be used later if it is ever needed.

Rules, all from ADR-012 and S-06:
- The spec is one file, `cdi/nvidia.json`, because two specs for one device name conflict.
  Regeneration overwrites it. The caller compares `digest` with the file it has, and writes only
  when they differ.
- A library whose version in its name differs from the kernel driver is stale (R-005). Its spec
  would fail at the mount step after an upgrade, so `plan` refuses with GINN-E-NVIDIA-STALE.
- On musl the grant is unsupported. The plan says so as information, with no spec and no argv
  (FR-069 AC-2). The doctor check reports the same fact (T3.6).
- With no kernel driver, the status is absent, and the grant is not offered.
- `ldconfig` as container root is the fallback in `fallback`. ADR-012 says it writes
  `/etc/ld.so.cache` into the realm, which needs a clarification first (CL-10), so nothing selects
  it. It is data only.

`plan` reads the host through a `HostProbe`, so tests describe a host and no real driver is needed.
`OsProbe` is the real one. `plan` writes nothing. The caller writes `spec_text` and `ld_text`.
"""

from __future__ import annotations

import enum
import glob
import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from typing import Final, Protocol

from ginn.errors import make

LIB_DIR: Final = "/usr/lib/ginn-nvidia"
"""The realm directory of the libraries. The spec sets `LD_LIBRARY_PATH` to this (ADR-012)."""
LD_DROPIN_REALM: Final = "/etc/ld.so.conf.d/ginn-nvidia.conf"
SMI_REALM: Final = "/usr/bin/nvidia-smi"
SMI_NAME: Final = "nvidia-smi"
CDI_KIND: Final = "nvidia.com/gpu"
CDI_VERSION: Final = "0.6.0"
DEVICE: Final = "nvidia.com/gpu=all"
PROC_VERSION: Final = "/proc/driver/nvidia/version"
CONTROL_NODE: Final = "/dev/nvidiactl"
"""The control node. Without it the kernel driver is not loaded (S-06 section 4)."""
MUSL_LOADERS: Final[tuple[str, ...]] = ("/lib/ld-musl-*.so.1", "/usr/lib/ld-musl-*.so.1")
NODE_GLOBS: Final[tuple[str, ...]] = ("/dev/nvidia*", "/dev/nvidia-caps/*")
LIB_GLOBS: Final[tuple[str, ...]] = (
    "/usr/lib/libnvidia*.so*",
    "/usr/lib/libcuda*.so*",
    "/usr/lib/libnvcuvid*",
    "/usr/lib/libnvoptix*",
    "/usr/lib/libGLX_nvidia*",
    "/usr/lib/libEGL_nvidia*",
    "/usr/lib/libGLESv*_nvidia*",
    "/usr/lib/vdpau/libvdpau_nvidia*",
    "/usr/lib/libnvidia-egl-wayland*",
    "/usr/lib/libnvidia-egl-gbm*",
)
JSON_GLOBS: Final[tuple[str, ...]] = (
    "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
    "/usr/share/vulkan/icd.d/nvidia_icd.json",
    "/usr/share/vulkan/implicit_layer.d/nvidia_layers.json",
    "/usr/share/egl/egl_external_platform.d/*nvidia*",
    "/etc/vulkan/icd.d/*nvidia*",
)
"""Vendor files keep their host path in the realm, as the S-06 generator does."""
RO_OPTIONS: Final[tuple[str, ...]] = ("ro", "nosuid", "nodev", "bind")
DRIVER_VER: Final = re.compile(r"(\d{3}\.\d+(?:\.\d+)?)")
"""The driver version on the kernel module line. Real versions have two parts (550.67, 390.157)
or three (595.104.02), and both forms match."""
LIB_VER: Final = re.compile(r"\.so\.(\d{3}\.\d+(?:\.\d+)?)")
"""The version that a library file name carries, after `.so.`. Both forms match."""


class HostProbe(Protocol):
    """The host reads that the plan needs. Tests give a fake host. `OsProbe` reads the real one."""

    def read_text(self, path: str) -> str | None: ...

    def glob(self, pattern: str) -> list[str]: ...

    def realpath(self, path: str) -> str: ...

    def char_device(self, path: str) -> tuple[int, int] | None: ...

    def which(self, name: str) -> str | None: ...


class OsProbe:
    """The real host. Every read is a plain stat, read, or glob, and nothing is written."""

    def read_text(self, path: str) -> str | None:
        try:
            with open(path, encoding="ascii", errors="replace") as handle:
                return handle.read()
        except OSError:
            return None

    def glob(self, pattern: str) -> list[str]:
        return sorted(glob.glob(pattern))

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def char_device(self, path: str) -> tuple[int, int] | None:
        try:
            info = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISCHR(info.st_mode):
            return None
        return os.major(info.st_rdev), os.minor(info.st_rdev)

    def which(self, name: str) -> str | None:
        return shutil.which(name)


class Status(enum.Enum):
    """What the host offers: a usable driver, no driver, or musl (informational only)."""

    READY = "ready"
    ABSENT = "absent"
    MUSL = "musl"


@dataclass(frozen=True)
class Node:
    """A character device in the spec: `path`, with its major and minor numbers."""

    path: str
    major: int
    minor: int


@dataclass(frozen=True)
class Bind:
    """A read-only bind: `host` is a resolved path, and `realm` is where it appears."""

    host: str
    realm: str


@dataclass(frozen=True)
class ExecStep:
    """A command to run in the realm, as a user and an argv. Data only; no step runs it here."""

    user: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class NvidiaPlan:
    """The NVIDIA grant for one host. Pure data: the caller writes the files and builds argv.

    `global_argv` belongs before the podman subcommand, and `create_argv` belongs to `create`.
    `env` is what the spec sets, so a caller can check it. With MUSL or ABSENT, every field
    holding a path or argv is empty. `spec_text` is the file body, and `digest` is its SHA-256.
    """

    status: Status
    reason: str
    driver: str = ""
    spec_path: str = ""
    spec_text: str = ""
    ld_path: str = ""
    ld_text: str = ""
    global_argv: tuple[str, ...] = ()
    create_argv: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    fallback: ExecStep = ExecStep(user="0:0", argv=("ldconfig",))
    nodes: tuple[Node, ...] = ()
    binds: tuple[Bind, ...] = ()

    @property
    def digest(self) -> str:
        """The SHA-256 of `spec_text`, hex. A caller rewrites the spec only when this changes."""
        return hashlib.sha256(self.spec_text.encode("utf-8")).hexdigest()


def _driver(probe: HostProbe) -> str | None:
    """The kernel driver version, from the first line of the proc file, or None when absent."""
    text = probe.read_text(PROC_VERSION)
    first = (text or "").splitlines()[:1]
    found = DRIVER_VER.search(first[0]) if first else None
    return found.group(1) if found else None


def _nodes(probe: HostProbe) -> tuple[Node, ...]:
    """The character devices of the driver, in path order. Directories are skipped."""
    nodes = []
    seen: set[str] = set()
    for pattern in NODE_GLOBS:
        for path in probe.glob(pattern):
            if path in seen:
                continue
            seen.add(path)
            numbers = probe.char_device(path)
            if numbers is not None:
                nodes.append(Node(path, numbers[0], numbers[1]))
    return tuple(nodes)


def _spec(nodes: tuple[Node, ...], binds: tuple[Bind, ...]) -> str:
    """The CDI spec body. Its key order is fixed, so the same host gives the same bytes."""
    edits = {
        "deviceNodes": [
            {"path": n.path, "type": "c", "major": n.major, "minor": n.minor, "permissions": "rwm"}
            for n in nodes
        ],
        "mounts": [
            {"hostPath": b.host, "containerPath": b.realm, "options": list(RO_OPTIONS)}
            for b in binds
        ],
        "env": [f"LD_LIBRARY_PATH={LIB_DIR}"],
    }
    spec = {
        "cdiVersion": CDI_VERSION,
        "kind": CDI_KIND,
        "devices": [{"name": "all", "containerEdits": edits}],
    }
    return json.dumps(spec, indent=2) + "\n"


def _stale(path: str, driver: str) -> bool:
    """True when the version in the library's name is not the kernel driver's (R-005)."""
    found = LIB_VER.search(os.path.basename(path))
    return found is not None and found.group(1) != driver


def plan(probe: HostProbe, data_dir: str) -> NvidiaPlan:
    """The NVIDIA grant for this host, with the spec and drop-in texts under `data_dir`.

    `data_dir` is the ginn data directory, an Owned Path. It must be absolute. Raises
    GINN-E-NVIDIA-STALE when a library version differs from the kernel driver (R-005).
    """
    if not os.path.isabs(data_dir):
        raise ValueError("the ginn data directory is an absolute path")
    if any(probe.glob(pattern) for pattern in MUSL_LOADERS):
        return NvidiaPlan(Status.MUSL, "NVIDIA unsupported on musl")
    driver = _driver(probe)
    if driver is None or probe.char_device(CONTROL_NODE) is None:
        return NvidiaPlan(Status.ABSENT, "no NVIDIA kernel driver is loaded")
    binds: list[Bind] = []
    libs: list[str] = []
    seen: set[str] = set()
    for pattern in LIB_GLOBS:
        for name in probe.glob(pattern):
            target = f"{LIB_DIR}/{os.path.basename(name)}"
            if target in seen:
                continue
            seen.add(target)
            resolved = probe.realpath(name)
            binds.append(Bind(resolved, target))
            libs.append(resolved)
    for pattern in JSON_GLOBS:
        for name in probe.glob(pattern):
            if name not in seen:
                seen.add(name)
                binds.append(Bind(probe.realpath(name), name))
    smi = probe.which(SMI_NAME)
    if smi is not None:
        binds.append(Bind(probe.realpath(smi), SMI_REALM))
    stale = sorted({os.path.basename(path) for path in libs if _stale(path, driver)})
    if stale:
        raise make(
            "GINN-E-NVIDIA-STALE",
            detail=f"driver {driver}, library names {', '.join(stale)}",
        )
    ld_path = f"{data_dir}/ld/ginn-nvidia.conf"
    ld_text = f"{LIB_DIR}\n"
    binds.append(Bind(ld_path, LD_DROPIN_REALM))
    cdi_dir = f"{data_dir}/cdi"
    nodes = _nodes(probe)
    spec_text = _spec(nodes, tuple(binds))
    return NvidiaPlan(
        status=Status.READY,
        reason="NVIDIA libraries and device nodes from the host driver",
        driver=driver,
        spec_path=f"{cdi_dir}/nvidia.json",
        spec_text=spec_text,
        ld_path=ld_path,
        ld_text=ld_text,
        global_argv=("--cdi-spec-dir", cdi_dir),
        create_argv=("--device", DEVICE),
        env={"LD_LIBRARY_PATH": LIB_DIR},
        nodes=nodes,
        binds=tuple(binds),
    )


def podman_argv(result: NvidiaPlan) -> tuple[str, ...]:
    """Both argv parts in podman order: the global flags first, then the create flags.

    Empty unless the plan is READY. The caller places the two parts with its own podman argv.
    """
    if result.status is not Status.READY:
        return ()
    return (*result.global_argv, *result.create_argv)
