# SPDX-License-Identifier: Apache-2.0
"""The progress dialog of a long operation, with Cancel (FR-107, IR-006).

The dialog runs the operation through the session's Control. Each progress line from the CLI
updates the step text. Cancel sends SIGINT to the running command, and the dialog stays open until
the command has ended, so the user sees the outcome. Close is enabled only then.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from ginn.gui.backend import CliResult
from ginn.gui.jobs import Operation
from ginn.gui.viewmodels.progress import ProgressModel
from ginn.gui.views import common
from ginn.gui.views._gtk import SOURCE_CONTINUE, SOURCE_REMOVE, Adw, GLib, Gtk

if TYPE_CHECKING:
    from ginn.gui.views.window import Session

PULSE_MS = 120
RUNNING_HINT = "The window stays open until the command ends."


def present_progress(
    session: Session,
    *,
    title: str,
    calls: Sequence[Sequence[str]],
    on_done: Callable[[ProgressModel], None],
) -> Operation:
    """Show the dialog and start `calls`. `on_done` runs once the run has ended."""
    model = ProgressModel(title=title)
    step = common.label(model.current, css=("heading",), wrap=True)
    summary = common.label("", wrap=True)
    lines = common.label("", css=("dim-label",), wrap=True)
    bar = Gtk.ProgressBar()
    cancel = Gtk.Button(label="Cancel")
    cancel.add_css_class("destructive-action")
    common.set_accessible_name(cancel, "Cancel the operation")
    close = Gtk.Button(label="Close")
    close.add_css_class("suggested-action")

    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    buttons.set_halign(Gtk.Align.END)
    buttons.append(cancel)
    buttons.append(close)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    for widget in (step, bar, common.scrolled(lines), summary, buttons):
        box.append(widget)
    box.set_margin_top(18)
    box.set_margin_bottom(18)
    box.set_margin_start(18)
    box.set_margin_end(18)

    dialog = Adw.Dialog.new()
    dialog.set_title(title)
    dialog.set_content_width(460)
    dialog.set_child(box)

    def refresh() -> None:
        step.set_label(model.current)
        lines.set_label("\n".join(model.lines))
        summary.set_label(
            model.summary if model.finished else f"{model.phase_text}. {RUNNING_HINT}"
        )
        cancel.set_visible(model.can_cancel)
        close.set_visible(model.finished)
        dialog.set_can_close(model.finished)

    def pulse() -> bool:
        if model.finished:
            return SOURCE_REMOVE
        bar.pulse()
        return SOURCE_CONTINUE

    def progress(text: str) -> None:
        model.add_line(text)
        refresh()

    def finished(
        results: tuple[CliResult, ...], error: BaseException | None, cancelled: bool
    ) -> None:
        session.ended(operation)
        model.finish(results, error, cancelled)
        refresh()
        on_done(model)

    def cancel_run() -> None:
        model.request_cancel()
        operation.cancel()
        refresh()

    operation = session.control.operation(calls, on_progress=progress, on_finished=finished)
    session.began(operation)
    cancel.connect("clicked", lambda _button: cancel_run())
    close.connect("clicked", lambda _button: dialog.close())
    refresh()
    GLib.timeout_add(PULSE_MS, pulse)
    dialog.present(session.window)
    operation.start()
    return operation
