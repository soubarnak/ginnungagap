# SPDX-License-Identifier: Apache-2.0
"""The GTK4 and libadwaita views of the Control Plane (FR-100..107, ADR-001).

The views render the viewmodels and forward each action to the Control, which starts the `ginn`
commands. Only `_gtk.py` in this package imports gi. The other view modules take the GTK names
from it. The coverage configuration omits this package, because its widgets need a display. The
logic they show is tested through the viewmodels and the Control.
"""
