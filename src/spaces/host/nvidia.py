"""Expose the host's NVIDIA driver userspace to spaces on Void Linux.

The proprietary driver's libraries, ICD files and tools live in the host's
/usr. ``spaces-nvidia-sync`` (this module) builds a farm of symlinks under
/var/lib/spaces/.host/nvidia/<pkgver>/ from the xbps file lists, with the layout

    lib/    64-bit vendor libraries (plus gbm/ and vdpau/)
    lib32/  32-bit vendor libraries
    share/  glvnd, EGL external platform, Vulkan and nvoptix files
    bin/    nvidia-smi and friends
    opencl/ nvidia.icd, when the host ships one

and generates /etc/spaces/config.json from the shipped base configuration
(/usr/share/spaces/config.base.json), the optional user extras
(/etc/spaces/void.json) and mounts and overlays for that farm. Only NVIDIA
vendor files are exposed; no host glibc, libstdc++ or other system library.
The launcher's overlay walker binds each farm entry over the guest's library
directory, so a farm entry that is missing or dangling is simply skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

DEFAULT_PACKAGES = (
    "nvidia-libs",
    "nvidia-libs-32bit",
    "nvidia-opencl",
    "nvidia-opencl-32bit",
    "egl-wayland2",
    "egl-wayland2-32bit",
    "nvidia",
)
VERSION_PACKAGE = "nvidia-libs"
FARM_ROOT = Path("/var/lib/spaces/.host/nvidia")
CONFIG_PATH = Path("/etc/spaces/config.json")
BASE_PATH = Path("/usr/share/spaces/config.base.json")
EXTRAS_PATH = Path("/etc/spaces/void.json")
DRIVER_VERSION_PATH = Path("/proc/driver/nvidia/version")
SIDECAR_SUFFIX = ".generated"
# Host paths the farm may point at; anything that resolves elsewhere is skipped.
TRUSTED_PREFIXES = ("/usr/", "/etc/OpenCL/")

# Guest library directories, (64-bit, 32-bit), per distribution.
LIBRARY_DESTINATIONS: Mapping[str, tuple[str, str]] = {
    "arch": ("/usr/lib", "/usr/lib32"),
    "fedora": ("/usr/lib64", "/usr/lib"),
    "ubuntu": ("/usr/lib/x86_64-linux-gnu", "/usr/lib/i386-linux-gnu"),
    "kali": ("/usr/lib/x86_64-linux-gnu", "/usr/lib/i386-linux-gnu"),
}
BINARIES = (
    "nvidia-cuda-mps-control",
    "nvidia-cuda-mps-server",
    "nvidia-debugdump",
    "nvidia-ngx-updater",
    "nvidia-pcc",
    "nvidia-smi",
)
_LIBRARY = re.compile(
    r"^(?:lib(?:nvidia-(?!gtk)[A-Za-z0-9_.-]+|cuda|nvcuvid|nvoptix|"
    r"EGL_nvidia|GLX_nvidia|GLESv1_CM_nvidia|GLESv2_nvidia|"
    r"vdpau_nvidia))\.so\.\d[\w.]*$"
)
_SHARE_DIRECTORIES = ("glvnd", "egl", "vulkan", "vulkansc", "nvidia")

logger = logging.getLogger("spaces.nvidia")

FileLister = Callable[[str], Sequence[str]]


def xbps_files(package: str) -> list[str]:
    """Return the files of an installed xbps package, or [] if absent."""

    completed = subprocess.run(
        ["xbps-query", "-f", package],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return [line.split(" -> ", 1)[0].strip() for line in completed.stdout.splitlines()]


def xbps_version(package: str = VERSION_PACKAGE) -> str | None:
    completed = subprocess.run(
        ["xbps-query", "-p", "pkgver", package],
        check=False,
        capture_output=True,
        text=True,
    )
    pkgver = completed.stdout.strip()
    if completed.returncode != 0 or not pkgver.startswith(package + "-"):
        return None
    return pkgver[len(package) + 1 :]


def classify(path: str) -> tuple[str, str] | None:
    """Map a host file to (farm directory, relative name), or None to skip."""

    parent, _separator, name = path.rpartition("/")
    if parent in ("/usr/lib", "/usr/lib32"):
        if _LIBRARY.match(name):
            return ("lib" if parent == "/usr/lib" else "lib32", name)
        return None
    if parent in ("/usr/lib/gbm", "/usr/lib32/gbm"):
        # nvidia-drm_gbm.so is a symlink to libnvidia-allocator, no version.
        if name.startswith("nvidia-drm_gbm"):
            return ("lib" if parent.startswith("/usr/lib/") else "lib32", f"gbm/{name}")
        return None
    if parent in ("/usr/lib/vdpau", "/usr/lib32/vdpau"):
        if _LIBRARY.match(name):
            return (
                "lib" if parent.startswith("/usr/lib/") else "lib32",
                f"vdpau/{name}",
            )
        return None
    if parent == "/usr/bin" and name in BINARIES:
        return ("bin", name)
    if path.startswith("/usr/share/"):
        relative = path[len("/usr/share/") :]
        top = relative.split("/", 1)[0]
        if top in _SHARE_DIRECTORIES and (
            name.endswith(".json")
            or name == "nvoptix.bin"
            or name.endswith("-rc")
        ):
            return ("share", relative)
        return None
    if path == "/etc/OpenCL/vendors/nvidia.icd":
        return ("opencl", "nvidia.icd")
    return None


def _trusted(target: str) -> bool:
    return any(target.startswith(prefix) for prefix in TRUSTED_PREFIXES)


def collect(
    packages: Iterable[str] = DEFAULT_PACKAGES,
    *,
    lister: FileLister = xbps_files,
    host_root: Path = Path("/"),
) -> dict[str, str]:
    """Return {farm relative path: absolute host target} for installed packages."""

    entries: dict[str, str] = {}
    candidates = [
        path for package in packages for path in lister(package)
    ]
    # The OpenCL ICD is a plain file some driver packages ship; it is not
    # always in an xbps list that matched, so look for it directly.
    candidates.append("/etc/OpenCL/vendors/nvidia.icd")
    for path in sorted(set(candidates)):
        placement = classify(path)
        if placement is None:
            continue
        host_path = host_root / path.lstrip("/")
        try:
            resolved = host_path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_file():
            continue
        try:
            target = "/" + str(resolved.relative_to(host_root.resolve()))
        except ValueError:
            continue
        if host_root == Path("/") and not _trusted(target):
            continue
        directory, relative = placement
        entries[f"{directory}/{relative}"] = str(host_root / target.lstrip("/"))
    return entries


def build_farm(
    root: Path,
    version: str,
    entries: Mapping[str, str],
) -> Path:
    """Create root/<version>/ with a symlink per entry and point root/current at it."""

    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    final = root / version
    staging = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=root))
    try:
        for relative, target in sorted(entries.items()):
            link = staging / relative
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, link)
        for directory in ("lib", "lib32", "share", "bin", "opencl"):
            (staging / directory).mkdir(exist_ok=True)
        for path in (staging, *(p for p in staging.rglob("*") if p.is_dir())):
            os.chmod(path, 0o755)
        if final.exists():
            old = root / f".old-{os.getpid()}"
            os.rename(final, old)
            os.rename(staging, final)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    current = root / "current"
    temporary = root / f".current-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    os.symlink(version, temporary)
    os.rename(temporary, current)
    for entry in root.iterdir():
        if entry.name not in (version, "current") and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
    return final


def remove_farm(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def arch_multilib(rootfs: Path = Path("/var/lib/spaces/arch/rootfs")) -> bool:
    """True when the Arch guest's pacman.conf enables [multilib].

    Without it /usr/lib32 does not exist in the guest, and the 32-bit overlay
    would only create empty placeholder files there.
    """

    try:
        lines = (rootfs / "etc/pacman.conf").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(line.strip() == "[multilib]" for line in lines)


def nvidia_distro_config(
    farm: Path | None, distro: str, lib32_overlay: bool = True
) -> dict[str, list[dict[str, str]]]:
    """Return the mounts and overlays that expose farm to one distribution."""

    if farm is None or distro not in LIBRARY_DESTINATIONS:
        return {"mounts": [], "overlays": []}
    lib64, lib32 = LIBRARY_DESTINATIONS[distro]
    mounts: list[dict[str, str]] = []
    present_bins = (
        sorted(p.name for p in (farm / "bin").iterdir())
        if (farm / "bin").is_dir()
        else []
    )
    for name in present_bins:
        mounts.append(
            {"source": f"{farm}/bin/{name}", "destination": f"/usr/bin/{name}"}
        )
    if (farm / "opencl" / "nvidia.icd").is_symlink():
        mounts.append(
            {
                "source": f"{farm}/opencl/nvidia.icd",
                "destination": "/etc/OpenCL/vendors/nvidia.icd",
            }
        )
    overlays = [
        {"source": f"{farm}/share", "destination": "/usr/share"},
        {"source": f"{farm}/lib", "destination": lib64},
        {"source": f"{farm}/lib32", "destination": lib32},
    ]
    if not lib32_overlay:
        overlays.pop()
    return {"mounts": mounts, "overlays": overlays}


def _merge_distro(
    target: dict[str, object], source: Mapping[str, object], where: str
) -> None:
    for key in ("packages", "mounts", "overlays"):
        values = source.get(key)
        if values is None:
            continue
        if not isinstance(values, list):
            raise ValueError(f"{where}: {key} must be a list")
        merged = target.setdefault(key, [])
        assert isinstance(merged, list)
        for value in values:
            if value not in merged:
                merged.append(value)
    for key in source:
        if key not in ("packages", "mounts", "overlays"):
            raise ValueError(f"{where}: unknown option {key}")


def _read_json(path: Path, required: bool) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data


def generate(
    base_path: Path,
    extras_path: Path,
    farm: Path | None,
    flavor_resolver: Callable[[str], str] | None = None,
    arch_lib32: bool = True,
) -> str:
    """Return the text of the generated config.json.

    flavor_resolver maps the `desktop_flavor` setting ("auto", "kde", "gtk";
    base config, overridden by void.json) to "kde" or "gtk"; the gtk flavour
    adds its packages (spaces.host.flavor). Without a resolver nothing is added.
    """

    base = _read_json(base_path, True)
    extras = _read_json(extras_path, False)
    if base.get("version") != 1:
        raise ValueError(f"{base_path}: unsupported version")
    distros: dict[str, dict[str, object]] = {}
    base_distros = base.get("distros", {})
    if not isinstance(base_distros, dict):
        raise ValueError(f"{base_path}: distros must be an object")
    for distro, value in base_distros.items():
        if not isinstance(value, dict):
            raise ValueError(f"{base_path}: bad entry for {distro}")
        distros[distro] = {}
        _merge_distro(distros[distro], value, f"{base_path}:{distro}")
    if flavor_resolver is not None:
        from . import flavor

        setting = extras.get("desktop_flavor", base.get("desktop_flavor", flavor.DEFAULT))
        if setting not in flavor.FLAVORS:
            raise ValueError(
                f"{extras_path}: desktop_flavor must be one of {', '.join(flavor.FLAVORS)}"
            )
        chosen = flavor_resolver(str(setting))
        for distro in distros:
            added = flavor.packages_for(chosen, distro)
            if added:
                _merge_distro(distros[distro], {"packages": list(added)}, f"flavor:{distro}")
    for distro in distros:
        _merge_distro(
            distros[distro],
            nvidia_distro_config(farm, distro, arch_lib32 or distro != "arch"),
            f"nvidia:{distro}",
        )
    extra_distros = extras.get("distros", {})
    if not isinstance(extra_distros, dict):
        raise ValueError(f"{extras_path}: distros must be an object")
    for key in extras:
        if key not in ("version", "distros", "desktop_flavor"):
            raise ValueError(f"{extras_path}: unknown option {key}")
    for distro, value in extra_distros.items():
        if not isinstance(value, dict):
            raise ValueError(f"{extras_path}: bad entry for {distro}")
        _merge_distro(distros.setdefault(distro, {}), value, f"{extras_path}:{distro}")
    # json forbids comments and host_config warns on unknown keys, so the
    # "generated" marker is the sidecar hash file, not a key in the document.
    return json.dumps({"version": 1, "distros": distros}, indent=2) + "\n"


def validate(text: str) -> list[str]:
    """Load text through host_config; return its warnings (empty means accepted)."""

    from .. import host_config

    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = Collect()
    config_logger = logging.getLogger(host_config.__name__)
    config_logger.addHandler(handler)
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            config = host_config.load(Path(file.name))
    finally:
        config_logger.removeHandler(handler)
    if config.version != 1 and not records:
        records.append("configuration was not accepted")
    return records


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_legacy(text: str) -> bool:
    """The file M3's dev-install wrote: plain packages, no mounts."""

    try:
        data = json.loads(text)
    except ValueError:
        return False
    packages = ["fastfetch", "screen", "tmux", "zsh"]
    return data == {
        "version": 1,
        "distros": {d: {"packages": packages} for d in ("arch", "fedora", "kali", "ubuntu")},
    }


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, mode)
    os.rename(temporary, path)


def install_config(config_path: Path, text: str) -> str:
    """Write text to config_path unless the file was edited by hand.

    Returns "written", "unchanged" or "kept" (hand-edited: text went to
    config_path + ".new" instead).
    """

    sidecar = config_path.with_name(config_path.name + SIDECAR_SUFFIX)
    new_path = config_path.with_name(config_path.name + ".new")
    digest = _sha256(text)
    try:
        current = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _atomic_write(config_path, text)
        _atomic_write(sidecar, digest + "\n")
        new_path.unlink(missing_ok=True)
        return "written"
    recorded = ""
    try:
        recorded = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    ours = (recorded and _sha256(current) == recorded) or _is_legacy(current)
    if current == text:
        _atomic_write(sidecar, digest + "\n")
        new_path.unlink(missing_ok=True)
        return "unchanged"
    if ours:
        _atomic_write(config_path, text)
        _atomic_write(sidecar, digest + "\n")
        new_path.unlink(missing_ok=True)
        return "written"
    _atomic_write(new_path, text)
    return "kept"


def driver_version() -> str | None:
    try:
        match = re.search(
            r"(\d+(?:\.\d+)+)\s+Release",
            DRIVER_VERSION_PATH.read_text(encoding="utf-8"),
        )
    except OSError:
        return None
    return match.group(1) if match else None


def sync(
    *,
    farm_root: Path = FARM_ROOT,
    config_path: Path = CONFIG_PATH,
    base_path: Path = BASE_PATH,
    extras_path: Path = EXTRAS_PATH,
    lister: FileLister = xbps_files,
    version: str | None = None,
    host_root: Path = Path("/"),
    desktop_uid: int | None = None,
) -> dict[str, object]:
    """Rebuild the farm and the generated config; return a status report."""

    report: dict[str, object] = {}
    pkgver = version if version is not None else xbps_version()
    entries = collect(lister=lister, host_root=host_root) if pkgver else {}
    farm: Path | None = None
    if pkgver and any(name.startswith("lib/") for name in entries):
        farm = build_farm(farm_root, pkgver, entries)
        farm = farm_root / "current"
        report["farm"] = str(farm)
        report["entries"] = len(entries)
        loaded = driver_version()
        report["driver"] = loaded
        if loaded and not pkgver.startswith(loaded):
            report["warning"] = (
                f"loaded kernel module {loaded} differs from userspace {pkgver}; "
                "reboot or reload the driver"
            )
    else:
        remove_farm(farm_root)
        report["farm"] = None
    from . import flavor

    text = generate(
        base_path,
        extras_path,
        farm,
        lambda setting: flavor.resolve(setting, desktop_uid),
        arch_lib32=arch_multilib(),
    )
    problems = validate(text)
    if problems:
        raise ValueError("generated configuration rejected: " + "; ".join(problems))
    report["config"] = install_config(config_path, text)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spaces-nvidia-sync",
        description="Expose the host NVIDIA userspace to spaces and "
        "regenerate /etc/spaces/config.json.",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--print", action="store_true", help="print the generated config and exit"
    )
    arguments = parser.parse_args(argv)
    logging.basicConfig(format="spaces-nvidia-sync: %(message)s")
    if os.geteuid() != 0 and not arguments.print:
        print("spaces-nvidia-sync: must run as root", file=sys.stderr)
        return 1
    try:
        if arguments.print:
            version = xbps_version()
            farm = FARM_ROOT / "current" if version else None
            from . import flavor

            sys.stdout.write(
                generate(
                    BASE_PATH, EXTRAS_PATH, farm, flavor.resolve, arch_multilib()
                )
            )
            return 0
        report = sync()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"spaces-nvidia-sync: {error}", file=sys.stderr)
        return 1
    if report.get("warning"):
        print(f"spaces-nvidia-sync: warning: {report['warning']}", file=sys.stderr)
    if report.get("config") == "kept":
        print(
            f"spaces-nvidia-sync: {CONFIG_PATH} was edited by hand and was not "
            f"replaced; the new version is {CONFIG_PATH}.new",
            file=sys.stderr,
        )
    if not arguments.quiet:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
