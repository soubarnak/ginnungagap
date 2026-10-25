#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
#
# S-06 (FR-069, R-005): hand-written CDI spec for nvidia.com/gpu=all, built from
# the host NVIDIA proprietary driver files. No nvidia-container-toolkit.
#
#   python3 -I spikes/s06/nvidia-cdi.py DATA_DIR
#
# Writes DATA_DIR/cdi/nvidia.json (cdiVersion 0.6.0) and DATA_DIR/ld/ginn-nvidia.conf
# (an ld.so.conf drop-in). Prints a JSON summary on stdout. Exits 1 when the
# kernel driver is not loaded.
#
# Every library is bind-mounted read-only into LIB_DIR in the realm under the
# name the host glob matched (a real file or a symlink name). hostPath is the
# realpath, so the SONAME names exist without any symlink creation. The
# drop-in adds LIB_DIR to the ld search path; ldconfig must run once after the
# realm starts, or the loader must get LD_LIBRARY_PATH=LIB_DIR.
import glob
import json
import os
import re
import shutil
import stat
import sys

LIB_DIR = "/usr/lib/ginn-nvidia"
LD_DROPIN = "/etc/ld.so.conf.d/ginn-nvidia.conf"
RO = ["ro", "nosuid", "nodev", "bind"]
LIB_GLOBS = [
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
]
# Vendor JSON files keep their host path in the realm.
JSON_GLOBS = [
    "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
    "/usr/share/vulkan/icd.d/nvidia_icd.json",
    "/usr/share/vulkan/implicit_layer.d/nvidia_layers.json",
    "/usr/share/egl/egl_external_platform.d/*nvidia*",
    "/etc/vulkan/icd.d/*nvidia*",
]
PROC_VERSION = "/proc/driver/nvidia/version"
BIN_HOST = "nvidia-smi"
BIN_REALM = "/usr/bin/nvidia-smi"
DRIVER_VER = re.compile(r"(\d{3}\.\d+\.\d+)")
LIB_VER = re.compile(r"\.so\.(\d{3}\.\d+\.\d+)")


def host_driver_version():
    with open(PROC_VERSION, encoding="ascii") as f:
        m = DRIVER_VER.search(f.readline())  # "NVRM version: ... 595.104.02 ..."
    if not m:
        raise SystemExit("cannot parse driver version from " + PROC_VERSION)
    return m.group(1)


def device_nodes():
    nodes = []
    for p in sorted(glob.glob("/dev/nvidia*") + glob.glob("/dev/nvidia-caps/*")):
        st = os.stat(p)
        if not stat.S_ISCHR(st.st_mode):
            continue  # skips the /dev/nvidia-caps directory
        nodes.append({
            "path": p,
            "type": "c",
            "major": os.major(st.st_rdev),
            "minor": os.minor(st.st_rdev),
            "permissions": "rwm",
        })
    return nodes


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    if not os.path.exists("/dev/nvidiactl"):
        raise SystemExit("no /dev/nvidiactl: NVIDIA kernel driver not loaded")
    data = os.path.abspath(sys.argv[1])
    drv = host_driver_version()

    nodes = device_nodes()
    mounts = []
    seen = set()
    lib_hits = []
    for pat in LIB_GLOBS:
        for name in sorted(glob.glob(pat)):
            target = LIB_DIR + "/" + os.path.basename(name)
            if target in seen:
                continue
            seen.add(target)
            mounts.append({"hostPath": os.path.realpath(name),
                           "containerPath": target, "options": RO})
            lib_hits.append(name)

    json_hits = []
    for pat in JSON_GLOBS:
        for name in sorted(glob.glob(pat)):
            if name in seen:
                continue
            seen.add(name)
            mounts.append({"hostPath": os.path.realpath(name),
                           "containerPath": name, "options": RO})
            json_hits.append(name)

    smi = shutil.which(BIN_HOST)
    if smi:
        mounts.append({"hostPath": os.path.realpath(smi),
                       "containerPath": BIN_REALM, "options": RO})

    ld_dir = os.path.join(data, "ld")
    os.makedirs(ld_dir, mode=0o700, exist_ok=True)
    ld_conf = os.path.join(ld_dir, "ginn-nvidia.conf")
    with open(ld_conf, "w", encoding="ascii") as f:
        f.write(LIB_DIR + "\n")
    mounts.append({"hostPath": ld_conf, "containerPath": LD_DROPIN, "options": RO})

    spec = {
        "cdiVersion": "0.6.0",
        "kind": "nvidia.com/gpu",
        "devices": [{
            "name": "all",
            "containerEdits": {"deviceNodes": nodes, "mounts": mounts},
        }],
    }
    cdi_dir = os.path.join(data, "cdi")
    os.makedirs(cdi_dir, mode=0o700, exist_ok=True)
    spec_path = os.path.join(cdi_dir, "nvidia.json")
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)
        f.write("\n")

    # R-005: versions baked into the realpath names of the driver libraries.
    lib_vers = set()
    for m in mounts:
        if m["containerPath"].startswith(LIB_DIR + "/"):
            v = LIB_VER.search(os.path.basename(m["hostPath"]))
            if v:
                lib_vers.add(v.group(1))

    summary = {
        "spec": spec_path,
        "ld_conf": ld_conf,
        "device_nodes": [n["path"] for n in nodes],
        "lib_names": [os.path.basename(n) for n in lib_hits],
        "json_files": json_hits,
        "binary": smi,
        "mount_count": len(mounts),
        "proc_driver_version": drv,
        "lib_versions": sorted(lib_vers),
        "stale_libs": sorted(v for v in lib_vers if v != drv),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
