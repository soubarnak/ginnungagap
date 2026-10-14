# SPDX-License-Identifier: Apache-2.0
"""Per-tier capability options and the security modules that add to them (NFR-SEC-002).

`caps` is the enumerated set of podman options that a tier adds beyond the base argv
(docs/capabilities.md). `caps.TierExtras` is the one way another module adds a create-time option,
and it is an allowlist. The NVIDIA injection (nvidia.py) supplies a typed `CdiGrant`. The Agent
Proxy (agent.py) adds no create-time option, and the session D-Bus filter (dbus_filter.py) is not a
podman option.
"""
