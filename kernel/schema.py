"""Draft 2020-12 schema authority for accepted project snapshots."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as JSONSchemaDefinitionError
from jsonschema.exceptions import ValidationError

from .jsonpatch import PatchError, _array_index, _pointer_tokens
from .kernel import (
    STATE_TREE_DIRECTORY,
    StateTreeError,
    _canonical_json_bytes,
    _is_collection_reference,
    _object_directory,
    _read_collection_object,
    _read_json_mapping_object,
    _read_snapshot_object,
    _resolve_project_root,
    _store_object_at,
    _validated_kernel_state,
    entries,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SCHEMA_MAP_KEYWORDS = (
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
)
_SCHEMA_SINGLE_KEYWORDS = (
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_SCHEMA_LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")


class SchemaError(StateTreeError):
    """Raised when a schema or candidate snapshot is not acceptable."""


def schema(project_root: str | Path) -> dict[str, Any] | None:
    """Return the Draft 2020-12 schema currently in force, if any."""

    from .blueprint import _blueprint_authorities, blueprint

    current_schema, _ = _blueprint_authorities(blueprint(project_root))
    return deepcopy(current_schema)


def validate(
    document: dict[str, Any], schema: dict[str, Any] | None
) -> None:
    """Raise SchemaError unless document satisfies a valid Draft 2020-12 schema."""

    if not isinstance(document, dict):
        raise SchemaError("schema validation requires a JSON object")
    _check_schema(schema)
    if schema is None:
        return
    try:
        Draft202012Validator(schema).validate(document)
    except ValidationError as error:
        raise SchemaError(_validation_message(error)) from error
    except Exception as error:
        raise SchemaError(f"schema evaluation failed: {error}") from error


def audit_schema(project_root: str | Path) -> list[dict[str, Any]]:
    """Re-evaluate every recorded snapshot against its entry's schema."""

    root = _resolve_project_root(project_root)
    ledger_entries = entries(root)
    object_directory = _object_directory(root / STATE_TREE_DIRECTORY)
    from .blueprint import (
        BlueprintError,
        _blueprint_authorities,
        _read_blueprint_object,
    )

    results: list[dict[str, Any]] = []
    for entry in ledger_entries:
        document = _read_snapshot_object(
            object_directory,
            entry["state"],
            label=f"ledger sequence {entry['sequence']} state",
        )
        document = _hydrate_collections(document, object_directory)
        try:
            entry_blueprint = _read_blueprint_object(
                object_directory, entry["blueprint"]
            )
            entry_schema, _ = _blueprint_authorities(entry_blueprint)
            validate(document, entry_schema)
        except (BlueprintError, SchemaError) as error:
            valid = False
            message: str | None = str(error)
        else:
            valid = True
            message = None
        results.append(
            {
                "blueprint": entry["blueprint"],
                "error": message,
                "sequence": entry["sequence"],
                "state": entry["state"],
                "valid": valid,
            }
        )
    return results


def collection(project_root: str | Path, pointer: str) -> list[Any]:
    """Resolve one collection reference from the current persisted Snapshot."""

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    kernel_state = _validated_kernel_state(state_tree)
    object_directory = _object_directory(state_tree)
    snapshot = _read_snapshot_object(
        object_directory,
        kernel_state["state_head"],
        label="current state",
    )
    try:
        value = _resolve_document_pointer(snapshot, pointer)
    except PatchError as error:
        raise SchemaError(f"invalid collection pointer: {error}") from error
    if not _is_collection_reference(value):
        raise SchemaError(f"path is not a collection reference: {pointer}")
    hydrated = _hydrate_collections(value, object_directory)
    if not isinstance(hydrated, list):
        raise SchemaError(f"path does not resolve to a collection: {pointer}")
    return hydrated


def _read_schema_object(
    object_directory: Path, reference: str | None
) -> dict[str, Any] | None:
    if reference is None:
        return None
    return _read_json_mapping_object(
        object_directory, reference, label="schema"
    )


def _check_schema(schema: dict[str, Any] | None) -> None:
    if schema is None:
        return
    if not isinstance(schema, dict):
        raise SchemaError("schema must be a JSON object or null")
    try:
        Draft202012Validator.check_schema(schema)
    except JSONSchemaDefinitionError as error:
        raise SchemaError(f"invalid Draft 2020-12 schema: {error.message}") from error
    _check_collection_annotations(schema, root_schema=schema)


def _validation_message(error: ValidationError) -> str:
    location = error.json_path
    return f"snapshot violates schema at {location}: {error.message}"


def _hydrate_collections(
    value: Any,
    object_directory: Path,
    *,
    seen: set[str] | None = None,
) -> Any:
    active = set() if seen is None else seen
    if _is_collection_reference(value):
        reference = value["$collection"]
        if reference in active:
            raise SchemaError("collection references form a cycle")
        stored = _read_collection_object(
            object_directory, reference, label="collection"
        )
        active.add(reference)
        try:
            return _hydrate_collections(
                stored, object_directory, seen=active
            )
        finally:
            active.remove(reference)
    if isinstance(value, dict):
        return {
            key: _hydrate_collections(child, object_directory, seen=active)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _hydrate_collections(child, object_directory, seen=active)
            for child in value
        ]
    return deepcopy(value)


def _externalize_collections(
    document: dict[str, Any],
    schema: dict[str, Any] | None,
    object_directory: Path,
) -> dict[str, Any]:
    if schema is None:
        return deepcopy(document)
    value = _externalize_value(
        document,
        schema,
        root_schema=schema,
        object_directory=object_directory,
    )
    if not isinstance(value, dict):
        raise SchemaError("state snapshot must remain a JSON object")
    return value


def _externalize_value(
    value: Any,
    schema_node: Any,
    *,
    root_schema: dict[str, Any],
    object_directory: Path,
) -> Any:
    effective = _resolve_schema_node(root_schema, schema_node)
    if isinstance(effective, dict) and effective.get("x-kernel-collection") is True:
        if not isinstance(value, list):
            raise SchemaError("x-kernel-collection can only store a JSON array")
        stored = _externalize_array_items(
            value,
            effective,
            root_schema=root_schema,
            object_directory=object_directory,
        )
        reference = _store_object_at(
            object_directory, _canonical_json_bytes(stored)
        )
        return {"$collection": reference}

    if isinstance(value, dict):
        properties = (
            effective.get("properties", {}) if isinstance(effective, dict) else {}
        )
        additional = (
            effective.get("additionalProperties")
            if isinstance(effective, dict)
            else None
        )
        result: dict[str, Any] = {}
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is None and isinstance(additional, (dict, bool)):
                child_schema = additional
            result[key] = _externalize_value(
                child,
                child_schema,
                root_schema=root_schema,
                object_directory=object_directory,
            )
        return result
    if isinstance(value, list):
        return _externalize_array_items(
            value,
            effective,
            root_schema=root_schema,
            object_directory=object_directory,
        )
    return deepcopy(value)


def _externalize_array_items(
    value: list[Any],
    schema_node: Any,
    *,
    root_schema: dict[str, Any],
    object_directory: Path,
) -> list[Any]:
    if not isinstance(schema_node, dict):
        return deepcopy(value)
    prefix = schema_node.get("prefixItems", [])
    items = schema_node.get("items")
    result = []
    for index, child in enumerate(value):
        child_schema = prefix[index] if index < len(prefix) else items
        result.append(
            _externalize_value(
                child,
                child_schema,
                root_schema=root_schema,
                object_directory=object_directory,
            )
        )
    return result


def _resolve_schema_node(root_schema: dict[str, Any], schema_node: Any) -> Any:
    if not isinstance(schema_node, dict):
        return schema_node
    current = dict(schema_node)
    seen: set[str] = set()
    while True:
        reference = current.get("$ref")
        if (
            not isinstance(reference, str)
            or not reference.startswith("#")
            or reference in seen
        ):
            return current
        try:
            target = _resolve_document_pointer(root_schema, reference[1:])
        except PatchError:
            return current
        if not isinstance(target, dict):
            return current
        seen.add(reference)
        siblings = {key: value for key, value in current.items() if key != "$ref"}
        current = dict(target)
        current.update(siblings)


def _check_collection_annotations(
    value: Any, *, root_schema: dict[str, Any]
) -> None:
    if not isinstance(value, dict):
        return
    if "x-kernel-collection" in value:
        annotation = value["x-kernel-collection"]
        if not isinstance(annotation, bool):
            raise SchemaError("x-kernel-collection must be a boolean")
        effective = _resolve_schema_node(root_schema, value)
        schema_type = effective.get("type") if isinstance(effective, dict) else None
        permits_array = schema_type == "array" or (
            isinstance(schema_type, list) and "array" in schema_type
        )
        if annotation and not permits_array:
            raise SchemaError(
                "x-kernel-collection requires a schema with type array"
            )

    for keyword in _SCHEMA_MAP_KEYWORDS:
        children = value.get(keyword)
        if isinstance(children, dict):
            for child in children.values():
                _check_collection_annotations(child, root_schema=root_schema)
    for keyword in _SCHEMA_SINGLE_KEYWORDS:
        _check_collection_annotations(value.get(keyword), root_schema=root_schema)
    for keyword in _SCHEMA_LIST_KEYWORDS:
        children = value.get(keyword)
        if isinstance(children, list):
            for child in children:
                _check_collection_annotations(child, root_schema=root_schema)


def _resolve_document_pointer(document: Any, pointer: str) -> Any:
    current = document
    for token in _pointer_tokens(pointer):
        if isinstance(current, dict):
            if token not in current:
                raise PatchError(f"object member does not exist: {token}")
            current = current[token]
        elif isinstance(current, list):
            current = current[_array_index(token, len(current), allow_end=False)]
        else:
            raise PatchError("path traverses a scalar value")
    return current


def _validate_identifier(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SchemaError(
            f"{label} must be 1-64 letters, digits, dots, hyphens, or underscores"
        )
