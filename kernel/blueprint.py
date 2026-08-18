"""Atomic Schema and Write Contract authority for one project."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .contracts import (
    ContractError,
    _check_contract,
    _check_schema_paths,
    _contract_pattern_tokens,
)
from .jsonpatch import PatchError
from .kernel import (
    STATE_TREE_DIRECTORY,
    StateTreeError,
    _GENESIS_STATE_REFERENCE,
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
    _check_schema,
    _externalize_collections,
    _hydrate_collections,
    _resolve_document_pointer,
    validate,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MINIMUM_ACTOR_BUDGET = 256

META_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "additionalProperties": False,
    "properties": {
        "circuit": {
            "additionalProperties": False,
            "properties": {
                "consecutive_rejections": {"type": "integer"},
                "cycle_window": {"type": "integer"},
            },
            "type": "object",
        },
        "contracts": {
            "additionalProperties": False,
            "properties": {
                "actors": {
                    "additionalProperties": {
                        "additionalProperties": False,
                        "properties": {
                            "allow_remove": {"type": "boolean"},
                            "budget": {
                                "minimum": _MINIMUM_ACTOR_BUDGET,
                                "type": "integer",
                            },
                            "read": {
                                "items": {"type": "string"},
                                "type": "array",
                            },
                            "write": {
                                "items": {"type": "string"},
                                "type": "array",
                            },
                        },
                        "type": "object",
                    },
                    "type": "object",
                },
                "version": {"type": "integer"},
            },
            "required": ["actors", "version"],
            "type": "object",
        },
        "initial_state": {"type": "object"},
        "rules": {
            "items": {
                "additionalProperties": False,
                "properties": {
                    "on": {
                        "additionalProperties": False,
                        "properties": {
                            "op": {"type": "string"},
                            "path": {"type": "string"},
                        },
                        "required": ["op", "path"],
                        "type": "object",
                    },
                    "wake": {"type": "string"},
                },
                "required": ["on", "wake"],
                "type": "object",
            },
            "type": "array",
        },
        "schema": {"type": ["object", "null"]},
        "version": {"type": "integer"},
    },
    "required": ["contracts", "schema", "version"],
    "type": "object",
}
Draft202012Validator.check_schema(META_SCHEMA)
_META_VALIDATOR = Draft202012Validator(META_SCHEMA)


class BlueprintError(StateTreeError):
    """Raised when a Blueprint is malformed or cannot become authority."""


@dataclass(frozen=True)
class BlueprintRecord:
    """The durable identities produced by one Blueprint transition."""

    project_root: Path
    actor: str
    task_id: str
    blueprint: str | None
    payload_hash: str
    entry_hash: str
    sequence: int


def blueprint(project_root: str | Path) -> dict[str, Any] | None:
    """Return the Blueprint currently in force, if any."""

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    kernel_state = _validated_kernel_state(state_tree)
    return _read_blueprint_object(
        _object_directory(state_tree), kernel_state["blueprint_head"]
    )


def set_blueprint(
    project_root: str | Path,
    *,
    actor: str,
    task_id: str,
    blueprint: dict[str, Any] | None,
) -> BlueprintRecord:
    """Validate and atomically place one Blueprint in force."""

    _validate_identifier(actor, label="actor")
    _validate_identifier(task_id, label="task id")
    document = deepcopy(blueprint)
    check_blueprint(document)
    if document is not None:
        try:
            content = _canonical_json_bytes(document)
        except (TypeError, ValueError) as error:
            raise BlueprintError(
                "blueprint must contain canonical JSON values"
            ) from error

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    with _kernel_lock(state_tree):
        kernel_state = _validated_kernel_state(state_tree)
        object_directory = _object_directory(state_tree)
        current = _read_snapshot_object(
            object_directory,
            kernel_state["state_head"],
            label="current state",
        )
        next_schema = None if document is None else document["schema"]
        try:
            initial_state = None if document is None else document.get("initial_state")
            if initial_state is None:
                next_snapshot = current
                validate(_hydrate_collections(current, object_directory), next_schema)
            else:
                if kernel_state["state_head"] != _GENESIS_STATE_REFERENCE:
                    raise BlueprintError(
                        "blueprint initial_state requires the genesis empty state"
                    )
                validate(initial_state, next_schema)
                next_snapshot = _externalize_collections(
                    initial_state, next_schema, object_directory
                )
        except SchemaError as error:
            raise BlueprintError(str(error)) from error

        try:
            state_content = _canonical_json_bytes(next_snapshot)
        except (TypeError, ValueError) as error:
            raise BlueprintError(
                "blueprint initial_state must contain canonical JSON values"
            ) from error
        state_reference = _store_object_at(object_directory, state_content)

        if document is None:
            blueprint_reference = None
            payload_hash = _GENESIS_STATE_REFERENCE
        else:
            payload_hash = _store_object_at(object_directory, content)
            blueprint_reference = payload_hash

        append = _append_ledger_entry_locked(
            state_tree,
            kernel_state,
            actor=actor,
            kind="blueprint",
            task_id=task_id,
            payload_hash=payload_hash,
            metadata={},
            parent_state=kernel_state["state_head"],
            state=state_reference,
            blueprint=blueprint_reference,
            view=None,
            blueprint_transition=True,
        )

    return BlueprintRecord(
        project_root=root,
        actor=actor,
        task_id=task_id,
        blueprint=blueprint_reference,
        payload_hash=payload_hash,
        entry_hash=append.entry_hash,
        sequence=append.sequence,
    )


def check_blueprint(document: dict[str, Any] | None) -> None:
    """Raise BlueprintError unless a Blueprint is structurally and semantically valid."""

    if document is None:
        return
    if not isinstance(document, dict) or document.get("version") != 3:
        raise BlueprintError("blueprint version must be 3")
    _check_actor_budgets(document)
    try:
        _META_VALIDATOR.validate(document)
    except ValidationError as error:
        location = error.json_path
        raise BlueprintError(
            f"blueprint violates meta-schema at {location}: {error.message}"
        ) from error

    schema = document["schema"]
    contracts = document["contracts"]
    try:
        _check_schema(schema)
        _check_contract(contracts)
        _check_schema_paths(contracts, schema)
    except (ContractError, SchemaError) as error:
        raise BlueprintError(str(error)) from error
    _check_collection_reachability(schema, contracts)
    _check_workflow_rules(schema, contracts, document.get("rules", []))
    _check_circuit_policy(document.get("circuit", {}))


def _check_actor_budgets(document: dict[str, Any]) -> None:
    contracts = document.get("contracts")
    actors = contracts.get("actors") if isinstance(contracts, dict) else None
    if not isinstance(actors, dict):
        return
    for actor, rule in actors.items():
        budget = rule.get("budget") if isinstance(rule, dict) else None
        if type(budget) is int and budget < _MINIMUM_ACTOR_BUDGET:
            raise BlueprintError(
                f"contract actor {actor!r} budget must be at least "
                f"{_MINIMUM_ACTOR_BUDGET} characters; one elision marker is "
                "104 characters and a useful View may need two"
            )


def meta_schema() -> dict[str, Any]:
    """Return an independent copy of the Blueprint meta-schema."""

    return deepcopy(META_SCHEMA)


def _read_blueprint_object(
    object_directory: Path, reference: str | None
) -> dict[str, Any] | None:
    if reference is None:
        return None
    return _read_json_mapping_object(
        object_directory, reference, label="blueprint"
    )


def _blueprint_authorities(
    document: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if document is None:
        return None, None
    schema = document.get("schema")
    contracts = document.get("contracts")
    if schema is not None and not isinstance(schema, dict):
        raise BlueprintError("blueprint schema is not a JSON object or null")
    if not isinstance(contracts, dict):
        raise BlueprintError("blueprint contracts is not a JSON object")
    return schema, contracts


def _check_collection_reachability(
    schema: dict[str, Any] | None,
    contracts: dict[str, Any],
) -> None:
    if schema is None:
        return
    patterns = [
        pattern
        for rule in contracts["actors"].values()
        for permission in ("read", "write")
        for pattern in rule.get(permission, [])
    ]
    for collection_path in sorted(_collection_paths(schema)):
        if not any(
            _paths_reach_each_other(collection_path, pattern)
            for pattern in patterns
        ):
            label = collection_path or "<root>"
            raise BlueprintError(
                f"collection path is unreachable by every contract: {label}"
            )


def _check_workflow_rules(
    schema: dict[str, Any] | None,
    contracts: dict[str, Any],
    rules: list[dict[str, Any]],
) -> None:
    for index, rule in enumerate(rules):
        event = rule["on"]
        if event["op"] not in {"add", "replace", "remove"}:
            raise BlueprintError(
                f"workflow rule {index} op must be add, replace, or remove"
            )
        if rule["wake"] not in contracts["actors"]:
            raise BlueprintError(
                f"workflow rule {index} wakes undeclared actor {rule['wake']!r}"
            )
        synthetic_contract = {
            "version": 2,
            "actors": {"rule": {"read": [event["path"]]}},
        }
        try:
            _check_schema_paths(synthetic_contract, schema)
        except ContractError as error:
            raise BlueprintError(f"workflow rule {index}: {error}") from error


def _check_circuit_policy(policy: dict[str, Any]) -> None:
    for name in ("consecutive_rejections", "cycle_window"):
        value = policy.get(name)
        if value is not None and (type(value) is not int or value < 1):
            raise BlueprintError(
                f"circuit {name} must be a positive integer"
            )


def _collection_paths(root_schema: dict[str, Any]) -> set[str]:
    paths: set[str] = set()

    def walk(node: Any, pointer: str, active_references: frozenset[str]) -> None:
        if not isinstance(node, dict):
            return
        if node.get("x-kernel-collection") is True:
            paths.add(pointer)

        reference = node.get("$ref")
        if (
            isinstance(reference, str)
            and reference.startswith("#")
            and reference not in active_references
        ):
            try:
                target = _resolve_document_pointer(root_schema, reference[1:])
            except PatchError:
                target = None
            walk(target, pointer, active_references | {reference})

        for keyword in ("allOf", "anyOf", "oneOf"):
            children = node.get(keyword)
            if isinstance(children, list):
                for child in children:
                    walk(child, pointer, active_references)
        for keyword in ("if", "then", "else", "not"):
            walk(node.get(keyword), pointer, active_references)
        dependencies = node.get("dependentSchemas")
        if isinstance(dependencies, dict):
            for child in dependencies.values():
                walk(child, pointer, active_references)

        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                walk(child, _join_pointer(pointer, name), active_references)
        patterns = node.get("patternProperties")
        if isinstance(patterns, dict):
            for child in patterns.values():
                walk(child, _join_pointer(pointer, "*"), active_references)
        for keyword in ("additionalProperties", "unevaluatedProperties"):
            walk(
                node.get(keyword),
                _join_pointer(pointer, "*"),
                active_references,
            )

        prefix_items = node.get("prefixItems")
        if isinstance(prefix_items, list):
            for index, child in enumerate(prefix_items):
                walk(
                    child,
                    _join_pointer(pointer, str(index)),
                    active_references,
                )
        for keyword in ("items", "contains", "unevaluatedItems"):
            walk(node.get(keyword), _join_pointer(pointer, "*"), active_references)

    walk(root_schema, "", frozenset())
    return paths


def _paths_reach_each_other(collection_path: str, pattern: str) -> bool:
    collection_tokens = _contract_pattern_tokens(collection_path)
    pattern_tokens = _contract_pattern_tokens(pattern)
    return all(
        collection == granted or collection == "*" or granted == "*"
        for collection, granted in zip(
            collection_tokens, pattern_tokens, strict=False
        )
    )


def _join_pointer(pointer: str, token: str) -> str:
    encoded = token.replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{encoded}"


def _validate_identifier(value: Any, *, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise BlueprintError(
            f"{label} must be 1-64 letters, digits, dots, hyphens, or underscores"
        )
