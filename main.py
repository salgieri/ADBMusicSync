"""MTPSync - Entry point."""

import sys

from mtpsync.cli import run


def main():
    """Console script entry point."""
    exit_code = run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()