"""Deterministic circuit and Workflow Rule projections over the ledger."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .blueprint import _read_blueprint_object
from .contracts import _pattern_matches
from .kernel import (
    STATE_TREE_DIRECTORY,
    _object_directory,
    _read_canonical_json,
    _read_json_mapping_object,
    _resolve_project_root,
    entries,
)

_DEFAULT_CONSECUTIVE_REJECTIONS = 2
_DEFAULT_CYCLE_WINDOW = 3


@dataclass(frozen=True)
class CircuitVerdict:
    """One deterministic advisory judgment over the current ledger tail."""

    action: str
    reason: str
    signals: dict[str, Any]


def circuit(project_root: str | Path) -> CircuitVerdict:
    """Derive the current circuit verdict without advancing project state."""

    root = _resolve_project_root(project_root)
    ledger_entries = entries(root)
    object_directory = _object_directory(root / STATE_TREE_DIRECTORY)
    current_blueprint = _entry_blueprint(object_directory, ledger_entries)
    rejection_threshold, cycle_window = _policy(current_blueprint)

    attempt_kind, rejection_actor, rejection_count = _consecutive_failures(
        ledger_entries, object_directory
    )
    cycle_state = _cycle_state(ledger_entries, cycle_window)
    no_op_sequences = _no_op_sequences(ledger_entries, cycle_window)
    repeated_actor, repeated_hash, repeated_count, repeated_kind = _repeated_payload(
        ledger_entries, object_directory, cycle_window
    )
    signals = {
        "consecutive_rejections": {
            "actor": rejection_actor,
            "count": rejection_count,
            "threshold": rejection_threshold,
        },
        "cycle": {
            "detected": cycle_state is not None,
            "state": cycle_state,
            "window": cycle_window,
        },
        "no_op_commits": no_op_sequences,
        "repeated_payload": {
            "actor": repeated_actor,
            "count": repeated_count,
            "payload_hash": repeated_hash,
        },
    }

    if cycle_state is not None:
        return CircuitVerdict("halt", "state cycle detected", signals)
    if repeated_count > 1:
        reason = (
            "actor repeated an identical failure"
            if repeated_kind == "failure"
            else "actor repeated an identical payload"
        )
        return CircuitVerdict("halt", reason, signals)
    if rejection_count >= rejection_threshold:
        reason = (
            "actor reached the consecutive failure threshold"
            if attempt_kind == "failure"
            else "actor reached the consecutive rejection threshold"
        )
        return CircuitVerdict(
            "switch_actor",
            reason,
            signals,
        )
    if no_op_sequences:
        return CircuitVerdict(
            "tighten_budget", "accepted patch did not advance state", signals
        )
    if rejection_count:
        reason = (
            "latest actor attempt failed before kernel evaluation"
            if attempt_kind == "failure"
            else "latest actor attempt was rejected"
        )
        return CircuitVerdict("retry", reason, signals)
    return CircuitVerdict("continue", "no circuit signal requires intervention", signals)


def events(
    entry: dict[str, Any], blueprint: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Match decoded Patch operations against one Blueprint's Workflow Rules."""

    if blueprint is None or not isinstance(blueprint, dict):
        return []
    if "kind" in entry and entry.get("kind") != "patch":
        return []
    patch = entry.get("patch")
    if patch is None and "op" in entry:
        patch = [entry]
    if not isinstance(patch, list):
        return []

    scheduled: list[dict[str, Any]] = []
    for operation in patch:
        if not isinstance(operation, dict):
            continue
        operation_name = operation.get("op")
        path = operation.get("path")
        if not isinstance(operation_name, str) or not isinstance(path, str):
            continue
        event = {"op": operation_name, "path": path}
        for rule in blueprint.get("rules", []):
            expected = rule["on"]
            if expected["op"] == operation_name and _pattern_matches(
                expected["path"], path
            ):
                scheduled.append({"actor": rule["wake"], "event": dict(event)})
    return scheduled


def schedule(project_root: str | Path) -> list[dict[str, Any]]:
    """Return actor responses implied by the newest committed Patch."""

    root = _resolve_project_root(project_root)
    ledger_entries = entries(root)
    patch_entry = next(
        (entry for entry in reversed(ledger_entries) if entry["kind"] == "patch"),
        None,
    )
    if patch_entry is None:
        return []

    object_directory = _object_directory(root / STATE_TREE_DIRECTORY)
    blueprint = _read_blueprint_object(
        object_directory, patch_entry["blueprint"]
    )
    patch = _read_canonical_json(
        object_directory,
        patch_entry["payload_hash"],
        label=f"ledger sequence {patch_entry['sequence']} patch",
    )
    enriched_entry = dict(patch_entry)
    enriched_entry["patch"] = patch
    return events(enriched_entry, blueprint)


def _entry_blueprint(
    object_directory: Path, ledger_entries: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not ledger_entries:
        return None
    return _read_blueprint_object(
        object_directory, ledger_entries[-1]["blueprint"]
    )


def _policy(blueprint: dict[str, Any] | None) -> tuple[int, int]:
    policy = {} if blueprint is None else blueprint.get("circuit", {})
    return (
        policy.get("consecutive_rejections", _DEFAULT_CONSECUTIVE_REJECTIONS),
        policy.get("cycle_window", _DEFAULT_CYCLE_WINDOW),
    )


def _consecutive_failures(
    ledger_entries: list[dict[str, Any]], object_directory: Path
) -> tuple[str | None, str | None, int]:
    attempt_kind: str | None = None
    actor: str | None = None
    count = 0
    for entry in reversed(ledger_entries):
        if entry["kind"] not in {"failure", "rejection"}:
            break
        if (
            entry["kind"] == "rejection"
            and _rejection_stage(object_directory, entry) == "stale_parent"
        ):
            continue
        if actor is None:
            actor = entry["actor"]
            attempt_kind = entry["kind"]
        if entry["actor"] != actor:
            break
        count += 1
    return attempt_kind, actor, count


def _cycle_state(
    ledger_entries: list[dict[str, Any]], cycle_window: int
) -> str | None:
    transitions = [
        entry
        for entry in ledger_entries
        if entry["parent_state"] != entry["state"]
    ]
    if not transitions:
        return None
    state_trace = [transitions[0]["parent_state"]]
    state_trace.extend(entry["state"] for entry in transitions)
    tail = state_trace[-cycle_window:]
    seen: set[str] = set()
    for state_hash in tail:
        if state_hash in seen:
            return state_hash
        seen.add(state_hash)
    return None


def _no_op_sequences(
    ledger_entries: list[dict[str, Any]], cycle_window: int
) -> list[int]:
    patches = [entry for entry in ledger_entries if entry["kind"] == "patch"]
    return [
        entry["sequence"]
        for entry in patches[-cycle_window:]
        if entry["parent_state"] == entry["state"]
    ]


def _repeated_payload(
    ledger_entries: list[dict[str, Any]],
    object_directory: Path,
    cycle_window: int,
) -> tuple[str | None, str | None, int, str | None]:
    candidates = []
    kinds: dict[tuple[str, str], str] = {}
    for entry in ledger_entries[-cycle_window:]:
        if (
            entry["kind"] == "rejection"
            and _rejection_stage(object_directory, entry) == "stale_parent"
        ):
            continue
        key = (entry["actor"], entry["payload_hash"])
        candidates.append(key)
        kinds[key] = entry["kind"]
    counts = Counter(candidates)
    if not counts:
        return None, None, 0, None
    (actor, payload_hash), count = min(
        counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
    )
    if count < 2:
        return None, None, 0, None
    return actor, payload_hash, count, kinds[(actor, payload_hash)]


def _rejection_stage(
    object_directory: Path, entry: dict[str, Any]
) -> str | None:
    rejection = _read_json_mapping_object(
        object_directory,
        entry["payload_hash"],
        label=f"ledger sequence {entry['sequence']} rejection",
    )
    stage = rejection.get("stage")
    return stage if isinstance(stage, str) else None
