# SPDX-License-Identifier: Apache-2.0
"""Trust Manifest: pinned image digests shipped with ginn (DR-040). See manifest.py."""

from ginn.trust.manifest import (
    SignatureKind,
    TrustEntry,
    TrustManifest,
    load,
    load_file,
    pull_reference,
)

__all__ = [
    "SignatureKind",
    "TrustEntry",
    "TrustManifest",
    "load",
    "load_file",
    "pull_reference",
]
