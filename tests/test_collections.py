import json
from pathlib import Path
import tempfile
import unittest

from kernel import (
    SchemaError,
    apply_patch,
    audit_schema,
    collection,
    set_schema,
    state,
)
from kernel.kernel import LedgerIntegrityError, initialize, verify


class CollectionTests(unittest.TestCase):
    def test_marked_array_is_externalized_and_patch_hydrates_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            schema_record = set_schema(
                project,
                actor="human",
                task_id="collections",
                schema=self._collection_schema(),
            )
            parent = self._kernel_state(project)["state_head"]
            first = apply_patch(
                project,
                actor="codex",
                task_id="collections",
                parent_state=parent,
                patch=[
                    {
                        "op": "add",
                        "path": "/actions",
                        "value": [{"name": "inspect"}],
                    }
                ],
            )

            persisted = state(project)
            reference = persisted["actions"]["$collection"]
            self.assertEqual(set(persisted["actions"]), {"$collection"})
            self.assertEqual(collection(project, "/actions"), [{"name": "inspect"}])
            self.assertEqual(
                self._object_json(project, reference), [{"name": "inspect"}]
            )
            self.assertEqual(
                self._entry(project, first.entry_hash)["schema"], schema_record.schema
            )

            second = apply_patch(
                project,
                actor="codex",
                task_id="collections",
                parent_state=first.state,
                patch=[
                    {
                        "op": "add",
                        "path": "/actions/-",
                        "value": {"name": "commit"},
                    }
                ],
            )

            self.assertEqual(
                collection(project, "/actions"),
                [{"name": "inspect"}, {"name": "commit"}],
            )
            self.assertNotEqual(
                state(project)["actions"]["$collection"], reference
            )
            self.assertEqual(verify(project, strict=True), second.entry_hash)

    def test_schema_rejection_happens_before_collection_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_schema(
                project,
                actor="human",
                task_id="collection-gate",
                schema={
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "minItems": 2,
                            "x-kernel-collection": True,
                        }
                    },
                },
            )
            before_state = self._kernel_state(project)
            before_objects = set(self._object_directory(project).iterdir())

            with self.assertRaises(SchemaError):
                apply_patch(
                    project,
                    actor="codex",
                    task_id="collection-gate",
                    parent_state=before_state["state_head"],
                    patch=[
                        {"op": "add", "path": "/actions", "value": ["one"]}
                    ],
                )

            self.assertEqual(self._kernel_state(project), before_state)
            self.assertEqual(set(self._object_directory(project).iterdir()), before_objects)

    def test_schema_change_can_materialize_an_existing_collection_later(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_schema(
                project,
                actor="human",
                task_id="collection-schema-change",
                schema=self._collection_schema(),
            )
            parent = self._kernel_state(project)["state_head"]
            externalized = apply_patch(
                project,
                actor="codex",
                task_id="collection-schema-change",
                parent_state=parent,
                patch=[{"op": "add", "path": "/actions", "value": []}],
            )
            set_schema(
                project,
                actor="human",
                task_id="collection-schema-change",
                schema={
                    "type": "object",
                    "properties": {"actions": {"type": "array"}},
                    "required": ["actions"],
                },
            )
            self.assertEqual(collection(project, "/actions"), [])

            materialized = apply_patch(
                project,
                actor="codex",
                task_id="collection-schema-change",
                parent_state=externalized.state,
                patch=[{"op": "add", "path": "/actions/-", "value": "visible"}],
            )

            self.assertEqual(state(project), {"actions": ["visible"]})
            with self.assertRaises(SchemaError):
                collection(project, "/actions")
            self.assertEqual(verify(project, strict=True), materialized.entry_hash)

    def test_missing_collection_object_breaks_structural_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            set_schema(
                project,
                actor="human",
                task_id="collection-integrity",
                schema=self._collection_schema(),
            )
            parent = self._kernel_state(project)["state_head"]
            apply_patch(
                project,
                actor="codex",
                task_id="collection-integrity",
                parent_state=parent,
                patch=[
                    {
                        "op": "add",
                        "path": "/actions",
                        "value": [{"name": "one"}],
                    }
                ],
            )
            reference = state(project)["actions"]["$collection"]
            self._object_path(project, reference).unlink()

            with self.assertRaises(LedgerIntegrityError):
                verify(project, strict=True)

    def test_collection_pointer_escapes_and_schema_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            definition = {
                "type": "object",
                "properties": {
                    "a/b": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "x-kernel-collection": True,
                    }
                },
            }
            set_schema(
                project,
                actor="human",
                task_id="collection-pointer",
                schema=definition,
            )
            parent = self._kernel_state(project)["state_head"]
            apply_patch(
                project,
                actor="codex",
                task_id="collection-pointer",
                parent_state=parent,
                patch=[{"op": "add", "path": "/a~1b", "value": [1, 2]}],
            )

            self.assertEqual(collection(project, "/a~1b"), [1, 2])
            self.assertTrue(all(result["valid"] for result in audit_schema(project)))

    def test_collection_annotation_requires_an_array_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)

            with self.assertRaisesRegex(SchemaError, "requires a schema with type array"):
                set_schema(
                    project,
                    actor="human",
                    task_id="bad-collection-schema",
                    schema={
                        "type": "object",
                        "properties": {
                            "actions": {
                                "type": "object",
                                "x-kernel-collection": True,
                            }
                        },
                    },
                )

    @staticmethod
    def _collection_schema() -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {"type": "object"},
                    "x-kernel-collection": True,
                }
            },
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
    def _object_path(cls, project: Path, reference: str) -> Path:
        return cls._object_directory(project) / reference.removeprefix("sha256:")

    @classmethod
    def _object_json(cls, project: Path, reference: str):
        return json.loads(cls._object_path(project, reference).read_text(encoding="utf-8"))

    @classmethod
    def _entry(cls, project: Path, reference: str) -> dict[str, object]:
        return cls._object_json(project, reference)


if __name__ == "__main__":
    unittest.main()
