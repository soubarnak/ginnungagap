# SPDX-License-Identifier: Apache-2.0
"""Hypothesis profiles for the property tests (roadmap M2).

`ci` runs 500 examples per test. `fuzz` runs 10,000 and is selected with GINN_FUZZ=1. Deadlines
are off because the path tests call realpath on every example. The example database is off, so
no .hypothesis/ directory is written into the checkout.

Hypothesis is a dev dependency. Without it, the property modules skip (importorskip) and this file
registers nothing.
"""

import os

try:
    from hypothesis import HealthCheck, settings
except ImportError:
    pass
else:
    settings.register_profile(
        "ci",
        max_examples=500,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    settings.register_profile(
        "fuzz",
        max_examples=10_000,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    settings.load_profile("fuzz" if os.environ.get("GINN_FUZZ") == "1" else "ci")
