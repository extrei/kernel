from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator

from kernel import (
    BlueprintError,
    StaleViewError,
    UnauthorizedWriteError,
    apply_patch,
    audit_schema,
    audit_views,
    blueprint,
    check_blueprint,
    contracts,
    meta_schema,
    schema,
    set_blueprint,
    state,
    verify,
    view,
)
from kernel.kernel import (
    _append_ledger_entry_locked,
    _canonical_json_bytes,
    _kernel_lock,
    _validated_kernel_state,
    initialize,
    store_object,
)


class BlueprintTests(unittest.TestCase):
    def test_valid_blueprint_installs_as_one_unchanged_state_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            definition = self._blueprint(
                self._plan_schema(),
                {
                    "worker": {
                        "read": ["/plan/status"],
                        "write": ["/plan/status"],
                    }
                },
            )
            genesis = self._kernel_state(project)["state_head"]

            record = set_blueprint(
                project,
                actor="human",
                task_id="install-blueprint",
                blueprint=definition,
            )

            kernel_state = self._kernel_state(project)
            entry = self._object_json(project, record.entry_hash)
            self.assertEqual(kernel_state["blueprint_head"], record.blueprint)
            self.assertEqual(record.payload_hash, record.blueprint)
            self.assertEqual(entry["blueprint"], record.blueprint)
            self.assertEqual(entry["kind"], "blueprint")
            self.assertEqual(entry["parent_state"], genesis)
            self.assertEqual(entry["state"], genesis)
            self.assertEqual(entry["version"], 5)
            self.assertEqual(blueprint(project), definition)
            self.assertEqual(schema(project), definition["schema"])
            self.assertEqual(contracts(project), definition["contracts"])

    def test_meta_schema_rejects_structure_before_semantic_checks(self) -> None:
        malformed = self._blueprint(
            {"type": "not-a-json-schema-type"},
            {"bad actor": {"budget": 0}},
        )
        malformed["unexpected"] = True

        with self.assertRaisesRegex(BlueprintError, "meta-schema"):
            check_blueprint(malformed)

    def test_meta_schema_is_valid_and_returned_as_an_independent_copy(self) -> None:
        first = meta_schema()
        Draft202012Validator.check_schema(first)
        first["properties"].clear()

        second = meta_schema()
        self.assertIn("contracts", second["properties"])
        self.assertIn("rules", second["properties"])
        self.assertIn("circuit", second["properties"])
        Draft202012Validator.check_schema(second)

    def test_schema_cannot_drop_a_path_still_granted_by_contracts(self) -> None:
        definition = self._blueprint(
            {
                "additionalProperties": False,
                "properties": {"result": {"type": "string"}},
                "type": "object",
            },
            {
                "worker": {
                    "read": ["/plan/status"],
                    "write": ["/plan/status"],
                }
            },
        )

        with self.assertRaisesRegex(BlueprintError, "absent from schema"):
            check_blueprint(definition)

    def test_blueprint_v1_is_rejected(self) -> None:
        definition = self._blueprint(None, {})
        definition["version"] = 1

        with self.assertRaisesRegex(BlueprintError, "version must be 2"):
            check_blueprint(definition)

    def test_workflow_rule_requires_declared_wake_actor_and_schema_path(self) -> None:
        actors = {
            "worker": {
                "read": ["/plan/status"],
                "write": ["/plan/status"],
            }
        }
        undeclared = self._blueprint(self._plan_schema(), actors)
        undeclared["rules"] = [
            {
                "on": {"op": "replace", "path": "/plan/status"},
                "wake": "verifier",
            }
        ]
        missing_path = self._blueprint(self._plan_schema(), actors)
        missing_path["rules"] = [
            {
                "on": {"op": "add", "path": "/missing"},
                "wake": "worker",
            }
        ]

        with self.assertRaisesRegex(BlueprintError, "undeclared actor"):
            check_blueprint(undeclared)
        with self.assertRaisesRegex(BlueprintError, "absent from schema"):
            check_blueprint(missing_path)

    def test_workflow_op_and_circuit_thresholds_fail_closed(self) -> None:
        invalid_op = self._blueprint(self._plan_schema(), {"worker": {}})
        invalid_op["rules"] = [
            {
                "on": {"op": "test", "path": "/plan/status"},
                "wake": "worker",
            }
        ]
        invalid_circuit = self._blueprint(self._plan_schema(), {"worker": {}})
        invalid_circuit["circuit"] = {"consecutive_rejections": 0}

        with self.assertRaisesRegex(BlueprintError, "op must be"):
            check_blueprint(invalid_op)
        with self.assertRaises(BlueprintError):
            check_blueprint(invalid_circuit)

    def test_blueprint_cannot_invalidate_live_state_and_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            apply_patch(
                project,
                actor="setup",
                task_id="live-state",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/name", "value": "kernel"}],
            )
            before_state = self._kernel_state(project)
            before_objects = self._object_names(project)
            incompatible = self._blueprint(
                {
                    "additionalProperties": False,
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                    "type": "object",
                },
                {},
            )

            with self.assertRaisesRegex(BlueprintError, "violates schema"):
                set_blueprint(
                    project,
                    actor="human",
                    task_id="live-state",
                    blueprint=incompatible,
                )

            self.assertEqual(self._kernel_state(project), before_state)
            self.assertEqual(self._object_names(project), before_objects)

    def test_collection_must_be_reachable_by_a_contract_pattern(self) -> None:
        definition = self._blueprint(
            {
                "properties": {
                    "actions": {
                        "items": {"type": "string"},
                        "type": "array",
                        "x-kernel-collection": True,
                    }
                },
                "type": "object",
            },
            {},
        )

        with self.assertRaisesRegex(BlueprintError, "collection path.*unreachable"):
            check_blueprint(definition)

    def test_clear_restores_null_authority_fail_closed_remove_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            base = apply_patch(
                project,
                actor="setup",
                task_id="clear-blueprint",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/temporary", "value": True}],
            )
            set_blueprint(
                project,
                actor="human",
                task_id="clear-blueprint",
                blueprint=self._blueprint(
                    None,
                    {
                        "worker": {
                            "allow_remove": True,
                            "budget": 1000,
                            "read": ["/temporary"],
                            "write": ["/temporary"],
                        }
                    },
                ),
            )
            cleared = set_blueprint(
                project,
                actor="human",
                task_id="clear-blueprint",
                blueprint=None,
            )

            self.assertIsNone(cleared.blueprint)
            self.assertIsNone(blueprint(project))
            with self.assertRaises(UnauthorizedWriteError):
                apply_patch(
                    project,
                    actor="worker",
                    task_id="clear-blueprint",
                    parent_state=base.state,
                    patch=[{"op": "remove", "path": "/temporary"}],
                )
            added = apply_patch(
                project,
                actor="worker",
                task_id="clear-blueprint",
                parent_state=base.state,
                patch=[{"op": "add", "path": "/next", "value": True}],
            )
            self.assertEqual(state(project, at=added.state)["next"], True)

    def test_history_and_audits_use_each_entry_blueprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            base = apply_patch(
                project,
                actor="setup",
                task_id="blueprint-history",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/value", "value": 1}],
            )
            first = set_blueprint(
                project,
                actor="human",
                task_id="blueprint-history",
                blueprint=self._blueprint(
                    {
                        "properties": {"value": {"minimum": 0, "type": "integer"}},
                        "required": ["value"],
                        "type": "object",
                    },
                    {
                        "worker": {
                            "budget": 1000,
                            "read": ["/value"],
                            "write": ["/value"],
                        }
                    },
                ),
            )
            worker_view = view(project, actor="worker")
            changed = apply_patch(
                project,
                actor="worker",
                task_id="blueprint-history",
                parent_state=base.state,
                view=worker_view.view_hash,
                patch=[{"op": "replace", "path": "/value", "value": 2}],
            )
            second = set_blueprint(
                project,
                actor="human",
                task_id="blueprint-history",
                blueprint=self._blueprint(
                    {
                        "properties": {"value": {"minimum": 2, "type": "integer"}},
                        "required": ["value"],
                        "type": "object",
                    },
                    {"observer": {"read": ["/value"], "write": []}},
                ),
            )

            changed_entry = self._object_json(project, changed.entry_hash)
            self.assertEqual(changed_entry["blueprint"], first.blueprint)
            self.assertNotEqual(first.blueprint, second.blueprint)
            self.assertTrue(all(item["valid"] for item in audit_schema(project)))
            view_audit = audit_views(project)
            self.assertTrue(view_audit[2]["valid"])

    def test_strict_verify_checks_invalid_blueprint_only_structurally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            invalid_reference = store_object(
                project, _canonical_json_bytes({"structurally": "present"})
            )
            state_tree = project / ".state-tree"
            with _kernel_lock(state_tree):
                kernel_state = _validated_kernel_state(state_tree)
                forged = _append_ledger_entry_locked(
                    state_tree,
                    kernel_state,
                    actor="forger",
                    kind="blueprint",
                    task_id="structural-verify",
                    payload_hash=invalid_reference,
                    metadata={},
                    parent_state=kernel_state["state_head"],
                    state=kernel_state["state_head"],
                    blueprint=invalid_reference,
                    view=None,
                    blueprint_transition=True,
                )

            self.assertEqual(verify(project, strict=True), forged.entry_hash)
            with self.assertRaises(BlueprintError):
                check_blueprint(blueprint(project))
            self.assertFalse(audit_schema(project)[-1]["valid"])

    def test_supplied_view_is_rebound_after_blueprint_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            base = apply_patch(
                project,
                actor="setup",
                task_id="blueprint-view",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/value", "value": 1}],
            )
            initial = self._blueprint(
                None,
                {
                    "worker": {
                        "read": ["/value"],
                        "write": ["/value"],
                    }
                },
            )
            set_blueprint(
                project,
                actor="human",
                task_id="blueprint-view",
                blueprint=initial,
            )
            old_view = view(project, actor="worker")
            changed_authority = self._blueprint(
                None,
                {
                    "worker": {
                        "budget": 1000,
                        "read": [],
                        "write": ["/value"],
                    }
                },
            )
            set_blueprint(
                project,
                actor="human",
                task_id="blueprint-view",
                blueprint=changed_authority,
            )

            with self.assertRaises(StaleViewError):
                apply_patch(
                    project,
                    actor="worker",
                    task_id="blueprint-view",
                    parent_state=base.state,
                    view=old_view.view_hash,
                    patch=[{"op": "replace", "path": "/value", "value": 2}],
                )

    @staticmethod
    def _plan_schema() -> dict[str, object]:
        return {
            "additionalProperties": False,
            "properties": {
                "plan": {
                    "additionalProperties": False,
                    "properties": {"status": {"type": "string"}},
                    "type": "object",
                }
            },
            "type": "object",
        }

    @staticmethod
    def _blueprint(
        schema: dict[str, object] | None,
        actors: dict[str, object],
    ) -> dict[str, object]:
        return {
            "version": 2,
            "schema": schema,
            "contracts": {"version": 2, "actors": actors},
        }

    @staticmethod
    def _kernel_state(project: Path) -> dict[str, object]:
        return json.loads(
            (project / ".state-tree" / "kernel.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def _object_directory(project: Path) -> Path:
        return project / ".state-tree" / "objects" / "sha256"

    @classmethod
    def _object_names(cls, project: Path) -> set[str]:
        return {path.name for path in cls._object_directory(project).iterdir()}

    @classmethod
    def _object_json(cls, project: Path, reference: str) -> dict[str, object]:
        path = cls._object_directory(project) / reference.removeprefix("sha256:")
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
