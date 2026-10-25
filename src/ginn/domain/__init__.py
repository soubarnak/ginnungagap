# SPDX-License-Identifier: Apache-2.0
"""Domain primitives (DR-002..013, DR-020, NFR-SEC-005).

Each primitive is validated when it is built. The rest of the code accepts only these types.
"""

from ginn.domain.enums import DistroId, RealmState, Tier
from ginn.domain.names import RealmName
from ginn.domain.packages import InstallMethod, InstallSource, ManifestEntry, PackageName
from ginn.domain.paths import ALLOWED_ROOTS, FORBIDDEN_RULES, ForbiddenRule, Mode, SharedPath
from ginn.domain.realm import RealmSettings
from ginn.domain.states import (
    REMOVED,
    TRANSITIONS,
    IllegalTransition,
    Removed,
    Target,
    assert_transition,
    can_transition,
    transition,
)

__all__ = [
    "ALLOWED_ROOTS",
    "FORBIDDEN_RULES",
    "REMOVED",
    "TRANSITIONS",
    "DistroId",
    "ForbiddenRule",
    "IllegalTransition",
    "InstallMethod",
    "InstallSource",
    "ManifestEntry",
    "Mode",
    "PackageName",
    "RealmName",
    "RealmSettings",
    "RealmState",
    "Removed",
    "SharedPath",
    "Target",
    "Tier",
    "assert_transition",
    "can_transition",
    "transition",
]
