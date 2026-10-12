# SPDX-License-Identifier: Apache-2.0
"""Parse arguments, dispatch to a command and render failures (IR-010, IR-011, NFR-052).

Each failure exits with the status of its error-catalogue entry. The exit-status categories are
listed in ginn.errors.EXIT_CATEGORIES.

Missing and unknown commands are detected before argparse runs, so their message text does not
depend on the Python version. Help and version flags count only before `--`. After `--` every
token is a positional, so the first token after `--` is the command candidate when no command
name comes before it.
"""

import argparse
import sys
import traceback
from collections.abc import Sequence
from typing import NoReturn

from ginn import __version__
from ginn.cli import commands as registry
from ginn.cli.commands import CommandSpec, Context
from ginn.cli.output import Output
from ginn.errors import GinnError, make

HELP_OR_VERSION = frozenset({"-h", "--help", "--version"})
ROOT_FLAGS = frozenset({"--json", "--verbose"})
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
    Abbreviations are rejected (allow_abbrev=False), so `--js` is an unknown option, not `--json`.
    """
    parser = _Parser(
        prog="ginn",
        description="Ginnungagap: isolated per-distribution Realms on rootless Podman.",
        epilog=MAIN_EPILOG,
        formatter_class=_HELP_FORMAT,
        allow_abbrev=False,
    )
    _add_json_verbose(parser, default=False)
    parser.add_argument("--version", action="version", version=f"ginn {__version__}")
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="COMMAND",
        required=True,
        help="command to run; see 'ginn COMMAND --help'",
    )
    for spec in registry.COMMANDS:
        _check_example(spec)
        sub = subparsers.add_parser(
            spec.name,
            help=spec.help,
            description=spec.help,
            epilog=f"Example: {spec.example}",
            formatter_class=_HELP_FORMAT,
            allow_abbrev=False,
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
    # Assumes every root option is a flag (nargs == 0). test_root_options_are_flags_only pins
    # this. A root option that took a value would make its value look like the command name.
    for word in tokens:
        if not word.startswith("-"):
            return word
    return None


def _first_after_separator(tokens: Sequence[str]) -> str | None:
    """Return the first token after `--`, or None when there is none."""
    if "--" not in tokens:
        return None
    rest = tokens[tokens.index("--") + 1 :]
    return rest[0] if rest else None


def _reject_unknown_root_option(before: Sequence[str]) -> None:
    """Report an unknown root option before the missing-command error."""
    for word in before:
        if word.startswith("-") and word != "-" and word not in ROOT_FLAGS:
            raise make("GINN-E-USAGE", detail=f"unknown option '{word}'")


def run(argv: Sequence[str] | None = None) -> int:
    """Run one ginn invocation and return the process exit status.

    Failures go to Output.error and exit with the catalogue status. An unexpected exception
    becomes GINN-E-INTERNAL (70). Under --verbose its traceback is written before the error.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    before_sep = _before_separator(tokens)
    # Pre-scan the global flags so failures before parsing (for example a registry error) still
    # honour --json and --verbose.
    out = Output(json_mode="--json" in before_sep, verbose="--verbose" in before_sep)
    try:
        parser = build_parser()
        names = {spec.name: spec for spec in registry.COMMANDS}
        first = _first_positional(before_sep)
        if first is None:
            first = _first_after_separator(tokens)
        if first is None and HELP_OR_VERSION.isdisjoint(before_sep):
            _reject_unknown_root_option(before_sep)
            if not out.json_mode:
                parser.print_help(sys.stderr)
            raise make("GINN-E-USAGE", detail="missing command")
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
        interrupted = make("GINN-E-INTERRUPTED")
        out.error(interrupted)
        return interrupted.exit_code
    except Exception:
        # Last-resort boundary: any unexpected failure becomes GINN-E-INTERNAL. The traceback
        # goes first so that the error object stays the last line on stderr.
        out.verbose_line(traceback.format_exc())
        internal = make("GINN-E-INTERNAL")
        out.error(internal)
        return internal.exit_code
