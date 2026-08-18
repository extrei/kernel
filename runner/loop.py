"""Bounded runner policy over Circuit Verdicts and Schedules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kernel import blueprint, circuit, schedule, state

from .architect import install
from .providers import Provider
from .worker import step


def run(
    project_root: str | Path,
    task: str,
    *,
    task_id: str,
    provider: Provider,
    max_steps: int = 40,
    default_actor: str = "worker",
) -> dict[str, Any]:
    """Install authority and execute a bounded, deterministic runner loop."""

    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("max_steps must be a positive integer")
    installed = install(project_root, task, task_id=task_id)
    current_actor = default_actor
    results: list[dict[str, Any]] = []
    notices: list[str] = []
    input_tokens = 0
    output_tokens = 0
    total_cost = 0.0
    cost_known = True
    halt_reason: str | None = None

    while len(results) < max_steps:
        verdict = circuit(project_root)
        if verdict.action == "halt":
            halt_reason = verdict.reason
            break
        if verdict.action == "switch_actor":
            current_actor = _next_actor(
                project_root,
                current_actor,
                verdict.signals["consecutive_rejections"].get("actor"),
            )
            notices.append(f"switch_actor: {current_actor}")
        if verdict.action == "tighten_budget":
            notices.append("tighten_budget requested; Blueprint left unchanged")

        pending = schedule(project_root)
        actor = pending[0]["actor"] if pending else current_actor
        result = step(
            project_root,
            actor=actor,
            task_id=task_id,
            task=task,
            provider=provider,
        )
        results.append(result)
        current_actor = actor
        input_tokens += result["input_tokens"]
        output_tokens += result["output_tokens"]
        if result["cost_usd"] is None:
            cost_known = False
        else:
            total_cost += result["cost_usd"]

    return {
        "blueprint": installed,
        "cost_usd": total_cost if cost_known else None,
        "final_state": state(project_root),
        "halt_reason": halt_reason,
        "notices": notices,
        "results": results,
        "steps": len(results),
        "tokens": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total": input_tokens + output_tokens,
        },
    }


def _next_actor(
    project_root: str | Path,
    current_actor: str,
    rejected_actor: Any,
) -> str:
    authority = blueprint(project_root)
    if not isinstance(authority, dict):
        return current_actor
    contracts = authority.get("contracts")
    if not isinstance(contracts, dict):
        return current_actor
    actors = contracts.get("actors")
    if not isinstance(actors, dict):
        return current_actor
    excluded = rejected_actor if isinstance(rejected_actor, str) else current_actor
    return next(
        (actor for actor in sorted(actors) if actor != excluded),
        current_actor,
    )
