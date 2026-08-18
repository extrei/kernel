"""One View-bound worker attempt through a completion provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kernel import (
    PatchError,
    SchemaError,
    StaleParentError,
    StaleViewError,
    UnauthorizedWriteError,
    ViewError,
    apply_patch,
    entries,
    read_artifact,
    view,
)

from .providers import Completion, JSON_PATCH_OUTPUT_SCHEMA, Provider

_SYSTEM_PROMPT = """You are one worker in a project-local State Tree.
Return exactly one JSON Patch array with no Markdown fences or prose.
Use only add, replace, remove, or test operations.
The Patch must satisfy the supplied task, View, Schema fragments, and prior refusals.
Do not claim success; the kernel alone accepts or rejects the Patch."""

_KERNEL_FAILURES = (
    PatchError,
    SchemaError,
    UnauthorizedWriteError,
    StaleViewError,
    ViewError,
    StaleParentError,
)


def step(
    project_root: str | Path,
    *,
    actor: str,
    task_id: str,
    task: str,
    provider: Provider,
) -> dict[str, Any]:
    """Ask for and submit one Patch bound to exactly one derived View."""

    try:
        record = view(project_root, actor=actor)
    except _KERNEL_FAILURES as error:
        return _failure(actor, error, completion=None, state=None, view_hash=None)

    completion = provider.complete(
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "actor": actor,
                        "recent_refusals": _recent_refusals(project_root),
                        "task": task,
                        "view": record.document,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }
        ],
    )
    patch = _decode_candidate(completion.text)
    try:
        accepted = apply_patch(
            project_root,
            actor=actor,
            task_id=task_id,
            patch=patch,
            parent_state=record.state,
            view=record.view_hash,
        )
    except _KERNEL_FAILURES as error:
        return _failure(
            actor,
            error,
            completion=completion,
            state=record.state,
            view_hash=record.view_hash,
        )
    return {
        "accepted": True,
        "actor": actor,
        "cost_usd": completion.cost_usd,
        "entry_hash": accepted.entry_hash,
        "error": None,
        "error_type": None,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
        "parent_state": accepted.parent_state,
        "state": accepted.state,
        "view": accepted.view,
    }


def _recent_refusals(
    project_root: str | Path,
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    refusals: list[dict[str, Any]] = []
    for entry in reversed(entries(project_root)):
        if entry["kind"] != "rejection":
            continue
        try:
            payload: Any = json.loads(
                read_artifact(project_root, entry["payload_hash"])
            )
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            refusals.append(
                {
                    "actor": entry["actor"],
                    "paths": payload.get("paths"),
                    "reason": payload.get("reason"),
                    "stage": payload.get("stage"),
                }
            )
        if len(refusals) == limit:
            break
    refusals.reverse()
    return refusals


def _decode_candidate(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _failure(
    actor: str,
    error: Exception,
    *,
    completion: Completion | None,
    state: str | None,
    view_hash: str | None,
) -> dict[str, Any]:
    return {
        "accepted": False,
        "actor": actor,
        "cost_usd": None if completion is None else completion.cost_usd,
        "entry_hash": None,
        "error": str(error),
        "error_type": type(error).__name__,
        "input_tokens": 0 if completion is None else completion.input_tokens,
        "output_tokens": 0 if completion is None else completion.output_tokens,
        "parent_state": state,
        "state": state,
        "view": view_hash,
    }
