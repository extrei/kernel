from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kernel import apply_patch, initialize, set_blueprint
from runner.loop import run
from runner.providers import Completion


class _FakeProvider:
    name = "fake"

    def __init__(self, patches: list[list[dict[str, object]]]) -> None:
        self.patches = iter(patches)
        self.calls: list[dict[str, object]] = []

    def preflight(self) -> None:
        return None

    def complete(self, **arguments: object) -> Completion:
        self.calls.append(arguments)
        return Completion(json.dumps(next(self.patches)), 10, 5, 0.01)


class RunnerLoopTests(unittest.TestCase):
    def test_two_identical_refusals_halt_the_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            authority = self._blueprint(
                {"worker": {"read": [], "write": ["/allowed"]}}
            )
            set_blueprint(
                project,
                actor="architect",
                task_id="loop-halt",
                blueprint=authority,
            )
            refused = [{"op": "add", "path": "/blocked", "value": True}]
            provider = _FakeProvider([refused, refused])

            with patch("runner.loop.install", return_value=authority):
                result = run(
                    project,
                    "write result",
                    task_id="loop-halt",
                    provider=provider,
                    max_steps=10,
                )

            self.assertEqual(result["steps"], 2)
            self.assertEqual(result["halt_reason"], "actor repeated an identical payload")
            self.assertEqual(result["tokens"]["input_tokens"], 20)
            self.assertEqual(result["tokens"]["output_tokens"], 10)
            self.assertEqual(result["cost_usd"], 0.02)
            self.assertTrue(all(not item["accepted"] for item in result["results"]))

    def test_matching_schedule_runs_the_wake_actor_next(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            parent = self._kernel_state(project)["state_head"]
            apply_patch(
                project,
                actor="setup",
                task_id="schedule-loop",
                parent_state=parent,
                patch=[
                    {"op": "add", "path": "/task", "value": 0},
                    {"op": "add", "path": "/review", "value": 0},
                ],
            )
            authority = self._blueprint(
                {
                    "worker": {"read": ["/task"], "write": ["/task"]},
                    "verifier": {"read": ["/review"], "write": ["/review"]},
                },
                rules=[
                    {
                        "on": {"op": "replace", "path": "/task"},
                        "wake": "verifier",
                    }
                ],
            )
            set_blueprint(
                project,
                actor="architect",
                task_id="schedule-loop",
                blueprint=authority,
            )
            provider = _FakeProvider(
                [
                    [{"op": "replace", "path": "/task", "value": 1}],
                    [{"op": "replace", "path": "/review", "value": 1}],
                ]
            )

            with patch("runner.loop.install", return_value=authority):
                result = run(
                    project,
                    "run scheduled work",
                    task_id="schedule-loop",
                    provider=provider,
                    max_steps=2,
                )

            self.assertEqual(
                [item["actor"] for item in result["results"]],
                ["worker", "verifier"],
            )
            self.assertEqual(result["final_state"], {"review": 1, "task": 1})

    @staticmethod
    def _blueprint(
        actors: dict[str, object],
        *,
        rules: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        document: dict[str, object] = {
            "version": 2,
            "schema": None,
            "contracts": {"version": 2, "actors": actors},
        }
        if rules is not None:
            document["rules"] = rules
        return document

    @staticmethod
    def _kernel_state(project: Path) -> dict[str, object]:
        return json.loads(
            (project / ".state-tree" / "kernel.json").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
