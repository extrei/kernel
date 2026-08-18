"""Small, typed RFC 6901/6902 subset for immutable state updates."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class PatchError(ValueError):
    """Raised when a JSON Patch document cannot be applied."""


def apply_patch(
    document: Any,
    patch: Any,
    *,
    allow_remove: bool = False,
) -> Any:
    """Apply add, replace, test, and optionally remove to a deep copy."""

    if not isinstance(patch, list):
        raise PatchError("patch must be an array")

    try:
        result = deepcopy(document)
    except Exception as error:
        raise PatchError("document cannot be copied") from error
    for index, operation in enumerate(patch):
        try:
            result = _apply_operation(result, operation, allow_remove=allow_remove)
        except PatchError as error:
            raise PatchError(f"operation {index}: {error}") from error
        except Exception as error:
            raise PatchError(f"operation {index} failed: {error}") from error
    return result


def _apply_operation(document: Any, operation: Any, *, allow_remove: bool) -> Any:
    if not isinstance(operation, dict):
        raise PatchError("operation must be an object")
    name = operation.get("op")
    if name in {"move", "copy"}:
        raise PatchError(f"{name} is not supported")
    if name not in {"add", "replace", "test", "remove"}:
        raise PatchError("op must be add, replace, test, or remove")
    if name == "remove" and not allow_remove:
        raise PatchError("remove is not allowed")

    path = operation.get("path")
    tokens = _pointer_tokens(path)
    if name in {"add", "replace", "test"} and "value" not in operation:
        raise PatchError(f"{name} requires value")
    if not tokens:
        return _apply_at_root(document, operation)

    parent, token = _resolve_parent(document, tokens)
    if isinstance(parent, dict):
        return _apply_to_object(document, parent, token, operation)
    if isinstance(parent, list):
        return _apply_to_array(document, parent, token, operation)
    raise PatchError("path parent is not an object or array")


def _apply_at_root(document: Any, operation: dict[str, Any]) -> Any:
    name = operation["op"]
    if name in {"add", "replace"}:
        return deepcopy(operation["value"])
    if name == "test":
        if not _json_equal(document, operation["value"]):
            raise PatchError("test failed")
        return document
    return None


def _apply_to_object(
    document: Any,
    parent: dict[str, Any],
    token: str,
    operation: dict[str, Any],
) -> Any:
    name = operation["op"]
    if name == "add":
        parent[token] = deepcopy(operation["value"])
        return document
    if token not in parent:
        raise PatchError(f"object member does not exist: {token}")
    if name == "replace":
        parent[token] = deepcopy(operation["value"])
    elif name == "test":
        if not _json_equal(parent[token], operation["value"]):
            raise PatchError("test failed")
    else:
        del parent[token]
    return document


def _apply_to_array(
    document: Any,
    parent: list[Any],
    token: str,
    operation: dict[str, Any],
) -> Any:
    name = operation["op"]
    if name == "add":
        if token == "-":
            parent.append(deepcopy(operation["value"]))
        else:
            index = _array_index(token, len(parent), allow_end=True)
            parent.insert(index, deepcopy(operation["value"]))
        return document

    index = _array_index(token, len(parent), allow_end=False)
    if name == "replace":
        parent[index] = deepcopy(operation["value"])
    elif name == "test":
        if not _json_equal(parent[index], operation["value"]):
            raise PatchError("test failed")
    else:
        del parent[index]
    return document


def _resolve_parent(document: Any, tokens: list[str]) -> tuple[Any, str]:
    current = document
    for token in tokens[:-1]:
        if isinstance(current, dict):
            if token not in current:
                raise PatchError(f"object member does not exist: {token}")
            current = current[token]
        elif isinstance(current, list):
            current = current[_array_index(token, len(current), allow_end=False)]
        else:
            raise PatchError("path traverses a scalar value")
    return current, tokens[-1]


def _pointer_tokens(pointer: Any) -> list[str]:
    if not isinstance(pointer, str):
        raise PatchError("path must be a JSON pointer string")
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PatchError("JSON pointer must be empty or start with /")
    return [_decode_token(token) for token in pointer[1:].split("/")]


def _decode_token(token: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(token):
        if token[index] != "~":
            decoded.append(token[index])
            index += 1
            continue
        if index + 1 == len(token) or token[index + 1] not in "01":
            raise PatchError("JSON pointer contains an invalid ~ escape")
        decoded.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _array_index(token: str, length: int, *, allow_end: bool) -> int:
    if not token or not token.isascii() or not token.isdigit():
        raise PatchError(f"invalid array index: {token}")
    if len(token) > 1 and token.startswith("0"):
        raise PatchError(f"invalid array index: {token}")
    index = int(token)
    maximum = length if allow_end else length - 1
    if index > maximum:
        raise PatchError(f"array index is out of bounds: {token}")
    return index


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return left == right
