# SPDX-License-Identifier: Apache-2.0
"""The unit tests cannot run a host security binary through a default hook (NFR-SEC-007).

The autouse fence in tests/unit/conftest.py makes the default gpg hook fail, and it leaves no
xdg-dbus-proxy on PATH for the default proxy. These tests prove the fence is in force. If the fence
goes, the default gpg hook would run the host's gpgconf, and the first test fails.
"""

import pytest

import ginn.runtime.realm_security as realm_security
from ginn.runtime.realm_security import SecurityHooks


@pytest.mark.fr("NFR-SEC-007")
def test_the_default_gpg_hook_fails_instead_of_running_gpgconf() -> None:
    with pytest.raises(RuntimeError, match="gpgconf"):
        SecurityHooks().gpg_extra()


@pytest.mark.fr("NFR-SEC-007")
def test_the_default_proxy_binary_is_not_found_so_no_bus_proxy_starts() -> None:
    assert realm_security.proxy_binary(SecurityHooks()) is None
