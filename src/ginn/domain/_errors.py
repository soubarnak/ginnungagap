# SPDX-License-Identifier: Apache-2.0
"""Shared rejection helper for the domain primitives.

`errors.make()` accepts only the placeholders of a catalogue summary. The primitives also record
the rule that failed, so they add those keys to `GinnError.details` after `make()` builds the error.
"""

from collections.abc import Mapping

from ginn.errors import GinnError, make


def reject(code: str, extra: Mapping[str, object], **fmt: object) -> GinnError:
    """Build the catalogue error for `code` and add `extra` to its details."""
    err = make(code, **fmt)
    err.details.update(extra)
    return err
