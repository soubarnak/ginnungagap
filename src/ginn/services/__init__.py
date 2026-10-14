# SPDX-License-Identifier: Apache-2.0
"""Runit supervision of services-on realms (FR-081, AS-028, NFR-033, ADR-003).

- runit_user: the per-realm service directory under the user's runit supervisor (`run`, `finish`,
  `down`), its atomic install and removal, and the per-realm files of the supervisor
- supervise: the crash window (AS-028), the restart delay and the runsv control write that stops
  a restart

The operations that the hidden commands `ginn _service-run` and `ginn _service-finish` run are in
ops/service.py.
"""
