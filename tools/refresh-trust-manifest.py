#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Refresh the pinned platform digests in the Trust Manifest with skopeo (DR-040, FR-011, R-003).

Dev-time only. ginn never imports or runs this file (DR-040). For each entry in trust.toml it asks
the registry, through `skopeo inspect`, for the current linux/amd64 platform digest of the entry's
image tag, and compares that with the pinned digest.

Modes:
  (default)  print each entry's pinned and current digest, and write nothing
  --check    the same, and exit 1 when any digest moved; write nothing
  --write    rewrite the `digest` line of each moved entry in place; comments and other fields
             are kept. The new text is validated with ginn.trust.manifest.parse before anything is
             written.

Only `digest` is rewritten. The sizes, the signature kind and `verified_on` describe the pulled
image, so they need the by-hand steps in spikes/results/S-10.md section 6 (steps 2 to 4, and the
other fields of step 5). After a write the tool says so, rather than leaving them stale without
notice.

Resolution uses argv lists only, never a shell (NFR-SEC-005). A registry answer has one of two
shapes:
- an index (`manifests` list): take the single entry with platform os=linux, architecture=amd64.
  Attestation entries, whose platform is unknown/unknown, are skipped.
- a single manifest (Arch on quay.io): it is the platform image. Its digest is sha256 of the raw
  bytes, and skopeo's own Digest, Architecture and Os must agree with that.

Exit status: 0 nothing moved, or the write succeeded; 1 --check found a moved digest; 2 skopeo is
missing or failed, the registry answer is not a linux/amd64 image, or the manifest is invalid.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

if TYPE_CHECKING:
    from ginn.trust.manifest import TrustEntry, TrustManifest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MANIFEST = SRC / "ginn" / "trust" / "data" / "trust.toml"
SKOPEO: Final = "skopeo"
TIMEOUT_S: Final = 300
EXIT_OK: Final = 0
EXIT_MOVED: Final = 1
EXIT_ERROR: Final = 2

_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
_DIGEST_LINE: Final = re.compile(r'digest = "(sha256:[0-9a-f]{64})"')
_TABLE_HEADER: Final = re.compile(r"\[[^\]]+\][ \t]*")

Runner = Callable[[Sequence[str]], bytes]
"""Runs `skopeo` with the given arguments and returns its stdout. Raises RefreshError on failure."""


class RefreshError(Exception):
    """A refresh cannot finish. The message says why, for stderr."""


class Resolved(NamedTuple):
    """The pinned and the current linux/amd64 platform digest of one entry."""

    key: str
    image: str
    pinned: str
    current: str

    @property
    def moved(self) -> bool:
        return self.current != self.pinned


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _subprocess_runner(exe: str) -> Runner:
    """Return a runner that executes `exe` with an argv list. No shell is involved."""

    def run(argv: Sequence[str]) -> bytes:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv list, no shell
                [exe, *argv],
                capture_output=True,
                check=False,
                timeout=TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RefreshError(f"skopeo {argv[0]} did not complete: {exc}") from exc
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", errors="replace").strip()
            raise RefreshError(f"skopeo {argv[0]} exited {done.returncode}: {detail}")
        return done.stdout

    return run


def _default_runner() -> Runner:
    exe = shutil.which(SKOPEO)
    if exe is None:
        raise RefreshError("skopeo is not on PATH; install it to refresh the manifest")
    return _subprocess_runner(exe)


def _amd64_digests(manifests: Sequence[object]) -> list[str]:
    found: list[str] = []
    for item in manifests:
        if not isinstance(item, dict):
            continue
        platform = item.get("platform")
        digest = item.get("digest")
        if not isinstance(platform, dict) or not isinstance(digest, str):
            continue
        if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
            found.append(digest)
    return found


def _json_object(raw: bytes, what: str) -> dict[str, object]:
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise RefreshError(f"{what} is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise RefreshError(f"{what} is not a JSON object")
    return doc


def resolve(entry: TrustEntry, run: Runner) -> Resolved:
    """Return the current linux/amd64 platform digest of `entry.image` (S-10 section 6, step 1)."""
    ref = f"docker://{entry.image}"
    raw = run(["inspect", "--raw", ref])
    doc = _json_object(raw, f"{entry.image} manifest")
    manifests = doc.get("manifests")
    if isinstance(manifests, list):
        found = _amd64_digests(manifests)
        if len(found) != 1:
            raise RefreshError(
                f"{entry.image}: expected one linux/amd64 manifest in the index, found {len(found)}"
            )
        current = found[0]
    else:
        info = _json_object(run(["inspect", ref]), f"{entry.image} inspect")
        if info.get("Architecture") != "amd64" or info.get("Os") != "linux":
            raise RefreshError(
                f"{entry.image}: single manifest is {info.get('Os')}/{info.get('Architecture')}, "
                "not linux/amd64"
            )
        current = _sha256(raw)
        if info.get("Digest") != current:
            raise RefreshError(
                f"{entry.image}: sha256 of the manifest is {current}, skopeo reports "
                f"{info.get('Digest')}"
            )
    if not _DIGEST_RE.fullmatch(current):
        raise RefreshError(f"{entry.image}: registry returned an invalid digest {current!r}")
    return Resolved(key=entry.key, image=entry.image, pinned=entry.digest, current=current)


def rewrite(text: str, moved: Mapping[str, str]) -> str:
    """Return `text` with the `digest` line of each distro in `moved` replaced.

    Only the line inside the matching `[distro.<key>]` table changes. Comments, other fields and
    the line endings stay as they are. Each table must hold exactly one digest line.
    """
    lines = text.splitlines(keepends=True)
    for key, digest in moved.items():
        header = f"[distro.{key}]"
        starts = [i for i, line in enumerate(lines) if line.strip() == header]
        if len(starts) != 1:
            raise RefreshError(f"{header} must appear exactly once in the manifest")
        found: list[tuple[int, str]] = []
        for i in range(starts[0] + 1, len(lines)):
            if _TABLE_HEADER.match(lines[i]):
                break
            match = _DIGEST_LINE.match(lines[i])
            if match is not None:
                found.append((i, match.group(1)))
        if len(found) != 1:
            raise RefreshError(f"{header} must hold exactly one digest line, found {len(found)}")
        index, old = found[0]
        lines[index] = lines[index].replace(f'"{old}"', f'"{digest}"', 1)
    return "".join(lines)


def _load(path: Path) -> TrustManifest:
    """Validate the manifest with ginn's loader. Imported late: this tool is not part of ginn."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from ginn.errors import GinnError
    from ginn.trust.manifest import load_file

    try:
        return load_file(path)
    except GinnError as exc:
        raise RefreshError(str(exc)) from exc


def _write(path: Path, text: str) -> None:
    """Validate the new text, then replace the file. The file is never left half-written."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from ginn.errors import GinnError
    from ginn.trust.manifest import parse

    try:
        parse(text, str(path))
    except GinnError as exc:
        raise RefreshError(f"refusing to write an invalid manifest: {exc}") from exc
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _report(resolved: Sequence[Resolved]) -> None:
    for item in resolved:
        print(f"{item.key:<8} {item.image}  {'MOVED' if item.moved else 'unchanged'}")
        print(f"  pinned  {item.pinned}")
        if item.moved:
            print(f"  current {item.current}")


def main(argv: Sequence[str] | None = None, *, run: Runner | None = None) -> int:
    """Run the tool. `run` replaces skopeo in tests. Returns the exit status."""
    parser = argparse.ArgumentParser(
        prog="refresh-trust-manifest.py",
        description="Compare the pinned platform digests in trust.toml with the registries.",
        epilog="Example: python3 -I tools/refresh-trust-manifest.py --check",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 when a digest moved")
    mode.add_argument("--write", action="store_true", help="rewrite the digest of moved entries")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST,
        help="manifest to read (default: the packaged trust.toml)",
    )
    args = parser.parse_args(argv)
    try:
        manifest = _load(args.manifest)
        runner = run if run is not None else _default_runner()
        resolved = [resolve(entry, runner) for entry in manifest.entries]
        _report(resolved)
        moved = [item for item in resolved if item.moved]
        if args.write and moved:
            text = args.manifest.read_text(encoding="utf-8")
            _write(args.manifest, rewrite(text, {item.key: item.current for item in moved}))
            names = ", ".join(item.key for item in moved)
            print(f"wrote new digests for: {names}")
            print(
                "not refreshed: compressed_size_bytes, estimated_installed_bytes, "
                "measured_installed_bytes, signature, verified_on and the index digest in the "
                "table comments. Still by hand, per S-10 section 6: steps 2 to 4, the other "
                "fields of step 5, step 7, and the commit in step 8."
            )
    except RefreshError as exc:
        sys.stderr.write(f"refresh-trust-manifest: {exc}\n")
        return EXIT_ERROR
    if args.check and moved:
        names = ", ".join(item.key for item in moved)
        sys.stderr.write(f"digest moved for: {names}\n")
        return EXIT_MOVED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
