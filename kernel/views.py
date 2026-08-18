"""Deterministic actor views over accepted state snapshots."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Any

from .blueprint import (
    BlueprintError,
    _blueprint_authorities,
    _read_blueprint_object,
)
from .contracts import (
    ContractError,
    _check_contract,
    _pattern_matches,
)
from .jsonpatch import PatchError, _pointer_tokens
from .kernel import (
    HASH_ALGORITHM,
    STATE_TREE_DIRECTORY,
    StateTreeError,
    _canonical_json_bytes,
    _kernel_lock,
    _object_directory,
    _read_snapshot_object,
    _resolve_project_root,
    _store_object_at,
    _validated_kernel_state,
    entries,
)
from .schema import _resolve_schema_node

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MISSING = object()
_SCHEMA_KEY = "$schema"
_COLLECTION_KEY = "$collection"


class ViewError(StateTreeError):
    """Raised when an actor View cannot be derived or represented safely."""


class StaleViewError(ViewError):
    """Raised when a supplied View is not the one derived from its parent state."""


@dataclass(frozen=True)
class ViewRecord:
    """One derived View and the authority references used to construct it."""

    project_root: Path
    actor: str
    document: dict[str, Any]
    view_hash: str
    state: str
    blueprint: str | None


def view(
    project_root: str | Path,
    *,
    actor: str,
    at: str | None = None,
) -> ViewRecord:
    """Derive and store one actor's View of current or referenced state."""

    _validate_actor(actor)
    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    with _kernel_lock(state_tree):
        kernel_state = _validated_kernel_state(state_tree)
        object_directory = _object_directory(state_tree)
        state_reference = kernel_state["state_head"] if at is None else at
        snapshot = _read_snapshot_object(
            object_directory, state_reference, label="view state"
        )
        blueprint_reference = kernel_state["blueprint_head"]
        current_blueprint = _read_blueprint_object(
            object_directory, blueprint_reference
        )
        schema, contract = _blueprint_authorities(current_blueprint)
        document, elisions = _derive_view(snapshot, contract, schema, actor)

        for reference, content in sorted(elisions.items()):
            if _store_object_at(object_directory, content) != reference:
                raise ViewError("elided value did not retain its derived hash")
        view_hash = _store_object_at(
            object_directory, _canonical_view_bytes(document)
        )

    return ViewRecord(
        project_root=root,
        actor=actor,
        document=document,
        view_hash=view_hash,
        state=state_reference,
        blueprint=blueprint_reference,
    )


def derive_view(
    snapshot: dict[str, Any],
    contract: dict[str, Any] | None,
    schema: dict[str, Any] | None,
    actor: str,
) -> dict[str, Any]:
    """Purely derive an actor View from its four authoritative inputs."""

    document, _ = _derive_view(snapshot, contract, schema, actor)
    return document


def audit_views(project_root: str | Path) -> list[dict[str, Any]]:
    """Re-evaluate recorded patch Views against their entry authorities."""

    root = _resolve_project_root(project_root)
    ledger_entries = entries(root)
    object_directory = _object_directory(root / STATE_TREE_DIRECTORY)
    results: list[dict[str, Any]] = []
    for entry in ledger_entries:
        is_patch = entry["kind"] == "patch" or entry["parent_state"] != entry["state"]
        expected: str | None = None
        message: str | None = None
        valid = True
        if is_patch and entry["blueprint"] is not None:
            try:
                snapshot = _read_snapshot_object(
                    object_directory,
                    entry["parent_state"],
                    label=f"ledger sequence {entry['sequence']} parent state",
                )
                entry_blueprint = _read_blueprint_object(
                    object_directory, entry["blueprint"]
                )
                schema, contract = _blueprint_authorities(entry_blueprint)
                document = derive_view(
                    snapshot, contract, schema, entry["actor"]
                )
                expected = _view_reference(document)
                if entry["view"] is None:
                    valid = not _view_required(contract, entry["actor"])
                    if not valid:
                        message = "contract budget required a view"
                elif entry["view"] != expected:
                    valid = False
                    message = "recorded view does not match its parent state"
            except (BlueprintError, ViewError) as error:
                valid = False
                message = str(error)
        results.append(
            {
                "actor": entry["actor"],
                "error": message,
                "expected": expected,
                "patch": is_patch,
                "sequence": entry["sequence"],
                "valid": valid,
                "view": entry["view"],
            }
        )
    return results


def _derive_view(
    snapshot: dict[str, Any],
    contract: dict[str, Any] | None,
    schema: dict[str, Any] | None,
    actor: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not isinstance(snapshot, dict):
        raise ViewError("view snapshot must be a JSON object")
    if schema is not None and not isinstance(schema, dict):
        raise ViewError("view schema must be a JSON object or null")
    _validate_actor(actor)
    try:
        _check_contract(contract)
    except ContractError as error:
        raise ViewError(str(error)) from error

    if contract is None:
        document = deepcopy(snapshot)
        selected_paths = [""]
        candidates = _root_candidates(snapshot)
        budget = None
    else:
        actor_rule = contract["actors"].get(actor)
        if actor_rule is None:
            raise ViewError(f"actor {actor!r} is absent from the active contract")
        patterns = actor_rule.get("read", [])
        projected, selected_paths, candidates = _project_value(
            snapshot, "", patterns
        )
        document = {} if projected is _MISSING else projected
        budget = actor_rule.get("budget")

    if not isinstance(document, dict):
        raise ViewError("derived view must remain a JSON object")
    if schema is not None:
        if _SCHEMA_KEY in document:
            raise ViewError("state path '$schema' collides with view schema metadata")
        document[_SCHEMA_KEY] = _schema_fragment(schema, selected_paths)
        candidates.append(f"/{_SCHEMA_KEY}")

    return _apply_budget(document, budget, candidates)


def _project_value(
    value: Any,
    pointer: str,
    patterns: list[str],
) -> tuple[Any, list[str], list[str]]:
    if any(_pattern_matches(pattern, pointer) for pattern in patterns):
        return deepcopy(value), [pointer], _selected_candidates(pointer, value)

    if _is_collection_reference(value):
        if any(_pattern_descends_from(pattern, pointer) for pattern in patterns):
            return deepcopy(value), [pointer], [pointer]
        return _MISSING, [], []

    if isinstance(value, dict):
        document: dict[str, Any] = {}
        selected: list[str] = []
        candidates: list[str] = []
        for key in sorted(value):
            child_pointer = _join_pointer(pointer, key)
            child, child_selected, child_candidates = _project_value(
                value[key], child_pointer, patterns
            )
            if child is not _MISSING:
                document[key] = child
                selected.extend(child_selected)
                candidates.extend(child_candidates)
        if document:
            return document, selected, candidates
        return _MISSING, [], []

    if isinstance(value, list):
        included: dict[int, Any] = {}
        selected = []
        candidates = []
        for index, child_value in enumerate(value):
            child_pointer = _join_pointer(pointer, str(index))
            child, child_selected, child_candidates = _project_value(
                child_value, child_pointer, patterns
            )
            if child is not _MISSING:
                included[index] = child
                selected.extend(child_selected)
                candidates.extend(child_candidates)
        if not included:
            return _MISSING, [], []
        document = [None] * (max(included) + 1)
        for index, child in included.items():
            document[index] = child
        return document, selected, candidates

    return _MISSING, [], []


def _schema_fragment(
    root_schema: dict[str, Any], selected_paths: list[str]
) -> dict[str, Any]:
    return {
        pointer: deepcopy(_schema_at_pointer(root_schema, pointer))
        for pointer in sorted(set(selected_paths))
    }


def _schema_at_pointer(root_schema: dict[str, Any], pointer: str) -> Any:
    current: Any = root_schema
    try:
        tokens = _pointer_tokens(pointer)
    except PatchError as error:
        raise ViewError(f"cannot resolve view schema path: {error}") from error
    for token in tokens:
        current = _resolve_schema_node(root_schema, current)
        if current is True or current is None:
            return True
        if current is False or not isinstance(current, dict):
            return {}

        properties = current.get("properties", {})
        patterns = current.get("patternProperties", {})
        matches = []
        if isinstance(properties, dict) and token in properties:
            matches.append(properties[token])
        if isinstance(patterns, dict):
            matches.extend(
                child
                for expression, child in patterns.items()
                if re.search(expression, token)
            )
        if matches:
            current = matches[0] if len(matches) == 1 else {"allOf": matches}
            continue

        prefix = current.get("prefixItems", [])
        items = current.get("items", True)
        if token.isascii() and token.isdigit():
            index = int(token)
            if isinstance(prefix, list) and index < len(prefix):
                current = prefix[index]
            else:
                current = items
            continue
        current = current.get("additionalProperties", True)
    return _resolve_schema_node(root_schema, current)


def _apply_budget(
    document: dict[str, Any],
    budget: int | None,
    candidate_paths: list[str],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    result = deepcopy(document)
    if budget is None or _canonical_characters(result) <= budget:
        _canonical_view_bytes(result)
        return result, {}

    candidates = []
    for pointer in sorted(set(candidate_paths)):
        value = _value_at_pointer(result, pointer)
        if value is _MISSING:
            continue
        content = _canonical_value_bytes(value)
        reference = f"{HASH_ALGORITHM}:{sha256(content).hexdigest()}"
        marker = {"$elided": {"bytes": len(content), "hash": reference}}
        candidates.append((-len(content), pointer, content, marker, reference))

    elisions: dict[str, bytes] = {}
    for _, pointer, original_content, marker, reference in sorted(candidates):
        current = _value_at_pointer(result, pointer)
        if current is _MISSING or _canonical_value_bytes(current) != original_content:
            continue
        before = _canonical_characters(result)
        candidate = deepcopy(result)
        _replace_at_pointer(candidate, pointer, marker)
        after = _canonical_characters(candidate)
        if after >= before:
            continue
        result = candidate
        elisions[reference] = original_content
        if after <= budget:
            return result, elisions

    raise ViewError(
        f"derived view cannot fit its {budget}-character budget"
    )


def _view_required(contract: dict[str, Any] | None, actor: str) -> bool:
    if contract is None:
        return False
    actor_rule = contract["actors"].get(actor)
    return actor_rule is not None and actor_rule.get("budget") is not None


def _view_reference(document: dict[str, Any]) -> str:
    return f"{HASH_ALGORITHM}:{sha256(_canonical_view_bytes(document)).hexdigest()}"


def _canonical_view_bytes(document: dict[str, Any]) -> bytes:
    if not isinstance(document, dict):
        raise ViewError("view document must be a JSON object")
    return _canonical_value_bytes(document)


def _canonical_value_bytes(value: Any) -> bytes:
    try:
        return _canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise ViewError("view contains non-canonical JSON values") from error


def _canonical_characters(value: Any) -> int:
    return len(_canonical_value_bytes(value).decode("utf-8"))


def _root_candidates(snapshot: dict[str, Any]) -> list[str]:
    return [_join_pointer("", key) for key in sorted(snapshot)]


def _selected_candidates(pointer: str, value: Any) -> list[str]:
    if pointer == "" and isinstance(value, dict):
        return _root_candidates(value)
    return [pointer]


def _pattern_descends_from(pattern: str, pointer: str) -> bool:
    pattern_tokens = _pointer_tokens(pattern)
    pointer_tokens = _pointer_tokens(pointer)
    return len(pattern_tokens) > len(pointer_tokens) and all(
        granted == "*" or granted == actual
        for granted, actual in zip(
            pattern_tokens, pointer_tokens, strict=False
        )
    )


def _join_pointer(pointer: str, token: str) -> str:
    encoded = token.replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{encoded}"


def _value_at_pointer(document: Any, pointer: str) -> Any:
    current = document
    try:
        tokens = _pointer_tokens(pointer)
    except PatchError:
        return _MISSING
    for token in tokens:
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isascii() and token.isdigit():
            index = int(token)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _replace_at_pointer(document: Any, pointer: str, value: Any) -> None:
    tokens = _pointer_tokens(pointer)
    if not tokens:
        raise ViewError("the View root cannot be elided")
    parent = document
    for token in tokens[:-1]:
        parent = parent[int(token)] if isinstance(parent, list) else parent[token]
    token = tokens[-1]
    if isinstance(parent, list):
        parent[int(token)] = value
    else:
        parent[token] = value


def _is_collection_reference(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {_COLLECTION_KEY}


def _validate_actor(actor: Any) -> None:
    if not isinstance(actor, str) or not _IDENTIFIER.fullmatch(actor):
        raise ViewError(
            "actor must be 1-64 letters, digits, dots, hyphens, or underscores"
        )
