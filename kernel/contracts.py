"""Path-scoped authority for state patches."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from .jsonpatch import PatchError, _pointer_tokens, touched_paths
from .kernel import (
    STATE_TREE_DIRECTORY,
    StateTreeError,
    _GENESIS_STATE_REFERENCE,
    _append_ledger_entry_locked,
    _canonical_json_bytes,
    _digest_from_reference,
    _kernel_lock,
    _object_directory,
    _read_hashed_object,
    _read_json_mapping_object,
    _resolve_project_root,
    _store_object_at,
    _validated_kernel_state,
    entries,
)
from .schema import _read_schema_object, _resolve_schema_node

_CONTRACT_VERSION = 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ACTOR_RULE_KEYS = {"allow_remove", "read", "write"}


class ContractError(StateTreeError):
    """Raised when a write contract is malformed or cannot be installed."""


class UnauthorizedWriteError(ContractError):
    """Raised when an actor's contract does not grant a patch operation."""


@dataclass(frozen=True)
class ContractRecord:
    """The durable identities produced by one write-contract change."""

    project_root: Path
    actor: str
    task_id: str
    contracts: str | None
    payload_hash: str
    entry_hash: str
    sequence: int


def contracts(project_root: str | Path) -> dict[str, Any] | None:
    """Return the write contract currently in force, if any."""

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    kernel_state = _validated_kernel_state(state_tree)
    return _read_contract_object(
        _object_directory(state_tree), kernel_state["contracts_head"]
    )


def set_contracts(
    project_root: str | Path,
    *,
    actor: str,
    task_id: str,
    contracts: dict[str, Any] | None,
) -> ContractRecord:
    """Validate and atomically place a write contract in force."""

    _validate_identifier(actor, label="actor")
    _validate_identifier(task_id, label="task id")
    _check_contract(contracts)

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    with _kernel_lock(state_tree):
        kernel_state = _validated_kernel_state(state_tree)
        object_directory = _object_directory(state_tree)
        current_schema = _read_schema_object(
            object_directory, kernel_state["schema_head"]
        )
        _check_schema_paths(contracts, current_schema)

        if contracts is None:
            contracts_reference = None
            payload_hash = _GENESIS_STATE_REFERENCE
        else:
            payload_hash = _store_object_at(
                object_directory, _canonical_json_bytes(contracts)
            )
            contracts_reference = payload_hash

        append = _append_ledger_entry_locked(
            state_tree,
            kernel_state,
            actor=actor,
            kind="contracts",
            task_id=task_id,
            payload_hash=payload_hash,
            metadata={},
            parent_state=kernel_state["state_head"],
            state=kernel_state["state_head"],
            schema=kernel_state["schema_head"],
            contracts=contracts_reference,
            view=None,
            contracts_transition=True,
        )

    return ContractRecord(
        project_root=root,
        actor=actor,
        task_id=task_id,
        contracts=contracts_reference,
        payload_hash=payload_hash,
        entry_hash=append.entry_hash,
        sequence=append.sequence,
    )


def authorize(
    patch_paths: Any,
    contract: dict[str, Any] | None,
    *,
    actor: str,
) -> None:
    """Raise unless the contract grants every operation and target path."""

    _validate_identifier(actor, label="actor")
    _check_contract(contract)
    parsed_paths = _check_patch_paths(patch_paths)

    if contract is None:
        if any(operation == "remove" for operation, _ in parsed_paths):
            raise UnauthorizedWriteError(
                "remove requires an active contract with allow_remove"
            )
        return

    actor_rule = contract["actors"].get(actor)
    if actor_rule is None:
        raise UnauthorizedWriteError(
            f"actor {actor!r} is absent from the active contract"
        )

    for operation, path in parsed_paths:
        permission = "read" if operation == "test" else "write"
        patterns = actor_rule.get(permission, [])
        if not any(_pattern_matches(pattern, path) for pattern in patterns):
            raise UnauthorizedWriteError(
                f"actor {actor!r} has no {permission} grant for {path!r}"
            )
        if operation == "remove" and actor_rule.get("allow_remove") is not True:
            raise UnauthorizedWriteError(
                f"actor {actor!r} is not allowed to remove {path!r}"
            )


def audit_contracts(project_root: str | Path) -> list[dict[str, Any]]:
    """Re-evaluate recorded state patches against their entry contracts."""

    root = _resolve_project_root(project_root)
    ledger_entries = entries(root)
    object_directory = _object_directory(root / STATE_TREE_DIRECTORY)
    results: list[dict[str, Any]] = []
    for entry in ledger_entries:
        is_patch = entry["kind"] == "patch" or entry["parent_state"] != entry["state"]
        valid = True
        message: str | None = None
        if is_patch:
            try:
                content = _read_hashed_object(
                    object_directory,
                    _digest_from_reference(entry["payload_hash"], label="payload"),
                    label=f"ledger sequence {entry['sequence']} payload",
                )
                patch = json.loads(content)
                patch_paths = touched_paths(patch)
                contract = _read_contract_object(
                    object_directory, entry["contracts"]
                )
                authorize(patch_paths, contract, actor=entry["actor"])
            except (ContractError, PatchError, json.JSONDecodeError, UnicodeDecodeError) as error:
                valid = False
                message = str(error)
        results.append(
            {
                "actor": entry["actor"],
                "contracts": entry["contracts"],
                "error": message,
                "patch": is_patch,
                "sequence": entry["sequence"],
                "valid": valid,
            }
        )
    return results


def _read_contract_object(
    object_directory: Path, reference: str | None
) -> dict[str, Any] | None:
    if reference is None:
        return None
    return _read_json_mapping_object(
        object_directory, reference, label="contracts"
    )


def _check_contract(contract: dict[str, Any] | None) -> None:
    if contract is None:
        return
    if not isinstance(contract, dict) or set(contract) != {"actors", "version"}:
        raise ContractError("contracts must contain exactly actors and version")
    if type(contract["version"]) is not int or contract["version"] != _CONTRACT_VERSION:
        raise ContractError("contracts version must be 1")
    actors = contract["actors"]
    if not isinstance(actors, dict):
        raise ContractError("contracts actors must be an object")
    for actor, rule in actors.items():
        _validate_identifier(actor, label="contract actor")
        if not isinstance(rule, dict) or not set(rule) <= _ACTOR_RULE_KEYS:
            raise ContractError(
                f"contract actor {actor!r} has an invalid rule object"
            )
        for permission in ("read", "write"):
            patterns = rule.get(permission, [])
            if not isinstance(patterns, list):
                raise ContractError(
                    f"contract actor {actor!r} {permission} grants must be an array"
                )
            for pattern in patterns:
                _contract_pattern_tokens(pattern)
        allow_remove = rule.get("allow_remove", False)
        if not isinstance(allow_remove, bool):
            raise ContractError(
                f"contract actor {actor!r} allow_remove must be a boolean"
            )


def _check_patch_paths(patch_paths: Any) -> list[tuple[str, str]]:
    if not isinstance(patch_paths, (list, tuple)):
        raise ContractError("patch paths must be a sequence")
    result = []
    for item in patch_paths:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ContractError("each patch path must contain an operation and path")
        operation, path = item
        if operation not in {"add", "remove", "replace", "test"}:
            raise ContractError(f"unsupported patch operation: {operation!r}")
        try:
            _pointer_tokens(path)
        except PatchError as error:
            raise ContractError(f"invalid patch path: {error}") from error
        result.append((operation, path))
    return result


def _contract_pattern_tokens(pattern: Any) -> list[str]:
    try:
        return _pointer_tokens(pattern)
    except PatchError as error:
        raise ContractError(f"invalid contract path pattern: {error}") from error


def _pattern_matches(pattern: str, path: str) -> bool:
    pattern_tokens = _contract_pattern_tokens(pattern)
    path_tokens = _pointer_tokens(path)
    return len(pattern_tokens) == len(path_tokens) and all(
        granted == "*" or granted == actual
        for granted, actual in zip(pattern_tokens, path_tokens, strict=True)
    )


def _check_schema_paths(
    contract: dict[str, Any] | None,
    current_schema: dict[str, Any] | None,
) -> None:
    if contract is None or current_schema is None:
        return
    for actor, rule in contract["actors"].items():
        for permission in ("read", "write"):
            for pattern in rule.get(permission, []):
                tokens = tuple(_contract_pattern_tokens(pattern))
                if not _schema_path_exists(current_schema, current_schema, tokens):
                    raise ContractError(
                        f"contract actor {actor!r} {permission} path "
                        f"is absent from schema: {pattern}"
                    )


def _schema_path_exists(
    root_schema: dict[str, Any],
    schema_node: Any,
    tokens: tuple[str, ...],
) -> bool:
    effective = _resolve_schema_node(root_schema, schema_node)
    if effective is False:
        return False
    if not tokens:
        return True
    if effective is True or effective is None:
        return True
    if not isinstance(effective, dict):
        return False

    token, remaining = tokens[0], tokens[1:]
    candidates = _schema_children(effective, token)
    possible = any(
        _schema_path_exists(root_schema, candidate, remaining)
        for candidate in candidates
    )

    all_of = effective.get("allOf")
    if isinstance(all_of, list):
        possible = possible and all(
            _schema_path_exists(root_schema, branch, tokens) for branch in all_of
        )
    for keyword in ("anyOf", "oneOf"):
        branches = effective.get(keyword)
        if isinstance(branches, list):
            possible = possible and any(
                _schema_path_exists(root_schema, branch, tokens)
                for branch in branches
            )
    return possible


def _schema_children(schema_node: dict[str, Any], token: str) -> list[Any]:
    schema_type = schema_node.get("type")
    allowed_types = (
        set(schema_type) if isinstance(schema_type, list) else {schema_type}
    )
    type_unspecified = schema_type is None
    candidates: list[Any] = []

    if type_unspecified or "object" in allowed_types:
        properties = schema_node.get("properties", {})
        patterns = schema_node.get("patternProperties", {})
        additional = schema_node.get("additionalProperties", True)
        if token == "*":
            if isinstance(properties, dict):
                candidates.extend(properties.values())
            if isinstance(patterns, dict):
                candidates.extend(patterns.values())
            if additional is not False:
                candidates.append(additional)
        else:
            matched = False
            if isinstance(properties, dict) and token in properties:
                candidates.append(properties[token])
                matched = True
            if isinstance(patterns, dict):
                for expression, child in patterns.items():
                    if re.search(expression, token):
                        candidates.append(child)
                        matched = True
            if not matched and additional is not False:
                candidates.append(additional)

    if type_unspecified or "array" in allowed_types:
        prefix = schema_node.get("prefixItems", [])
        items = schema_node.get("items", True)
        if token in {"*", "-"}:
            if isinstance(prefix, list):
                candidates.extend(prefix)
            if items is not False:
                candidates.append(items)
        elif token.isascii() and token.isdigit() and not (
            len(token) > 1 and token.startswith("0")
        ):
            index = int(token)
            if isinstance(prefix, list) and index < len(prefix):
                candidates.append(prefix[index])
            elif items is not False:
                candidates.append(items)
    return candidates


def _validate_identifier(value: Any, *, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ContractError(
            f"{label} must be 1-64 letters, digits, dots, hyphens, or underscores"
        )
