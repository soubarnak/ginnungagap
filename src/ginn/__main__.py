# SPDX-License-Identifier: Apache-2.0
"""Process entry: `python -m ginn` and the `ginn` console script both reach main() here."""

from typing import NoReturn

from ginn.cli.main import run


def main(argv: list[str] | None = None) -> int:
    """Run one ginn invocation and return its exit status.

    With argv None, the arguments are read from sys.argv without the program name.
    """
    return run(argv)


def _exit_with_status() -> NoReturn:
    """Pass the status from main() to the interpreter when this file runs as a script."""
    raise SystemExit(main())


if __name__ == "__main__":
    _exit_with_status()
