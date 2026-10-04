#!/usr/bin/env python3
"""Check that the guest-native helpers resolve against a guest rootfs' GLib.

The helpers in /usr/lib/spaces/guest are built on the host and link libglib,
libgobject and libgio dynamically. GLib exports unversioned symbols, so a host
newer than the guest can leave a symbol the guest does not have; the helpers
link with -z now, so that fails at startup. This lists every undefined g_*/G*
symbol the helpers need that the guest's three libraries do not define.

    sudo python3 void/tools/check-guest-glib.py [ROOTFS] [HELPER ...]

Exit status 0 when every symbol resolves (prints the guest GLib file version).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

GUEST = Path("/usr/lib/spaces/guest")
LIBRARIES = ("libglib-2.0.so.0", "libgobject-2.0.so.0", "libgio-2.0.so.0")
SEARCH = ("usr/lib/x86_64-linux-gnu", "usr/lib64", "usr/lib", "lib/x86_64-linux-gnu")


def symbols(path: Path, *flags: str) -> set[str]:
    out = subprocess.run(
        ["nm", "-D", *flags, str(path)], capture_output=True, text=True, check=True
    ).stdout
    return {line.split()[-1].partition("@")[0] for line in out.splitlines() if line.split()}


def find_library(rootfs: Path, name: str) -> Path | None:
    for directory in SEARCH:
        candidate = rootfs / directory / name
        if candidate.exists():
            return candidate.resolve()
    return None


def main(argv: list[str]) -> int:
    rootfs = Path(argv[0]) if argv else Path("/var/lib/spaces/ubuntu/rootfs")
    helpers = [Path(item) for item in argv[1:]] or sorted(GUEST.glob("spaces-*"))
    provided: set[str] = set()
    for name in LIBRARIES:
        library = find_library(rootfs, name)
        if library is None:
            print(f"missing in guest: {name}")
            return 1
        provided |= symbols(library, "--defined-only")
        if name == LIBRARIES[0]:
            print(f"guest glib: {library.name}")
    missing: dict[str, list[str]] = {}
    for helper in helpers:
        for symbol in symbols(helper, "--undefined-only"):
            if symbol.startswith(("g_", "G")) and symbol not in provided:
                missing.setdefault(symbol, []).append(helper.name)
    for symbol, users in sorted(missing.items()):
        print(f"unresolved in guest: {symbol} ({', '.join(users)})")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
