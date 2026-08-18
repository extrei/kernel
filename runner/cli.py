"""Command-line entry point for the bounded model runner."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from kernel import BlueprintError, StateTreeError

from .architect import compose
from .loop import run
from .providers import (
    APIProvider,
    ClaudeCodeProvider,
    CodexProvider,
    JSON_PATCH_OUTPUT_SCHEMA,
    Provider,
    ProviderError,
)


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kernel-run",
        description="Run a bounded model loop over one project State Tree.",
    )
    parser.add_argument("project", help="project containing .state-tree")
    parser.add_argument("--task", required=True, help="task for the Architect and workers")
    parser.add_argument("--task-id", required=True, help="ledger task identifier")
    parser.add_argument(
        "--provider",
        choices=("api", "claude-code", "codex"),
        default="api",
        help="worker completion provider (default: api)",
    )
    parser.add_argument(
        "--max-steps",
        type=_positive_integer,
        default=40,
        help="maximum worker attempts (default: 40)",
    )
    parser.add_argument(
        "--compose",
        action="store_true",
        help="replace an existing Blueprint through the Architect",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compose and validate a Blueprint without installing it",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.dry_run:
            document = compose(
                arguments.project,
                arguments.task,
                task_id=arguments.task_id,
            )
            print(_canonical_json(document))
            return 0

        provider = _worker_provider(arguments.provider, arguments.project)
        provider.preflight()
        result = run(
            arguments.project,
            arguments.task,
            task_id=arguments.task_id,
            provider=provider,
            max_steps=arguments.max_steps,
            compose=arguments.compose,
        )
    except (BlueprintError, ProviderError, StateTreeError, ValueError) as error:
        print(f"kernel-run: {error}", file=sys.stderr)
        return 2
    print(_canonical_json(result))
    return 0


def _worker_provider(name: str, project: str) -> Provider:
    if name == "claude-code":
        return ClaudeCodeProvider(project)
    if name == "codex":
        return CodexProvider(project)
    return APIProvider(output_schema=JSON_PATCH_OUTPUT_SCHEMA)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
