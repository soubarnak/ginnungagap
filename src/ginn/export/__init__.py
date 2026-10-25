# SPDX-License-Identifier: Apache-2.0
"""Desktop and CLI export of realm applications to the host (FR-044..047, IR-029).

- desktop: the Desktop Entry parser and the export rewrite of one entry
- icons: icon lookup inside the realm, and the copy out with its size and path checks
- sync: `ginn _sync`, the diff of a realm's desktop entries against the host menu, and its apply
- bins: `ginn export --bin` and `ginn unexport --bin` launchers in the ginn bin directory
- _files: the atomic file write that the writers share
- _realm: the bounded listings of a realm (timeout and output cap, GINN-E-EXPORT-LIST)

The CLI commands are in `ginn.cli.commands`: export, unexport and `_sync`.
"""
