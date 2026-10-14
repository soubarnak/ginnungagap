# SPDX-License-Identifier: Apache-2.0
"""Viewmodels of the Control Plane (ADR-001, NFR-061).

Each viewmodel holds the state of one screen and computes what it shows. A viewmodel never
imports gi and never runs a process. Its inputs are typed backend results, and its outputs are
argv tuples for `ginn --json` or plain values. The views are thin. They render the viewmodel
and forward the user's actions to it. Tests for the viewmodels run without a display.
"""
