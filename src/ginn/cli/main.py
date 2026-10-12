# SPDX-License-Identifier: Apache-2.0
"""Parse arguments, dispatch to a command and render failures (IR-010, IR-011, NFR-052).

Exit statuses: 0 success, 2 usage or validation, 130 interrupted, 70 internal error, and the
per-code status from the error catalogue. Usage problems are detected before argparse runs
so the message text does not depend on the Python version.
"""

import argparse
import sys
import traceback
from collections.abc import Sequence
from typing import NoReturn

from ginn import __version__
from ginn.cli.commands import COMMANDS, CommandSpec, Context
from ginn.cli.output import Output
from ginn.errors import GinnError, make

HELP_OR_VERSION = frozenset({"-h", "--help", "--version"})
MAIN_EPILOG = "Example: ginn --version"
_HELP_FORMAT = argparse.RawDescriptionHelpFormatter


class _ParserExit(Exception):
    """Carries the status of a help or version request back to run()."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that raises instead of printing usage or exiting the process."""

    def error(self, message: str) -> NoReturn:
        raise make("GINN-E-USAGE", detail=message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            sys.stderr.write(message)
        raise _ParserExit(status)


def _add_json_verbose(parser: argparse.ArgumentParser, default: object) -> None:
    """Add --json and --verbose. Subcommand copies pass SUPPRESS so they keep the root value."""
    parser.add_argument(
        "--json",
        action="store_true",
        default=default,
        help="print machine-readable JSON output (AS-025)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=default,
        help="echo every runtime and package-manager command to stderr (NFR-042)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the root parser with global options and one subparser per registered command.

    --json and --verbose are accepted before and after the command name. The subcommand copies
    use default=SUPPRESS, so `ginn --json list` is not reset to False by the subparser.
    """
    parser = _Parser(
        prog="ginn",
        description="Ginnungagap: isolated per-distribution Realms on rootless Podman.",
        epilog=MAIN_EPILOG,
        formatter_class=_HELP_FORMAT,
    )
    _add_json_verbose(parser, default=False)
    parser.add_argument("--version", action="version", version=f"ginn {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    for spec in COMMANDS:
        _check_example(spec)
        sub = subparsers.add_parser(
            spec.name,
            help=spec.help,
            description=spec.help,
            epilog=f"Example: {spec.example}",
            formatter_class=_HELP_FORMAT,
        )
        _add_json_verbose(sub, default=argparse.SUPPRESS)
        spec.configure(sub)
    return parser


def _check_example(spec: CommandSpec) -> None:
    if not spec.example.strip():
        raise RuntimeError(f"command {spec.name!r} has no example (NFR-052)")


def _before_separator(tokens: Sequence[str]) -> list[str]:
    """Return the tokens before `--`, the part where global flags may appear."""
    return list(tokens[: tokens.index("--")]) if "--" in tokens else list(tokens)


def _first_positional(tokens: Sequence[str]) -> str | None:
    for word in tokens:
        if not word.startswith("-"):
            return word
    return None


def run(argv: Sequence[str] | None = None) -> int:
    """Run one ginn invocation and return the process exit status."""
    tokens = list(sys.argv[1:] if argv is None else argv)
    out = Output(json_mode="--json" in _before_separator(tokens), verbose=False)
    parser = build_parser()
    try:
        first = _first_positional(tokens)
        if first is None and HELP_OR_VERSION.isdisjoint(tokens):
            if not out.json_mode:
                parser.print_help(sys.stderr)
            raise make("GINN-E-USAGE", detail="missing command")
        names = {spec.name: spec for spec in COMMANDS}
        if first is not None and first not in names:
            raise make("GINN-E-USAGE", detail=f"unknown command '{first}'")
        args = parser.parse_args(tokens)
        out = Output(json_mode=bool(args.json), verbose=bool(args.verbose))
        spec = names.get(str(args.command))
        if spec is None:
            raise make("GINN-E-INTERNAL")
        return spec.run(args, Context(output=out))
    except _ParserExit as exc:
        return exc.status
    except GinnError as err:
        out.error(err)
        return err.exit_code
    except KeyboardInterrupt:
        out.error(make("GINN-E-INTERRUPTED"))
        return 130
    except Exception:
        # Last-resort boundary: any unexpected failure becomes GINN-E-INTERNAL.
        out.error(make("GINN-E-INTERNAL"))
        out.verbose_line(traceback.format_exc())
        return 70
