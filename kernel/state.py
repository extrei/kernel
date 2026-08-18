"""Mutable JSON state committed as immutable snapshots and patch objects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .blueprint import _blueprint_authorities, _read_blueprint_object
from .contracts import UnauthorizedWriteError, authorize
from .jsonpatch import PatchError, apply_patch as apply_json_patch, touched_paths
from .kernel import (
    STATE_TREE_DIRECTORY,
    StateTreeError,
    _append_ledger_entry_locked,
    _canonical_json_bytes,
    _kernel_lock,
    _object_directory,
    _read_json_mapping_object,
    _read_snapshot_object,
    _resolve_project_root,
    _store_object_at,
    _validated_kernel_state,
)
from .schema import (
    SchemaError,
    _externalize_collections,
    _hydrate_collections,
    validate,
)
from .views import (
    StaleViewError,
    ViewError,
    _view_reference,
    _view_required,
    derive_view,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_KIND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_REJECTION_VERSION = 1


class StaleParentError(StateTreeError):
    """Raised when a patch was prepared from a state that is no longer current."""


@dataclass(frozen=True)
class PatchRecord:
    """The durable identities produced by one state patch commit."""

    project_root: Path
    actor: str
    task_id: str
    kind: str
    patch_hash: str
    parent_state: str
    state: str
    view: str | None
    entry_hash: str
    sequence: int


def state(project_root: str | Path, *, at: str | None = None) -> dict[str, Any]:
    """Read the current or explicitly referenced canonical state snapshot."""

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    kernel_state = _validated_kernel_state(state_tree)
    reference = kernel_state["state_head"] if at is None else at
    return _read_snapshot_object(
        _object_directory(state_tree), reference, label="state snapshot"
    )


def apply_patch(
    project_root: str | Path,
    *,
    actor: str,
    task_id: str,
    patch: Any,
    parent_state: str,
    view: str | None = None,
    kind: str = "patch",
) -> PatchRecord:
    """Compare parent_state and atomically commit a patch plus its new snapshot."""

    _validate_identifier(actor, label="actor")
    _validate_identifier(task_id, label="task id")
    if not isinstance(kind, str) or not _KIND.fullmatch(kind):
        raise PatchError(
            "kind must be 1-32 letters, digits, dots, hyphens, or underscores"
        )
    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    with _kernel_lock(state_tree):
        kernel_state = _validated_kernel_state(state_tree)
        object_directory = _object_directory(state_tree)

        try:
            patch_paths = touched_paths(patch)
        except PatchError as error:
            try:
                patch_hash = _store_candidate_patch(object_directory, patch)
            except Exception:
                pass
            else:
                _record_rejection_best_effort(
                    state_tree,
                    kernel_state,
                    actor=actor,
                    task_id=task_id,
                    patch_hash=patch_hash,
                    paths=[],
                    reason=str(error),
                    stage="syntax",
                    view=view,
                )
            raise

        patch_hash = _store_candidate_patch(object_directory, patch)

        try:
            if parent_state != kernel_state["state_head"]:
                raise StaleParentError(
                    f"parent state {parent_state!r} is not current state "
                    f"{kernel_state['state_head']!r}"
                )
        except StaleParentError as error:
            _record_rejection_best_effort(
                state_tree,
                kernel_state,
                actor=actor,
                task_id=task_id,
                patch_hash=patch_hash,
                paths=patch_paths,
                reason=str(error),
                stage="stale_parent",
                view=view,
            )
            raise

        blueprint_reference = kernel_state["blueprint_head"]
        current_blueprint = _read_blueprint_object(
            object_directory, blueprint_reference
        )
        current_schema, current_contract = _blueprint_authorities(
            current_blueprint
        )
        try:
            authorize(patch_paths, current_contract, actor=actor)
        except UnauthorizedWriteError as error:
            _record_rejection_best_effort(
                state_tree,
                kernel_state,
                actor=actor,
                task_id=task_id,
                patch_hash=patch_hash,
                paths=patch_paths,
                reason=str(error),
                stage="auth",
                view=view,
            )
            raise
        allow_remove = any(
            operation == "remove" for operation, _ in patch_paths
        )

        stored_current = _read_snapshot_object(
            object_directory, parent_state, label="parent state"
        )
        try:
            if current_contract is not None:
                if view is None and _view_required(current_contract, actor):
                    raise ViewError("active contract budget requires a view")
                if view is not None:
                    expected_view = _view_reference(
                        derive_view(
                            stored_current,
                            current_contract,
                            current_schema,
                            actor,
                        )
                    )
                    if view != expected_view:
                        raise StaleViewError(
                            "supplied view does not match the current parent state"
                        )
                    try:
                        _read_json_mapping_object(
                            object_directory, view, label="view"
                        )
                    except StateTreeError as error:
                        raise StaleViewError(
                            "supplied view object is unavailable or invalid"
                        ) from error
            elif view is not None:
                _read_json_mapping_object(
                    object_directory, view, label="view"
                )
        except StateTreeError as error:
            _record_rejection_best_effort(
                state_tree,
                kernel_state,
                actor=actor,
                task_id=task_id,
                patch_hash=patch_hash,
                paths=patch_paths,
                reason=str(error),
                stage="view",
                view=view,
            )
            raise

        current = _hydrate_collections(stored_current, object_directory)
        try:
            next_snapshot = apply_json_patch(
                current, patch, allow_remove=allow_remove
            )
            if not isinstance(next_snapshot, dict):
                raise PatchError("state snapshot must remain a JSON object")
        except PatchError as error:
            _record_rejection_best_effort(
                state_tree,
                kernel_state,
                actor=actor,
                task_id=task_id,
                patch_hash=patch_hash,
                paths=patch_paths,
                reason=str(error),
                stage="apply",
                view=view,
            )
            raise

        try:
            validate(next_snapshot, current_schema)
            stored_snapshot = _externalize_collections(
                next_snapshot, current_schema, object_directory
            )
        except SchemaError as error:
            _record_rejection_best_effort(
                state_tree,
                kernel_state,
                actor=actor,
                task_id=task_id,
                patch_hash=patch_hash,
                paths=patch_paths,
                reason=str(error),
                stage="schema",
                view=view,
            )
            raise

        try:
            state_content = _canonical_json_bytes(stored_snapshot)
        except (TypeError, ValueError) as error:
            patch_error = PatchError(
                "patch and state must contain canonical JSON values"
            )
            _record_rejection_best_effort(
                state_tree,
                kernel_state,
                actor=actor,
                task_id=task_id,
                patch_hash=patch_hash,
                paths=patch_paths,
                reason=str(patch_error),
                stage="apply",
                view=view,
            )
            raise patch_error from error

        state_hash = _store_object_at(object_directory, state_content)
        append = _append_ledger_entry_locked(
            state_tree,
            kernel_state,
            actor=actor,
            kind=kind,
            task_id=task_id,
            payload_hash=patch_hash,
            metadata={},
            parent_state=parent_state,
            state=state_hash,
            blueprint=blueprint_reference,
            view=view,
        )

    return PatchRecord(
        project_root=root,
        actor=actor,
        task_id=task_id,
        kind=kind,
        patch_hash=patch_hash,
        parent_state=parent_state,
        state=state_hash,
        view=view,
        entry_hash=append.entry_hash,
        sequence=append.sequence,
    )


def _store_candidate_patch(object_directory: Path, patch: Any) -> str:
    try:
        content = _canonical_json_bytes(patch)
    except (TypeError, ValueError) as error:
        raise PatchError(
            "patch and state must contain canonical JSON values"
        ) from error
    return _store_object_at(object_directory, content)


def _record_rejection_best_effort(
    state_tree: Path,
    kernel_state: dict[str, Any],
    *,
    actor: str,
    task_id: str,
    patch_hash: str,
    paths: list[tuple[str, str]],
    reason: str,
    stage: str,
    view: str | None,
) -> None:
    try:
        object_directory = _object_directory(state_tree)
        rejection = {
            "patch": patch_hash,
            "paths": [list(path) for path in paths],
            "reason": reason,
            "stage": stage,
            "version": _REJECTION_VERSION,
        }
        rejection_hash = _store_object_at(
            object_directory, _canonical_json_bytes(rejection)
        )
        current_state = kernel_state["state_head"]
        _append_ledger_entry_locked(
            state_tree,
            kernel_state,
            actor=actor,
            kind="rejection",
            task_id=task_id,
            payload_hash=rejection_hash,
            metadata={},
            parent_state=current_state,
            state=current_state,
            blueprint=kernel_state["blueprint_head"],
            view=view,
        )
    except Exception:
        return


def _validate_identifier(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PatchError(
            f"{label} must be 1-64 letters, digits, dots, hyphens, or underscores"
        )
