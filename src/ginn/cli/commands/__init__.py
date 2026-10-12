# SPDX-License-Identifier: Apache-2.0
"""Command registry (IR-010).

To add a command, define a CommandSpec and append it to COMMANDS. The parser is built from
this list, and every command's --help gets an "Example:" line from `example`.
"""

import argparse
from collections.abc import Callable
from dataclasses import dataclass

from ginn.cli.output import Output


@dataclass(frozen=True)
class Context:
    """Per-invocation state passed to every command."""

    output: Output


@dataclass(frozen=True)
class CommandSpec:
    """One `ginn` subcommand.

    `example` is a single invocation such as "ginn list --json"; the word "Example:" is added
    when the help text is rendered.
    """

    name: str
    help: str
    example: str
    configure: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace, Context], int]


COMMANDS: list[CommandSpec] = []
