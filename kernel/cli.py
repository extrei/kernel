"""Command-line interface for the local state tree."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .kernel import StateTreeError, initialize


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="state-tree",
        description="Create and inspect a project's local accepted-state tree.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser(
        "init",
        help="create the local state tree for an existing project",
    )
    init_parser.add_argument(
        "project",
        nargs="?",
        default=".",
        help="existing project directory (default: current directory)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``state-tree`` command and return its exit status."""

    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "init":
        try:
            result = initialize(arguments.project)
        except StateTreeError as error:
            print(f"state-tree: {error}", file=sys.stderr)
            return 2

        action = "Initialized" if result.created else "Already initialized"
        print(f"{action} state tree at {result.state_tree}")
        return 0

    parser.error(f"Unknown command: {arguments.command}")
    return 2
