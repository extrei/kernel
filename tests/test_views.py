from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from kernel import (
    StaleViewError,
    ViewError,
    apply_patch,
    audit_views,
    derive_view,
    set_contracts,
    view,
    verify,
)
from kernel.kernel import (
    _append_ledger_entry_locked,
    _canonical_json_bytes,
    _kernel_lock,
    _validated_kernel_state,
    initialize,
    read_object,
    store_object,
)


class ViewDerivationTests(unittest.TestCase):
    def test_view_contains_only_granted_paths_and_schema_fragments(self) -> None:
        snapshot = {
            "plan": {"secret": "hidden", "status": "draft"},
            "private": True,
            "task": "qk-14",
        }
        contract = self._contract(
            {
                "planner": {
                    "read": ["/plan/status", "/task"],
                    "write": [],
                }
            }
        )
        schema = {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "properties": {
                        "secret": {"type": "string"},
                        "status": {"type": "string"},
                    },
                },
                "private": {"type": "boolean"},
                "task": {"type": "string"},
            },
        }

        document = derive_view(snapshot, contract, schema, "planner")

        self.assertEqual(document["plan"], {"status": "draft"})
        self.assertEqual(document["task"], "qk-14")
        self.assertNotIn("private", document)
        self.assertNotIn("secret", document["plan"])
        self.assertEqual(
            set(document["$schema"]), {"/plan/status", "/task"}
        )
        self.assertEqual(
            document["$schema"]["/plan/status"], {"type": "string"}
        )

    def test_repeated_derivation_is_byte_identical_and_actor_specific(self) -> None:
        snapshot = {"left": {"value": 1}, "right": {"value": 2}}
        contract = self._contract(
            {
                "left-reader": {"read": ["/left"], "write": []},
                "right-reader": {"read": ["/right"], "write": []},
            }
        )

        first = derive_view(snapshot, contract, None, "left-reader")
        repeated = derive_view(snapshot, contract, None, "left-reader")
        other = derive_view(snapshot, contract, None, "right-reader")

        self.assertEqual(self._canonical(first), self._canonical(repeated))
        self.assertNotEqual(self._canonical(first), self._canonical(other))
        self.assertEqual(first, {"left": {"value": 1}})
        self.assertEqual(other, {"right": {"value": 2}})

    def test_budget_elision_is_deterministic_and_resolvable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            state_record = apply_patch(
                project,
                actor="setup",
                task_id="view-budget",
                parent_state=genesis,
                patch=[
                    {
                        "op": "add",
                        "path": "/large",
                        "value": {
                            "alpha": "a" * 800,
                            "beta": "b" * 800,
                            "small": "kept",
                        },
                    }
                ],
            )
            set_contracts(
                project,
                actor="human",
                task_id="view-budget",
                contracts=self._contract(
                    {
                        "worker": {
                            "budget": 1000,
                            "read": ["/large/*"],
                            "write": ["/large/*"],
                        }
                    }
                ),
            )

            first = view(project, actor="worker", at=state_record.state)
            repeated = view(project, actor="worker", at=state_record.state)

            self.assertEqual(first.view_hash, repeated.view_hash)
            self.assertEqual(self._canonical(first.document), self._canonical(repeated.document))
            self.assertLessEqual(len(self._canonical(first.document)), 1000)
            markers = self._elided_markers(first.document)
            self.assertTrue(markers)
            for marker in markers:
                content = read_object(project, marker["hash"])
                self.assertEqual(len(content), marker["bytes"])
                self.assertIn(json.loads(content), {"a" * 800, "b" * 800})
            self.assertEqual(
                json.loads(read_object(project, first.view_hash)), first.document
            )

    def test_budget_that_cannot_hold_elision_markers_fails(self) -> None:
        contract = self._contract(
            {
                "worker": {
                    "budget": 10,
                    "read": ["/value"],
                    "write": [],
                }
            }
        )

        with self.assertRaisesRegex(ViewError, "cannot fit"):
            derive_view({"value": "x" * 200}, contract, None, "worker")

    def test_collection_reference_is_never_hydrated(self) -> None:
        reference = f"sha256:{'a' * 64}"
        snapshot = {"items": {"$collection": reference}, "private": True}
        contract = self._contract(
            {"worker": {"read": ["/items/*"], "write": []}}
        )

        document = derive_view(snapshot, contract, None, "worker")

        self.assertEqual(document, {"items": {"$collection": reference}})

    def test_budgeted_patch_requires_and_records_the_derived_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            base = apply_patch(
                project,
                actor="setup",
                task_id="view-binding",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/value", "value": "draft"}],
            )
            set_contracts(
                project,
                actor="human",
                task_id="view-binding",
                contracts=self._contract(
                    {
                        "worker": {
                            "budget": 1000,
                            "read": ["/value"],
                            "write": ["/value"],
                        }
                    }
                ),
            )

            before_state = self._kernel_state(project)
            before_objects = self._object_names(project)
            with self.assertRaisesRegex(ViewError, "requires a view"):
                apply_patch(
                    project,
                    actor="worker",
                    task_id="view-binding",
                    parent_state=base.state,
                    patch=[
                        {"op": "replace", "path": "/value", "value": "ready"}
                    ],
                )
            self.assertEqual(self._kernel_state(project), before_state)
            self.assertEqual(self._object_names(project), before_objects)

            worker_view = view(project, actor="worker")
            committed = apply_patch(
                project,
                actor="worker",
                task_id="view-binding",
                parent_state=base.state,
                view=worker_view.view_hash,
                patch=[
                    {"op": "replace", "path": "/value", "value": "ready"}
                ],
            )
            entry = self._object_json(project, committed.entry_hash)
            self.assertEqual(entry["view"], worker_view.view_hash)

    def test_old_or_forged_view_is_a_stale_view_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            base = apply_patch(
                project,
                actor="setup",
                task_id="stale-view",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/value", "value": 1}],
            )
            set_contracts(
                project,
                actor="human",
                task_id="stale-view",
                contracts=self._contract(
                    {
                        "competitor": {
                            "read": ["/value"],
                            "write": ["/value"],
                        },
                        "worker": {
                            "budget": 1000,
                            "read": ["/value"],
                            "write": ["/value"],
                        },
                    }
                ),
            )
            old_view = view(project, actor="worker")
            moved = apply_patch(
                project,
                actor="competitor",
                task_id="stale-view",
                parent_state=base.state,
                patch=[{"op": "replace", "path": "/value", "value": 2}],
            )

            with self.assertRaises(StaleViewError):
                apply_patch(
                    project,
                    actor="worker",
                    task_id="stale-view",
                    parent_state=moved.state,
                    view=old_view.view_hash,
                    patch=[{"op": "replace", "path": "/value", "value": 3}],
                )

            forged = store_object(project, _canonical_json_bytes({"forged": True}))
            with self.assertRaises(StaleViewError):
                apply_patch(
                    project,
                    actor="worker",
                    task_id="stale-view",
                    parent_state=moved.state,
                    view=forged,
                    patch=[{"op": "replace", "path": "/value", "value": 3}],
                )

    def test_verify_stays_structural_while_view_audit_reports_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_contracts(
                project,
                actor="human",
                task_id="view-audit",
                contracts=self._contract(
                    {
                        "worker": {
                            "budget": 1000,
                            "read": ["/value"],
                            "write": ["/value"],
                        }
                    }
                ),
            )
            patch = [{"op": "add", "path": "/value", "value": "done"}]
            patch_hash = store_object(project, _canonical_json_bytes(patch))
            state_hash = store_object(
                project, _canonical_json_bytes({"value": "done"})
            )
            forged_view = store_object(
                project, _canonical_json_bytes({"forged": True})
            )

            state_tree = project / ".state-tree"
            with _kernel_lock(state_tree):
                kernel_state = _validated_kernel_state(state_tree)
                forged = _append_ledger_entry_locked(
                    state_tree,
                    kernel_state,
                    actor="worker",
                    kind="patch",
                    task_id="view-audit",
                    payload_hash=patch_hash,
                    metadata={},
                    parent_state=kernel_state["state_head"],
                    state=state_hash,
                    schema=kernel_state["schema_head"],
                    contracts=kernel_state["contracts_head"],
                    view=forged_view,
                )

            self.assertEqual(verify(project, strict=True), forged.entry_hash)
            verdicts = audit_views(project)
            self.assertFalse(verdicts[-1]["valid"])
            self.assertIn("does not match", verdicts[-1]["error"])

    @staticmethod
    def _contract(actors: dict[str, object]) -> dict[str, object]:
        return {"version": 2, "actors": actors}

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

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

    @classmethod
    def _elided_markers(cls, value: object) -> list[dict[str, object]]:
        if isinstance(value, dict):
            if set(value) == {"$elided"}:
                return [value["$elided"]]
            result = []
            for child in value.values():
                result.extend(cls._elided_markers(child))
            return result
        if isinstance(value, list):
            result = []
            for child in value:
                result.extend(cls._elided_markers(child))
            return result
        return []


if __name__ == "__main__":
    unittest.main()
