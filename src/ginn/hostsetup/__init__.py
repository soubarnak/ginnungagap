# SPDX-License-Identifier: Apache-2.0
"""One-time Host Setup primitives (FR-001..003, FR-007, FR-081, FR-084).

Every module here is pure logic over a root directory prefix, so tests run under `tmp_path`:

- subids: subordinate UID/GID ranges in etc/subuid and etc/subgid (AS-005)
- cgroups: cgroup v2 delegation plan and verification (S-01)
- runit_system: the system runit service that runs the user's runsvdir
- record: the Host Setup Record, the paths Host Setup wrote with their pre-images (DR-009)
- setup: Host Setup orchestration for one user (FR-001, FR-002)
- _atomic: atomic replacement of small host files
"""
