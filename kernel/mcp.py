"""Local stdio MCP boundary for one project kernel and one agent identity."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .controller import read_artifact as read_artifact_bytes
from .controller import record_step
from .kernel import StateTreeError, verify
from .state import apply_patch
from .views import view as derive_actor_view

PROJECT_ENVIRONMENT_VARIABLE = "KERNEL_PROJECT"
ACTOR_ENVIRONMENT_VARIABLE = "KERNEL_ACTOR"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SERVER_INSTRUCTIONS = (
    "This server is bound to one project-local kernel and one agent "
    "identity. Read the bound actor's current View with get_view, then change "
    "project state with submit_patch, quoting the state and view_hash that "
    "get_view returned. A refused patch is recorded in the ledger and raises "
    "with the stage that refused it. Submit finished project files with "
    "submit_step and share their content hashes with other agents through "
    "read_artifact. The actor identity is fixed when the server starts and "
    "must never come from a tool argument."
)


class MCPConfigurationError(RuntimeError):
    """Raised when the MCP process is not bound to a valid project and actor."""


@dataclass(frozen=True)
class ServerBinding:
    """The immutable authority assigned to one running MCP server."""

    project_root: Path
    actor: str


def create_server(
    project_root: str | Path | None = None,
    actor: str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> MCPServer:
    """Create a server whose tools resolve one launch-bound project and actor."""

    process_environment = os.environ if environment is None else environment
    server = MCPServer(
        "project-kernel",
        title="Project kernel",
        instructions=_SERVER_INSTRUCTIONS,
        version="0.6.0",
    )

    def binding() -> ServerBinding:
        return _resolve_binding(
            project_root,
            actor,
            environment=process_environment,
        )

    @server.tool(
        title="Inspect kernel status",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def kernel_status() -> dict[str, str]:
        """Verify the complete ledger and return this server's fixed binding."""

        current = binding()
        return {
            "actor": current.actor,
            "ledger_head": verify(current.project_root),
            "project_root": str(current.project_root),
        }

    @server.tool(
        title="Get actor view",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def get_view() -> dict[str, Any]:
        """Return the current View for this server's launch-bound actor."""

        current = binding()
        record = derive_actor_view(
            current.project_root,
            actor=current.actor,
        )
        return {
            "actor": record.actor,
            "blueprint": record.blueprint,
            "document": record.document,
            "state": record.state,
            "view_hash": record.view_hash,
        }

    @server.tool(
        title="Submit a patch",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def submit_patch(
        task_id: str,
        patch: list[dict[str, Any]],
        parent_state: str,
        view: str | None = None,
    ) -> dict[str, Any]:
        """Propose a JSON Patch against the state get_view reported.

        ``parent_state`` and ``view`` come from get_view. The patch is refused
        unless it satisfies patch syntax, this actor's write contract, the view
        it was prepared from, patch application, and the blueprint schema.
        """

        current = binding()
        record = apply_patch(
            current.project_root,
            actor=current.actor,
            task_id=task_id,
            patch=patch,
            parent_state=parent_state,
            view=view,
        )
        return {
            "actor": record.actor,
            "entry_hash": record.entry_hash,
            "kind": record.kind,
            "parent_state": record.parent_state,
            "patch_hash": record.patch_hash,
            "sequence": record.sequence,
            "state": record.state,
            "task_id": record.task_id,
            "view": record.view,
        }

    @server.tool(
        title="Submit a step",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def submit_step(
        task_id: str,
        artifact_path: str,
        kind: str,
    ) -> dict[str, str | int]:
        """Store one project file and append its immutable ledger step."""

        current = binding()
        record = record_step(
            current.project_root,
            agent=current.actor,
            task_id=task_id,
            artifact=artifact_path,
            kind=kind,
        )
        return {
            "actor": record.agent,
            "artifact_path": str(record.artifact_path),
            "content_hash": record.content_hash,
            "entry_hash": record.entry_hash,
            "kind": record.kind,
            "sequence": record.sequence,
            "size": record.size,
            "task_id": record.task_id,
        }

    @server.tool(
        title="Read an artifact",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    def read_artifact(content_hash: str) -> dict[str, str | int]:
        """Verify the ledger and return stored artifact bytes by SHA-256 reference."""

        current = binding()
        content = read_artifact_bytes(current.project_root, content_hash)
        try:
            value = content.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            value = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        return {
            "content": value,
            "content_hash": content_hash,
            "encoding": encoding,
            "size": len(content),
        }

    return server


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the stdio server."""

    parser = argparse.ArgumentParser(
        prog="kernel-mcp",
        description="Run a project-local kernel over MCP stdio.",
    )
    parser.add_argument(
        "--project",
        help="project containing .state-tree; required unless KERNEL_PROJECT is set",
    )
    parser.add_argument(
        "--actor",
        help="fixed agent identity; defaults to KERNEL_ACTOR",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the launch binding, then serve MCP over stdin and stdout."""

    arguments = build_parser().parse_args(argv)
    try:
        current = _resolve_binding(arguments.project, arguments.actor)
        verify(current.project_root)
    except (MCPConfigurationError, StateTreeError) as error:
        print(f"kernel-mcp: {error}", file=sys.stderr)
        return 2

    create_server(current.project_root, current.actor).run()
    return 0


def _resolve_binding(
    project_root: str | Path | None,
    actor: str | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> ServerBinding:
    process_environment = os.environ if environment is None else environment
    project_value = project_root or process_environment.get(PROJECT_ENVIRONMENT_VARIABLE)
    if not project_value:
        raise MCPConfigurationError("project must be fixed with --project or KERNEL_PROJECT")
    root = Path(project_value).expanduser().resolve()
    if not root.is_dir():
        raise MCPConfigurationError(f"project directory does not exist: {root}")

    actor_value = actor or process_environment.get(ACTOR_ENVIRONMENT_VARIABLE)
    if not isinstance(actor_value, str) or not _IDENTIFIER.fullmatch(actor_value):
        raise MCPConfigurationError(
            "actor must be fixed with --actor or KERNEL_ACTOR and contain "
            "1-64 letters, digits, dots, hyphens, or underscores"
        )
    return ServerBinding(root, actor_value)


# The module-level object supports `mcp run kernel/mcp.py` when the two
# KERNEL_* environment variables are supplied. Configuration is resolved
# lazily so importing the package cannot accidentally bind the wrong project.
mcp = create_server()


if __name__ == "__main__":
    raise SystemExit(main())
