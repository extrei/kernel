from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from kernel import (
    StaleParentError,
    UnauthorizedWriteError,
    apply_patch,
    state,
)
from kernel.controller import record_step
from kernel.kernel import LedgerIntegrityError, initialize, verify


class StateTests(unittest.TestCase):
    def test_genesis_state_is_the_canonical_empty_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)

            kernel_state = self._kernel_state(project)
            expected = f"sha256:{sha256(b'{}').hexdigest()}"
            self.assertEqual(kernel_state["state_head"], expected)
            self.assertEqual(state(project), {})
            self.assertEqual(self._object_path(project, expected).read_bytes(), b"{}")

    def test_patch_commits_canonical_snapshots_and_state_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]

            first = apply_patch(
                project,
                actor="codex",
                task_id="state-model",
                parent_state=genesis,
                patch=[
                    {"op": "add", "path": "/name", "value": "kernel"},
                    {"op": "add", "path": "/items", "value": [1]},
                ],
            )
            second = apply_patch(
                project,
                actor="codex",
                task_id="state-model",
                parent_state=first.state,
                patch=[
                    {"op": "test", "path": "/name", "value": "kernel"},
                    {"op": "replace", "path": "/name", "value": "project-kernel"},
                    {"op": "add", "path": "/items/-", "value": 2},
                ],
            )

            self.assertEqual(first.parent_state, genesis)
            self.assertEqual(second.parent_state, first.state)
            self.assertEqual(
                state(project), {"items": [1, 2], "name": "project-kernel"}
            )
            self.assertEqual(state(project, at=first.state), {"items": [1], "name": "kernel"})
            self.assertEqual(self._kernel_state(project)["state_head"], second.state)

            entry = self._object_json(project, second.entry_hash)
            self.assertEqual(entry["version"], 4)
            self.assertEqual(entry["payload_hash"], second.patch_hash)
            self.assertEqual(entry["parent_state"], first.state)
            self.assertEqual(entry["state"], second.state)
            self.assertIsNone(entry["schema"])
            self.assertIsNone(entry["contracts"])
            self.assertIsNone(entry["view"])

    def test_remove_has_no_caller_supplied_privilege(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            created = apply_patch(
                project,
                actor="codex",
                task_id="remove-test",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/temporary", "value": True}],
            )
            removal = [{"op": "remove", "path": "/temporary"}]

            with self.assertRaises(UnauthorizedWriteError):
                apply_patch(
                    project,
                    actor="codex",
                    task_id="remove-test",
                    parent_state=created.state,
                    patch=removal,
                )
            with self.assertRaises(TypeError):
                apply_patch(
                    project,
                    actor="codex",
                    task_id="remove-test",
                    parent_state=created.state,
                    patch=removal,
                    allow_remove=True,
                )

    def test_stale_parent_leaves_all_durable_heads_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            before = self._kernel_state(project)
            objects_before = set(self._object_directory(project).iterdir())

            with self.assertRaises(StaleParentError):
                apply_patch(
                    project,
                    actor="codex",
                    task_id="stale-test",
                    parent_state=f"sha256:{'0' * 64}",
                    patch=[{"op": "add", "path": "/bad", "value": True}],
                )

            self.assertEqual(self._kernel_state(project), before)
            self.assertEqual(set(self._object_directory(project).iterdir()), objects_before)

    def test_artifact_step_annotates_unchanged_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            changed = apply_patch(
                project,
                actor="codex",
                task_id="mixed-task",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/phase", "value": 1}],
            )
            artifact = project / "result.txt"
            artifact.write_text("done", encoding="utf-8")
            record = record_step(
                project,
                agent="claude",
                task_id="mixed-task",
                artifact=artifact,
                kind="result",
            )

            entry = self._object_json(project, record.entry_hash)
            self.assertEqual(entry["parent_state"], changed.state)
            self.assertEqual(entry["state"], changed.state)
            self.assertEqual(self._kernel_state(project)["state_head"], changed.state)

    def test_tampered_snapshot_and_v3_entry_fail_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            genesis = self._kernel_state(project)["state_head"]
            record = apply_patch(
                project,
                actor="codex",
                task_id="tamper-test",
                parent_state=genesis,
                patch=[{"op": "add", "path": "/safe", "value": True}],
            )

            self._object_path(project, record.state).write_bytes(b"{}")
            with self.assertRaises(LedgerIntegrityError):
                verify(project, strict=True)

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            initialize(project)
            artifact = project / "result.txt"
            artifact.write_text("result", encoding="utf-8")
            record = record_step(
                project,
                agent="codex",
                task_id="v3-rejection",
                artifact=artifact,
                kind="result",
            )
            entry = self._object_json(project, record.entry_hash)
            entry["version"] = 3
            content = json.dumps(entry, separators=(",", ":"), sort_keys=True).encode()
            digest = sha256(content).hexdigest()
            (self._object_directory(project) / digest).write_bytes(content)
            kernel_state = self._kernel_state(project)
            kernel_state["ledger_head"] = f"sha256:{digest}"
            self._write_kernel_state(project, kernel_state)

            with self.assertRaises(LedgerIntegrityError):
                verify(project, strict=True)

    @staticmethod
    def _kernel_state(project: Path) -> dict[str, object]:
        return json.loads((project / ".state-tree" / "kernel.json").read_text())

    @staticmethod
    def _write_kernel_state(project: Path, value: dict[str, object]) -> None:
        (project / ".state-tree" / "kernel.json").write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @staticmethod
    def _object_directory(project: Path) -> Path:
        return project / ".state-tree" / "objects" / "sha256"

    @classmethod
    def _object_path(cls, project: Path, reference: str) -> Path:
        return cls._object_directory(project) / reference.removeprefix("sha256:")

    @classmethod
    def _object_json(cls, project: Path, reference: str) -> dict[str, object]:
        return json.loads(cls._object_path(project, reference).read_text())


if __name__ == "__main__":
    unittest.main()
