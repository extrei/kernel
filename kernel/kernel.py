"""Project-local state, immutable objects, and a chained durable ledger."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any, BinaryIO
from uuid import uuid4

STATE_TREE_DIRECTORY = ".state-tree"
OBJECTS_DIRECTORY = "objects"
CACHE_DIRECTORY = "cache"
VERIFIED_CHECKPOINT_FILE = "verified"
HASH_ALGORITHM = "sha256"
KERNEL_STATE_FILE = "kernel.json"
KERNEL_LOCK_FILE = "kernel.lock"
FORMAT_NAME = "state-tree"
FORMAT_VERSION = 1
_RUNTIME_IGNORE_RULES = (
    f"/{KERNEL_LOCK_FILE}\n"
    f"/.{KERNEL_STATE_FILE}.tmp-*\n"
    f"/{OBJECTS_DIRECTORY}/{HASH_ALGORITHM}/.*.tmp-*\n"
    f"/{CACHE_DIRECTORY}/\n"
)

_GENESIS_HASH = "0" * 64
_GENESIS_STATE_CONTENT = b"{}"
_GENESIS_STATE_REFERENCE = f"{HASH_ALGORITHM}:{sha256(_GENESIS_STATE_CONTENT).hexdigest()}"
_LEDGER_ENTRY_VERSION = 5
_CHECKPOINT_FORMAT_VERSION = 2
_COLLECTION_REFERENCE_KEY = "$collection"
_LEDGER_ENTRY_KEYS = {
    "actor",
    "blueprint",
    "kind",
    "metadata",
    "parent_state",
    "payload_hash",
    "previous_hash",
    "recorded_at",
    "sequence",
    "state",
    "task_id",
    "version",
    "view",
}
_KERNEL_STATE_KEYS = {
    "blueprint_head",
    "format",
    "format_version",
    "ledger_head",
    "revision",
    "state_head",
}


class StateTreeError(RuntimeError):
    """Raised when a state tree cannot be safely initialized or read."""


class LedgerIntegrityError(StateTreeError):
    """Raised when a ledger entry, chain, or referenced object is invalid."""


@dataclass(frozen=True)
class InitResult:
    """The outcome of initializing a project-local state tree."""

    project_root: Path
    state_tree: Path
    created: bool


@dataclass(frozen=True)
class LedgerAppend:
    """The durable identity of one newly appended ledger entry."""

    sequence: int
    entry_hash: str
    previous_hash: str
    payload_hash: str
    recorded_at: str
    parent_state: str
    state: str
    blueprint: str | None
    view: str | None


def initialize(project_root: str | Path = ".") -> InitResult:
    """Create a new state tree without changing an existing one.

    ``project_root`` must already exist. A valid existing tree is left untouched
    and reported as a no-op. An invalid or incomplete existing tree is rejected
    rather than repaired or replaced.
    """

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    if state_tree.exists() or state_tree.is_symlink():
        _validate_state_tree(state_tree)
        return InitResult(root, state_tree, created=False)

    staging_tree = root / f"{STATE_TREE_DIRECTORY}.tmp-{uuid4().hex}"
    try:
        _create_state_tree(staging_tree)
        staging_tree.rename(state_tree)
    except Exception:
        if staging_tree.exists():
            shutil.rmtree(staging_tree)
        raise

    return InitResult(root, state_tree, created=True)


def verify(project_root: str | Path = ".", *, strict: bool = False) -> str:
    """Verify the ledger, optionally from its checkpoint, and return its head."""

    root = _resolve_project_root(project_root)
    return _validate_state_tree(root / STATE_TREE_DIRECTORY, strict=strict)


def entries(project_root: str | Path = ".") -> list[dict[str, Any]]:
    """Return every verified ledger entry, ordered from oldest to newest."""

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    state = _read_kernel_state(state_tree)
    head_digest = _digest_from_reference(state["ledger_head"], label="ledger head")
    return _verify_ledger(
        _object_directory(state_tree),
        revision=state["revision"],
        head_digest=head_digest,
        state_head=state["state_head"],
        blueprint_head=state["blueprint_head"],
        checkpoint=None,
    )


def store_object(project_root: str | Path, content: bytes) -> str:
    """Store immutable bytes and return their SHA-256 reference."""

    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    _validate_state_tree(state_tree)
    return _store_object_at(_object_directory(state_tree), content)


def read_object(project_root: str | Path, reference: str) -> bytes:
    """Read and verify one immutable object by SHA-256 reference."""

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    _validate_state_tree(state_tree)
    digest = _digest_from_reference(reference, label="object")
    return _read_hashed_object(_object_directory(state_tree), digest, label="object")


def append_ledger_entry(
    project_root: str | Path,
    *,
    actor: str,
    kind: str,
    task_id: str,
    payload_hash: str,
    metadata: Mapping[str, Any],
) -> LedgerAppend:
    """Append one immutable fact and atomically advance the durable ledger head."""

    if not isinstance(actor, str) or not actor:
        raise StateTreeError("ledger actor must be a non-empty string")
    if not isinstance(kind, str) or not kind:
        raise StateTreeError("ledger kind must be a non-empty string")
    if not isinstance(task_id, str) or not task_id:
        raise StateTreeError("ledger task id must be a non-empty string")
    if not isinstance(metadata, Mapping):
        raise StateTreeError("ledger metadata must be an object")

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    with _kernel_lock(state_tree):
        kernel_state = _validated_kernel_state(state_tree)
        state_head = kernel_state["state_head"]
        return _append_ledger_entry_locked(
            state_tree,
            kernel_state,
            actor=actor,
            kind=kind,
            task_id=task_id,
            payload_hash=payload_hash,
            metadata=metadata,
            parent_state=state_head,
            state=state_head,
            blueprint=kernel_state["blueprint_head"],
            view=None,
        )


def _append_ledger_entry_locked(
    state_tree: Path,
    kernel_state: Mapping[str, Any],
    *,
    actor: str,
    kind: str,
    task_id: str,
    payload_hash: str,
    metadata: Mapping[str, Any],
    parent_state: str,
    state: str,
    blueprint: str | None,
    view: str | None,
    blueprint_transition: bool = False,
) -> LedgerAppend:
    """Append after the caller has locked and verified the current state."""

    if parent_state != kernel_state["state_head"]:
        raise StateTreeError("ledger parent state does not match the current state head")
    if not blueprint_transition and blueprint != kernel_state["blueprint_head"]:
        raise StateTreeError(
            "ledger blueprint does not match the current blueprint head"
        )
    if blueprint_transition and kind != "blueprint":
        raise StateTreeError("blueprint transitions must use blueprint entries")

    object_directory = _object_directory(state_tree)
    payload_digest = _digest_from_reference(payload_hash, label="payload")
    _read_hashed_object(object_directory, payload_digest, label="payload")
    _read_snapshot_object(object_directory, parent_state, label="parent state")
    _read_snapshot_object(object_directory, state, label="state")
    if blueprint is not None:
        _read_json_mapping_object(object_directory, blueprint, label="blueprint")
    if view is not None:
        _read_json_mapping_object(object_directory, view, label="view")

    sequence = kernel_state["revision"] + 1
    previous_digest = _digest_from_reference(
        kernel_state["ledger_head"], label="ledger head"
    )
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    entry = {
        "actor": actor,
        "blueprint": blueprint,
        "kind": kind,
        "metadata": dict(metadata),
        "parent_state": parent_state,
        "payload_hash": payload_hash,
        "previous_hash": previous_digest,
        "recorded_at": recorded_at,
        "sequence": sequence,
        "state": state,
        "task_id": task_id,
        "version": _LEDGER_ENTRY_VERSION,
        "view": view,
    }
    try:
        entry_content = _canonical_json_bytes(entry)
    except (TypeError, ValueError) as error:
        raise StateTreeError("ledger metadata must contain canonical JSON values") from error

    entry_hash = _store_object_at(object_directory, entry_content)
    next_kernel_state = dict(kernel_state)
    next_kernel_state["ledger_head"] = entry_hash
    next_kernel_state["revision"] = sequence
    next_kernel_state["state_head"] = state
    next_kernel_state["blueprint_head"] = blueprint
    _write_kernel_state(state_tree, next_kernel_state)

    return LedgerAppend(
        sequence=sequence,
        entry_hash=entry_hash,
        previous_hash=f"{HASH_ALGORITHM}:{previous_digest}",
        payload_hash=payload_hash,
        recorded_at=recorded_at,
        parent_state=parent_state,
        state=state,
        blueprint=blueprint,
        view=view,
    )


def _create_state_tree(state_tree: Path) -> None:
    object_directory = _object_directory(state_tree)
    state_tree.mkdir(mode=0o700)
    object_directory.mkdir(parents=True)
    state_head = _store_object_at(object_directory, _GENESIS_STATE_CONTENT)
    (state_tree / KERNEL_LOCK_FILE).touch(mode=0o600)
    (state_tree / ".gitignore").write_text(_RUNTIME_IGNORE_RULES, encoding="utf-8")

    kernel_state = {
        "blueprint_head": None,
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "ledger_head": f"{HASH_ALGORITHM}:{_GENESIS_HASH}",
        "revision": 0,
        "state_head": state_head,
    }
    _write_kernel_state(state_tree, kernel_state)


def _validate_state_tree(state_tree: Path, *, strict: bool = False) -> str:
    state = _validated_kernel_state(state_tree, strict=strict)
    return state["ledger_head"]


def _read_kernel_state(state_tree: Path) -> dict[str, Any]:
    if state_tree.is_symlink() or not state_tree.is_dir():
        raise StateTreeError(f"State tree is not a directory: {state_tree}")

    object_directory = _object_directory(state_tree)
    if object_directory.is_symlink() or not object_directory.is_dir():
        raise StateTreeError(f"State tree is missing object storage: {object_directory}")

    state_file = state_tree / KERNEL_STATE_FILE
    if state_file.is_symlink() or not state_file.is_file():
        raise StateTreeError(f"State tree is missing kernel state: {state_file}")

    try:
        parsed_state: Any = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateTreeError(f"Kernel state is unreadable: {state_file}") from error

    if not isinstance(parsed_state, dict):
        raise StateTreeError(f"Kernel state is not an object: {state_file}")
    if set(parsed_state) != _KERNEL_STATE_KEYS:
        raise StateTreeError(f"Kernel state has an invalid schema: {state_file}")
    if parsed_state.get("format") != FORMAT_NAME:
        raise StateTreeError(f"Unsupported state-tree format: {state_file}")
    if parsed_state.get("format_version") != FORMAT_VERSION:
        raise StateTreeError(f"Unsupported state-tree version: {state_file}")

    revision = parsed_state.get("revision")
    if type(revision) is not int or revision < 0:
        raise StateTreeError(f"Invalid kernel revision: {state_file}")
    _digest_from_reference(parsed_state.get("state_head"), label="state head")
    blueprint_head = parsed_state["blueprint_head"]
    if blueprint_head is not None:
        _digest_from_reference(blueprint_head, label="blueprint head")

    return parsed_state


def _validated_kernel_state(
    state_tree: Path, *, strict: bool = False
) -> dict[str, Any]:
    state = _read_kernel_state(state_tree)
    head_digest = _digest_from_reference(state["ledger_head"], label="ledger head")
    checkpoint = None if strict else _read_checkpoint(state_tree, state["revision"])
    _verify_ledger(
        _object_directory(state_tree),
        revision=state["revision"],
        head_digest=head_digest,
        state_head=state["state_head"],
        blueprint_head=state["blueprint_head"],
        checkpoint=checkpoint,
    )
    _write_checkpoint_best_effort(
        state_tree, sequence=state["revision"], entry_hash=head_digest
    )
    return state


def _verify_ledger(
    object_directory: Path,
    *,
    revision: int,
    head_digest: str,
    state_head: str,
    blueprint_head: str | None,
    checkpoint: tuple[int, str] | None,
) -> list[dict[str, Any]]:
    if revision == 0:
        if head_digest != _GENESIS_HASH:
            raise LedgerIntegrityError("empty ledger does not point to the genesis hash")
        if state_head != _GENESIS_STATE_REFERENCE:
            raise LedgerIntegrityError("empty ledger does not point to the genesis state")
        if blueprint_head is not None:
            raise LedgerIntegrityError("empty ledger has a blueprint head")
        _read_snapshot_object(object_directory, state_head, label="genesis state")
        return []

    reverse_chain: list[tuple[str, dict[str, Any]]] = []
    current_digest = head_digest
    checkpoint_reached = False
    for expected_sequence in range(revision, 0, -1):
        if current_digest == _GENESIS_HASH:
            raise LedgerIntegrityError("ledger reaches genesis before sequence 1")

        entry_digest = current_digest
        content = _read_hashed_object(
            object_directory,
            entry_digest,
            label=f"ledger sequence {expected_sequence}",
        )
        try:
            entry: Any = json.loads(content)
        except json.JSONDecodeError as error:
            raise LedgerIntegrityError(
                f"ledger sequence {expected_sequence} is not valid JSON"
            ) from error
        _validate_ledger_entry(
            entry,
            expected_sequence=expected_sequence,
            object_directory=object_directory,
        )
        reverse_chain.append((entry_digest, entry))
        current_digest = entry["previous_hash"]

        if checkpoint is not None and expected_sequence == checkpoint[0]:
            if entry_digest == checkpoint[1]:
                checkpoint_reached = True
                break
            checkpoint = None

    full_walk = not checkpoint_reached
    if full_walk and current_digest != _GENESIS_HASH:
        raise LedgerIntegrityError("ledger sequence 1 is not rooted at the genesis hash")

    ordered_chain = list(reversed(reverse_chain))
    if full_walk:
        previous_digest = _GENESIS_HASH
        previous_state = _GENESIS_STATE_REFERENCE
        unchecked_chain = ordered_chain
    else:
        previous_digest, checkpoint_entry = ordered_chain[0]
        previous_state = checkpoint_entry["state"]
        unchecked_chain = ordered_chain[1:]

    for digest, entry in unchecked_chain:
        if entry["previous_hash"] != previous_digest:
            raise LedgerIntegrityError(
                f"ledger sequence {entry['sequence']} does not extend the preceding entry"
            )
        if entry["parent_state"] != previous_state:
            raise LedgerIntegrityError(
                f"ledger sequence {entry['sequence']} does not extend the preceding state"
            )
        previous_digest = digest
        previous_state = entry["state"]

    if previous_digest != head_digest:
        raise LedgerIntegrityError("kernel ledger head does not match the verified chain")
    if ordered_chain[-1][1]["state"] != state_head:
        raise LedgerIntegrityError("kernel state head does not match the verified chain")
    if ordered_chain[-1][1]["blueprint"] != blueprint_head:
        raise LedgerIntegrityError(
            "kernel blueprint head does not match the verified chain"
        )
    return [entry for _, entry in ordered_chain]


def _validate_ledger_entry(
    entry: Any,
    *,
    expected_sequence: int,
    object_directory: Path,
) -> None:
    if not isinstance(entry, dict) or set(entry) != _LEDGER_ENTRY_KEYS:
        raise LedgerIntegrityError(f"ledger sequence {expected_sequence} has an invalid schema")
    if entry.get("version") != _LEDGER_ENTRY_VERSION:
        raise LedgerIntegrityError(f"ledger sequence {expected_sequence} has an invalid version")
    if entry.get("sequence") != expected_sequence:
        raise LedgerIntegrityError(f"ledger sequence {expected_sequence} is out of order")
    if not isinstance(entry.get("actor"), str) or not entry["actor"]:
        raise LedgerIntegrityError(f"ledger sequence {expected_sequence} has an invalid actor")
    if not isinstance(entry.get("kind"), str) or not entry["kind"]:
        raise LedgerIntegrityError(f"ledger sequence {expected_sequence} has an invalid kind")
    if not isinstance(entry.get("task_id"), str) or not entry["task_id"]:
        raise LedgerIntegrityError(f"ledger sequence {expected_sequence} has an invalid task id")
    if not isinstance(entry.get("metadata"), dict):
        raise LedgerIntegrityError(f"ledger sequence {expected_sequence} has invalid metadata")
    if not isinstance(entry.get("recorded_at"), str) or not entry["recorded_at"]:
        raise LedgerIntegrityError(f"ledger sequence {expected_sequence} has an invalid timestamp")

    previous_hash = entry.get("previous_hash")
    if not _is_digest(previous_hash):
        raise LedgerIntegrityError(
            f"ledger sequence {expected_sequence} has an invalid previous hash"
        )
    _read_snapshot_object(
        object_directory,
        entry.get("parent_state"),
        label=f"ledger sequence {expected_sequence} parent state",
    )
    _read_snapshot_object(
        object_directory,
        entry.get("state"),
        label=f"ledger sequence {expected_sequence} state",
    )
    blueprint = entry.get("blueprint")
    if blueprint is not None:
        _read_json_mapping_object(
            object_directory,
            blueprint,
            label=f"ledger sequence {expected_sequence} blueprint",
        )
    view = entry.get("view")
    if view is not None:
        _read_json_mapping_object(
            object_directory,
            view,
            label=f"ledger sequence {expected_sequence} view",
        )
    payload_digest = _digest_from_reference(entry.get("payload_hash"), label="payload")
    _read_hashed_object(
        object_directory,
        payload_digest,
        label=f"ledger sequence {expected_sequence} payload",
    )


def _resolve_project_root(project_root: str | Path) -> Path:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise StateTreeError(f"Project directory does not exist: {root}")
    return root


def _object_directory(state_tree: Path) -> Path:
    return state_tree / OBJECTS_DIRECTORY / HASH_ALGORITHM


def _read_checkpoint(state_tree: Path, revision: int) -> tuple[int, str] | None:
    checkpoint_file = state_tree / CACHE_DIRECTORY / VERIFIED_CHECKPOINT_FILE
    if checkpoint_file.is_symlink() or not checkpoint_file.is_file():
        return None
    try:
        checkpoint: Any = json.loads(checkpoint_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(checkpoint, dict) or set(checkpoint) != {
        "entry_hash",
        "format_version",
        "sequence",
    }:
        return None
    sequence = checkpoint.get("sequence")
    entry_hash = checkpoint.get("entry_hash")
    if (
        checkpoint.get("format_version") != _CHECKPOINT_FORMAT_VERSION
        or type(sequence) is not int
        or sequence < 1
        or sequence > revision
        or not _is_digest(entry_hash)
    ):
        return None
    return sequence, entry_hash


def _write_checkpoint_best_effort(
    state_tree: Path, *, sequence: int, entry_hash: str
) -> None:
    if sequence == 0:
        return
    cache_directory = state_tree / CACHE_DIRECTORY
    temporary_file = cache_directory / f".{VERIFIED_CHECKPOINT_FILE}.tmp-{uuid4().hex}"
    try:
        cache_directory.mkdir(mode=0o700, exist_ok=True)
        if cache_directory.is_symlink() or not cache_directory.is_dir():
            return
        content = _canonical_json_bytes(
            {
                "entry_hash": entry_hash,
                "format_version": _CHECKPOINT_FORMAT_VERSION,
                "sequence": sequence,
            }
        )
        with temporary_file.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_file, cache_directory / VERIFIED_CHECKPOINT_FILE)
    except OSError:
        pass
    finally:
        try:
            temporary_file.unlink(missing_ok=True)
        except OSError:
            pass


def _store_object_at(object_directory: Path, content: bytes) -> str:
    digest = sha256(content).hexdigest()
    object_file = object_directory / digest
    if object_file.exists() or object_file.is_symlink():
        existing = _read_hashed_object(object_directory, digest, label="object")
        if existing != content:
            raise StateTreeError(f"Object hash collision at: {object_file}")
        return f"{HASH_ALGORITHM}:{digest}"

    temporary_file = object_directory / f".{digest}.tmp-{uuid4().hex}"
    try:
        with temporary_file.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_file, object_file)
        except FileExistsError:
            existing = _read_hashed_object(object_directory, digest, label="object")
            if existing != content:
                raise StateTreeError(f"Object hash collision at: {object_file}")
    finally:
        temporary_file.unlink(missing_ok=True)
    return f"{HASH_ALGORITHM}:{digest}"


def _read_hashed_object(object_directory: Path, digest: str, *, label: str) -> bytes:
    object_file = object_directory / digest
    if object_file.is_symlink() or not object_file.is_file():
        raise LedgerIntegrityError(f"{label.capitalize()} object is missing: {object_file}")
    try:
        content = object_file.read_bytes()
    except OSError as error:
        raise LedgerIntegrityError(
            f"{label.capitalize()} object is unreadable: {object_file}"
        ) from error
    if sha256(content).hexdigest() != digest:
        raise LedgerIntegrityError(
            f"{label.capitalize()} object does not match its hash: {object_file}"
        )
    return content


def _read_snapshot_object(
    object_directory: Path, reference: Any, *, label: str
) -> dict[str, Any]:
    snapshot = _read_canonical_json(object_directory, reference, label=label)
    if not isinstance(snapshot, dict):
        raise LedgerIntegrityError(f"{label.capitalize()} is not a JSON object")
    _validate_collection_references(object_directory, snapshot, seen=set())
    return snapshot


def _read_json_mapping_object(
    object_directory: Path, reference: Any, *, label: str
) -> dict[str, Any]:
    value = _read_canonical_json(object_directory, reference, label=label)
    if not isinstance(value, dict):
        raise LedgerIntegrityError(f"{label.capitalize()} is not a JSON object")
    return value


def _read_collection_object(
    object_directory: Path, reference: Any, *, label: str
) -> list[Any]:
    value = _read_canonical_json(object_directory, reference, label=label)
    if not isinstance(value, list):
        raise LedgerIntegrityError(f"{label.capitalize()} is not a JSON array")
    return value


def _validate_collection_references(
    object_directory: Path,
    value: Any,
    *,
    seen: set[str],
) -> None:
    if _is_collection_reference(value):
        reference = value[_COLLECTION_REFERENCE_KEY]
        if reference in seen:
            raise LedgerIntegrityError("collection references form a cycle")
        collection = _read_collection_object(
            object_directory, reference, label="collection"
        )
        seen.add(reference)
        try:
            _validate_collection_references(
                object_directory, collection, seen=seen
            )
        finally:
            seen.remove(reference)
        return
    if isinstance(value, dict):
        for child in value.values():
            _validate_collection_references(object_directory, child, seen=seen)
    elif isinstance(value, list):
        for child in value:
            _validate_collection_references(object_directory, child, seen=seen)


def _is_collection_reference(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {_COLLECTION_REFERENCE_KEY}


def _read_canonical_json(
    object_directory: Path, reference: Any, *, label: str
) -> Any:
    digest = _digest_from_reference(reference, label=label)
    content = _read_hashed_object(object_directory, digest, label=label)
    try:
        value: Any = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise LedgerIntegrityError(f"{label.capitalize()} is not valid JSON") from error
    try:
        canonical = _canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise LedgerIntegrityError(
            f"{label.capitalize()} contains non-canonical JSON values"
        ) from error
    if canonical != content:
        raise LedgerIntegrityError(f"{label.capitalize()} is not canonical JSON")
    return value


def _digest_from_reference(reference: Any, *, label: str) -> str:
    prefix = f"{HASH_ALGORITHM}:"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise LedgerIntegrityError(f"Kernel has no valid {label} reference")

    digest = reference.removeprefix(prefix)
    if not _is_digest(digest):
        raise LedgerIntegrityError(f"Kernel has no valid SHA-256 {label} reference")
    return digest


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_kernel_state(state_tree: Path, state: Mapping[str, Any]) -> None:
    state_file = state_tree / KERNEL_STATE_FILE
    temporary_file = state_tree / f".{KERNEL_STATE_FILE}.tmp-{uuid4().hex}"
    try:
        with temporary_file.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(state), indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_file, state_file)
    finally:
        temporary_file.unlink(missing_ok=True)


@contextmanager
def _kernel_lock(state_tree: Path):
    lock_file = state_tree / KERNEL_LOCK_FILE
    try:
        descriptor = os.open(
            lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        handle = os.fdopen(descriptor, "r+b")
    except OSError as error:
        raise StateTreeError(f"Cannot create or open kernel lock: {lock_file}") from error

    with handle:
        try:
            _lock_exclusive(handle)
        except OSError as error:
            raise StateTreeError(f"Cannot lock state tree: {state_tree}") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_exclusive(handle: BinaryIO) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
