# SPDX-License-Identifier: Apache-2.0
"""Test-session setup for the distribution fixture gate (NFR-060).

The `_test` definition is a fake distribution. ginn refuses it unless GINN_TEST_DISTROS=1
(ginn.distros.loader). This module sets the switch when it is imported, not in a fixture: the test
modules build their parametrisation from `available(include_test=True)` during collection, and a
fixture would run after that. Tests that check the refusal remove the switch with monkeypatch.
"""

import os

from ginn.distros import TEST_SWITCH_ENV

os.environ[TEST_SWITCH_ENV] = "1"
