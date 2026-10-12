# SPDX-License-Identifier: Apache-2.0
"""Entry point for `python -m ginn` and the `ginn` console script."""

from ginn.cli.main import run


def main(argv: list[str] | None = None) -> int:
    """Run the ginn CLI and return its exit status."""
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
