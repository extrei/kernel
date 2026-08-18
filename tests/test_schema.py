import json
from pathlib import Path
import shutil
import tempfile
import unittest

from kernel import (
    BlueprintError,
    SchemaError,
    apply_patch,
    audit_schema,
    schema,
    set_blueprint,
    state,
    validate,
)
from kernel.controller import record_step
from kernel.kernel import initialize, verify


class SchemaTests(unittest.TestCase):
    def test_null_schema_allows_any_well_formed_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            parent = self._kernel_state(project)["state_head"]

            record = apply_patch(
                project,
                actor="codex",
                task_id="no-schema",
                parent_state=parent,
                patch=[{"op": "add", "path": "/anything", "value": [1, "two"]}],
            )

            self.assertIsNone(schema(project))
            self.assertEqual(state(project), {"anything": [1, "two"]})
            self.assertIsNone(self._entry(project, record.entry_hash)["blueprint"])

    def test_invalid_patch_leaves_heads_and_objects_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            current = apply_patch(
                project,
                actor="codex",
                task_id="schema-gate",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/count", "value": 1}],
            )
            self._install_schema_blueprint(
                project,
                actor="human",
                task_id="schema-gate",
                schema={
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
                paths=["/count"],
            )
            before_state = self._kernel_state(project)
            before_objects = set(self._object_directory(project).iterdir())

            with self.assertRaises(SchemaError):
                apply_patch(
                    project,
                    actor="codex",
                    task_id="schema-gate",
                    parent_state=current.state,
                    patch=[
                        {"op": "replace", "path": "/count", "value": "invalid"}
                    ],
                )

            after_state = self._kernel_state(project)
            self.assertEqual(after_state["state_head"], before_state["state_head"])
            self.assertEqual(
                after_state["blueprint_head"], before_state["blueprint_head"]
            )
            self.assertEqual(after_state["revision"], before_state["revision"] + 1)
            self.assertLess(
                len(before_objects), len(set(self._object_directory(project).iterdir()))
            )

    def test_schema_cannot_invalidate_the_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            apply_patch(
                project,
                actor="codex",
                task_id="live-state",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/name", "value": "kernel"}],
            )
            before_state = self._kernel_state(project)
            before_objects = set(self._object_directory(project).iterdir())

            with self.assertRaises(BlueprintError):
                self._install_schema_blueprint(
                    project,
                    actor="human",
                    task_id="live-state",
                    schema={
                        "type": "object",
                        "required": ["count"],
                        "properties": {"count": {"type": "integer"}},
                    },
                )

            self.assertEqual(self._kernel_state(project), before_state)
            self.assertEqual(set(self._object_directory(project).iterdir()), before_objects)

    def test_invalid_draft_2020_12_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            before_objects = set(self._object_directory(project).iterdir())

            with self.assertRaisesRegex(BlueprintError, "invalid Draft 2020-12"):
                self._install_schema_blueprint(
                    project,
                    actor="human",
                    task_id="bad-schema",
                    schema={"type": "not-a-json-schema-type"},
                )

            self.assertEqual(set(self._object_directory(project).iterdir()), before_objects)
            self.assertEqual(self._kernel_state(project)["revision"], 0)

    def test_blueprint_change_is_an_unchanged_state_commit_and_can_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            definition = {"type": "object", "additionalProperties": True}

            record = self._install_schema_blueprint(
                project,
                actor="human",
                task_id="schema-change",
                schema=definition,
            )

            kernel_state = self._kernel_state(project)
            entry = self._entry(project, record.entry_hash)
            self.assertEqual(kernel_state["blueprint_head"], record.blueprint)
            self.assertEqual(record.payload_hash, record.blueprint)
            self.assertEqual(entry["kind"], "blueprint")
            self.assertEqual(entry["parent_state"], genesis)
            self.assertEqual(entry["state"], genesis)
            self.assertEqual(entry["blueprint"], record.blueprint)
            self.assertEqual(entry["version"], 5)
            self.assertEqual(schema(project), definition)

            cleared = set_blueprint(
                project,
                actor="human",
                task_id="schema-change",
                blueprint=None,
            )
            cleared_entry = self._entry(project, cleared.entry_hash)
            self.assertIsNone(cleared.blueprint)
            self.assertIsNone(cleared_entry["blueprint"])
            self.assertEqual(cleared_entry["parent_state"], genesis)
            self.assertEqual(cleared_entry["state"], genesis)
            self.assertIsNone(self._kernel_state(project)["blueprint_head"])

    def test_artifact_entry_records_the_schema_in_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            blueprint_record = self._install_schema_blueprint(
                project,
                actor="human",
                task_id="artifact-schema",
                schema={"type": "object"},
            )
            artifact = project / "result.txt"
            artifact.write_text("done", encoding="utf-8")

            step = record_step(
                project,
                agent="claude",
                task_id="artifact-schema",
                artifact=artifact,
                kind="result",
            )

            entry = self._entry(project, step.entry_hash)
            self.assertEqual(entry["blueprint"], blueprint_record.blueprint)
            self.assertEqual(entry["parent_state"], entry["state"])

    def test_history_keeps_its_schema_and_audit_uses_entry_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            draft = apply_patch(
                project,
                actor="codex",
                task_id="schema-history",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/phase", "value": "draft"}],
            )
            first_blueprint = self._install_schema_blueprint(
                project,
                actor="human",
                task_id="schema-history",
                schema={
                    "type": "object",
                    "properties": {"phase": {"enum": ["draft", "done"]}},
                    "required": ["phase"],
                },
                paths=["/phase"],
            )
            done = apply_patch(
                project,
                actor="codex",
                task_id="schema-history",
                parent_state=draft.state,
                patch=[{"op": "replace", "path": "/phase", "value": "done"}],
            )
            second_blueprint = self._install_schema_blueprint(
                project,
                actor="human",
                task_id="schema-history",
                schema={
                    "type": "object",
                    "properties": {"phase": {"const": "done"}},
                    "required": ["phase"],
                },
                paths=["/phase"],
            )

            with self.assertRaises(SchemaError):
                validate(state(project, at=draft.state), schema(project))
            self.assertEqual(
                self._entry(project, done.entry_hash)["blueprint"],
                first_blueprint.blueprint,
            )
            self.assertEqual(
                self._entry(project, second_blueprint.entry_hash)["blueprint"],
                second_blueprint.blueprint,
            )
            self.assertEqual(verify(project, strict=True), second_blueprint.entry_hash)

            audit = audit_schema(project)
            self.assertEqual([result["sequence"] for result in audit], [1, 2, 3, 4])
            self.assertTrue(all(result["valid"] for result in audit))
            self.assertEqual(
                [result["blueprint"] for result in audit],
                [
                    None,
                    first_blueprint.blueprint,
                    first_blueprint.blueprint,
                    second_blueprint.blueprint,
                ],
            )

            cache = project / ".state-tree" / "cache"
            shutil.rmtree(cache)
            self.assertEqual(verify(project), second_blueprint.entry_hash)
            self.assertTrue((cache / "verified").is_file())

    @staticmethod
    def _install_schema_blueprint(
        project: Path,
        *,
        actor: str,
        task_id: str,
        schema: dict[str, object],
        paths: list[str] | None = None,
    ):
        grants = [] if paths is None else paths
        return set_blueprint(
            project,
            actor=actor,
            task_id=task_id,
            blueprint={
                "version": 3,
                "schema": schema,
                "contracts": {
                    "version": 2,
                    "actors": {
                        "codex": {"read": grants, "write": grants}
                    },
                },
            },
        )

    @staticmethod
    def _kernel_state(project: Path) -> dict[str, object]:
        return json.loads(
            (project / ".state-tree" / "kernel.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def _object_directory(project: Path) -> Path:
        return project / ".state-tree" / "objects" / "sha256"

    @classmethod
    def _entry(cls, project: Path, reference: str) -> dict[str, object]:
        path = cls._object_directory(project) / reference.removeprefix("sha256:")
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
