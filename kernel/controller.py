"""Validated step ingress for agents working in one local project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .kernel import (
    STATE_TREE_DIRECTORY,
    StateTreeError,
    append_ledger_entry,
    read_object,
    store_object,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_KIND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")


class StepError(StateTreeError):
    """Raised when a step cannot safely enter the project state tree."""


@dataclass(frozen=True)
class StepRecord:
    """A content object and the durable ledger fact that records one step."""

    project_root: Path
    artifact_path: Path
    agent: str
    task_id: str
    kind: str
    content_hash: str
    entry_hash: str
    sequence: int
    size: int


def record_step(
    project_root: str | Path,
    *,
    agent: str,
    task_id: str,
    artifact: str | Path,
    kind: str,
) -> StepRecord:
    """Store one project file and append its immutable ledger step."""

    _validate_identifier(agent, label="agent")
    _validate_identifier(task_id, label="task id")
    if not isinstance(kind, str) or not _KIND.fullmatch(kind):
        raise StepError(
            "kind must be 1-32 letters, digits, dots, hyphens, or underscores"
        )

    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise StepError(f"Project directory does not exist: {root}")
    artifact_path = _resolve_artifact_path(root, artifact)

    try:
        content = artifact_path.read_bytes()
    except OSError as error:
        raise StepError(f"Artifact is unreadable: {artifact_path}") from error

    content_hash = store_object(root, content)
    relative_path = artifact_path.relative_to(root).as_posix()
    ledger_entry = append_ledger_entry(
        root,
        actor=agent,
        kind=kind,
        task_id=task_id,
        payload_hash=content_hash,
        metadata={
            "bytes": len(content),
            "name": artifact_path.name,
            "path": relative_path,
        },
    )
    return StepRecord(
        project_root=root,
        artifact_path=artifact_path,
        agent=agent,
        task_id=task_id,
        kind=kind,
        content_hash=content_hash,
        entry_hash=ledger_entry.entry_hash,
        sequence=ledger_entry.sequence,
        size=len(content),
    )


def read_artifact(project_root: str | Path, content_hash: str) -> bytes:
    """Read an artifact by content hash after verifying the complete ledger."""

    return read_object(project_root, content_hash)


def _resolve_artifact_path(root: Path, artifact: str | Path) -> Path:
    candidate = Path(artifact).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        artifact_path = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise StepError(f"Artifact does not exist: {candidate}") from error
    if not artifact_path.is_file():
        raise StepError(f"Artifact is not a regular file: {artifact_path}")

    try:
        artifact_path.relative_to(root)
    except ValueError as error:
        raise StepError("Artifact must be inside the project") from error

    state_tree = root / STATE_TREE_DIRECTORY
    try:
        artifact_path.relative_to(state_tree)
    except ValueError:
        return artifact_path
    raise StepError("Artifact cannot come from inside .state-tree")


def _validate_identifier(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise StepError(
            f"{label} must be 1-64 letters, digits, dots, hyphens, or underscores"
        )
