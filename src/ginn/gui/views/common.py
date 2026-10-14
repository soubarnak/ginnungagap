# SPDX-License-Identifier: Apache-2.0
"""Widget helpers that the views share. Each one returns a plain GTK or libadwaita widget.

Every control gets an accessible name: a button label, or a tooltip set as the accessible label
(NFR-051). A button with an icon only has its tooltip for that name.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ginn.gui.backend import CliResult
from ginn.gui.viewmodels.labels import Failure, failure_of
from ginn.gui.views._gtk import Adw, Gtk

Action = Callable[[], object]
"""A click handler. Its result is ignored, so a handler may return the page it opened."""


def label(text: str, *, css: tuple[str, ...] = (), wrap: bool = False) -> Any:
    widget = Gtk.Label(label=text)
    widget.set_xalign(0.0)
    widget.set_wrap(wrap)
    widget.set_selectable(False)
    for name in css:
        widget.add_css_class(name)
    return widget


def set_accessible_name(widget: Any, name: str) -> None:
    widget.update_property([Gtk.AccessibleProperty.LABEL], [name])


def text_button(text: str, on_click: Action, *, css: tuple[str, ...] = ()) -> Any:
    button = Gtk.Button(label=text)
    for name in css:
        button.add_css_class(name)
    button.connect("clicked", lambda _button: on_click())
    return button


def icon_button(icon: str, tooltip: str, on_click: Action) -> Any:
    button = Gtk.Button.new_from_icon_name(icon)
    button.set_tooltip_text(tooltip)
    set_accessible_name(button, tooltip)
    button.connect("clicked", lambda _button: on_click())
    return button


def clear_box(box: Any) -> None:
    """Remove every child of a ListBox or a Box, so the rows can be built again.

    Not for an Adw.PreferencesGroup: it has children of its own that cannot be removed. A group
    is cleared by removing the rows that were added to it.
    """
    child = box.get_first_child()
    while child is not None:
        box.remove(child)
        child = box.get_first_child()


def scrolled(child: Any) -> Any:
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_vexpand(True)
    scroller.set_child(child)
    return scroller


def error_text(failure: Failure) -> str:
    """`CODE: message`, with the remedy on its own line when the CLI gave one."""
    head = f"{failure.code}: {failure.message}" if failure.code else failure.message
    return f"{head}\n{failure.remedy}" if failure.remedy else head


def failure_text(result: CliResult) -> str | None:
    """The text to show for a failed run, or None when it succeeded."""
    failure = failure_of(result)
    return None if failure is None else error_text(failure)


def inform(parent: Any, *, heading: str, body: str) -> Any:
    """Tell the user why an action did not run. One button closes the dialog."""
    dialog = Adw.AlertDialog.new(heading, body)
    dialog.set_heading_use_markup(False)
    dialog.set_body_use_markup(False)
    dialog.add_response("ok", "OK")
    dialog.set_default_response("ok")
    dialog.set_close_response("ok")
    dialog.present(parent)
    return dialog


def confirm(
    parent: Any,
    *,
    heading: str,
    body: str,
    confirm_label: str,
    destructive: bool,
    on_confirm: Callable[[bool], None],
    check_label: str | None = None,
) -> Any:
    """Ask before a change. `on_confirm` gets the state of the extra check box, unchecked by
    default. Cancel is the default answer, and Escape cancels too."""
    dialog = Adw.AlertDialog.new(heading, body)
    dialog.set_heading_use_markup(False)
    dialog.set_body_use_markup(False)
    check = None
    if check_label is not None:
        check = Gtk.CheckButton(label=check_label)
        check.set_active(False)
        dialog.set_extra_child(check)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", confirm_label)
    appearance = (
        Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED
    )
    dialog.set_response_appearance("confirm", appearance)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def answered(_dialog: Any, response: str) -> None:
        if response == "confirm":
            on_confirm(bool(check is not None and check.get_active()))

    dialog.connect("response", answered)
    dialog.present(parent)
    return dialog
