# SPDX-License-Identifier: Apache-2.0
"""Control Plane: the GTK4 and libadwaita front end of ginn (FR-100..107, ADR-001).

The Control Plane is a client of the CLI. It reads state and runs operations by starting
`ginn --json <command>` as a child process, and it never imports ginn.ops or talks to a
container runtime (FR-106, IR-010).

Security (NFR-SEC-009, IR-031): there is no socket server, no listener and no named pipe. The
backend starts each `ginn` command itself, so the child runs as the same user with the same
Owned Paths. No other user can connect to a channel that does not exist. This process-ancestry
channel satisfies IR-031 and FR-105. ADR-009, which should record this decision, is not written
yet.

The package keeps GTK out of the logic. `backend.py`, `control.py`, `jobs.py`, `actions.py` and
`viewmodels/` import no gi module, so they load without GTK. `watch.py` defines the change-source
interface. Only `_gio.py` (the change source) and `views/_gtk.py` (the GTK names) import gi, and
`app.py` and `views/` need GTK to run.
"""
