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
HASH_ALGORITHM = "sha256"
KERNEL_STATE_FILE = "kernel.json"
KERNEL_LOCK_FILE = "kernel.lock"
FORMAT_NAME = "state-tree"
FORMAT_VERSION = 1

_GENESIS_HASH = "0" * 64
_LEDGER_ENTRY_VERSION = 1
_LEDGER_ENTRY_KEYS = {
    "actor",
    "kind",
    "metadata",
    "payload_hash",
    "previous_hash",
    "recorded_at",
    "sequence",
    "version",
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


def verify(project_root: str | Path = ".") -> str:
    """Verify every durable ledger entry and return its current head reference."""

    root = _resolve_project_root(project_root)
    return _validate_state_tree(root / STATE_TREE_DIRECTORY)


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
    payload_hash: str,
    metadata: Mapping[str, Any],
) -> LedgerAppend:
    """Append one immutable fact and atomically advance the durable ledger head."""

    if not isinstance(actor, str) or not actor:
        raise StateTreeError("ledger actor must be a non-empty string")
    if not isinstance(kind, str) or not kind:
        raise StateTreeError("ledger kind must be a non-empty string")
    if not isinstance(metadata, Mapping):
        raise StateTreeError("ledger metadata must be an object")

    root = _resolve_project_root(project_root)
    state_tree = root / STATE_TREE_DIRECTORY
    with _kernel_lock(state_tree):
        state = _validated_kernel_state(state_tree)
        object_directory = _object_directory(state_tree)
        payload_digest = _digest_from_reference(payload_hash, label="payload")
        _read_hashed_object(object_directory, payload_digest, label="payload")

        sequence = state["revision"] + 1
        previous_digest = _digest_from_reference(state["ledger_head"], label="ledger head")
        recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        entry = {
            "actor": actor,
            "kind": kind,
            "metadata": dict(metadata),
            "payload_hash": payload_hash,
            "previous_hash": previous_digest,
            "recorded_at": recorded_at,
            "sequence": sequence,
            "version": _LEDGER_ENTRY_VERSION,
        }
        try:
            entry_content = _canonical_json_bytes(entry)
        except (TypeError, ValueError) as error:
            raise StateTreeError("ledger metadata must contain canonical JSON values") from error

        entry_hash = _store_object_at(object_directory, entry_content)
        next_state = dict(state)
        next_state["ledger_head"] = entry_hash
        next_state["revision"] = sequence
        _write_kernel_state(state_tree, next_state)

    return LedgerAppend(
        sequence=sequence,
        entry_hash=entry_hash,
        previous_hash=f"{HASH_ALGORITHM}:{previous_digest}",
        payload_hash=payload_hash,
        recorded_at=recorded_at,
    )


def _create_state_tree(state_tree: Path) -> None:
    object_directory = _object_directory(state_tree)
    state_tree.mkdir(mode=0o700)
    object_directory.mkdir(parents=True)
    (state_tree / KERNEL_LOCK_FILE).touch(mode=0o600)

    initial_object = _canonical_json_bytes(
        {
            "kind": "accepted-state",
            "value": {},
            "version": 1,
        }
    )
    accepted_state = _store_object_at(object_directory, initial_object)
    kernel_state = {
        "accepted_state": accepted_state,
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "ledger_head": f"{HASH_ALGORITHM}:{_GENESIS_HASH}",
        "revision": 0,
    }
    _write_kernel_state(state_tree, kernel_state)


def _validate_state_tree(state_tree: Path) -> str:
    state = _validated_kernel_state(state_tree)
    return state["ledger_head"]


def _validated_kernel_state(state_tree: Path) -> dict[str, Any]:
    if state_tree.is_symlink() or not state_tree.is_dir():
        raise StateTreeError(f"State tree is not a directory: {state_tree}")

    object_directory = _object_directory(state_tree)
    if object_directory.is_symlink() or not object_directory.is_dir():
        raise StateTreeError(f"State tree is missing object storage: {object_directory}")

    lock_file = state_tree / KERNEL_LOCK_FILE
    if lock_file.is_symlink() or not lock_file.is_file():
        raise StateTreeError(f"State tree is missing kernel lock: {lock_file}")

    state_file = state_tree / KERNEL_STATE_FILE
    if state_file.is_symlink() or not state_file.is_file():
        raise StateTreeError(f"State tree is missing kernel state: {state_file}")

    try:
        parsed_state: Any = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateTreeError(f"Kernel state is unreadable: {state_file}") from error

    if not isinstance(parsed_state, dict):
        raise StateTreeError(f"Kernel state is not an object: {state_file}")
    if parsed_state.get("format") != FORMAT_NAME:
        raise StateTreeError(f"Unsupported state-tree format: {state_file}")
    if parsed_state.get("format_version") != FORMAT_VERSION:
        raise StateTreeError(f"Unsupported state-tree version: {state_file}")

    revision = parsed_state.get("revision")
    if type(revision) is not int or revision < 0:
        raise StateTreeError(f"Invalid kernel revision: {state_file}")

    accepted_digest = _digest_from_reference(
        parsed_state.get("accepted_state"), label="accepted state"
    )
    _read_hashed_object(object_directory, accepted_digest, label="accepted state")

    head_digest = _digest_from_reference(parsed_state.get("ledger_head"), label="ledger head")
    _verify_ledger(object_directory, revision=revision, head_digest=head_digest)
    return parsed_state


def _verify_ledger(object_directory: Path, *, revision: int, head_digest: str) -> str:
    if revision == 0:
        if head_digest != _GENESIS_HASH:
            raise LedgerIntegrityError("empty ledger does not point to the genesis hash")
        return head_digest

    reverse_chain: list[tuple[str, dict[str, Any]]] = []
    current_digest = head_digest
    for expected_sequence in range(revision, 0, -1):
        if current_digest == _GENESIS_HASH:
            raise LedgerIntegrityError("ledger reaches genesis before sequence 1")

        content = _read_hashed_object(
            object_directory,
            current_digest,
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
        reverse_chain.append((current_digest, entry))
        current_digest = entry["previous_hash"]

    if current_digest != _GENESIS_HASH:
        raise LedgerIntegrityError("ledger sequence 1 is not rooted at the genesis hash")

    previous_digest = _GENESIS_HASH
    for digest, entry in reversed(reverse_chain):
        if entry["previous_hash"] != previous_digest:
            raise LedgerIntegrityError(
                f"ledger sequence {entry['sequence']} does not extend the preceding entry"
            )
        previous_digest = digest

    if previous_digest != head_digest:
        raise LedgerIntegrityError("kernel ledger head does not match the verified chain")
    return head_digest


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
    if not isinstance(entry.get("metadata"), dict):
        raise LedgerIntegrityError(f"ledger sequence {expected_sequence} has invalid metadata")
    if not isinstance(entry.get("recorded_at"), str) or not entry["recorded_at"]:
        raise LedgerIntegrityError(f"ledger sequence {expected_sequence} has an invalid timestamp")

    previous_hash = entry.get("previous_hash")
    if not _is_digest(previous_hash):
        raise LedgerIntegrityError(
            f"ledger sequence {expected_sequence} has an invalid previous hash"
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
        raise LedgerIntegrityError(f"{label.capitalize()} object is unreadable: {object_file}") from error
    if sha256(content).hexdigest() != digest:
        raise LedgerIntegrityError(f"{label.capitalize()} object does not match its hash: {object_file}")
    return content


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
    if lock_file.is_symlink() or not lock_file.is_file():
        raise StateTreeError(f"State tree is missing kernel lock: {lock_file}")
    try:
        handle = lock_file.open("r+b")
    except OSError as error:
        raise StateTreeError(f"Cannot open kernel lock: {lock_file}") from error

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
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
