from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from kernel import (
    apply_patch,
    circuit,
    entries,
    initialize,
    set_blueprint,
    state,
)
from runner.providers import Completion
from runner.worker import step


class _FakeProvider:
    name = "fake"

    def __init__(
        self,
        texts: list[str],
        *,
        callbacks: list[object] | None = None,
    ) -> None:
        self.texts = iter(texts)
        self.callbacks = iter(callbacks or [])
        self.calls: list[dict[str, object]] = []

    def preflight(self) -> None:
        return None

    def complete(self, **arguments: object) -> Completion:
        self.calls.append(arguments)
        callback = next(self.callbacks, None)
        if callable(callback):
            callback()
        return Completion(next(self.texts), 11, 7, 0.001)


class RunnerWorkerTests(unittest.TestCase):
    def test_refusal_is_relayed_and_appears_in_the_next_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_blueprint(
                project,
                actor="architect",
                task_id="worker-refusal",
                blueprint=self._blueprint(
                    {"worker": {"read": [], "write": ["/allowed"]}}
                ),
            )
            before = self._kernel_state(project)
            patch_text = json.dumps(
                [{"op": "add", "path": "/blocked", "value": True}]
            )
            provider = _FakeProvider([patch_text, patch_text])

            first = step(
                project,
                actor="worker",
                task_id="worker-refusal",
                task="write the result",
                provider=provider,
            )
            second = step(
                project,
                actor="worker",
                task_id="worker-refusal",
                task="write the result",
                provider=provider,
            )

            self.assertFalse(first["accepted"])
            self.assertFalse(second["accepted"])
            self.assertEqual(first["error_type"], "UnauthorizedWriteError")
            self.assertIn("no write grant", first["error"])
            self.assertEqual(self._kernel_state(project)["state_head"], before["state_head"])
            self.assertEqual(state(project), {})
            self.assertEqual(entries(project)[-1]["kind"], "rejection")
            next_prompt = provider.calls[1]["messages"][0]["content"]
            self.assertIn("no write grant", next_prompt)
            self.assertEqual(circuit(project).action, "halt")

    def test_stale_view_is_relayed_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            parent = self._kernel_state(project)["state_head"]
            base = apply_patch(
                project,
                actor="setup",
                task_id="stale-view",
                parent_state=parent,
                patch=[
                    {"op": "add", "path": "/value", "value": 1},
                    {"op": "add", "path": "/other", "value": 0},
                ],
            )
            set_blueprint(
                project,
                actor="architect",
                task_id="stale-view",
                blueprint=self._blueprint(
                    {"worker": {"read": ["/value"], "write": ["/value"]}}
                ),
            )

            def change_view_authority() -> None:
                set_blueprint(
                    project,
                    actor="architect",
                    task_id="stale-view",
                    blueprint=self._blueprint(
                        {
                            "worker": {
                                "read": ["/value", "/other"],
                                "write": ["/value"],
                            }
                        }
                    ),
                )

            provider = _FakeProvider(
                [json.dumps([{"op": "replace", "path": "/value", "value": 2}])],
                callbacks=[change_view_authority],
            )

            result = step(
                project,
                actor="worker",
                task_id="stale-view",
                task="change value",
                provider=provider,
            )

            self.assertFalse(result["accepted"])
            self.assertEqual(result["error_type"], "StaleViewError")
            self.assertEqual(result["parent_state"], base.state)
            self.assertEqual(state(project)["value"], 1)
            self.assertEqual(entries(project)[-1]["kind"], "rejection")

    @staticmethod
    def _blueprint(actors: dict[str, object]) -> dict[str, object]:
        return {
            "version": 2,
            "schema": None,
            "contracts": {"version": 2, "actors": actors},
        }

    @staticmethod
    def _kernel_state(project: Path) -> dict[str, object]:
        return json.loads(
            (project / ".state-tree" / "kernel.json").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
