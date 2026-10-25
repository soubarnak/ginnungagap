# SPDX-License-Identifier: Apache-2.0
"""Realm operations. Every mutating operation runs through the choke point in `_mutating`.

`mutating()` takes the realm lock, runs the host integrity checks around the operation, writes the
journal entry and appends one audit entry (AGENTS.md, DR-032, FR-006, FR-123, FR-124).
"""

from ginn.ops._mutating import Mutation, OpEnv, mutating

__all__ = ["Mutation", "OpEnv", "mutating"]
