# SPDX-License-Identifier: Apache-2.0
"""Command table (IR-010).

Each command is a module in this package that defines SPEC, a CommandSpec. COMMAND_MODULES
lists those modules by short name, in help order. To add a command, write its module and
append the short name to COMMAND_MODULES. The parser is built from COMMANDS, and every
command's --help gets an "Example:" line from `example`.

Command modules import CommandSpec and Context from this package, so the table is loaded at
the end of the file, after both classes exist.
"""

import argparse
import importlib
from collections.abc import Callable
from typing import NamedTuple

from ginn.cli.output import Output


class Context(NamedTuple):
    """Per-invocation state passed to every command."""

    output: Output


ConfigureHook = Callable[[argparse.ArgumentParser], None]
RunHook = Callable[[argparse.Namespace, Context], int]


class CommandSpec(NamedTuple):
    """One `ginn` subcommand.

    `example` is a single invocation such as "ginn list --json". The word "Example:" is added
    when the help text is rendered.
    """

    name: str
    help: str
    example: str
    configure: ConfigureHook
    run: RunHook
    hidden: bool = False
    """A hidden command runs and parses, but is left out of the top-level help (`_sync`)."""


COMMAND_MODULES: tuple[str, ...] = (
    "host_setup",
    "integrity",
    "doctor",
    "log",
    "create",
    "start",
    "stop",
    "status",
    "list_cmd",
    "enter",
    "exec",
    "delete",
    "export",
    "unexport",
    "sync",
    "install",
    "remove",
)


def _load_spec(short_name: str) -> CommandSpec:
    module = importlib.import_module(f"{__name__}.{short_name}")
    spec = getattr(module, "SPEC", None)
    if not isinstance(spec, CommandSpec):
        raise RuntimeError(f"{__name__}.{short_name} does not define SPEC as a CommandSpec")
    return spec


def _load_table(short_names: tuple[str, ...]) -> list[CommandSpec]:
    specs: list[CommandSpec] = []
    for short_name in short_names:
        spec = _load_spec(short_name)
        if any(seen.name == spec.name for seen in specs):
            raise RuntimeError(f"command name {spec.name!r} is registered twice")
        specs.append(spec)
    return specs


COMMANDS: list[CommandSpec] = _load_table(COMMAND_MODULES)
