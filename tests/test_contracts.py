from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from kernel import (
    ContractError,
    UnauthorizedWriteError,
    apply_patch,
    audit_contracts,
    authorize,
    contracts,
    set_contracts,
    set_schema,
    state,
    verify,
)
from kernel.kernel import (
    _append_ledger_entry_locked,
    _canonical_json_bytes,
    _kernel_lock,
    _validated_kernel_state,
    initialize,
    store_object,
)


class ContractTests(unittest.TestCase):
    def test_no_contract_allows_non_remove_operations_but_denies_remove(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]

            created = apply_patch(
                project,
                actor="worker",
                task_id="uncontracted",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/value", "value": 1}],
            )
            replaced = apply_patch(
                project,
                actor="worker",
                task_id="uncontracted",
                parent_state=created.state,
                patch=[
                    {"op": "test", "path": "/value", "value": 1},
                    {"op": "replace", "path": "/value", "value": 2},
                ],
            )

            before_state = self._kernel_state(project)
            before_objects = self._object_names(project)
            with self.assertRaises(UnauthorizedWriteError):
                apply_patch(
                    project,
                    actor="worker",
                    task_id="uncontracted",
                    parent_state=replaced.state,
                    patch=[{"op": "remove", "path": "/value"}],
                )

            self.assertEqual(state(project), {"value": 2})
            self.assertEqual(self._kernel_state(project), before_state)
            self.assertEqual(self._object_names(project), before_objects)

    def test_contract_commit_and_later_entry_carry_the_contract_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            definition = self._contract(write=["/plan"])

            contract_record = set_contracts(
                project,
                actor="human",
                task_id="install-contract",
                contracts=definition,
            )

            kernel_state = self._kernel_state(project)
            contract_entry = self._object_json(project, contract_record.entry_hash)
            self.assertEqual(kernel_state["contracts_head"], contract_record.contracts)
            self.assertEqual(contract_record.payload_hash, contract_record.contracts)
            self.assertEqual(contract_entry["kind"], "contracts")
            self.assertEqual(contract_entry["contracts"], contract_record.contracts)
            self.assertEqual(contract_entry["parent_state"], contract_entry["state"])
            self.assertEqual(contracts(project), definition)

            changed = apply_patch(
                project,
                actor="worker",
                task_id="contracted-write",
                parent_state=kernel_state["state_head"],
                patch=[{"op": "add", "path": "/plan", "value": {}}],
            )
            patch_entry = self._object_json(project, changed.entry_hash)
            self.assertEqual(patch_entry["contracts"], contract_record.contracts)
            self.assertEqual(
                patch_entry["contracts"], self._kernel_state(project)["contracts_head"]
            )
            self.assertEqual(verify(project, strict=True), changed.entry_hash)

    def test_patterns_are_full_segment_matches(self) -> None:
        exact = self._contract(write=["/plan"])
        authorize([("add", "/plan")], exact, actor="worker")
        with self.assertRaises(UnauthorizedWriteError):
            authorize([("add", "/plan/status")], exact, actor="worker")

        wildcard = self._contract(write=["/plan/*", "/items/*"])
        authorize([("add", "/plan/status")], wildcard, actor="worker")
        authorize([("add", "/items/-")], wildcard, actor="worker")
        with self.assertRaises(UnauthorizedWriteError):
            authorize([("add", "/plan/a/b")], wildcard, actor="worker")
        with self.assertRaises(UnauthorizedWriteError):
            authorize([("add", "/")], exact, actor="worker")

    def test_granted_write_commits_and_sibling_rejection_has_no_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            base = apply_patch(
                project,
                actor="setup",
                task_id="contract-paths",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/plan", "value": {}}],
            )
            set_contracts(
                project,
                actor="human",
                task_id="contract-paths",
                contracts=self._contract(write=["/plan/*"]),
            )

            accepted = apply_patch(
                project,
                actor="worker",
                task_id="contract-paths",
                parent_state=base.state,
                patch=[{"op": "add", "path": "/plan/status", "value": "draft"}],
            )
            before_state = self._kernel_state(project)
            before_objects = self._object_names(project)
            with self.assertRaises(UnauthorizedWriteError):
                apply_patch(
                    project,
                    actor="worker",
                    task_id="contract-paths",
                    parent_state=accepted.state,
                    patch=[{"op": "add", "path": "/sibling", "value": True}],
                )

            self.assertEqual(state(project), {"plan": {"status": "draft"}})
            self.assertEqual(self._kernel_state(project), before_state)
            self.assertEqual(self._object_names(project), before_objects)

    def test_unknown_actor_and_write_to_read_only_path_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            base = apply_patch(
                project,
                actor="setup",
                task_id="read-contract",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/task", "value": "qk-14"}],
            )
            definition = {
                "version": 2,
                "actors": {"verifier": {"read": ["/task"], "write": []}},
            }
            set_contracts(
                project,
                actor="human",
                task_id="read-contract",
                contracts=definition,
            )

            tested = apply_patch(
                project,
                actor="verifier",
                task_id="read-contract",
                parent_state=base.state,
                patch=[{"op": "test", "path": "/task", "value": "qk-14"}],
            )
            with self.assertRaises(UnauthorizedWriteError):
                apply_patch(
                    project,
                    actor="verifier",
                    task_id="read-contract",
                    parent_state=tested.state,
                    patch=[{"op": "replace", "path": "/task", "value": "qk-15"}],
                )
            with self.assertRaises(UnauthorizedWriteError):
                apply_patch(
                    project,
                    actor="intruder",
                    task_id="read-contract",
                    parent_state=tested.state,
                    patch=[],
                )

    def test_remove_privilege_comes_only_from_the_active_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            base = apply_patch(
                project,
                actor="setup",
                task_id="remove-contract",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/temporary", "value": True}],
            )
            set_contracts(
                project,
                actor="human",
                task_id="remove-contract",
                contracts=self._contract(write=["/temporary"]),
            )
            with self.assertRaises(UnauthorizedWriteError):
                apply_patch(
                    project,
                    actor="worker",
                    task_id="remove-contract",
                    parent_state=base.state,
                    patch=[{"op": "remove", "path": "/temporary"}],
                )

            set_contracts(
                project,
                actor="human",
                task_id="remove-contract",
                contracts=self._contract(
                    write=["/temporary"], allow_remove=True
                ),
            )
            removed = apply_patch(
                project,
                actor="worker",
                task_id="remove-contract",
                parent_state=base.state,
                patch=[{"op": "remove", "path": "/temporary"}],
            )
            self.assertEqual(state(project, at=removed.state), {})

    def test_authorization_precedes_patch_and_schema_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            schema = {
                "type": "object",
                "properties": {"plan": {"type": "string"}},
                "additionalProperties": False,
            }
            set_schema(
                project,
                actor="human",
                task_id="auth-order",
                schema=schema,
            )
            set_contracts(
                project,
                actor="human",
                task_id="auth-order",
                contracts=self._contract(write=["/plan"]),
            )
            kernel_state = self._kernel_state(project)

            with self.assertRaises(UnauthorizedWriteError):
                apply_patch(
                    project,
                    actor="worker",
                    task_id="auth-order",
                    parent_state=kernel_state["state_head"],
                    patch=[
                        {"op": "add", "path": "/forbidden", "value": {"bad": True}}
                    ],
                )

    def test_contract_paths_must_exist_in_the_active_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_schema(
                project,
                actor="human",
                task_id="contract-schema",
                schema={
                    "type": "object",
                    "properties": {
                        "plan": {
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                            "additionalProperties": False,
                        }
                    },
                    "additionalProperties": False,
                },
            )

            set_contracts(
                project,
                actor="human",
                task_id="contract-schema",
                contracts=self._contract(write=["/plan/*"]),
            )
            with self.assertRaisesRegex(ContractError, "absent from schema"):
                set_contracts(
                    project,
                    actor="human",
                    task_id="contract-schema",
                    contracts=self._contract(write=["/missing"]),
                )

    def test_malformed_contracts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            invalid = (
                {"version": 1, "actors": {}},
                {"version": 2, "actors": {"bad actor": {"write": ["/x"]}}},
                {"version": 2, "actors": {"worker": {"write": ["not/a/pointer"]}}},
                {"version": 2, "actors": {"worker": {"budget": 0}}},
                {"version": 2, "actors": {"worker": {"budget": True}}},
            )
            for definition in invalid:
                with self.subTest(definition=definition):
                    with self.assertRaises(ContractError):
                        set_contracts(
                            project,
                            actor="human",
                            task_id="invalid-contract",
                            contracts=definition,
                        )

    def test_strict_verify_is_structural_and_human_audit_reports_violation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_contracts(
                project,
                actor="human",
                task_id="audit-contract",
                contracts=self._contract(write=["/allowed"]),
            )
            patch = [{"op": "add", "path": "/blocked", "value": True}]
            patch_hash = store_object(project, _canonical_json_bytes(patch))
            state_hash = store_object(
                project, _canonical_json_bytes({"blocked": True})
            )

            state_tree = project / ".state-tree"
            with _kernel_lock(state_tree):
                kernel_state = _validated_kernel_state(state_tree)
                forged = _append_ledger_entry_locked(
                    state_tree,
                    kernel_state,
                    actor="worker",
                    kind="patch",
                    task_id="audit-contract",
                    payload_hash=patch_hash,
                    metadata={},
                    parent_state=kernel_state["state_head"],
                    state=state_hash,
                    schema=kernel_state["schema_head"],
                    contracts=kernel_state["contracts_head"],
                    view=None,
                )

            self.assertEqual(verify(project, strict=True), forged.entry_hash)
            verdicts = audit_contracts(project)
            self.assertTrue(verdicts[-1]["patch"])
            self.assertFalse(verdicts[-1]["valid"])
            self.assertIn("no write grant", verdicts[-1]["error"])

    @staticmethod
    def _contract(
        *,
        write: list[str],
        read: list[str] | None = None,
        allow_remove: bool = False,
    ) -> dict[str, object]:
        return {
            "version": 2,
            "actors": {
                "worker": {
                    "write": write,
                    "read": [] if read is None else read,
                    "allow_remove": allow_remove,
                }
            },
        }

    @staticmethod
    def _kernel_state(project: Path) -> dict[str, object]:
        return json.loads((project / ".state-tree" / "kernel.json").read_text())

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
