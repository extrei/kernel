from __future__ import annotations

import contextlib
from dataclasses import asdict
import io
import json
from pathlib import Path
import tempfile
import unittest

from kernel import (
    StaleParentError,
    UnauthorizedWriteError,
    apply_patch,
    circuit,
    events,
    record_failure,
    schedule,
    set_blueprint,
)
from kernel.cli import main
from kernel.kernel import initialize


class CircuitTests(unittest.TestCase):
    def test_repeated_calls_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            parent = self._kernel_state(project)["state_head"]
            apply_patch(
                project,
                actor="worker",
                task_id="pure-circuit",
                parent_state=parent,
                patch=[{"op": "add", "path": "/ready", "value": True}],
            )
            before = self._tree_bytes(project)

            first = circuit(project)
            second = circuit(project)
            self.assertEqual(schedule(project), [])

            self.assertEqual(self._canonical(asdict(first)), self._canonical(asdict(second)))
            self.assertEqual(first.action, "continue")
            self.assertEqual(self._tree_bytes(project), before)

    def test_two_consecutive_auth_rejections_switch_actor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_blueprint(
                project,
                actor="human",
                task_id="auth-circuit",
                blueprint=self._blueprint(
                    None,
                    {"worker": {"read": [], "write": ["/allowed"]}},
                ),
            )
            parent = self._kernel_state(project)["state_head"]
            for path in ("/blocked-one", "/blocked-two"):
                with self.assertRaises(UnauthorizedWriteError):
                    apply_patch(
                        project,
                        actor="worker",
                        task_id="auth-circuit",
                        parent_state=parent,
                        patch=[{"op": "add", "path": path, "value": True}],
                    )

            verdict = circuit(project)

            self.assertEqual(verdict.action, "switch_actor")
            self.assertEqual(
                verdict.signals["consecutive_rejections"],
                {"actor": "worker", "count": 2, "threshold": 2},
            )

    def test_failure_entries_count_and_are_named_by_the_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            record_failure(
                project,
                actor="worker",
                task_id="failure-circuit",
                stage="view",
                reason="first view failure",
            )
            record_failure(
                project,
                actor="worker",
                task_id="failure-circuit",
                stage="view",
                reason="second view failure",
            )

            verdict = circuit(project)

            self.assertEqual(verdict.action, "switch_actor")
            self.assertIn("failure", verdict.reason)
            self.assertEqual(
                verdict.signals["consecutive_rejections"],
                {"actor": "worker", "count": 2, "threshold": 2},
            )

    def test_identical_failure_payloads_have_a_failure_specific_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            for _ in range(2):
                record_failure(
                    project,
                    actor="worker",
                    task_id="repeated-failure",
                    stage="provider",
                    reason="provider unavailable",
                )

            verdict = circuit(project)

            self.assertEqual(verdict.action, "halt")
            self.assertEqual(verdict.reason, "actor repeated an identical failure")

    def test_blueprint_circuit_policy_overrides_both_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_blueprint(
                project,
                actor="human",
                task_id="custom-circuit",
                blueprint=self._blueprint(
                    None,
                    {"worker": {"read": [], "write": ["/allowed"]}},
                    circuit={"consecutive_rejections": 3, "cycle_window": 5},
                ),
            )
            parent = self._kernel_state(project)["state_head"]
            for path in ("/blocked-one", "/blocked-two"):
                with self.assertRaises(UnauthorizedWriteError):
                    apply_patch(
                        project,
                        actor="worker",
                        task_id="custom-circuit",
                        parent_state=parent,
                        patch=[{"op": "add", "path": path, "value": True}],
                    )

            verdict = circuit(project)

            self.assertEqual(verdict.action, "retry")
            self.assertEqual(
                verdict.signals["consecutive_rejections"]["threshold"], 3
            )
            self.assertEqual(verdict.signals["cycle"]["window"], 5)

    def test_two_stale_parent_rejections_are_discounted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            stale = f"sha256:{'0' * 64}"
            for path in ("/first", "/second"):
                with self.assertRaises(StaleParentError):
                    apply_patch(
                        project,
                        actor="worker",
                        task_id="stale-circuit",
                        parent_state=stale,
                        patch=[{"op": "add", "path": path, "value": True}],
                    )

            verdict = circuit(project)

            self.assertEqual(verdict.action, "continue")
            self.assertEqual(
                verdict.signals["consecutive_rejections"]["count"], 0
            )

    def test_return_to_an_earlier_state_is_a_halting_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_blueprint(
                project,
                actor="human",
                task_id="state-cycle",
                blueprint=self._blueprint(
                    None,
                    {
                        "worker": {
                            "allow_remove": True,
                            "read": ["/value"],
                            "write": ["/value"],
                        }
                    },
                ),
            )
            genesis = self._kernel_state(project)["state_head"]
            added = apply_patch(
                project,
                actor="worker",
                task_id="state-cycle",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/value", "value": 1}],
            )
            apply_patch(
                project,
                actor="worker",
                task_id="state-cycle",
                parent_state=added.state,
                patch=[{"op": "remove", "path": "/value"}],
            )

            verdict = circuit(project)

            self.assertEqual(verdict.action, "halt")
            self.assertTrue(verdict.signals["cycle"]["detected"])
            self.assertEqual(verdict.signals["cycle"]["state"], genesis)

    def test_one_no_op_patch_requests_a_tighter_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            parent = self._kernel_state(project)["state_head"]
            record = apply_patch(
                project,
                actor="worker",
                task_id="no-op",
                parent_state=parent,
                patch=[],
            )

            verdict = circuit(project)

            self.assertEqual(verdict.action, "tighten_budget")
            self.assertEqual(verdict.signals["no_op_commits"], [record.sequence])

    def test_schedule_matches_newest_patch_and_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            base = apply_patch(
                project,
                actor="setup",
                task_id="workflow-rule",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/claims", "value": {}}],
            )
            definition = self._blueprint(
                {
                    "additionalProperties": False,
                    "properties": {
                        "claims": {
                            "additionalProperties": {"type": "string"},
                            "type": "object",
                        }
                    },
                    "required": ["claims"],
                    "type": "object",
                },
                {
                    "codex": {
                        "read": ["/claims/*"],
                        "write": ["/claims/*"],
                    },
                    "verifier": {"read": ["/claims/*"], "write": []},
                },
                rules=[
                    {
                        "on": {"op": "add", "path": "/claims/*"},
                        "wake": "verifier",
                    }
                ],
            )
            set_blueprint(
                project,
                actor="human",
                task_id="workflow-rule",
                blueprint=definition,
            )
            apply_patch(
                project,
                actor="codex",
                task_id="workflow-rule",
                parent_state=base.state,
                patch=[{"op": "add", "path": "/claims/one", "value": "draft"}],
            )

            expected = [
                {
                    "actor": "verifier",
                    "event": {"op": "add", "path": "/claims/one"},
                }
            ]
            self.assertEqual(schedule(project), expected)
            self.assertEqual(
                events(
                    {
                        "kind": "patch",
                        "patch": [
                            {"op": "add", "path": "/claims/one", "value": "draft"}
                        ],
                    },
                    definition,
                ),
                expected,
            )

    def test_schedule_is_empty_without_a_matching_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            parent = self._kernel_state(project)["state_head"]
            apply_patch(
                project,
                actor="worker",
                task_id="no-rule",
                parent_state=parent,
                patch=[{"op": "add", "path": "/value", "value": 1}],
            )

            self.assertEqual(schedule(project), [])

    def test_cli_prints_circuit_json_and_marks_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            parent = self._kernel_state(project)["state_head"]
            with self.assertRaises(UnauthorizedWriteError):
                apply_patch(
                    project,
                    actor="worker",
                    task_id="cli-rejection",
                    parent_state=parent,
                    patch=[{"op": "remove", "path": "/missing"}],
                )

            circuit_output = io.StringIO()
            with contextlib.redirect_stdout(circuit_output):
                self.assertEqual(main(["circuit", str(project)]), 0)
            self.assertEqual(
                json.loads(circuit_output.getvalue())["action"], "retry"
            )

            log_output = io.StringIO()
            with contextlib.redirect_stdout(log_output):
                self.assertEqual(main(["log", str(project)]), 0)
            self.assertTrue(log_output.getvalue().rstrip().endswith("[REJECTED]"))

    @staticmethod
    def _blueprint(
        schema: dict[str, object] | None,
        actors: dict[str, object],
        *,
        circuit: dict[str, int] | None = None,
        rules: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        document: dict[str, object] = {
            "version": 3,
            "schema": schema,
            "contracts": {"version": 2, "actors": actors},
        }
        if rules is not None:
            document["rules"] = rules
        if circuit is not None:
            document["circuit"] = circuit
        return document

    @staticmethod
    def _canonical(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _kernel_state(project: Path) -> dict[str, object]:
        return json.loads(
            (project / ".state-tree" / "kernel.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def _tree_bytes(project: Path) -> dict[str, bytes]:
        state_tree = project / ".state-tree"
        return {
            str(path.relative_to(state_tree)): path.read_bytes()
            for path in state_tree.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
