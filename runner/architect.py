"""Blueprint composition through the Anthropic API provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kernel import BlueprintError, check_blueprint, meta_schema, set_blueprint

from .providers import APIProvider

_SYSTEM_PROMPT = """You are the Architect for one project-local State Tree.
Return exactly one JSON Blueprint version 2 object, with no Markdown fences or prose.
The Blueprint must satisfy the supplied Meta-schema and the task.
Declare a default actor named worker. Every Workflow Rule wake actor must be declared.
Write Contract and Workflow Rule paths must exist in the Schema when it is non-null.
Keep the Blueprint minimal; do not include implementation instructions outside it."""


def install(
    project_root: str | Path,
    task: str,
    *,
    actor: str = "architect",
    task_id: str,
    attempts: int = 3,
) -> dict[str, Any]:
    """Compose, validate, and install one Blueprint through the API provider."""

    return _attempt(
        project_root,
        task,
        actor=actor,
        task_id=task_id,
        attempts=attempts,
        commit=True,
    )


def compose(
    project_root: str | Path,
    task: str,
    *,
    actor: str = "architect",
    task_id: str,
    attempts: int = 3,
) -> dict[str, Any]:
    """Compose and validate one Blueprint without installing it."""

    return _attempt(
        project_root,
        task,
        actor=actor,
        task_id=task_id,
        attempts=attempts,
        commit=False,
    )


def _attempt(
    project_root: str | Path,
    task: str,
    *,
    actor: str,
    task_id: str,
    attempts: int,
    commit: bool,
) -> dict[str, Any]:
    if type(attempts) is not int or attempts < 1:
        raise ValueError("attempts must be a positive integer")
    authority_schema = meta_schema()
    provider = APIProvider(output_schema=authority_schema)
    provider.preflight()
    errors: list[str] = []
    last_error: BlueprintError | None = None

    for _ in range(attempts):
        completion = provider.complete(
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _request(task, authority_schema, errors),
                }
            ],
        )
        try:
            document = _decode_blueprint(completion.text)
            if commit:
                set_blueprint(
                    project_root,
                    actor=actor,
                    task_id=task_id,
                    blueprint=document,
                )
            else:
                check_blueprint(document)
            return document
        except BlueprintError as error:
            last_error = error
            errors.append(str(error))

    if last_error is None:
        raise BlueprintError("Blueprint composition failed")
    raise last_error


def _request(
    task: str,
    authority_schema: dict[str, Any],
    errors: list[str],
) -> str:
    return json.dumps(
        {
            "task": task,
            "meta_schema": authority_schema,
            "validation_errors": errors,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_blueprint(text: str) -> dict[str, Any]:
    try:
        document: Any = json.loads(text)
    except json.JSONDecodeError as error:
        raise BlueprintError(f"Blueprint is not valid JSON: {error.msg}") from error
    if not isinstance(document, dict):
        raise BlueprintError("Blueprint must be a JSON object")
    return document
