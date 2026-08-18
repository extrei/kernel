"""Command-line interface for the local state tree."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .kernel import StateTreeError, entries, initialize

HANDOFF_CONTRACT = """
Handoff contract (convention, not enforcement):
  1. Register the plan before the work starts.
  2. One task per handoff. A roadmap is not a task.
  3. The worker's first write is its result file, not its last.
  4. Register the result before starting the next task.
  5. A task with one ledger entry is an open task.

The kernel enforces none of this. It records only what you append.
""".strip()


def _nonnegative_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if limit < 0:
        raise argparse.ArgumentTypeError("limit must be non-negative")
    return limit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="state-tree",
        description="Create and read a project's local coordination ledger.",
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

    log_parser = commands.add_parser(
        "log",
        help="print verified ledger entries for a human reader",
    )
    log_parser.add_argument(
        "project",
        nargs="?",
        default=".",
        help="existing project directory (default: current directory)",
    )
    log_parser.add_argument(
        "--limit",
        type=_nonnegative_limit,
        metavar="N",
        help="print only the last N entries",
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
        print()
        print(HANDOFF_CONTRACT)
        return 0

    if arguments.command == "log":
        try:
            ledger_entries = entries(arguments.project)
        except StateTreeError as error:
            print(f"state-tree: {error}", file=sys.stderr)
            return 2

        if arguments.limit is not None:
            ledger_entries = ledger_entries[-arguments.limit :] if arguments.limit else []
        for entry in ledger_entries:
            print(
                f"{entry['sequence']} {entry['recorded_at']} {entry['actor']} "
                f"{entry['kind']} {entry['task_id']} {entry['payload_hash']}"
            )
        return 0

    parser.error(f"Unknown command: {arguments.command}")
    return 2
